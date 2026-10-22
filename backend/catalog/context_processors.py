"""Context processors for admin templates."""
from __future__ import annotations

# TTL-cache key for the sidebar's initial counters. Reset from
# notifications.broadcast_notification_counts() on any data change,
# so that after a notification is deleted/handled a reload doesn't flash a stale badge.
SIDEBAR_COUNTS_CACHE_KEY = 'sidebar_counts:v2'


def sidebar_counts(request):
    """Initial counts for sidebar badges — so they don't flicker on reload.

    Counted for staff only: no extra SQL queries for non-staff.
    """
    if not getattr(request, 'user', None) or not request.user.is_authenticated or not request.user.is_staff:
        return {'sidebar_initial_counts': '{}'}

    # Only an initial placeholder against flicker on reload — live numbers come via
    # WS-connect + AJAX /admin/notifications/count/, which count directly without a cache.
    # So the short TTL cache self-corrects and doesn't show stale badges.
    # The key is global: counts are the same for all staff (as with the WS broadcast).
    from django.core.cache import cache
    payload = cache.get(SIDEBAR_COUNTS_CACHE_KEY)
    if payload is None:
        try:
            from .notifications import get_notification_counts
            counts = get_notification_counts()
        except Exception:
            counts = {}

        # Chat — the SINGLE source: catalog.notifications.chat_active_count() (the same
        # count as in get_notification_counts/WS payload), so initial/WS match
        # and don't flicker. The badge = number of ACTIVE (open) conversations, not unread ones.
        try:
            from .notifications import chat_active_count
            chat_active = chat_active_count()
        except Exception:
            chat_active = 0

        payload = {
            'payments':   counts.get('payments', 0),
            'requisites': counts.get('requisites', 0),
            'reviews':   counts.get('reviews', 0),
            'questions': counts.get('questions', 0),
            'stock':     counts.get('stock', 0),
            'stock_out': counts.get('stock_out', 0),
            'stock_low': counts.get('stock_low', 0),
            'dropsync':  counts.get('dropsync', 0),
            'analyst':   counts.get('analyst', 0),
            'chat':      chat_active,
        }
        cache.set(SIDEBAR_COUNTS_CACHE_KEY, payload, 15)

    # "Analyst" — for the owner only: for the rest of staff, not a line in the sidebar and
    # not a number in the badge.
    from analyst.access import is_owner
    analyst_visible = is_owner(request.user)
    if not analyst_visible:
        payload = {**payload, 'analyst': 0}

    import json
    from django.conf import settings as _settings
    return {
        'sidebar_initial_counts': json.dumps(payload),
        'ANALYST_VISIBLE': analyst_visible,
        # Feature flags for admin templates (sidebar / notifications).
        'DROPSYNC_ENABLED': getattr(_settings, 'DROPSYNC_ENABLED', True),
        # COURSES_ENABLED=False hides the CRM "Courses" tab in base_site.html
        # (the courses section in the admin is not registered anyway — courses/admin.py).
        'COURSES_ENABLED': getattr(_settings, 'COURSES_ENABLED', True),
        # Frontend site URL — for the "Open site" button in the sidebar.
        'SITE_URL': getattr(_settings, 'SITE_URL', '/'),
        # Owner e-mail: gates the owner-only sidebar items in base_site.html.
        'OWNER_EMAIL': getattr(_settings, 'OWNER_EMAIL', ''),
        # DEBUG — in production (False) hide the "Settings" button in the sidebar.
        'DEBUG': bool(getattr(_settings, 'DEBUG', False)),
    }
