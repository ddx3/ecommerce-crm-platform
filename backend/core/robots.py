"""robots.txt - dynamic, so the Sitemap URL and Disallow live in one place with the canonical domain
(core.seo.abs_url)."""
from django.http import HttpResponse
from django.views.decorators.http import require_GET

from core.seo import abs_url

_LINES = [
    'User-agent: *',
    'Disallow: /checkout',
    'Disallow: /login',
    'Disallow: /profile',
    'Disallow: /orders',
    'Disallow: /liked',
    'Disallow: /my-courses',
    'Disallow: /reset-password',
    'Disallow: /api/',
    'Disallow: /admin/',
    'Disallow: /summernote/',
    'Allow: /',
]


@require_GET
def robots_txt(request):
    body = '\n'.join(_LINES + [f'Sitemap: {abs_url("/sitemap.xml")}', ''])
    return HttpResponse(body, content_type='text/plain; charset=utf-8')
