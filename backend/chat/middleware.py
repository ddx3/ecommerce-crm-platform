"""WS auth: chains Django session auth + JWT (query) + guest_id (query).

Order of resolution for scope['user']:
  1. AuthMiddlewareStack already populates from Django session cookie (admin).
  2. If a JWT `token` query arg is present and valid, it overrides (customer site uses JWT).
  3. Otherwise scope['user'] keeps whatever the session gave (AnonymousUser if none).

Always sets scope['guest_id'] from `guest` query arg (empty string if missing).
"""

from urllib.parse import parse_qs

from channels.auth import AuthMiddlewareStack
from channels.db import database_sync_to_async
from channels.middleware import BaseMiddleware
from django.contrib.auth.models import AnonymousUser


@database_sync_to_async
def _user_from_token(raw_token: str):
    if not raw_token:
        return None
    try:
        from rest_framework_simplejwt.authentication import JWTAuthentication
        from django.contrib.auth import get_user_model

        validated = JWTAuthentication().get_validated_token(raw_token)
        user_id = validated.get('user_id')
        if not user_id:
            return None
        User = get_user_model()
        try:
            return User.objects.get(pk=user_id, is_active=True)
        except User.DoesNotExist:
            return None
    except Exception:
        return None


class _QueryAuthMiddleware(BaseMiddleware):
    """Reads JWT/guest_id from the query string. Runs after AuthMiddlewareStack."""

    async def __call__(self, scope, receive, send):
        qs = parse_qs((scope.get('query_string') or b'').decode('utf-8'))
        token = (qs.get('token') or [''])[0]
        guest_id = (qs.get('guest') or [''])[0]

        scope.setdefault('user', AnonymousUser())
        if token:
            jwt_user = await _user_from_token(token)
            if jwt_user is not None:
                scope['user'] = jwt_user
        scope['guest_id'] = guest_id or ''
        return await super().__call__(scope, receive, send)


def ChatAuthMiddleware(inner):
    """Public stack: Django session → JWT-from-query → guest_id-from-query."""
    return AuthMiddlewareStack(_QueryAuthMiddleware(inner))
