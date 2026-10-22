"""Bundle composition is returned in a stable order (order of addition), not in the physical
row order of the table, which shifts with any UPDATE."""
from __future__ import annotations

from decimal import Decimal

from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from catalog.models import Bundle, BundleItem, Product, ProductVariant


def _component(name):
    p = Product.objects.create(name=name)
    return ProductVariant.objects.create(product=p, name=name, price=Decimal('50'), stock=5)


@override_settings(ANTIBOT_ENABLED=False)
class BundleItemOrderTests(TestCase):
    def test_components_keep_insertion_order_after_row_updates(self):
        b = Bundle.objects.create(name='Ordered Set')
        comps = [_component(f'Fertilizer {i}') for i in range(3)]
        items = [BundleItem.objects.create(bundle=b, product_variant=c, quantity=1) for c in comps]
        # Changing an indexed field (as the copy does during the variant merge) is not a HOT update:
        # the new row version lands at the end of the table, and without an explicit ordering it
        # would become the last one.
        other = Bundle.objects.create(name='Other Set')
        for it in items[:2]:
            BundleItem.objects.filter(pk=it.pk).update(bundle=other)
            BundleItem.objects.filter(pk=it.pk).update(bundle=b)

        self.assertEqual([i.pk for i in b.items.all()], [i.pk for i in items])
        r = APIClient().get(f'/api/bundles/{b.slug}/')
        self.assertEqual(r.status_code, 200)
        self.assertEqual([it['variant_id'] for it in r.data['items']], [c.pk for c in comps])
