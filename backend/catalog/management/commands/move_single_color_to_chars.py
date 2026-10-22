"""
Moves the "same for all" variant color into a shared product characteristic.

Logic: if among all variants of a product there is EXACTLY ONE unique
color value (attribute is_color), this color does not "select" anything — it is
just a characteristic of the product. The command:
  1) creates/updates a shared ProductCharacteristic(name='Color', variant=NULL)
     with the value and color_hex of this color;
  2) removes this color value from attribute_values of all variants of the product.

Products with several different colors (color is a choice) are NOT touched.

Dry-run by default (only shows). To apply: --apply
"""
from django.core.management.base import BaseCommand
from django.db import transaction

from catalog.models import Attribute, Product, ProductCharacteristic

COLOR_NAME = 'Color'


class Command(BaseCommand):
    help = 'Moves the same variant color into a shared product characteristic'

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true', help='Apply the changes (otherwise dry-run)')

    def handle(self, *args, **opts):
        apply = opts['apply']
        color_attr = Attribute.objects.filter(is_color=True).first()
        if not color_attr:
            self.stdout.write(self.style.ERROR('No color attribute (is_color=True).'))
            return

        # Build the plan: products with exactly one unique color among their variants.
        plan = []           # (product, av, [variant ids with this color])
        skipped_multi = 0
        for p in Product.objects.prefetch_related('variants__attribute_values'):
            color_avs = {}  # av_id -> [av, [variant ids]]
            for v in p.variants.all():
                for av in v.attribute_values.all():
                    if av.attribute_id == color_attr.id:
                        color_avs.setdefault(av.id, [av, []])[1].append(v.id)
            if not color_avs:
                continue
            if len(color_avs) > 1:
                skipped_multi += 1
                continue
            av, vids = next(iter(color_avs.values()))
            plan.append((p, av, vids))

        self.stdout.write(f'Candidates (one color per product): {len(plan)}')
        self.stdout.write(f'Skipped (several colors — a choice): {skipped_multi}\n')

        for p, av, vids in plan:
            has = p.characteristics.filter(variant__isnull=True, name__iexact=COLOR_NAME).exists()
            self.stdout.write(
                f'  prod {p.id:5} | {p.name[:43]:43} | Color={av.value!r} {av.color_hex} '
                f'| removed from {len(vids)} var. | {"update" if has else "create"}'
            )

        if not apply:
            self.stdout.write(self.style.WARNING('\nDRY-RUN. To apply: --apply'))
            return

        created = updated = removed_links = 0
        with transaction.atomic():
            for p, av, vids in plan:
                existing = p.characteristics.filter(
                    variant__isnull=True, name__iexact=COLOR_NAME,
                ).first()
                if existing:
                    if existing.value != av.value or existing.color_hex != (av.color_hex or ''):
                        existing.value = av.value
                        existing.color_hex = av.color_hex or ''
                        existing.save(update_fields=['value', 'color_hex'])
                    updated += 1
                else:
                    max_so = (
                        p.characteristics.filter(variant__isnull=True)
                        .order_by('-sort_order').values_list('sort_order', flat=True).first()
                    )
                    ProductCharacteristic.objects.create(
                        product=p, variant=None, name=COLOR_NAME,
                        value=av.value, color_hex=av.color_hex or '',
                        sort_order=(max_so or 0) + 1,
                    )
                    created += 1
                for v in p.variants.all():
                    if v.attribute_values.filter(pk=av.pk).exists():
                        v.attribute_values.remove(av)
                        removed_links += 1

        self.stdout.write(self.style.SUCCESS(
            f'\nAPPLIED: created {created}, updated {updated}, unlinked colors {removed_links}'
        ))
