"""Invisible anti-bot protection for public forms.

Idea: any legitimate form submission comes from the frontend, which first receives a
server-signed token (`GET /api/antibot-token/`) and then adds it to the
POST request. A bot hitting the API directly has no such token and gets rejected.
No captchas and no user-visible actions.

Two lines of defense:
  1. honeypot field `hp_field`: hidden, a human does not fill it; a bot that
     autofills everything gets caught.
  2. signed time-token `hp_ts`: has a valid server signature, is not expired
     and not 'too fresh' (the form cannot be filled instantly).

Disabled via `settings.ANTIBOT_ENABLED = False` (for tests/debugging).
"""
from __future__ import annotations

import logging
import time

from django.conf import settings
from django.core import signing
from rest_framework.decorators import api_view, permission_classes, throttle_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.throttling import UserRateThrottle

log = logging.getLogger(__name__)

_SALT = 'antibot.v1'
MAX_AGE = 3 * 60 * 60   # sec: the token lives 3 hours

HONEYPOT_FIELD = 'hp_field'
TOKEN_FIELD = 'hp_ts'


def issue_token() -> str:
    return signing.dumps({'t': time.time()}, salt=_SALT)


def is_human(data) -> tuple[bool, str]:
    """Returns (ok, reason). ok=True means the request looks human."""
    if not getattr(settings, 'ANTIBOT_ENABLED', True):
        return True, ''
    if not isinstance(data, dict):
        return False, 'bad_request'

    # 1) honeypot
    if (data.get(HONEYPOT_FIELD) or '').strip():
        return False, 'honeypot'

    # 2) signed time-token
    token = data.get(TOKEN_FIELD) or ''
    if not token:
        return False, 'missing'
    try:
        signing.loads(token, salt=_SALT, max_age=MAX_AGE)
    except signing.SignatureExpired:
        return False, 'expired'
    except signing.BadSignature:
        return False, 'bad_sign'
    return True, ''


def antibot_reject():
    """Standard rejection response. `code: antibot` lets the frontend reissue the
    token and repeat the request once (in case of an expired token)."""
    return Response(
        {'error': 'Could not verify the request. Refresh the page and try again.',
         'code': 'antibot'},
        status=400,
    )


def guard(data, label: str = '') -> Response | None:
    """Helper for views: returns a rejection Response or None if all is well.
    label is context for logs (e.g. 'screenshot:12496') so in production it is visible
    which form was rejected and why (missing/expired/honeypot)."""
    ok, reason = is_human(data)
    if ok:
        return None
    log.warning('antibot reject [%s]: %s', label or 'unknown', reason)
    return antibot_reject()


# UserRateThrottle, not ScopedRateThrottle: in Scoped, `allow_request` overwrites
# self.scope with view.throttle_scope, and function-based views do not have it, so
# scope becomes None and the limit SILENTLY lets all requests through. The key is user.pk or IP.
class _TokenThrottle(UserRateThrottle):
    scope = 'antibot_token'


@api_view(['GET'])
@permission_classes([AllowAny])
@throttle_classes([_TokenThrottle])
def antibot_token(request):
    return Response({'token': issue_token()})
