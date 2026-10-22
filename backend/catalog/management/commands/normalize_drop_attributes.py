"""
Converts "shared" variant attributes into product characteristics.

An attribute exists to DISTINGUISH variants. If all variants have the same
AttributeValue, it is not an attribute but a characteristic of the whole
product. The command fixes historical data where such values ended up in attributes.

Usage:
    python manage.py normalize_drop_attributes              # all dropshipping products
    python manage.py normalize_drop_attributes --product 9034
    python manage.py normalize_drop_attributes --all        # even non-dropshipping
    python manage.py normalize_drop_attributes --dry-run
"""
from django.core.management.base import BaseCommand

from catalog.models import Product
from catalog.management.commands.import_dropshipping import (
    normalize_variant_attrs_to_characteristics,
)


class Command(BaseCommand):
    help = "Move attributes that don't vary across variants into product characteristics."

    def add_arguments(self, parser):
        parser.add_argument('--product', type=int, default=None,
                            help='Process only this Product pk.')
        parser.add_argument('--all', action='store_true',
                            help='Also process non-drop products.')
        parser.add_argument('--dry-run', action='store_true')

    def handle(self, *args, **options):
        product_pk = options['product']
        do_all = options['all']
        dry = options['dry_run']

        if product_pk:
            qs = Product.objects.filter(pk=product_pk)
        elif do_all:
            qs = Product.objects.all()
        else:
            qs = Product.objects.filter(variants__is_dropshipping=True).distinct()

        stats = {
            'products_total': 0,
            'attrs_moved_to_characteristics': 0,
        }

        for p in qs:
            stats['products_total'] += 1
            if dry:
                # In dry-run mode just count, without changing anything.
                self._dry_count(p, stats)
                continue
            before = stats.get('attrs_moved_to_characteristics', 0)
            normalize_variant_attrs_to_characteristics(p, stats)
            after = stats.get('attrs_moved_to_characteristics', 0)
            if after > before:
                self.stdout.write(
                    f'  {p.pk}  {p.name!r}: +{after - before} attr→char'
                )

        self.stdout.write('\n=== STATS ===')
        for k, v in stats.items():
            self.stdout.write(f'  {k:35}: {v}')

    def _dry_count(self, product, stats):
        variants = list(product.variants.all().prefetch_related('attribute_values__attribute'))
        if not variants:
            return
        by_attr = {}
        names = {}
        for v in variants:
            for av in v.attribute_values.all():
                names[av.attribute_id] = av.attribute.name
                by_attr.setdefault(av.attribute_id, {}).setdefault(av.pk, av)

        for attr_id, av_map in by_attr.items():
            if len(av_map) != 1:
                continue
            av = next(iter(av_map.values()))
            if all(any(x.pk == av.pk for x in v.attribute_values.all()) for v in variants):
                stats['attrs_moved_to_characteristics'] += 1
                self.stdout.write(
                    f'  [DRY] {product.pk} {product.name!r}: '
                    f'{names[attr_id]} = {av.value!r} → characteristic'
                )
