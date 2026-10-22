"""sync_dropshipping: a failed or empty bradas listing must not disable a category.

Previously a category that failed to load (timeout, 403/Cloudflare, new markup → empty list) made
all its existing
dropshipping variants 'disappeared': is_active=False and stock=0, and the cleanup after 30 days
deleted them permanently. The "Supplier
changes" section is hidden in production (DROPSYNC_ENABLED=False), so the
manager would not have seen the events."""
from __future__ import annotations

from decimal import Decimal
from io import StringIO
from unittest import mock

from django.core.management import CommandError, call_command
from django.test import TestCase

from catalog.management.commands import sync_dropshipping as cmd
from catalog.models import DropSyncEvent, Product, ProductVariant


def _drop(product, n, stock=5):
    return ProductVariant.objects.create(
        product=product, name=f'd{n}', price=Decimal('100'), stock=stock, is_dropshipping=True,
        supplier_sku=f'SKU{n}', supplier_url=f'https://bradas.ua/{n}/')


def _item(n, stock=7, price=120.0):
    return {'name': f'd{n}', 'supplier_price': price, 'stock': stock, 'url': f'https://bradas.ua/{n}/'}


class SyncDropshippingGuardTests(TestCase):
    def setUp(self):
        self.product = Product.objects.create(name='Dropship')
        self.cfgs = {'a': {'url': 'URL-A'}, 'b': {'url': 'URL-B'}}

    def _run(self, listings):
        """listings: url → list of cards or an exception."""
        def fake(url):
            value = listings[url]
            if isinstance(value, Exception):
                raise value
            return value

        err = StringIO()
        self.error = None
        with mock.patch.object(cmd, 'CATEGORY_CONFIGS', self.cfgs), \
             mock.patch.object(cmd, 'parse_listing_quick', side_effect=fake):
            try:
                call_command('sync_dropshipping', stdout=StringIO(), stderr=err)
            except CommandError as exc:         # partial failure is loud, but what was already saved stays
                self.error = str(exc)
        return err.getvalue()

    def test_empty_listing_of_one_category_does_not_deactivate_it(self):
        a = _drop(self.product, 1)
        b = _drop(self.product, 2)
        err = self._run({'URL-A': [_item(1)], 'URL-B': []})
        a.refresh_from_db()
        b.refresh_from_db()
        self.assertEqual((a.is_active, a.stock), (True, 7))                 # the category that was read got updated
        self.assertEqual((b.is_active, b.stock), (True, 5))                 # the unread one is untouched
        self.assertFalse(DropSyncEvent.objects.filter(kind=DropSyncEvent.KIND_REMOVED).exists())
        self.assertIn('Skipping the deactivation', err)
        self.assertIn('b', self.error)                                      # and the run is marked as failed (heartbeat/card)

    def test_fetch_error_of_one_category_does_not_deactivate_it(self):
        _drop(self.product, 1)
        b = _drop(self.product, 2)
        self._run({'URL-A': [_item(1)], 'URL-B': RuntimeError('503')})
        b.refresh_from_db()
        self.assertEqual((b.is_active, b.stock), (True, 5))
        self.assertIsNotNone(self.error)

    def test_clean_run_does_not_raise(self):
        _drop(self.product, 1)
        _drop(self.product, 2)
        self._run({'URL-A': [_item(1)], 'URL-B': [_item(2)]})
        self.assertIsNone(self.error)

    def test_small_real_disappearance_is_still_deactivated(self):
        """1 of 5 (20%) with a fully read listing — an ordinary disappearance from bradas."""
        variants = [_drop(self.product, n) for n in range(1, 6)]
        self._run({'URL-A': [_item(n) for n in range(1, 5)], 'URL-B': [_item(99)]})
        gone = variants[4]
        gone.refresh_from_db()
        self.assertEqual((gone.is_active, gone.stock), (False, 0))
        self.assertTrue(DropSyncEvent.objects.filter(kind=DropSyncEvent.KIND_REMOVED, variant=gone).exists())

    def test_mass_disappearance_is_treated_as_a_failure(self):
        """3 of 5 (60%) 'disappeared' even though both listings are non-empty: looks like new markup — we
disable nothing."""
        variants = [_drop(self.product, n) for n in range(1, 6)]
        err = self._run({'URL-A': [_item(1), _item(2)], 'URL-B': [_item(99)]})
        for v in variants:
            v.refresh_from_db()
            self.assertTrue(v.is_active, v.supplier_sku)
        self.assertFalse(DropSyncEvent.objects.filter(kind=DropSyncEvent.KIND_REMOVED).exists())
        self.assertIn('Skipping the deactivation', err)

    def test_old_inactive_variants_are_not_pruned_on_a_suspicious_run(self):
        from datetime import timedelta
        from django.utils import timezone
        old = _drop(self.product, 1, stock=0)
        ProductVariant.objects.filter(pk=old.pk).update(is_active=False, last_synced_at=timezone.now() - timedelta(days=90))
        _drop(self.product, 2)
        self._run({'URL-A': [], 'URL-B': [_item(2)]})
        self.assertTrue(ProductVariant.objects.filter(pk=old.pk).exists())
