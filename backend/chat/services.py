"""Helpers shared between REST views and WS consumers."""

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from django.utils import timezone

from .models import Conversation, Message


GROUP_DASHBOARD = 'chat_dashboard'


def conversation_group(conversation_id: int) -> str:
    return f'chat_conv_{conversation_id}'


def _invalidate_sidebar_cache() -> None:
    """A chat event changed unread: invalidate the placeholder cache AND push fresh counts
    (including chat) to the admin_notifications group so the sidebar badge updates in realtime
    instead of catching up only after a reload/navigation. broadcast_notification_counts itself
    does cache.delete + group_send. Fallback: delete only, if broadcast is unavailable."""
    try:
        from catalog.notifications import broadcast_notification_counts
        broadcast_notification_counts()
    except Exception:
        try:
            from django.core.cache import cache
            from catalog.context_processors import SIDEBAR_COUNTS_CACHE_KEY
            cache.delete(SIDEBAR_COUNTS_CACHE_KEY)
        except Exception:
            pass


def serialize_message(msg: Message) -> dict:
    return {
        'id': msg.id,
        'conversation_id': msg.conversation_id,
        'sender_type': msg.sender_type,
        'sender_name': msg.sender_display(),
        'text': msg.text,
        'attachment': msg.attachment_info(),
        'created_at': msg.created_at.isoformat(),
        'edited_at': msg.edited_at.isoformat() if msg.edited_at else None,
    }


def serialize_conversation(conv: Conversation, *, include_preview: bool = False) -> dict:
    from orders.models import format_phone
    if conv.user_id and conv.user:
        contact_email = conv.user.email or ''
        contact_phone = conv.user.phone or ''
    else:
        contact_email = conv.guest_email or ''
        contact_phone = conv.guest_phone or ''
    data = {
        'id': conv.id,
        'display_name': conv.display_name(),
        'status': conv.status,
        'user_id': conv.user_id,
        'guest_id': conv.guest_id,
        'guest_name': conv.guest_name,
        'guest_email': conv.guest_email,
        'guest_phone': format_phone(conv.guest_phone),
        'contact_email': contact_email,
        'contact_phone': format_phone(contact_phone),
        'assigned_to_id': conv.assigned_to_id,
        'created_at': conv.created_at.isoformat(),
        'last_message_at': conv.last_message_at.isoformat() if conv.last_message_at else None,
        'unread_for_manager': conv.unread_for_manager(),
        'unread_for_customer': conv.unread_for_customer(),
    }
    if include_preview:
        last = conv.messages.order_by('-created_at').first()
        if last:
            if last.text:
                data['last_message_text'] = last.text[:200]
            elif last.attachment:
                info = last.attachment_info()
                data['last_message_text'] = '📎 ' + (info['name'] if info else 'attachment')
            else:
                data['last_message_text'] = ''
            data['last_message_sender'] = last.sender_type
        else:
            data['last_message_text'] = ''
            data['last_message_sender'] = ''
    return data


def broadcast_message(msg: Message) -> None:
    _invalidate_sidebar_cache()  # new message -> unread changed in the sidebar
    layer = get_channel_layer()
    if layer is None:
        return
    payload = serialize_message(msg)
    async_to_sync(layer.group_send)(conversation_group(msg.conversation_id), {
        'type': 'chat.message', 'payload': payload,
    })
    async_to_sync(layer.group_send)(GROUP_DASHBOARD, {
        'type': 'chat.message', 'payload': payload,
    })


def broadcast_conversation(conv: Conversation) -> None:
    _invalidate_sidebar_cache()  # read/status change -> unread changed
    layer = get_channel_layer()
    if layer is None:
        return
    payload = serialize_conversation(conv)
    async_to_sync(layer.group_send)(conversation_group(conv.id), {
        'type': 'chat.conversation', 'payload': payload,
    })
    async_to_sync(layer.group_send)(GROUP_DASHBOARD, {
        'type': 'chat.conversation', 'payload': payload,
    })


def broadcast_message_updated(msg: Message) -> None:
    """Broadcast the updated message text to the customer and to the dashboard."""
    layer = get_channel_layer()
    if layer is None:
        return
    payload = serialize_message(msg)
    async_to_sync(layer.group_send)(conversation_group(msg.conversation_id), {
        'type': 'chat.message_updated', 'payload': payload,
    })
    async_to_sync(layer.group_send)(GROUP_DASHBOARD, {
        'type': 'chat.message_updated', 'payload': payload,
    })


def broadcast_message_deleted(conversation_id: int, message_id: int) -> None:
    """Notify the customer and the dashboard about a message deletion."""
    _invalidate_sidebar_cache()  # the deleted message may have been unread -> recount the badge
    layer = get_channel_layer()
    if layer is None:
        return
    payload = {'conversation_id': conversation_id, 'id': message_id}
    async_to_sync(layer.group_send)(conversation_group(conversation_id), {
        'type': 'chat.message_deleted', 'payload': payload,
    })
    async_to_sync(layer.group_send)(GROUP_DASHBOARD, {
        'type': 'chat.message_deleted', 'payload': payload,
    })


def broadcast_conversation_deleted(conversation_id: int) -> None:
    """Notify the customer and the dashboard about full deletion of a conversation."""
    _invalidate_sidebar_cache()  # the deleted conversation may have had unread messages -> sidebar badge is stale
    layer = get_channel_layer()
    if layer is None:
        return
    payload = {'id': conversation_id}
    async_to_sync(layer.group_send)(conversation_group(conversation_id), {
        'type': 'chat.conversation_deleted', 'payload': payload,
    })
    async_to_sync(layer.group_send)(GROUP_DASHBOARD, {
        'type': 'chat.conversation_deleted', 'payload': payload,
    })


def post_message(conv: Conversation, *, sender_type: str, sender_user, text: str = '', attachment=None) -> Message:
    msg = Message.objects.create(
        conversation=conv,
        sender_type=sender_type,
        sender_user=sender_user if getattr(sender_user, 'is_authenticated', False) else None,
        text=(text or '')[:4000],
        attachment=attachment if attachment else None,
    )
    now = timezone.now()
    conv.last_message_at = now
    fields = ['last_message_at']
    if sender_type == Message.SENDER_MANAGER:
        conv.manager_seen_at = now
        fields.append('manager_seen_at')
        if conv.assigned_to_id is None and getattr(sender_user, 'is_authenticated', False):
            conv.assigned_to = sender_user
            fields.append('assigned_to')
    elif sender_type == Message.SENDER_CUSTOMER:
        conv.customer_seen_at = now
        fields.append('customer_seen_at')
    conv.save(update_fields=fields)
    broadcast_message(msg)
    return msg
