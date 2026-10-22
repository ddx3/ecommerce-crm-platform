"""
Redistributes dropshipping product photos across variants (via supplier_sku → variant).
The old scheme put all photos on the product (variant=None). Now each photo
is bound to the variant matching its SKU at the supplier.

Safe to re-run:
  - If the product already has photos with variant != None — skips it.
  - Otherwise: deletes the old variant=None ProductImage rows and downloads them again
    via get_product_details(supplier_url) of each variant.

Usage:
    python manage.py redistribute_drop_photos                # all dropshipping products
    python manage.py redistribute_drop_photos --product 9034 # one product
    python manage.py redistribute_drop_photos --dry-run
"""
from django.core.management.base import BaseCommand
from django.db import transaction

from catalog.models import Product, ProductVariant, ProductImage
from catalog.management.commands.import_dropshipping import (
    Command as ImportCmd, get_product_details,
)


class Command(BaseCommand):
    help = 'Re-bind drop product photos to their variants by supplier_sku.'

    def add_arguments(self, parser):
        parser.add_argument('--product', type=int, default=None,
                            help='Process only this Product pk (else all drop products).')
        parser.add_argument('--dry-run', action='store_true')

    def handle(self, *args, **options):
        product_pk = options['product']
        dry = options['dry_run']

        qs = Product.objects.filter(variants__is_dropshipping=True).distinct()
        if product_pk:
            qs = qs.filter(pk=product_pk)

        stats = {
            'products_total':       0,
            'products_skipped':     0,
            'products_processed':   0,
            'images_downloaded':    0,
            'images_failed':        0,
            'images_dedup_in_product': 0,
            'images_reused_url':    0,
            'images_reused_content':0,
            'images_label_stripped':0,
            'old_imgs_deleted':     0,
        }

        importer = ImportCmd()
        for p in qs:
            stats['products_total'] += 1
            if p.images.filter(color_value__isnull=False).exists():
                stats['products_skipped'] += 1
                self.stdout.write(f'SKIP  {p.pk}  {p.name!r}  (already has color-bound photos)')
                continue

            self.stdout.write(f'\n→ {p.pk}  {p.name!r}')
            variants = list(p.variants.filter(is_dropshipping=True).exclude(supplier_url=''))
            if not variants:
                self.stdout.write('  no drop variants with supplier_url, skip')
                continue

            group_rows = []
            variant_map = {}
            for v in variants:
                if not v.supplier_url:
                    continue
                self.stdout.write(f'  fetch {v.supplier_sku} {v.supplier_url}')
                desc, specs, stock, photos = get_product_details(v.supplier_url)
                row = {
                    'supplier_sku': v.supplier_sku,
                    'name':         v.name,
                    'url':          v.supplier_url,
                    'photos':       photos or [],
                    'description':  desc,
                    'specs':        specs,
                }
                group_rows.append((row, {}, []))
                variant_map[v.supplier_sku] = v
                self.stdout.write(f'    photos: {len(photos or [])}')

            if dry:
                continue

            with transaction.atomic():
                # Delete only the photos that do not belong to any color
                # (old variant=None ones or old variant-bound ones that have not been migrated yet).
                old_qs = p.images.filter(color_value__isnull=True)
                old_count = old_qs.count()
                old_qs.delete()
                stats['old_imgs_deleted'] += old_count
                importer._import_images(p, group_rows, variant_map, stats)
            stats['products_processed'] += 1

        self.stdout.write('\n=== STATS ===')
        for k, v in stats.items():
            self.stdout.write(f'  {k:30}: {v}')
