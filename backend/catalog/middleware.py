"""Maintenance mode middleware.

When SiteSettings.maintenance_mode == True — regular visitors see a 503
with a message. Exceptions:
  • /admin/* — so the mode can be switched back off
  • /api/auth/* — so staff can log in via JWT and switch it off via the CRM
  • staff users (request.user.is_staff) — full access
  • static and media — so the admin/placeholder page can load its assets

The flag is cached for 30 seconds to avoid hitting the DB on every request.
"""
from django.conf import settings
from django.core.cache import cache
from django.http import JsonResponse, HttpResponse
from django.template.loader import render_to_string


_CACHE_KEY = 'site_settings:maintenance'
_CACHE_TTL = 30  # seconds

# The message is hardcoded — there is no point giving a choice in the CRM, the text is always the
# same.
MAINTENANCE_MESSAGE = 'The site is temporarily down for maintenance. Please try again later.'


def _get_maintenance():
    """Returns (enabled: bool, message: str). Cached for _CACHE_TTL seconds."""
    cached = cache.get(_CACHE_KEY)
    if cached is not None:
        return cached
    try:
        from catalog.models import SiteSettings
        obj = SiteSettings.objects.filter(pk=1).only('maintenance_mode').first()
        enabled = bool(obj and obj.maintenance_mode)
    except Exception:
        # If the DB is down or the migration is not applied yet — do not block the site.
        enabled = False
    data = (enabled, MAINTENANCE_MESSAGE)
    cache.set(_CACHE_KEY, data, _CACHE_TTL)
    return data


def _is_exempt(request) -> bool:
    """Paths that must work even in maintenance mode."""
    path = request.path
    if path.startswith('/admin/'):
        return True
    if path.startswith(settings.STATIC_URL) or path.startswith('/' + settings.STATIC_URL.lstrip('/')):
        return True
    if path.startswith(settings.MEDIA_URL) or path.startswith('/' + settings.MEDIA_URL.lstrip('/')):
        return True
    # JWT login is needed so staff can log in from the frontend and switch the mode off.
    if path.startswith('/api/auth/') or path.startswith('/api/token/'):
        return True
    # The endpoint that tells whether we are under maintenance — let it always respond.
    if path == '/api/maintenance/':
        return True
    return False


class MaintenanceModeMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        enabled, message = _get_maintenance()
        if not enabled:
            return self.get_response(request)

        if _is_exempt(request):
            return self.get_response(request)

        # Staff — full access (to test the site in maintenance mode).
        user = getattr(request, 'user', None)
        if user is not None and user.is_authenticated and user.is_staff:
            return self.get_response(request)

        # API → JSON 503; the frontend will intercept it and show the page.
        if request.path.startswith('/api/'):
            return JsonResponse(
                {'maintenance': True, 'message': message},
                status=503,
            )

        # The rest (rare — the SPA frontend is separate) → simple HTML 503.
        html = render_to_string('maintenance.html', {'message': message})
        return HttpResponse(html, status=503, content_type='text/html; charset=utf-8')
