"""Tests for the Flora plant-assistant feed (read-only endpoints + change signals).

Run: python manage.py test catalog.tests_flora
"""
import json
import os
from unittest import mock

from django.test import TestCase

from catalog.models import Product, ProductVariant, Bundle

SECRET = "test-flora-secret"


class FloraEndpointTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.p = Product.objects.create(name="Test remedy", slug="test-zasib", is_active=True,
                                       description="description", application_norms="10 g per 5 l")
        ProductVariant.objects.create(product=cls.p, name="100 g", price=120, stock=7, is_active=True)
        cls.b = Bundle.objects.create(name="Test bundle", slug="test-komplekt", is_active=True,
                                      price=300, stock=2)

    def setUp(self):
        os.environ["SHOP_FLORA_API_SECRET"] = SECRET

    def _post(self, body, secret=SECRET):
        headers = {"HTTP_X_FLORA_SECRET": secret} if secret is not None else {}
        return self.client.post("/api/internal/flora/catalog/", data=json.dumps(body),
                                content_type="application/json", **headers)

    def test_batch_requires_secret(self):
        self.assertEqual(self._post({"ids": []}, secret=None).status_code, 403)
        self.assertEqual(self._post({"ids": []}, secret="wrong").status_code, 403)

    def test_batch_returns_card_fields(self):
        r = self._post({"ids": [f"prod-{self.p.id}", f"bundle-{self.b.id}", "prod-999999"]})
        self.assertEqual(r.status_code, 200)
        d = r.json()
        self.assertEqual(d["items"][f"prod-{self.p.id}"]["price"], 120.0)
        self.assertTrue(d["items"][f"prod-{self.p.id}"]["in_stock"])
        self.assertEqual(d["items"][f"bundle-{self.b.id}"]["price"], 300.0)
        self.assertIn("prod-999999", d["missing"])

    def test_batch_returns_variants_per_pack(self):
        # A multi-pack product exposes each size's price + live stock, so Flora can pick
        # the pack covering the client's need (product "in stock" while a size is out).
        # LANGUAGE DATA: the pack-size parser only knows Ukrainian unit words (г, мл, л), so the sizes stay Ukrainian.
        p = Product.objects.create(name="Multipack", slug="multi-pack", is_active=True)
        ProductVariant.objects.create(product=p, name="Multipack, 25 г", price=73, stock=5, is_active=True)
        ProductVariant.objects.create(product=p, name="Multipack, 100 мл", price=320, stock=0, is_active=True)
        ProductVariant.objects.create(product=p, name="Multipack, 1 л", price=1600, stock=0, is_active=True)
        it = self._post({"ids": [f"prod-{p.id}"]}).json()["items"][f"prod-{p.id}"]
        vs = it["variants"]
        self.assertEqual([v["size"] for v in vs], ["25 г", "100 мл", "1 л"])  # smallest pack first
        by_size = {v["size"]: v for v in vs}
        self.assertTrue(by_size["25 г"]["in_stock"])          # 25 г (g) has stock
        self.assertFalse(by_size["100 мл"]["in_stock"])       # 100 мл (ml) is out
        self.assertEqual(by_size["1 л"]["price"], 1600.0)     # per-size price
        self.assertEqual(by_size["1 л"]["base_amount"], 1000.0)  # 1 л (l) normalized to 1000 мл

    def test_batch_lean_omits_text(self):
        it = self._post({"ids": [f"prod-{self.p.id}"]}).json()["items"][f"prod-{self.p.id}"]
        self.assertNotIn("embed_text", it)  # lean = card fields only
        self.assertIn("price", it)

    def test_batch_include_text_has_embed(self):
        it = self._post({"ids": [f"prod-{self.p.id}"], "include": "text"}).json()["items"][f"prod-{self.p.id}"]
        self.assertTrue(it["embed_text"])
        self.assertIn("Test remedy", it["embed_text"])

    def test_batch_inactive_returned_realtime_missing_for_embed(self):
        self.p.is_active = False
        self.p.save(update_fields=["is_active"])
        pid = f"prod-{self.p.id}"
        # Realtime card refresh RETURNS the inactive product with active:false — so Flora marks it
        # "Withdrawn from sale" and can read its paired packaging. It is NOT
        # dropped to `missing`.
        d = self._post({"ids": [pid]}).json()
        self.assertIn(pid, d["items"])
        self.assertFalse(d["items"][pid]["active"])
        self.assertNotIn(pid, d["missing"])
        # Re-embed path (include=text) must NOT index a hidden product → still missing.
        d2 = self._post({"ids": [pid], "include": "text"}).json()
        self.assertIn(pid, d2["missing"])
        self.assertNotIn(pid, d2["items"])

    def test_batch_returns_paired_partner(self):
        # own ↔ factory: each product carries its partner's lean card so Flora can fall back
        # to the available packaging when this one is removed or out of stock.
        own = Product.objects.create(name="Booster own pack", slug="baikal-own", is_active=True,
                                     packaging_kind=Product.PACKAGING_OWN)
        ProductVariant.objects.create(product=own, name="100 ml", price=90, stock=0, is_active=True)  # out
        factory = Product.objects.create(name="Booster factory pack", slug="baikal-factory", is_active=True,
                                         packaging_kind=Product.PACKAGING_FACTORY, paired_product=own)
        ProductVariant.objects.create(product=factory, name="100 ml", price=110, stock=4, is_active=True)  # in
        it = self._post({"ids": [f"prod-{own.id}"]}).json()["items"][f"prod-{own.id}"]
        self.assertEqual(it["packaging_kind"], Product.PACKAGING_OWN)
        self.assertFalse(it["in_stock"])                       # own packaging out of stock
        self.assertEqual(it["paired"]["id"], f"prod-{factory.id}")
        self.assertTrue(it["paired"]["active"] and it["paired"]["in_stock"])   # factory available

    def test_delta_reports_changed(self):
        r = self.client.get("/api/internal/flora/catalog/delta/",
                            {"since": "2000-01-01T00:00:00+00:00", "limit": 100},
                            HTTP_X_FLORA_SECRET=SECRET)
        d = r.json()
        self.assertIn(f"prod-{self.p.id}", d["product_ids"])
        self.assertIn(f"bundle-{self.b.id}", d["bundle_ids"])

    def test_delta_future_since_empty(self):
        d = self.client.get("/api/internal/flora/catalog/delta/",
                            {"since": "2099-01-01T00:00:00+00:00"},
                            HTTP_X_FLORA_SECRET=SECRET).json()
        self.assertEqual(d["product_ids"], [])
        self.assertEqual(d["bundle_ids"], [])

    def test_delta_truncated_frontier(self):
        d = self.client.get("/api/internal/flora/catalog/delta/",
                            {"since": "2000-01-01T00:00:00+00:00", "limit": 1},
                            HTTP_X_FLORA_SECRET=SECRET).json()
        self.assertTrue(d["truncated"])
        self.assertIsNotNone(d["next_product_since"])  # frontier for forward-paging

    def test_delta_requires_secret(self):
        self.assertEqual(self.client.get("/api/internal/flora/catalog/delta/",
                                         {"since": "2000-01-01T00:00:00+00:00"}).status_code, 403)


class FloraSignalTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.p = Product.objects.create(name="Signal product", slug="signal-prod", is_active=True)
        cls.b = Bundle.objects.create(name="Signal bundle", slug="signal-bundle", is_active=True)

    def test_text_change_notifies(self):
        with mock.patch("catalog.signals._notify_flora") as mn:
            self.p.name = "New name"
            self.p.save(update_fields=["name"])
            mn.assert_called_with(f"prod-{self.p.id}")

    def test_price_change_does_not_notify(self):
        # a variant price save is not a product-text change → no product re-embed signal
        v = ProductVariant.objects.create(product=self.p, name="v", price=10, is_active=True)
        with mock.patch("catalog.signals._notify_flora") as mn:
            v.price = 20
            v.save(update_fields=["price"])
            self.assertFalse(any(c.args == (f"prod-{self.p.id}",) for c in mn.call_args_list))

    def test_deactivation_notifies(self):
        with mock.patch("catalog.signals._notify_flora") as mn:
            self.p.is_active = False
            self.p.save(update_fields=["is_active"])
            mn.assert_called_with(f"prod-{self.p.id}")  # is_active is watched

    def test_delete_notifies(self):
        pid = self.p.id
        with mock.patch("catalog.signals._notify_flora") as mn:
            self.p.delete()
            mn.assert_any_call(f"prod-{pid}")
