"""Sitemap.xml without django.contrib.sites.

The canonical domain (settings.SITE_URL, non-www) and the https protocol are pinned in
_Base.get_urls
via a fake site, so the sitemap does not depend on the request host (www/proxy) and does not produce
duplicates. location() returns a PATH (not an absolute URL) — the framework adds the domain itself.
"""
from __future__ import annotations

from django.conf import settings
from django.contrib.sitemaps import Sitemap


def _domain() -> str:
    base = (getattr(settings, 'SITE_URL', '') or '').split('://')[-1].rstrip('/')
    return base or 'magiyaroslyn.com'


class _Base(Sitemap):
    protocol = 'https'
    limit = 5000

    def get_urls(self, page=1, site=None, protocol=None):
        fake = type('Site', (), {'domain': _domain(), 'name': getattr(settings, 'SITE_NAME', '')})()
        return super().get_urls(page=page, site=fake, protocol='https')


def _image_urls(images, limit=3):
    """Up to N absolute image URLs (main one first) for the image sitemap."""
    from core.seo import abs_media
    ordered = sorted(images, key=lambda i: (not i.is_main, i.sort_order, i.pk))
    out = []
    for i in ordered:
        if i.is_hidden:
            continue
        u = abs_media(i.image)
        if u:
            out.append(u)
        if len(out) >= limit:
            break
    return out


class ProductSitemap(_Base):
    changefreq = 'weekly'
    priority = 0.8

    def items(self):
        from catalog.models import Product
        return (Product.objects.filter(is_active=True, variants__is_active=True)
                .distinct().order_by('id').prefetch_related('images'))

    def location(self, obj):
        return f'/product/{obj.slug}'

    def lastmod(self, obj):
        return obj.updated_at

    def get_urls(self, page=1, site=None, protocol=None):
        urls = super().get_urls(page=page, site=site, protocol=protocol)
        for u in urls:
            u['images'] = _image_urls(u['item'].images.all())
        return urls


class BundleSitemap(_Base):
    changefreq = 'weekly'
    priority = 0.7

    def items(self):
        from catalog.models import Bundle
        return (Bundle.objects.filter(is_active=True)
                .order_by('id').prefetch_related('images'))

    def location(self, obj):
        return f'/bundle/{obj.slug}'

    def lastmod(self, obj):
        return obj.updated_at

    def get_urls(self, page=1, site=None, protocol=None):
        urls = super().get_urls(page=page, site=site, protocol=protocol)
        for u in urls:
            u['images'] = _image_urls(u['item'].images.all())
        return urls


class CategorySitemap(_Base):
    changefreq = 'weekly'
    priority = 0.6

    def items(self):
        from catalog.models import Category
        from catalog.api_views import _visible_category_ids
        vis = _visible_category_ids()
        return Category.objects.filter(is_active=True, pk__in=vis).order_by('id').only('slug', 'updated_at')

    def location(self, obj):
        return f'/catalog?category={obj.slug}'

    def lastmod(self, obj):
        return obj.updated_at


class BrandSitemap(_Base):
    changefreq = 'weekly'
    priority = 0.5

    def items(self):
        from catalog.models import Brand, Product
        brand_ids = (Product.objects.filter(is_active=True, variants__is_active=True)
                     .values_list('brand_id', flat=True))
        return (Brand.objects.filter(is_active=True, pk__in=set(bid for bid in brand_ids if bid))
                .order_by('id').only('slug'))

    def location(self, obj):
        return f'/catalog?brand={obj.slug}'


class BlogSitemap(_Base):
    changefreq = 'monthly'
    priority = 0.5

    def items(self):
        from blog.models import BlogPost
        return BlogPost.objects.filter(is_active=True).order_by('id').only('slug', 'updated_at')

    def location(self, obj):
        return f'/blog/{obj.slug}'

    def lastmod(self, obj):
        return obj.updated_at


class CourseSitemap(_Base):
    changefreq = 'monthly'
    priority = 0.5

    def items(self):
        from courses.models import Course
        return Course.objects.filter(is_active=True).order_by('id').only('slug', 'updated_at')

    def location(self, obj):
        return f'/course/{obj.slug}'

    def lastmod(self, obj):
        return obj.updated_at


class StaticSitemap(_Base):
    changefreq = 'monthly'
    priority = 0.4

    def items(self):
        return ['/', '/catalog', '/blog', '/courses', '/reviews',
                '/about', '/contacts', '/delivery', '/offer', '/privacy', '/returns']

    def location(self, obj):
        return obj


SITEMAPS = {
    'products': ProductSitemap,
    'bundles': BundleSitemap,
    'categories': CategorySitemap,
    'brands': BrandSitemap,
    'blog': BlogSitemap,
    'courses': CourseSitemap,
    'static': StaticSitemap,
}
