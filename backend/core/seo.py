"""SEO: generation of the <head> fragment (meta tags, Open Graph, JSON-LD) for the shell view.

The single source of truth for server-side SEO injection. React renders the page body, while this
module builds a valid <head> for bots/social networks/fast indexing: unique title/description,
canonical, robots, OG/Twitter and structured data.

Absolute URLs are built from the canonical domain (settings.SITE_URL) - not from the request host,
so that behind a proxy/www duplicates are not created. JSON-LD is inlined only via
core.jsonutils.js_json (it escapes U+2028/U+2029 and '<').
"""
from __future__ import annotations

import re
from decimal import Decimal

from django.conf import settings
from django.db.models import Avg, Count
from django.utils.html import escape

from core.jsonutils import js_json


# ── basic helpers ────────────────────────────────────────────────────────────

def _site_base() -> str:
    return (getattr(settings, 'SITE_URL', '') or '').rstrip('/')


def _site_name() -> str:
    return getattr(settings, 'SITE_NAME', 'Magiya Roslyn')


def abs_url(path: str) -> str:
    """Absolute frontend URL: SITE_URL + path (path must start with '/')."""
    if not path.startswith('/'):
        path = '/' + path
    return f"{_site_base()}{path}"


def abs_media(image) -> str | None:
    """Absolute URL of the ORIGINAL media file on the canonical domain.

    MEDIA_URL='media/' is relative -> image.url may lack the leading '/'. We normalize the slash,
    otherwise we get 'https://domainmedia/...'. og:image needs the original (>=1200x630), not a
    thumbnail.
    """
    if not image:
        return None
    try:
        url = image.url
    except (ValueError, Exception):
        return None
    if url.startswith(('http://', 'https://')):
        return url
    if not url.startswith('/'):
        url = '/' + url
    return f"{_site_base()}{url}"


def strip_html(html: str) -> str:
    text = re.sub(r'<[^>]+>', ' ', html or '')
    return re.sub(r'\s+', ' ', text).strip()


def truncate(text: str, limit: int = 160) -> str:
    text = (text or '').strip()
    if len(text) <= limit:
        return text
    return text[:limit].rsplit(' ', 1)[0].rstrip() + '…'


def _rating(obj) -> tuple[float | None, int]:
    """(average rating, count) over approved top-level reviews with a rating."""
    agg = obj.reviews.filter(
        is_approved=True, parent__isnull=True, rating__isnull=False
    ).aggregate(a=Avg('rating'), c=Count('id'))
    avg = agg['a']
    return (round(avg, 1) if avg is not None else None, agg['c'] or 0)


def _review_samples(obj, limit: int = 5) -> list:
    """Up to N approved reviews with a rating for JSON-LD review[]."""
    out = []
    qs = obj.reviews.filter(
        is_approved=True, parent__isnull=True, rating__isnull=False
    ).order_by('-created_at')[:limit]
    for r in qs:
        out.append({
            '@type': 'Review',
            'reviewRating': {'@type': 'Rating', 'ratingValue': r.rating, 'bestRating': 5},
            'author': {'@type': 'Person', 'name': r.author_name or 'Customer'},
            'reviewBody': strip_html(r.text)[:500],
            'datePublished': r.created_at.date().isoformat(),
        })
    return out


# ── JSON-LD generators ───────────────────────────────────────────────────────

def organization_jsonld(site_settings=None) -> dict:
    data = {
        '@type': 'Organization',
        '@id': abs_url('/') + '#organization',
        'name': _site_name(),
        'url': abs_url('/'),
    }
    same_as = []
    if site_settings:
        if site_settings.org_instagram_url:
            same_as.append(site_settings.org_instagram_url)
        if site_settings.org_facebook_url:
            same_as.append(site_settings.org_facebook_url)
        if site_settings.org_telegram_url:
            same_as.append(site_settings.org_telegram_url)
        contact = {}
        if site_settings.org_phone:
            contact['telephone'] = site_settings.org_phone
        if site_settings.org_email:
            contact['email'] = site_settings.org_email
        if contact:
            contact['@type'] = 'ContactPoint'
            contact['contactType'] = 'customer service'
            data['contactPoint'] = contact
        if site_settings.org_address:
            data['address'] = {'@type': 'PostalAddress', 'streetAddress': site_settings.org_address}
    if same_as:
        data['sameAs'] = same_as
    return data


def website_jsonld() -> dict:
    return {
        '@type': 'WebSite',
        '@id': abs_url('/') + '#website',
        'url': abs_url('/'),
        'name': _site_name(),
        'inLanguage': 'uk-UA',
        'potentialAction': {
            '@type': 'SearchAction',
            'target': {
                '@type': 'EntryPoint',
                'urlTemplate': abs_url('/catalog?search={search_term_string}'),
            },
            'query-input': 'required name=search_term_string',
        },
    }


def breadcrumb_jsonld(crumbs: list[tuple[str, str]]) -> dict:
    """crumbs: [(name, absolute_url), ...] from the root to the current page."""
    return {
        '@type': 'BreadcrumbList',
        'itemListElement': [
            {'@type': 'ListItem', 'position': i + 1, 'name': name, 'item': url}
            for i, (name, url) in enumerate(crumbs)
        ],
    }


def _category_chain(category) -> list:
    """Chain of categories from the root to the given one (up via parent)."""
    chain = []
    c = category
    seen = set()
    while c is not None and c.pk not in seen:
        seen.add(c.pk)
        chain.append(c)
        c = c.parent
    chain.reverse()
    return chain


def _product_main_image(product):
    img = product.images.filter(is_hidden=False).order_by('-is_main', 'sort_order', 'pk').first()
    return img.image if img else None


def product_jsonld(product, *, avg=None, count=0) -> dict:
    variants = [v for v in product.variants.filter(is_active=True)]
    prices = []
    any_in_stock = False
    for v in variants:
        ep = v.effective_price
        if ep is not None:
            prices.append(Decimal(str(ep)))
        # Pure dropshipping with no supplier stock - OutOfStock (consistent with the storefront).
        if v.stock > 0 or getattr(v, 'is_hybrid', False):
            any_in_stock = True

    images = [abs_media(i.image) for i in
              product.images.filter(is_hidden=False).order_by('-is_main', 'sort_order', 'pk')]
    images = [u for u in images if u]

    data = {
        '@type': 'Product',
        'name': product.name,
        'url': abs_url(f'/product/{product.slug}'),
        'description': truncate(product.meta_description or strip_html(product.description), 300),
    }
    if images:
        data['image'] = images
    if product.brand_id and product.brand:
        data['brand'] = {'@type': 'Brand', 'name': product.brand.name}
    # SKU - take the first variant with a filled SKU (at product level it is a reference point).
    sku = next((v.sku for v in variants if v.sku), None)
    if sku:
        data['sku'] = sku

    if prices:
        availability = 'https://schema.org/InStock' if any_in_stock else 'https://schema.org/OutOfStock'
        data['offers'] = {
            '@type': 'AggregateOffer',
            'priceCurrency': 'UAH',
            'lowPrice': str(min(prices)),
            'highPrice': str(max(prices)),
            'offerCount': len(prices),
            'availability': availability,
            'itemCondition': 'https://schema.org/NewCondition',
            'url': abs_url(f'/product/{product.slug}'),
        }
    if count and avg:
        data['aggregateRating'] = {
            '@type': 'AggregateRating',
            'ratingValue': str(avg),
            'reviewCount': count,
            'bestRating': 5,
        }
        reviews = _review_samples(product)
        if reviews:
            data['review'] = reviews
    return data


def _bundle_main_image(bundle):
    img = bundle.images.filter(is_hidden=False)\
        .order_by('-is_main', 'sort_order', 'pk').first()
    return img.image if img else None


def bundle_jsonld(bundle, *, avg=None, count=0) -> dict:
    price = None
    try:
        p = bundle.get_price()
        if p:
            price = Decimal(str(p))
    except Exception:
        pass
    try:
        in_stock = bundle.get_stock() > 0
    except Exception:
        in_stock = False

    images = [abs_media(i.image) for i in
              bundle.images.filter(is_hidden=False)
              .order_by('-is_main', 'sort_order', 'pk')]
    images = [u for u in images if u]

    data = {
        '@type': 'Product',
        'name': bundle.name,
        'url': abs_url(f'/bundle/{bundle.slug}'),
        'description': truncate(bundle.meta_description or strip_html(bundle.description), 300),
    }
    if images:
        data['image'] = images
    if bundle.sku:
        data['sku'] = bundle.sku
    if price is not None:
        availability = 'https://schema.org/InStock' if in_stock else 'https://schema.org/OutOfStock'
        data['offers'] = {
            '@type': 'Offer',
            'priceCurrency': 'UAH',
            'price': str(price),
            'availability': availability,
            'itemCondition': 'https://schema.org/NewCondition',
            'url': abs_url(f'/bundle/{bundle.slug}'),
        }
    if count and avg:
        data['aggregateRating'] = {
            '@type': 'AggregateRating',
            'ratingValue': str(avg), 'reviewCount': count, 'bestRating': 5,
        }
        reviews = _review_samples(bundle)
        if reviews:
            data['review'] = reviews
    return data


def article_jsonld(post) -> dict:
    data = {
        '@type': 'Article',
        'headline': post.title,
        'url': abs_url(f'/blog/{post.slug}'),
        'datePublished': post.created_at.isoformat(),
        'dateModified': post.updated_at.isoformat(),
        'author': {'@type': 'Organization', 'name': _site_name()},
        'publisher': {'@type': 'Organization', 'name': _site_name()},
    }
    img = abs_media(post.preview_image)
    if img:
        data['image'] = [img]
    desc = post.meta_description or strip_html(post.content)
    if desc:
        data['description'] = truncate(desc, 300)
    return data


def course_jsonld(course) -> dict:
    data = {
        '@type': 'Course',
        'name': course.name,
        'url': abs_url(f'/course/{course.slug}'),
        'description': truncate(course.meta_description or strip_html(course.description) or course.short_description, 300),
        'provider': {'@type': 'Organization', 'name': _site_name(), 'sameAs': abs_url('/')},
    }
    img = abs_media(course.cover_image)
    if img:
        data['image'] = [img]
    if course.price is not None:
        data['offers'] = {
            '@type': 'Offer',
            'priceCurrency': 'UAH',
            'price': str(course.price),
            'availability': 'https://schema.org/InStock',
            'url': abs_url(f'/course/{course.slug}'),
        }
    return data


# ── canonical / robots ───────────────────────────────────────────────────────

# Private paths: do not index at all.
PRIVATE_PREFIXES = (
    '/checkout', '/login', '/profile', '/orders', '/liked',
    '/my-courses', '/reset-password', '/maintenance',
)

# Catalog filters whose presence makes the page noindex (to avoid creating duplicates).
_NOINDEX_CATALOG_PARAMS = ('search', 'label', 'attrs', 'price_min', 'price_max', 'in_stock')


def resolve_catalog_robots(query) -> str:
    """Indexing strategy for /catalog. query is a QueryDict or dict."""
    def _get(k):
        v = query.get(k)
        return v if v not in (None, '') else None

    # Any 'filtering' parameter, pagination >1, or a category+brand combination -> noindex.
    if any(_get(p) for p in _NOINDEX_CATALOG_PARAMS):
        return 'noindex,follow'
    page = _get('page')
    if page and page not in ('1',):
        return 'noindex,follow'
    has_cat = bool(_get('category'))
    has_brand = bool(_get('brand'))
    if has_cat and has_brand:
        return 'noindex,follow'
    return 'index,follow'


def catalog_canonical(query) -> str:
    """Canonical for /catalog: plain /catalog, or a single category=/brand=."""
    cat = query.get('category') or ''
    brand = query.get('brand') or ''
    if cat and not brand:
        return abs_url(f'/catalog?category={cat}')
    if brand and not cat:
        return abs_url(f'/catalog?brand={brand}')
    return abs_url('/catalog')


# ── rendering the <head> fragment ────────────────────────────────────────────

def render_head(
    *,
    title: str,
    description: str = '',
    canonical: str = '',
    robots: str = 'index,follow',
    image: str | None = None,
    og_type: str = 'website',
    jsonld: list | None = None,
    extra_meta: str = '',
) -> str:
    """Builds the HTML string for injection before </head>. All values are escaped."""
    parts = []
    t = escape(title)
    parts.append(f'<title>{t}</title>')
    parts.append(f'<meta name="robots" content="{escape(robots)}">')
    if description:
        d = escape(description)
        parts.append(f'<meta name="description" content="{d}">')
    if canonical:
        parts.append(f'<link rel="canonical" href="{escape(canonical)}">')

    # Open Graph
    parts.append(f'<meta property="og:type" content="{escape(og_type)}">')
    parts.append(f'<meta property="og:site_name" content="{escape(_site_name())}">')
    parts.append('<meta property="og:locale" content="uk_UA">')
    parts.append(f'<meta property="og:title" content="{escape(title)}">')
    if description:
        parts.append(f'<meta property="og:description" content="{escape(description)}">')
    if canonical:
        parts.append(f'<meta property="og:url" content="{escape(canonical)}">')
    if image:
        parts.append(f'<meta property="og:image" content="{escape(image)}">')

    # Twitter
    card = 'summary_large_image' if image else 'summary'
    parts.append(f'<meta name="twitter:card" content="{card}">')
    parts.append(f'<meta name="twitter:title" content="{escape(title)}">')
    if description:
        parts.append(f'<meta name="twitter:description" content="{escape(description)}">')
    if image:
        parts.append(f'<meta name="twitter:image" content="{escape(image)}">')

    if extra_meta:
        parts.append(extra_meta)

    for block in (jsonld or []):
        payload = dict(block)
        payload.setdefault('@context', 'https://schema.org')
        parts.append(f'<script type="application/ld+json">{js_json(payload)}</script>')

    return '\n'.join(parts)
