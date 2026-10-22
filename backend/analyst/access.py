"""Access to the Analyst: shop owner only (same as for the financial 'Analytics' page)."""
from __future__ import annotations

from functools import wraps

from django.conf import settings
from django.core.exceptions import PermissionDenied


def is_owner(user) -> bool:
    if not getattr(user, 'is_authenticated', False) or not getattr(user, 'is_staff', False):
        return False
    email = (getattr(user, 'email', '') or '').strip().lower()
    return bool(email) and email == (settings.ANALYST_OWNER_EMAIL or '').strip().lower()


def owner_only(view):
    """403 for everyone except the owner. Applied under admin_view, so anonymous users are already filtered
out."""
    @wraps(view)
    def wrapper(request, *args, **kwargs):
        if not is_owner(request.user):
            raise PermissionDenied
        return view(request, *args, **kwargs)
    return wrapper
