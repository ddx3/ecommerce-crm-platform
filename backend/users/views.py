import re
from datetime import datetime, timedelta
from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError as DjangoValidationError
from django.utils import timezone
from rest_framework.decorators import api_view, permission_classes, throttle_classes
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import UserRateThrottle
from rest_framework_simplejwt.tokens import RefreshToken

User = get_user_model()


# ─── Questionnaire: list of fields and their constraints ───
# Keep in sync with frontend/src/constants/questionnaire.ts
QUESTIONNAIRE_FIELDS = [
    'gender', 'birth_date', 'gardening_text', 'interests',
    'referral_source', 'referral_social', 'referral_other',
    'wishes',
]

# Max length for text fields (TextField), so they cannot be abused.
QUESTIONNAIRE_TEXT_MAX = 2000

# Allowed interest category keys, in sync with the frontend.
INTEREST_KEYS = {
    'seeds', 'seedlings', 'fertilizers', 'protection',
    'tools', 'indoor', 'lawn', 'other',
}


# UserRateThrottle, not ScopedRateThrottle: in Scoped, `allow_request` overwrites
# self.scope with view.throttle_scope, and function-based views do not have it, so
# scope becomes None and the limit SILENTLY lets all requests through. The key is user.pk or IP.
class LoginThrottle(UserRateThrottle):
    scope = 'login'


class RegisterThrottle(UserRateThrottle):
    scope = 'register'


class PasswordResetThrottle(UserRateThrottle):
    scope = 'password_reset'


def _normalize_phone(raw: str) -> str:
    """Normalize to digits (+ optionally). Makes the login/register lookup exact."""
    if not raw:
        return ''
    s = re.sub(r'[^\d+]', '', raw)
    return s


def _check_password_strong(password: str, user=None):
    """Calls AUTH_PASSWORD_VALIDATORS and returns an error string or None."""
    try:
        validate_password(password, user=user)
        return None
    except DjangoValidationError as e:
        return ' '.join(e.messages)


def get_tokens(user):
    refresh = RefreshToken.for_user(user)
    return {
        'access': str(refresh.access_token),
        'refresh': str(refresh),
    }


def blacklist_all_tokens(user):
    """Blacklists all of the user's active refresh tokens, so
    all previous sessions (other tabs/devices) stop refreshing.
    Access tokens (15 min) simply live out their term.
    """
    from rest_framework_simplejwt.token_blacklist.models import (
        OutstandingToken, BlacklistedToken,
    )
    outstanding = OutstandingToken.objects.filter(user=user)
    BlacklistedToken.objects.bulk_create(
        [BlacklistedToken(token=t) for t in outstanding],
        ignore_conflicts=True,  # already blacklisted, skip without an error
    )


def _questionnaire_filled_count(user) -> int:
    """How many questionnaire fields are filled in (0..6), for the UI progress bar.
    Note: referral_source/social/other are counted as 1 logical field.
    """
    count = 0
    if user.gender:
        count += 1
    if user.birth_date:
        count += 1
    if (user.gardening_text or '').strip():
        count += 1
    if user.interests:
        count += 1
    if user.referral_source:
        count += 1
    if (user.wishes or '').strip():
        count += 1
    return count


def user_data(user):
    bd = getattr(user, 'birth_date', None)
    return {
        'id': user.id,
        'email': user.email,
        'phone': user.phone,
        'first_name': user.first_name,
        'last_name': user.last_name,
        'np_city': getattr(user, 'np_city', ''),
        'np_city_ref': getattr(user, 'np_city_ref', ''),
        'np_warehouse': getattr(user, 'np_warehouse', ''),
        # Questionnaire
        'gender': getattr(user, 'gender', '') or '',
        'birth_date': bd.isoformat() if bd else None,  # 'YYYY-MM-DD'
        'gardening_text': getattr(user, 'gardening_text', '') or '',
        'interests': getattr(user, 'interests', None) or [],
        'referral_source': getattr(user, 'referral_source', '') or '',
        'referral_social': getattr(user, 'referral_social', '') or '',
        'referral_other': getattr(user, 'referral_other', '') or '',
        'wishes': getattr(user, 'wishes', '') or '',
        'questionnaire_dismissed_at': (
            user.questionnaire_dismissed_at.isoformat()
            if getattr(user, 'questionnaire_dismissed_at', None) else None
        ),
        'questionnaire_filled_count': _questionnaire_filled_count(user),
        'is_staff': user.is_staff,
        'is_superuser': user.is_superuser,
    }


def _validate_questionnaire_payload(data):
    """Returns (cleaned_dict, error_string|None).
    All fields are optional. If a value is provided, we validate it.
    """
    from datetime import date
    cleaned = {}

    if 'gender' in data:
        val = (data.get('gender') or '').strip()
        if val and val not in {k for k, _ in User.GENDER_CHOICES}:
            return None, 'Invalid value for the "gender" field'
        cleaned['gender'] = val

    if 'birth_date' in data:
        raw = data.get('birth_date')
        if not raw:
            cleaned['birth_date'] = None
        else:
            try:
                bd = date.fromisoformat(str(raw))
            except (TypeError, ValueError):
                return None, 'Invalid birth date format (YYYY-MM-DD expected)'
            today = date.today()
            if bd > today:
                return None, 'The birth date cannot be in the future'
            if bd.year < 1920:
                return None, 'The birth date must not be earlier than 1920'
            cleaned['birth_date'] = bd

    if 'gardening_text' in data:
        cleaned['gardening_text'] = (data.get('gardening_text') or '').strip()[:QUESTIONNAIRE_TEXT_MAX]

    if 'interests' in data:
        raw = data.get('interests') or []
        if not isinstance(raw, list):
            return None, 'The "interests" field must be an array'
        if len(raw) > len(INTEREST_KEYS):
            return None, 'Too many values in the "interests" field'
        for key in raw:
            if key not in INTEREST_KEYS:
                return None, f'Unknown interest category: {key}'
        seen = set()
        cleaned['interests'] = [k for k in raw if not (k in seen or seen.add(k))]

    if 'referral_source' in data:
        val = (data.get('referral_source') or '').strip()
        if val and val not in {k for k, _ in User.REFERRAL_SOURCE_CHOICES}:
            return None, 'Invalid value for the "how did you hear about us" field'
        cleaned['referral_source'] = val

    if 'referral_social' in data:
        val = (data.get('referral_social') or '').strip()
        if val and val not in {k for k, _ in User.REFERRAL_SOCIAL_CHOICES}:
            return None, 'Invalid value for the "social network" field'
        cleaned['referral_social'] = val

    if 'referral_other' in data:
        cleaned['referral_other'] = (data.get('referral_other') or '').strip()[:200]

    if 'wishes' in data:
        cleaned['wishes'] = (data.get('wishes') or '').strip()[:QUESTIONNAIRE_TEXT_MAX]

    return cleaned, None


# ── Protection against brute-forcing a login for an account ─────────────
from django.core.cache import cache

LOGIN_MAX_FAILS = 8        # attempts
LOGIN_LOCK_WINDOW = 15 * 60  # seconds


def _login_fail_key(user_id) -> str:
    return f'login_fail:{user_id}'


def _login_locked(user_id) -> bool:
    return (cache.get(_login_fail_key(user_id)) or 0) >= LOGIN_MAX_FAILS


def _login_fail(user_id):
    key = _login_fail_key(user_id)
    cache.set(key, (cache.get(key) or 0) + 1, LOGIN_LOCK_WINDOW)


def _login_reset(user_id):
    cache.delete(_login_fail_key(user_id))


@api_view(['POST'])
@permission_classes([AllowAny])
@throttle_classes([RegisterThrottle])
def register(request):
    from core.antibot import guard
    rejected = guard(request.data)
    if rejected:
        return rejected
    email = request.data.get('email', '').strip().lower()
    password = request.data.get('password', '')
    name = request.data.get('name', '').strip()
    phone = _normalize_phone(request.data.get('phone', ''))

    if not email or not password:
        return Response({'error': 'Email and password are required'}, status=400)

    err = _check_password_strong(password)
    if err:
        return Response({'error': err}, status=400)

    if User.objects.filter(email=email).exists():
        return Response({'error': 'A user with this email already exists'}, status=400)

    if phone and User.objects.filter(phone=phone).exists():
        return Response({'error': 'A user with this phone number already exists'}, status=400)

    parts = name.split()
    first_name = parts[0] if parts else ''
    last_name = ' '.join(parts[1:]) if len(parts) > 1 else ''

    user = User.objects.create_user(
        email=email,
        password=password,
        phone=phone,
        first_name=first_name,
        last_name=last_name,
    )

    try:
        from core.email import send_registration_email
        send_registration_email(user)
    except Exception:
        pass

    return Response({'user': user_data(user), **get_tokens(user)}, status=201)


@api_view(['POST'])
@permission_classes([AllowAny])
@throttle_classes([LoginThrottle])
def login(request):
    identifier = request.data.get('identifier', '').strip()
    password = request.data.get('password', '')

    if not identifier or not password:
        return Response({'error': 'Enter your email/phone and password'}, status=400)

    # Lookup by email or phone: exact match after normalization
    user = None
    if '@' in identifier:
        user = User.objects.filter(email=identifier.lower()).first()
    else:
        phone = _normalize_phone(identifier)
        if phone:
            user = User.objects.filter(phone=phone).first()
            if user is None and not phone.startswith('+'):
                # In case the DB stores it with a '+'
                user = User.objects.filter(phone='+' + phone).first()

    # Protection against brute-forcing a specific account: the IP throttle (10/min) can be bypassed
    # from a botnet/proxy, so we additionally count failed attempts per account.
    if user is not None and _login_locked(user.pk):
        return Response(
            {'error': 'Too many failed login attempts. Try again in 15 minutes.'},
            status=429,
        )

    if user is None or not user.check_password(password):
        if user is not None:
            _login_fail(user.pk)
        return Response({'error': 'Invalid email/phone or password'}, status=401)

    _login_reset(user.pk)

    if not user.is_active:
        return Response({'error': 'Account is blocked'}, status=403)

    user.last_login = timezone.now()
    user.save(update_fields=['last_login'])

    # Retroactively link guest orders (by email OR phone), in case the
    # post_save signal on User did not fire (the order was created after registration
    # as a guest via another button/device).
    try:
        from .signals import link_guest_orders
        link_guest_orders(user)
    except Exception:
        pass

    return Response({'user': user_data(user), **get_tokens(user)})


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def me(request):
    return Response(user_data(request.user))


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def logout(request):
    """Invalidates the refresh token via the blacklist (the client must send the refresh token)."""
    refresh = request.data.get('refresh')
    if not refresh:
        return Response({'ok': True})
    try:
        token = RefreshToken(refresh)
        token.blacklist()
    except Exception:
        # An invalid/expired token is also OK (logout succeeds)
        pass
    return Response({'ok': True})


@api_view(['PATCH'])
@permission_classes([IsAuthenticated])
def update_profile(request):
    user = request.user
    old_phone = user.phone
    old_email = user.email
    allowed = ['first_name', 'last_name', 'phone', 'np_city', 'np_city_ref', 'np_warehouse']
    for field in allowed:
        if field in request.data:
            setattr(user, field, request.data[field])

    if 'email' in request.data:
        new_email = request.data['email'].strip().lower()
        if new_email and new_email != user.email:
            if User.objects.filter(email=new_email).exclude(pk=user.pk).exists():
                return Response({'error': 'This email is already in use'}, status=400)
            user.email = new_email

    new_password = request.data.get('new_password', '')
    pwd_changed = False
    if new_password:
        err = _check_password_strong(new_password, user=user)
        if err:
            return Response({'error': err}, status=400)
        user.set_password(new_password)
        pwd_changed = True

    # ─── Questionnaire ───
    if any(f in request.data for f in QUESTIONNAIRE_FIELDS):
        cleaned, err = _validate_questionnaire_payload(request.data)
        if err:
            return Response({'error': err}, status=400)
        for f, v in cleaned.items():
            setattr(user, f, v)

    # Pop-up: "do not show" / "remind later"
    if request.data.get('questionnaire_dismissed') is True:
        user.questionnaire_dismissed_at = timezone.now()
    snooze = request.data.get('questionnaire_snooze')
    if snooze:
        try:
            days = max(1, min(int(snooze), 90))
            user.questionnaire_remind_after = timezone.now() + timedelta(days=days)
        except (TypeError, ValueError):
            pass

    if pwd_changed:
        # A password change invalidates all old sessions; the current device gets
        # a fresh pair, so the user is not kicked out of their own tab.
        # Atomic: either password + blacklist + new pair together, or nothing.
        from django.db import transaction
        with transaction.atomic():
            user.save()
            blacklist_all_tokens(user)
            tokens = get_tokens(user)
        data = user_data(user)
        data['_tokens'] = tokens
    else:
        user.save()
        data = user_data(user)

    # Phone or email was added/changed in the profile (registration may have been without them) ->
    # link guest orders by the new key. The post_save signal does not fire here
    # (guard on created), so we link manually. The function is idempotent.
    if user.phone != old_phone or user.email != old_email:
        try:
            from .signals import link_guest_orders
            link_guest_orders(user)
        except Exception:
            pass

    return Response(data)


@api_view(['POST'])
@permission_classes([AllowAny])
@throttle_classes([RegisterThrottle])
def finalize_guest(request):
    """Creates a User from a password for a guest who has just placed an order.

    Body: {email, password, order_number}
    We check that the email matches customer_email in the given order,
    otherwise anyone could 'hijack' someone else's order.

    The post_save signal on User automatically attaches all guest orders with this email.
    """
    from core.antibot import guard
    rejected = guard(request.data)
    if rejected:
        return rejected
    email = (request.data.get('email') or '').strip().lower()
    password = request.data.get('password') or ''
    order_number = (request.data.get('order_number') or '').strip()

    if not email or not password or not order_number:
        return Response({'error': 'Email, password and order number are required'}, status=400)

    from orders.models import Order
    order = Order.objects.filter(order_number=order_number).first()
    if not order:
        return Response({'error': 'Order not found'}, status=404)

    if (order.customer_email or '').strip().lower() != email:
        return Response({'error': 'The email does not match the order data'}, status=403)

    if order.user_id is not None:
        return Response({'error': 'This order is already linked to an account'}, status=409)

    if User.objects.filter(email=email).exists():
        return Response({'error': 'A user with this email already exists. Please sign in.', 'code': 'email_taken'}, status=409)

    err = _check_password_strong(password)
    if err:
        return Response({'error': err}, status=400)

    parts = (order.customer_name or '').split()
    first_name = parts[0] if parts else ''
    last_name = ' '.join(parts[1:]) if len(parts) > 1 else ''
    phone = _normalize_phone(order.customer_phone or '')

    user = User.objects.create_user(
        email=email,
        password=password,
        phone=phone,
        first_name=first_name,
        last_name=last_name,
    )
    # The link_guest_orders_on_user_create signal has already attached all guest
    # orders with this email, including the current order.

    try:
        from core.email import send_registration_email
        send_registration_email(user)
    except Exception:
        pass

    return Response(
        {'user': user_data(user), 'order_number': order_number, **get_tokens(user)},
        status=201,
    )


import os
from django.contrib.auth.tokens import default_token_generator
from django.utils.http import urlsafe_base64_encode, urlsafe_base64_decode
from django.utils.encoding import force_bytes, force_str

@api_view(['POST'])
@permission_classes([AllowAny])
@throttle_classes([PasswordResetThrottle])
def password_reset_request(request):
    from core.antibot import guard
    rejected = guard(request.data)
    if rejected:
        return rejected
    email = request.data.get('email', '').strip().lower()
    if not email:
        return Response({'error': 'Enter your email'}, status=400)
    user = User.objects.filter(email=email).first()
    if user:
        from django.conf import settings as conf
        from core.email import send_email
        uid = urlsafe_base64_encode(force_bytes(user.pk))
        token = default_token_generator.make_token(user)
        site_url = getattr(conf, 'SITE_URL', os.getenv('SITE_URL', 'http://localhost:5173')).rstrip('/')
        reset_url = f"{site_url}/reset-password?uid={uid}&token={token}"
        send_email(
            to=user.email,
            subject='Password reset',
            template='password_reset',
            context={'user': user, 'reset_url': reset_url},
        )
    # Always return OK (do not reveal whether the email exists)
    return Response({'ok': True})


@api_view(['POST'])
@permission_classes([AllowAny])
@throttle_classes([PasswordResetThrottle])
def password_reset_confirm(request):
    uid = request.data.get('uid', '')
    token = request.data.get('token', '')
    password = request.data.get('password', '')

    if not uid or not token or not password:
        return Response({'error': 'Invalid data'}, status=400)

    try:
        pk = force_str(urlsafe_base64_decode(uid))
        user = User.objects.get(pk=pk)
    except Exception:
        return Response({'error': 'Invalid link'}, status=400)

    err = _check_password_strong(password, user=user)
    if err:
        return Response({'error': err}, status=400)

    if not default_token_generator.check_token(user, token):
        return Response({'error': 'The link is invalid or has already been used'}, status=400)

    # A password reset invalidates all previous sessions (a stolen refresh token does not survive).
    from django.db import transaction
    with transaction.atomic():
        user.set_password(password)
        user.save()
        blacklist_all_tokens(user)
    return Response({'ok': True})


from django.contrib.admin.views.decorators import staff_member_required
from django.http import JsonResponse as _JsonResponse
from django.db.models import Q as _Q

@staff_member_required
def search_users(request):
    q = request.GET.get('q', '').strip()
    if not q:
        return _JsonResponse([], safe=False)
    users = User.objects.filter(
        _Q(first_name__icontains=q) | _Q(last_name__icontains=q) |
        _Q(email__icontains=q) | _Q(phone__icontains=q)
    ).order_by('first_name', 'last_name')[:15]
    from orders.models import format_phone
    result = []
    for u in users:
        result.append({
            'id': u.id,
            'pib': ' '.join(filter(None, [u.first_name, u.last_name])),
            'email': u.email,
            'phone': format_phone(u.phone),
            'np_city': u.np_city,
            'np_city_ref': u.np_city_ref,
            'np_warehouse': u.np_warehouse,
        })
    return _JsonResponse(result, safe=False)
