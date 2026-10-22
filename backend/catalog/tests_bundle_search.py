"""Bundle search finds bundles by composition (component name) too, not only by name/description.

Previously a kit was found via a word from the long bundle description, which after the
variants-into-bundle merge is shadowed by the short description; the composition guarantees
that «fertilizer» shows the bundles that contain it.
"""
from __future__ import annotations

from decimal import Decimal

from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from catalog.models import Bundle, BundleItem, Product, ProductVariant


def _component(name):
    p = Product.objects.create(name=name)
    return ProductVariant.objects.create(product=p, name=name, price=Decimal('50'), stock=5)


@override_settings(ANTIBOT_ENABLED=False)
class BundleSearchByComponentTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.hit = Bundle.objects.create(name='Roses Kit', description='Short description without the product name')
        BundleItem.objects.create(bundle=self.hit, product_variant=_component('Fertilizer Protector'), quantity=1)
        BundleItem.objects.create(bundle=self.hit, product_variant=_component('Fertilizer Starter'), quantity=1)
        self.miss = Bundle.objects.create(name='Lawn Kit')
        BundleItem.objects.create(bundle=self.miss, product_variant=_component('Nitroammophoska'), quantity=1)

    def test_global_search_matches_component_name(self):
        r = self.client.get('/api/search/', {'q': 'fertilizer'})
        self.assertEqual(r.status_code, 200)
        slugs = [b['slug'] for b in r.data['bundles']]
        self.assertEqual(slugs.count(self.hit.slug), 1)   # two matching components — but only one bundle
        self.assertNotIn(self.miss.slug, slugs)

    def test_catalog_search_matches_component_name_once(self):
        r = self.client.get('/api/bundles/', {'search': 'fertilizer'})
        self.assertEqual(r.status_code, 200)
        slugs = [b['slug'] for b in r.data['results']]
        self.assertEqual(slugs.count(self.hit.slug), 1)
        self.assertNotIn(self.miss.slug, slugs)
