"""REST endpoints for chat (customer side + admin side)."""

import io
import json
import logging
import time
import uuid
from functools import wraps

from PIL import Image, ImageOps

from django.contrib.admin.views.decorators import staff_member_required
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core.files.uploadedfile import InMemoryUploadedFile
from django.db.models import Max, Q
from django.http import JsonResponse, HttpResponseBadRequest, HttpResponseForbidden, HttpResponseNotFound
from django.shortcuts import render
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods, require_POST, require_GET

from rest_framework_simplejwt.authentication import JWTAuthentication

from .models import Conversation, Message
from .services import (
    GROUP_DASHBOARD,
    broadcast_conversation,
    broadcast_conversation_deleted,
    broadcast_message_deleted,
    broadcast_message_updated,
    post_message,
    serialize_conversation,
    serialize_message,
)


log = logging.getLogger(__name__)

User = get_user_model()
GUEST_HEADER = 'HTTP_X_CHAT_GUEST'  # request.META key for "X-Chat-Guest"
MAX_TEXT_LEN = 4000


def _resolve_user(request):
    """JWT-aware user resolver — bypasses DRF view, used by plain Django views."""
    try:
        auth = JWTAuthentication().authenticate(request)
    except Exception:
        auth = None
    if not auth:
        return None
    user, _ = auth
    return user if user and user.is_active else None


def _json_body(request) -> dict:
    if not request.body:
        return {}
    try:
        return json.loads(request.body.decode('utf-8'))
    except (ValueError, UnicodeDecodeError):
        return {}


# Limits for chat attachments
MAX_ATTACHMENT_BYTES = 10 * 1024 * 1024  # 10 MB
MAX_FILES_PER_MESSAGE = 3


def _client_ip(request) -> str:
    xff = request.META.get('HTTP_X_FORWARDED_FOR', '')
    if xff:
        return xff.split(',')[0].strip()
    return request.META.get('REMOTE_ADDR', '0.0.0.0')


def _throttle_check(scope: str, ident: str, limit: int, window: int) -> bool:
    """Sliding-window rate limit using the cache. Returns True if allowed."""
    if not ident:
        return True
    key = f'chat_throttle:{scope}:{ident}'
    now = int(time.time())
    cutoff = now - window
    times = [t for t in (cache.get(key) or []) if t > cutoff]
    if len(times) >= limit:
        return False
    times.append(now)
    cache.set(key, times, window + 5)
    return True


def _throttled_response():
    return JsonResponse({'error': 'rate_limited'}, status=429)


HEIC_EXTS = {'heic', 'heif'}


def _convert_heic_to_jpeg(f):
    """iPhone shoots HEIC by default. Chrome/Firefox/Edge (the CRM runs on
    Windows) cannot render <img src=".heic">; this is not an upload error,
    the image simply never appears in the dashboard. Safari can, so for the customer
    on iPhone everything looks like it works. We convert to JPEG right on intake
    so the preview works in any browser."""
    name = (f.name or '').lower()
    ext = name.rsplit('.', 1)[-1] if '.' in name else ''
    if ext not in HEIC_EXTS:
        return f
    try:
        import pillow_heif
        pillow_heif.register_heif_opener()
        f.seek(0)
        img = Image.open(f)
        img = ImageOps.exif_transpose(img)  # respect EXIF orientation
        img = img.convert('RGB')
        buf = io.BytesIO()
        img.save(buf, format='JPEG', quality=90, optimize=True)
        data = buf.getvalue()
        new_name = name.rsplit('.', 1)[0] + '.jpg'
        return InMemoryUploadedFile(io.BytesIO(data), None, new_name, 'image/jpeg', len(data), None)
    except Exception:
        log.warning('chat: HEIC→JPEG convert failed for %r, storing as-is', f.name, exc_info=True)
        f.seek(0)
        return f


def _read_message_payload(request):
    """Returns (text, files_list). Supports JSON and multipart/form-data with multiple files."""
    ctype = (request.content_type or '').lower()
    if 'multipart' in ctype:
        text = (request.POST.get('text') or '').strip()
        raw = request.FILES.getlist('file')
        files = [f for f in raw if f and f.size and f.size <= MAX_ATTACHMENT_BYTES][:MAX_FILES_PER_MESSAGE]
        files = [_convert_heic_to_jpeg(f) for f in files]
        return text, files
    body = _json_body(request)
    return (body.get('text') or '').strip(), []


def _customer_can_access(conv: Conversation, user, guest_id: str) -> bool:
    if user and conv.user_id == user.id:
        return True
    if guest_id and conv.guest_id == guest_id:
        return True
    return False


def _post_messages(conv, *, sender_type, sender_user, text, files):
    """Create one message per attachment (caption attached to the first); if no
    files, create a single text message. Always returns a list of Message objects."""
    msgs = []
    if not files:
        msgs.append(post_message(conv, sender_type=sender_type, sender_user=sender_user, text=text))
        return msgs
    for i, f in enumerate(files):
        msgs.append(post_message(
            conv, sender_type=sender_type, sender_user=sender_user,
            text=text if i == 0 else '',
            attachment=f,
        ))
    return msgs


# ── customer-facing endpoints ────────────────────────────────────────────

@csrf_exempt
@require_POST
def start_conversation(request):
    """Get or create the customer's open conversation.

    Body (json): { guest_id?, guest_name?, guest_email?, guest_phone? }.
    Authenticated users get their own thread keyed by user_id.
    Guests are keyed by guest_id; if missing, server generates one.
    """
    user = _resolve_user(request)
    # IP-level throttle: max 10 starts/min per IP for guests; users are JWT-throttled by DRF
    if not user and not _throttle_check('start', _client_ip(request), 10, 60):
        return _throttled_response()
    body = _json_body(request)
    # Anti-bot: a guest must send a valid signed token (the frontend adds it
    # automatically via an axios interceptor). Protects against creating junk conversations.
    if not user:
        from core.antibot import is_human
        ok, _reason = is_human(body)
        if not ok:
            return JsonResponse(
                {'error': 'Could not verify the request. Refresh the page.', 'code': 'antibot'},
                status=400,
            )
    raw_guest = (request.META.get(GUEST_HEADER) or body.get('guest_id') or '').strip()
    guest_name = (body.get('guest_name') or '').strip()[:255]
    guest_email = (body.get('guest_email') or '').strip()[:254]
    guest_phone = (body.get('guest_phone') or '').strip()[:32]

    # Create the conversation ONLY when the customer actually sends a first message
    # (the frontend passes create=true). Merely opening the widget does not spawn a conversation,
    # otherwise empty chats with no messages pile up in the CRM.
    want_create = bool(body.get('create'))
    is_new_guest = False
    conv = None

    if user:
        conv = Conversation.objects.filter(user=user, status=Conversation.STATUS_OPEN).order_by('-id').first()
        # If there is a guest conversation (with history), attach it to the account
        # so the correspondence is not lost and duplicates are not created.
        if raw_guest:
            guest_conv = Conversation.objects.filter(guest_id=raw_guest, user__isnull=True).order_by('-id').first()
            if guest_conv:
                if conv is None:
                    guest_conv.user = user
                    guest_conv.save(update_fields=['user'])
                    conv = guest_conv
                    broadcast_conversation(conv)
                elif guest_conv.id != conv.id:
                    guest_conv.messages.update(conversation=conv)
                    if guest_conv.last_message_at and (conv.last_message_at is None or guest_conv.last_message_at > conv.last_message_at):
                        conv.last_message_at = guest_conv.last_message_at
                        conv.save(update_fields=['last_message_at'])
                    guest_conv.delete()
                    broadcast_conversation(conv)
        if conv is None and want_create:
            conv = Conversation.objects.create(user=user, status=Conversation.STATUS_OPEN)
        guest_id_out = raw_guest or ''
    else:
        if not raw_guest:
            raw_guest = uuid.uuid4().hex
            is_new_guest = True
        conv = Conversation.objects.filter(guest_id=raw_guest, status=Conversation.STATUS_OPEN, user__isnull=True).order_by('-id').first()
        if conv is None:
            if want_create:
                conv = Conversation.objects.create(
                    guest_id=raw_guest,
                    guest_name=guest_name,
                    guest_email=guest_email,
                    guest_phone=guest_phone,
                    status=Conversation.STATUS_OPEN,
                )
        else:
            # Update guest contact info if newly provided
            updates = []
            if guest_name and not conv.guest_name:
                conv.guest_name = guest_name; updates.append('guest_name')
            if guest_email and not conv.guest_email:
                conv.guest_email = guest_email; updates.append('guest_email')
            if guest_phone and not conv.guest_phone:
                conv.guest_phone = guest_phone; updates.append('guest_phone')
            if updates:
                conv.save(update_fields=updates)
        guest_id_out = raw_guest

    return JsonResponse({
        'conversation': serialize_conversation(conv) if conv else None,
        'guest_id': guest_id_out,
        'new_guest': is_new_guest,
    })


@csrf_exempt
@require_http_methods(['GET', 'POST'])
def conversation_messages(request, conversation_id: int):
    """GET — paginated history. POST — send a customer message (json or multipart)."""
    try:
        conv = Conversation.objects.get(pk=conversation_id)
    except Conversation.DoesNotExist:
        return HttpResponseNotFound('not found')

    user = _resolve_user(request)
    guest_id = (request.META.get(GUEST_HEADER) or '').strip()

    if not _customer_can_access(conv, user, guest_id):
        return HttpResponseForbidden('forbidden')

    if request.method == 'GET':
        try:
            limit = max(1, min(int(request.GET.get('limit', '50')), 200))
        except ValueError:
            limit = 50
        before = request.GET.get('before')
        qs = conv.messages.all().order_by('-created_at')
        if before:
            try:
                bid = int(before)
                qs = qs.filter(id__lt=bid)
            except ValueError:
                pass
        rows = list(qs[:limit])
        rows.reverse()
        return JsonResponse({
            'messages': [serialize_message(m) for m in rows],
            'has_more': qs.count() > limit,
        })

    # POST — text and/or attachments (1..N files). Throttle per identity.
    ident = f'u{user.id}' if user else f'g{guest_id}'
    if not _throttle_check('msg', ident, 30, 60):
        return _throttled_response()
    text, files = _read_message_payload(request)
    if not text and not files:
        return HttpResponseBadRequest('text or file required')
    if conv.status == Conversation.STATUS_CLOSED:
        return JsonResponse({'error': 'conversation closed'}, status=409)

    sender = user if (user and user.id == conv.user_id) else None
    msgs = _post_messages(conv, sender_type=Message.SENDER_CUSTOMER, sender_user=sender,
                          text=text[:MAX_TEXT_LEN], files=files)
    return JsonResponse({'messages': [serialize_message(m) for m in msgs]})


@csrf_exempt
@require_POST
def mark_read(request, conversation_id: int):
    try:
        conv = Conversation.objects.get(pk=conversation_id)
    except Conversation.DoesNotExist:
        return HttpResponseNotFound('not found')

    user = _resolve_user(request)
    guest_id = (request.META.get(GUEST_HEADER) or '').strip()
    if not _customer_can_access(conv, user, guest_id):
        return HttpResponseForbidden('forbidden')

    conv.customer_seen_at = timezone.now()
    conv.save(update_fields=['customer_seen_at'])
    return JsonResponse({'ok': True})


# ── admin-facing endpoints ───────────────────────────────────────────────

def _staff_required(view):
    @wraps(view)
    def wrapper(request, *args, **kwargs):
        if not (request.user.is_authenticated and request.user.is_staff):
            if request.headers.get('Accept', '').startswith('application/json'):
                return JsonResponse({'error': 'forbidden'}, status=403)
            return HttpResponseForbidden('forbidden')
        return view(request, *args, **kwargs)
    return wrapper


@_staff_required
@require_GET
def admin_conversations_list(request):
    # Empty conversations (with no messages at all) are not shown in the list
    qs = Conversation.objects.filter(last_message_at__isnull=False).select_related('user', 'assigned_to')
    status_filter = request.GET.get('status', 'all')
    if status_filter in ('open', 'closed'):
        qs = qs.filter(status=status_filter)
    search = (request.GET.get('q') or '').strip()
    if search:
        qs = qs.filter(
            Q(user__email__icontains=search)
            | Q(user__first_name__icontains=search)
            | Q(user__last_name__icontains=search)
            | Q(user__phone__icontains=search)
            | Q(guest_name__icontains=search)
            | Q(guest_email__icontains=search)
            | Q(guest_phone__icontains=search)
            | Q(messages__text__icontains=search)
        ).distinct()
    qs = qs.order_by('-last_message_at', '-created_at')[:200]
    return JsonResponse({
        'conversations': [serialize_conversation(c, include_preview=True) for c in qs],
    })


@_staff_required
@require_GET
def admin_conversation_messages(request, conversation_id: int):
    try:
        conv = Conversation.objects.get(pk=conversation_id)
    except Conversation.DoesNotExist:
        return HttpResponseNotFound('not found')
    try:
        limit = max(1, min(int(request.GET.get('limit', '50')), 500))
    except ValueError:
        limit = 50
    before = request.GET.get('before')
    qs = conv.messages.all().order_by('-created_at')
    if before:
        try:
            qs = qs.filter(id__lt=int(before))
        except ValueError:
            pass
    rows = list(qs[:limit])
    has_more = qs.count() > limit
    rows.reverse()
    return JsonResponse({
        'conversation': serialize_conversation(conv),
        'messages': [serialize_message(m) for m in rows],
        'has_more': has_more,
    })


@_staff_required
@csrf_exempt
@require_POST
def admin_send_message(request, conversation_id: int):
    try:
        conv = Conversation.objects.get(pk=conversation_id)
    except Conversation.DoesNotExist:
        return HttpResponseNotFound('not found')
    text, files = _read_message_payload(request)
    if not text and not files:
        return HttpResponseBadRequest('text or file required')
    if conv.status == Conversation.STATUS_CLOSED:
        return JsonResponse({'error': 'conversation closed'}, status=409)
    msgs = _post_messages(conv, sender_type=Message.SENDER_MANAGER, sender_user=request.user,
                          text=text[:MAX_TEXT_LEN], files=files)
    return JsonResponse({'messages': [serialize_message(m) for m in msgs]})


@_staff_required
@csrf_exempt
@require_POST
def admin_assign(request, conversation_id: int):
    try:
        conv = Conversation.objects.get(pk=conversation_id)
    except Conversation.DoesNotExist:
        return HttpResponseNotFound('not found')
    conv.assigned_to = request.user
    conv.save(update_fields=['assigned_to'])
    broadcast_conversation(conv)
    return JsonResponse({'conversation': serialize_conversation(conv)})


@_staff_required
@csrf_exempt
@require_POST
def admin_close(request, conversation_id: int):
    try:
        conv = Conversation.objects.get(pk=conversation_id)
    except Conversation.DoesNotExist:
        return HttpResponseNotFound('not found')
    conv.status = Conversation.STATUS_CLOSED
    conv.save(update_fields=['status'])
    broadcast_conversation(conv)
    return JsonResponse({'conversation': serialize_conversation(conv)})


@_staff_required
@csrf_exempt
@require_POST
def admin_reopen(request, conversation_id: int):
    try:
        conv = Conversation.objects.get(pk=conversation_id)
    except Conversation.DoesNotExist:
        return HttpResponseNotFound('not found')
    conv.status = Conversation.STATUS_OPEN
    conv.save(update_fields=['status'])
    broadcast_conversation(conv)
    return JsonResponse({'conversation': serialize_conversation(conv)})


@_staff_required
@csrf_exempt
@require_POST
def admin_mark_read(request, conversation_id: int):
    try:
        conv = Conversation.objects.get(pk=conversation_id)
    except Conversation.DoesNotExist:
        return HttpResponseNotFound('not found')
    conv.manager_seen_at = timezone.now()
    conv.save(update_fields=['manager_seen_at'])
    broadcast_conversation(conv)
    return JsonResponse({'ok': True})


@_staff_required
@csrf_exempt
@require_POST
def admin_delete_conversation(request, conversation_id: int):
    """Full deletion of a conversation along with all messages and attachments."""
    try:
        conv = Conversation.objects.get(pk=conversation_id)
    except Conversation.DoesNotExist:
        return HttpResponseNotFound('not found')
    # CASCADE removes the Message rows; attachment files are removed from disk manually
    for m in conv.messages.all():
        if m.attachment:
            m.attachment.delete(save=False)
    conv.delete()
    broadcast_conversation_deleted(conversation_id)
    return JsonResponse({'ok': True, 'id': conversation_id})


@_staff_required
@csrf_exempt
@require_POST
def admin_delete_message(request, conversation_id: int, message_id: int):
    """Deleting a manager's message (customer messages must not be touched)."""
    try:
        msg = Message.objects.get(pk=message_id, conversation_id=conversation_id)
    except Message.DoesNotExist:
        return HttpResponseNotFound('not found')
    if msg.sender_type != Message.SENDER_MANAGER:
        return JsonResponse({'error': 'only manager messages can be deleted'}, status=403)
    if msg.attachment:
        msg.attachment.delete(save=False)
    msg.delete()
    broadcast_message_deleted(conversation_id, message_id)
    return JsonResponse({'ok': True, 'id': message_id})


@_staff_required
@csrf_exempt
@require_POST
def admin_edit_message(request, conversation_id: int, message_id: int):
    """Editing the text of a manager's message."""
    try:
        msg = Message.objects.get(pk=message_id, conversation_id=conversation_id)
    except Message.DoesNotExist:
        return HttpResponseNotFound('not found')
    if msg.sender_type != Message.SENDER_MANAGER:
        return JsonResponse({'error': 'only manager messages can be edited'}, status=403)
    text = (_json_body(request).get('text') or '').strip()
    if not text:
        return HttpResponseBadRequest('text required')
    msg.text = text[:MAX_TEXT_LEN]
    msg.edited_at = timezone.now()
    msg.save(update_fields=['text', 'edited_at'])
    broadcast_message_updated(msg)
    return JsonResponse({'message': serialize_message(msg)})


@_staff_required
def admin_chat_page(request):
    """Admin SPA-like page: HTML shell, JS does the rest via REST + WS."""
    from django.contrib import admin as django_admin
    context = django_admin.site.each_context(request)
    context['title'] = 'Customer chat'
    return render(request, 'admin/chat/dashboard.html', context)
