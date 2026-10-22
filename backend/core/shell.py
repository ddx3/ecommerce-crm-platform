"""Shell view: serves index.html with an injected SEO <head> for all navigation pages (for humans and
bots alike - no User-Agent sniffing).

nginx proxies HTML navigation here; static/assets/api/media bypass it. React renders the body; we
only insert unique meta/OG/JSON-LD from the DB (core.seo) into <head>, plus the correct HTTP status
(404 for missing slugs - the soft-404 fix).

The fragment is cached in Redis by (index.html mtime + path + query). Versioning by mtime =
auto-invalidation on deploy (new asset hashes -> new mtime -> new keys).
"""
from __future__ import annotations

import os
import re

from django.conf import settings
from django.core.cache import cache
from django.http import HttpResponse, HttpResponsePermanentRedirect
from django.utils.html import escape
from django.views.decorators.http import require_safe

from core import seo

SITE_NAME = lambda: getattr(settings, 'SITE_NAME', 'Magiya Roslyn')  # noqa: E731

_TITLE_RE = re.compile(r'<title>.*?</title>', re.S)
_index_cache: dict = {}

_FALLBACK_HTML = (
    '<!doctype html><html lang="uk"><head><meta charset="UTF-8">'
    '<meta name="viewport" content="width=device-width, initial-scale=1.0">'
    '<title>Magiya Roslyn</title></head><body><div id="root"></div></body></html>'
)

# Static pages: path -> (title part, description).
_STATIC_PAGES = {
    '/about': ('About us', 'An online shop of products for the garden, vegetable plot and plants, "Magiya Roslyn".'),
    '/contacts': ('Contacts', 'Get in touch with us: phone, email, opening hours.'),
    '/delivery': ('Delivery and payment', 'Nova Poshta delivery terms, payment methods.'),
    '/offer': ('Public offer', 'Public offer agreement of the online shop.'),
    '/privacy': ('Privacy policy', 'How we process and protect your personal data.'),
    '/returns': ('Returns and exchange', 'Terms for returning and exchanging goods.'),
    '/reviews': ('Customer reviews', 'Reviews from our customers about products and service.'),
    '/blog': ('Blog', 'Tips on caring for plants, the garden and the vegetable plot.'),
    '/courses': ('Courses', 'Training courses on plant care and gardening.'),
}


# ── reading index.html ───────────────────────────────────────────────────────

def _index_path() -> str:
    return os.path.join(settings.FRONTEND_DIST, 'index.html')


def _read_index() -> tuple[str | None, float]:
    """Returns (html without the static <title>, mtime). (None, 0) if the file does not exist."""
    path = _index_path()
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return None, 0.0
    cached = _index_cache.get('v')
    if cached and cached[0] == mtime:
        return cached[1], mtime
    try:
        with open(path, encoding='utf-8') as f:
            html = f.read()
    except OSError:
        return None, 0.0
    # Remove the static <title> - otherwise there will be two <title> tags (the double-title
    # pitfall).
    html = _TITLE_RE.sub('', html, count=1)
    _index_cache['v'] = (mtime, html)
    return html, mtime


def _inject(html: str, fragment: str) -> str:
    if '</head>' in html:
        return html.replace('</head>', fragment + '\n</head>', 1)
    return html + fragment


# ── view ─────────────────────────────────────────────────────────────────────

@require_safe       # GET and HEAD: HEAD checks (uptime monitors, link checkers) would otherwise see 405
def shell_view(request):
    html, mtime = _read_index()
    if html is None:
        return HttpResponse(_FALLBACK_HTML, content_type='text/html; charset=utf-8')

    path = request.path.rstrip('/') or '/'
    qs = request.GET.urlencode()
    cache_key = f'seo:head:{mtime}:{path}:{qs}'
    cached = cache.get(cache_key)
    if cached is None:
        fragment, status = _render(path, request.GET)
        cache.set(cache_key, {'f': fragment, 's': status}, 300)
    else:
        fragment, status = cached['f'], cached['s']

    # Before returning a 404, try a 301 to the canonical URL (old Horoshop URLs
    # /{alias}/ and bookmarks where alias == the current slug of a product/bundle/category).
    if status == 404:
        target = _legacy_redirect(path)
        if target:
            return HttpResponsePermanentRedirect(target)

    body = _inject(html, fragment)
    return HttpResponse(body, content_type='text/html; charset=utf-8', status=status)


# Root paths that can NOT be a legacy product (real routes / service paths).
_RESERVED_ROOT = {
    'catalog', 'blog', 'courses', 'reviews', 'about', 'contacts', 'delivery',
    'offer', 'privacy', 'returns', 'checkout', 'login', 'profile', 'orders',
    'liked', 'my-courses', 'reset-password', 'maintenance', 'cart', 'favicon.ico',
}


def _match_slug(s: str) -> str | None:
    """Exact slug match with an active product/bundle/category/brand."""
    from catalog.models import Product, Bundle, Category, Brand
    if Product.objects.filter(slug=s, is_active=True).exists():
        return f'/product/{s}'
    if Bundle.objects.filter(slug=s, is_active=True).exists():
        return f'/bundle/{s}'
    if Category.objects.filter(slug=s, is_active=True).exists():
        return f'/catalog?category={s}'
    if Brand.objects.filter(slug=s, is_active=True).exists():
        return f'/catalog?brand={s}'
    return None


def _fuzzy_redirect(s: str) -> str | None:
    """Fuzzy matching of an old product/bundle slug to a new one. Horoshop used a
    different transliteration than slugify (especially Latin brands/models), so
    y→i normalization is not enough. Only long slugs and a high threshold, so we do NOT
    redirect to a random product. PostgreSQL only (pg_trgm); silently skipped on SQLite."""
    if len(s) < 12:
        return None
    try:
        from django.contrib.postgres.search import TrigramSimilarity
    except Exception:
        return None
    from catalog.models import Product, Bundle
    for model, prefix in ((Product, '/product/'), (Bundle, '/bundle/')):
        try:
            best = (model.objects.filter(is_active=True)
                    .annotate(sim=TrigramSimilarity('slug', s))
                    .filter(sim__gt=0.45).order_by('-sim')
                    .values_list('slug', flat=True).first())
        except Exception:
            return None
        if best:
            return f'{prefix}{best}'
    return None


def _legacy_redirect(path: str) -> str | None:
    """301 for root /{slug}: restores the canonical URL for old Horoshop URLs
    (/{alias}/) and bookmarks. DB queries only on paths that would otherwise give a 404 (rare).
    Three levels: direct match → transliteration normalization (Cyrillic "и" rendered as "y" vs "i") → fuzzy."""
    seg = path.strip('/')
    if not seg or '/' in seg or seg in _RESERVED_ROOT:
        return None
    # 1. Direct match (slug unchanged).
    t = _match_slug(seg)
    if t:
        return t
    # 2. Horoshop transliterated the Cyrillic letter "и" as "y", while slugify does it as "i". Normalize and try again.
    norm = seg.replace('y', 'i')
    if norm != seg:
        t = _match_slug(norm)
        if t:
            return t
    # 3. Fuzzy matching for products with complex names (Latin brands/models).
    return _fuzzy_redirect(norm)


# ── routing (mirrors react-router) ───────────────────────────────────────────

def _render(path: str, query) -> tuple[str, int]:
    if path == '/':
        return _home(), 200
    if path == '/catalog':
        return _catalog(query), 200
    if path.startswith('/product/'):
        return _product(path[len('/product/'):])
    if path.startswith('/bundle/'):
        return _bundle(path[len('/bundle/'):])
    if path == '/blog':
        return _static('/blog'), 200
    if path.startswith('/blog/'):
        return _blog_post(path[len('/blog/'):])
    if path == '/courses':
        return _static('/courses'), 200
    if path.startswith('/course/'):
        # /course/:slug/learn[/:lessonId] — private (access only after purchase).
        rest = path[len('/course/'):]
        if '/' in rest:
            return _private('Course lesson'), 200
        return _course(rest)
    if any(path == p or path.startswith(p) for p in seo.PRIVATE_PREFIXES):
        return _private(), 200
    if path in _STATIC_PAGES:
        return _static(path), 200
    return _not_found()


# ── page builders ────────────────────────────────────────────────────────────

def _home() -> str:
    from catalog.models import SiteSettings
    try:
        ss = SiteSettings.get()
    except Exception:
        ss = None
    title = f'{SITE_NAME()} — products for the garden, vegetable plot and plants'
    desc = ('Online shop "Magiya Roslyn": fertilizers, plant protection products, seeds, '
            'products for the garden and vegetable plot. Delivery across Ukraine.')
    jsonld = [seo.website_jsonld(), seo.organization_jsonld(ss)]
    gsv = getattr(settings, 'GOOGLE_SITE_VERIFICATION', '')
    extra = f'<meta name="google-site-verification" content="{escape(gsv)}">' if gsv else ''
    return seo.render_head(
        title=title, description=desc, canonical=seo.abs_url('/'),
        robots='index,follow', og_type='website', jsonld=jsonld, extra_meta=extra,
    )


def _catalog(query) -> str:
    from catalog.models import Category, Brand
    robots = seo.resolve_catalog_robots(query)
    canonical = seo.catalog_canonical(query)
    cat_slug = query.get('category')
    brand_slug = query.get('brand')
    title = f'Product catalog | {SITE_NAME()}'
    desc = 'Catalog of products for the garden, vegetable plot and plant care.'
    if cat_slug and not brand_slug:
        cat = Category.objects.filter(slug=cat_slug, is_active=True).first()
        if cat:
            title = cat.meta_title or f'{cat.name} — catalog | {SITE_NAME()}'
            desc = cat.meta_description or f'{cat.name}: wide choice, prices and availability. {SITE_NAME()}.'
    elif brand_slug and not cat_slug:
        brand = Brand.objects.filter(slug=brand_slug, is_active=True).first()
        if brand:
            title = brand.meta_title or f'{brand.name} — brand products | {SITE_NAME()}'
            desc = brand.meta_description or f'Products of the brand {brand.name}. {SITE_NAME()}.'
    return seo.render_head(
        title=title, description=seo.truncate(desc), canonical=canonical,
        robots=robots, og_type='website',
    )


def _product(slug: str) -> tuple[str, int]:
    from catalog.models import Product
    try:
        p = (Product.objects.select_related('brand')
             .prefetch_related('variants', 'images', 'categories')
             .get(slug=slug, is_active=True))
    except Product.DoesNotExist:
        return _not_found()
    avg, count = seo._rating(p)
    title = p.meta_title or f'{p.name} — buy at a good price | {SITE_NAME()}'
    desc = seo.truncate(p.meta_description or seo.strip_html(p.description))
    image = seo.abs_media(seo._product_main_image(p))
    crumbs = [('Home', seo.abs_url('/'))]
    cat = p.categories.first()
    if cat:
        crumbs += [(c.name, c.get_absolute_url()) for c in seo._category_chain(cat)]
    crumbs.append((p.name, seo.abs_url(f'/product/{p.slug}')))
    jsonld = [seo.product_jsonld(p, avg=avg, count=count), seo.breadcrumb_jsonld(crumbs)]
    return seo.render_head(
        title=title, description=desc, canonical=seo.abs_url(f'/product/{p.slug}'),
        robots='index,follow', image=image, og_type='product', jsonld=jsonld,
    ), 200


def _bundle(slug: str) -> tuple[str, int]:
    from catalog.models import Bundle
    try:
        b = (Bundle.objects.prefetch_related('items__product_variant', 'images', 'categories')
             .get(slug=slug, is_active=True))
    except Bundle.DoesNotExist:
        return _not_found()
    avg, count = seo._rating(b)
    title = b.meta_title or f'{b.name} — bundle | {SITE_NAME()}'
    desc = seo.truncate(b.meta_description or seo.strip_html(b.description))
    image = seo.abs_media(seo._bundle_main_image(b))
    crumbs = [('Home', seo.abs_url('/'))]
    cat = b.categories.first()
    if cat:
        crumbs += [(c.name, c.get_absolute_url()) for c in seo._category_chain(cat)]
    crumbs.append((b.name, seo.abs_url(f'/bundle/{b.slug}')))
    jsonld = [seo.bundle_jsonld(b, avg=avg, count=count), seo.breadcrumb_jsonld(crumbs)]
    return seo.render_head(
        title=title, description=desc, canonical=seo.abs_url(f'/bundle/{b.slug}'),
        robots='index,follow', image=image, og_type='product', jsonld=jsonld,
    ), 200


def _blog_post(slug: str) -> tuple[str, int]:
    from blog.models import BlogPost
    try:
        post = BlogPost.objects.get(slug=slug, is_active=True)
    except BlogPost.DoesNotExist:
        return _not_found()
    title = post.meta_title or f'{post.title} | {SITE_NAME()}'
    desc = seo.truncate(post.meta_description or seo.strip_html(post.content))
    image = seo.abs_media(post.preview_image)
    crumbs = [
        ('Home', seo.abs_url('/')),
        ('Blog', seo.abs_url('/blog')),
        (post.title, seo.abs_url(f'/blog/{post.slug}')),
    ]
    jsonld = [seo.article_jsonld(post), seo.breadcrumb_jsonld(crumbs)]
    return seo.render_head(
        title=title, description=desc, canonical=seo.abs_url(f'/blog/{post.slug}'),
        robots='index,follow', image=image, og_type='article', jsonld=jsonld,
    ), 200


def _course(slug: str) -> tuple[str, int]:
    from courses.models import Course
    try:
        c = Course.objects.get(slug=slug)
    except Course.DoesNotExist:
        return _not_found()
    # A hidden course is not indexed, but not 404'd either: its buyers must still be
    # able to open the page (access is decided by CoursePurchase, not is_active).
    if not c.is_active:
        return seo.render_head(
            title=c.meta_title or f'{c.name} — course | {SITE_NAME()}',
            robots='noindex,nofollow', og_type='website',
        ), 200
    title = c.meta_title or f'{c.name} — course | {SITE_NAME()}'
    desc = seo.truncate(c.meta_description or c.short_description or seo.strip_html(c.description))
    image = seo.abs_media(c.cover_image)
    crumbs = [
        ('Home', seo.abs_url('/')),
        ('Courses', seo.abs_url('/courses')),
        (c.name, seo.abs_url(f'/course/{c.slug}')),
    ]
    jsonld = [seo.course_jsonld(c), seo.breadcrumb_jsonld(crumbs)]
    return seo.render_head(
        title=title, description=desc, canonical=seo.abs_url(f'/course/{c.slug}'),
        robots='index,follow', image=image, og_type='website', jsonld=jsonld,
    ), 200


def _static(path: str) -> str:
    name, desc = _STATIC_PAGES[path]
    title = f'{name} | {SITE_NAME()}'
    return seo.render_head(
        title=title, description=seo.truncate(desc), canonical=seo.abs_url(path),
        robots='index,follow', og_type='website',
    )


def _private(label: str = '') -> str:
    title = f'{label} | {SITE_NAME()}' if label else SITE_NAME()
    return seo.render_head(title=title, robots='noindex,nofollow', og_type='website')


def _not_found() -> tuple[str, int]:
    frag = seo.render_head(
        title=f'Page not found | {SITE_NAME()}',
        robots='noindex,follow', og_type='website',
    )
    return frag, 404
