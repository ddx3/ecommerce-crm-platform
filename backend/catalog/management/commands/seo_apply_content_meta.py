"""Fills meta_title / meta_description for products, bundles, brands and blog posts — generated from
real data (name + description). Unique, no duplicates.

meta is a marketing description for the Google results page (name + purpose + "buy, delivery"),
NOT dosage instructions. Texts are taken from the existing descriptions, so no new
facts about the products are invented.

Via bulk_update — bypasses save()/signals (does not touch the product's is_active logic).
Idempotent: by default only empty fields, --force overwrites.

    python manage.py seo_apply_content_meta                  # all: products+bundles+brands+blog
    python manage.py seo_apply_content_meta --only products  # products only
    python manage.py seo_apply_content_meta --dry-run
    python manage.py seo_apply_content_meta --force
"""
import re

from django.core.management.base import BaseCommand

SITE = 'Magiya Roslyn'


def _clean(html: str) -> str:
    t = re.sub(r'<[^>]+>', ' ', html or '')
    return re.sub(r'\s+', ' ', t).strip()


def _truncate(text: str, limit: int) -> str:
    text = (text or '').strip()
    if len(text) <= limit:
        return text
    return text[:limit].rsplit(' ', 1)[0].rstrip()


def _name(name: str) -> str:
    # Strip the service prefix "*" (an import artifact) and extra spaces.
    return (name or '').lstrip('*').strip()


def title_for(name: str, suffix: str = 'buy') -> str:
    base = _truncate(_name(name), 65)
    return f'{base} — {suffix} | {SITE}'


def desc_for(name: str, description: str) -> str:
    body = _clean(description)
    if body:
        snip = _truncate(body, 125)
        return _truncate(f'{snip} — buy at {SITE}, delivery across Ukraine.', 170)
    return (f'{_name(name)} — buy at the {SITE} online store. '
            f'Original, price, availability, delivery across Ukraine.')


class Command(BaseCommand):
    help = 'Fills meta for products/bundles/brands/blog from real descriptions.'

    def add_arguments(self, parser):
        parser.add_argument('--only', choices=['products', 'bundles', 'brands', 'blog', 'courses'],
                            help='Only one type (all by default)')
        parser.add_argument('--force', action='store_true', help='Overwrite non-empty ones')
        parser.add_argument('--dry-run', action='store_true', help='Without writing')

    def handle(self, *args, **opts):
        only, force, dry = opts['only'], opts['force'], opts['dry_run']
        targets = [only] if only else ['products', 'bundles', 'brands', 'blog', 'courses']
        for t in targets:
            getattr(self, f'_do_{t}')(force, dry)

    # ── per-type ──────────────────────────────────────────────────────────────
    def _bulk(self, model, objs, dry, label):
        if dry:
            for o in objs[:3]:
                self.stdout.write(f'  [dry] {label}: {o.meta_title}')
            self.stdout.write(self.style.SUCCESS(f'{label}: prepared {len(objs)}'))
            return
        model.objects.bulk_update(objs, ['meta_title', 'meta_description'], batch_size=200)
        self.stdout.write(self.style.SUCCESS(f'{label}: written {len(objs)}'))

    def _collect(self, qs, force, title_fn, desc_fn):
        out = []
        for o in qs:
            if o.meta_title and not force:
                continue
            o.meta_title = title_fn(o)
            o.meta_description = desc_fn(o)
            out.append(o)
        return out

    def _do_products(self, force, dry):
        from catalog.models import Product
        qs = Product.objects.filter(is_active=True)
        objs = self._collect(
            qs, force,
            lambda p: title_for(p.name),
            lambda p: desc_for(p.name, p.description))
        self._bulk(Product, objs, dry, 'Products')

    def _do_bundles(self, force, dry):
        from catalog.models import Bundle
        qs = Bundle.objects.filter(is_active=True)
        objs = self._collect(
            qs, force,
            lambda b: title_for(b.name, suffix='bundle, buy'),
            lambda b: desc_for(b.name, b.description))
        self._bulk(Bundle, objs, dry, 'Bundles')

    def _do_brands(self, force, dry):
        from catalog.models import Brand
        qs = Brand.objects.filter(is_active=True)

        def bdesc(b):
            body = _clean(b.description)
            if body:
                return _truncate(f'{_truncate(body, 120)} — buy at {SITE}, delivery across Ukraine.', 170)
            return (f'{_name(b.name)} brand products: original, prices and availability. '
                    f'Delivery across Ukraine — {SITE}.')
        objs = self._collect(
            qs, force,
            lambda b: title_for(b.name, suffix='brand products'),
            bdesc)
        self._bulk(Brand, objs, dry, 'Brands')

    def _do_blog(self, force, dry):
        from blog.models import BlogPost
        qs = BlogPost.objects.filter(is_active=True)

        def bdesc(p):
            body = _clean(p.content)
            if body:
                return _truncate(body, 165)
            return f'{_name(p.title)} — read it on the {SITE} blog.'
        objs = self._collect(
            qs, force,
            lambda p: f'{_truncate(_name(p.title), 70)} | {SITE}',
            bdesc)
        self._bulk(BlogPost, objs, dry, 'Blog')

    def _do_courses(self, force, dry):
        from courses.models import Course
        qs = Course.objects.filter(is_active=True)
        objs = self._collect(
            qs, force,
            lambda c: title_for(c.name, suffix='course'),
            lambda c: desc_for(c.name, c.description or c.short_description))
        self._bulk(Course, objs, dry, 'Courses')
