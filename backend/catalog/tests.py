"""Subscription "notify me when it appears" for a bundle and the CRM "Subscriptions"
section.

Previously the storefront sent a bundle packaging id in the variant_id field, the backend looked for
it among product variants and always returned 404 — subscribing to a bundle was impossible.
"""
from __future__ import annotations

from decimal import Decimal

from django.core import mail
from django.db import IntegrityError, transaction
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from catalog.models import (
    Bundle, BundleItem, Product, ProductVariant, StockNotification,
)


def _variant(stock=0, name='Fertilizer'):
    p = Product.objects.create(name=name)
    return ProductVariant.objects.create(product=p, name=name, price=Decimal('100'), stock=stock)


def _bundle(component, qty=2):
    b = Bundle.objects.create(name='Rose Set')
    BundleItem.objects.create(bundle=b, product_variant=component, quantity=qty)
    return b


@override_settings(ANTIBOT_ENABLED=False)
class StockNotifyApiTests(TestCase):
    def setUp(self):
        self.client = APIClient()

    def _post(self, **data):
        return self.client.post('/api/stock-notify/', {'email': 'a@example.com', **data}, format='json')

    def test_subscribe_to_out_of_stock_bundle(self):
        b = _bundle(_variant(stock=1), qty=2)
        r = self._post(bundle_id=b.id)
        self.assertEqual(r.status_code, 201)
        n = StockNotification.objects.get()
        self.assertEqual(n.bundle, b)
        self.assertIsNone(n.variant)
        self.assertEqual(self._post(bundle_id=b.id).status_code, 200)  # "You are already subscribed"

    def test_bundle_in_stock_rejected(self):
        b = _bundle(_variant(stock=10), qty=2)
        self.assertEqual(self._post(bundle_id=b.id).status_code, 400)

    def test_unknown_or_hidden_bundle_404(self):
        b = _bundle(_variant(stock=0))
        self.assertEqual(self._post(bundle_id=999999).status_code, 404)
        self.assertEqual(self._post(bundle_id='abc').status_code, 404)
        Bundle.objects.filter(pk=b.pk).update(is_active=False)
        self.assertEqual(self._post(bundle_id=b.id).status_code, 404)
        self.assertFalse(StockNotification.objects.exists())

    def test_variant_subscription_unchanged(self):
        v = _variant(stock=0)
        self.assertEqual(self._post(variant_id=v.id).status_code, 201)
        self.assertEqual(StockNotification.objects.get().variant, v)
        self.assertEqual(self._post(variant_id='abc').status_code, 404)

    def test_null_email_is_400_not_500(self):
        v = _variant(stock=0)
        self.assertEqual(self._post(variant_id=v.id, email=None).status_code, 400)

    def test_resubscribe_after_notification_rearms(self):
        # The email was already sent, the bundle ran out again — a repeat subscription waits again.
        b = _bundle(_variant(stock=0))
        StockNotification.objects.create(email='a@example.com', bundle=b, is_notified=True)
        self.assertEqual(self._post(bundle_id=b.id).status_code, 201)
        self.assertFalse(StockNotification.objects.get().is_notified)
        self.assertEqual(self._post(bundle_id=b.id).status_code, 200)  # now "already subscribed"


class StockNotifySendTests(TestCase):
    def test_bundle_notified_when_component_restocked(self):
        comp = _variant(stock=1)
        b = _bundle(comp, qty=2)
        n = StockNotification.objects.create(email='a@example.com', bundle=b)

        with self.captureOnCommitCallbacks(execute=True):
            comp.stock = 1  # still short by one bundle
            comp.save()
        n.refresh_from_db()
        self.assertFalse(n.is_notified)
        self.assertEqual(len(mail.outbox), 0)

        with self.captureOnCommitCallbacks(execute=True):
            comp.stock = 4
            comp.save()
        n.refresh_from_db()
        self.assertTrue(n.is_notified)
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn('Bundle', mail.outbox[0].subject)
        self.assertIn(f'/bundle/{b.slug}', mail.outbox[0].alternatives[0][0])
        self.assertIn('Go to the bundle', mail.outbox[0].alternatives[0][0])

    def test_bundle_explicit_stock_triggers(self):
        b = _bundle(_variant(stock=0))
        StockNotification.objects.create(email='a@example.com', bundle=b)
        with self.captureOnCommitCallbacks(execute=True):
            b.stock = 3
            b.save()
        self.assertEqual(len(mail.outbox), 1)

    def test_hidden_bundle_not_notified(self):
        comp = _variant(stock=0)
        b = _bundle(comp, qty=1)
        StockNotification.objects.create(email='a@example.com', bundle=b)
        Bundle.objects.filter(pk=b.pk).update(is_active=False)
        with self.captureOnCommitCallbacks(execute=True):
            comp.stock = 5
            comp.save()
        self.assertEqual(len(mail.outbox), 0)

    def test_sweep_catches_update_bypass_and_is_idempotent(self):
        # .update() bypasses post_save — the scheduler pass must send the email, once.
        comp = _variant(stock=0)
        b = _bundle(comp, qty=1)
        StockNotification.objects.create(email='a@example.com', bundle=b)
        ProductVariant.objects.filter(pk=comp.pk).update(stock=5)
        self.assertEqual(StockNotification.send_due(), 1)
        self.assertEqual(StockNotification.send_due(), 0)
        self.assertEqual(len(mail.outbox), 1)

    def test_variant_notification_still_sent(self):
        v = _variant(stock=0)
        StockNotification.objects.create(email='a@example.com', variant=v)
        with self.captureOnCommitCallbacks(execute=True):
            v.stock = 2
            v.save()
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn(f'/product/{v.product.slug}', mail.outbox[0].alternatives[0][0])
        self.assertIn('Go to the product', mail.outbox[0].alternatives[0][0])
        self.assertEqual(mail.outbox[0].subject, 'Product is back in stock')

    def test_hidden_variant_waits_until_shown(self):
        v = _variant(stock=0)
        StockNotification.objects.create(email='a@example.com', variant=v)
        with self.captureOnCommitCallbacks(execute=True):
            v.is_active = False
            v.stock = 2
            v.save()
        self.assertEqual(len(mail.outbox), 0)
        with self.captureOnCommitCallbacks(execute=True):
            v.is_active = True
            v.save()
        self.assertEqual(len(mail.outbox), 1)

    def test_exactly_one_target_enforced(self):
        v = _variant()
        b = _bundle(v)
        for kw in ({}, {'variant': v, 'bundle': b}):
            with self.subTest(kw=kw), self.assertRaises(IntegrityError), transaction.atomic():
                StockNotification.objects.create(email='a@example.com', **kw)


class StockDemandItemsTests(TestCase):
    """The "Subscriptions" section by item: subscriptions + likes (the "Favorites" list) on
out-of-stock items."""

    def _items(self):
        from catalog.views import _stock_demand_items
        subs = StockNotification.objects.filter(is_notified=False).select_related(
            'variant__product', 'bundle')
        return {it['key']: it for it in _stock_demand_items(list(subs), {})}

    def _setup(self):
        from django.contrib.auth import get_user_model
        from catalog.models import Favorite
        user = get_user_model().objects.create(email='fan@example.com')
        gone, in_stock, hybrid = _variant(stock=0, name='A'), _variant(stock=5, name='B'), _variant(stock=0, name='C')
        ProductVariant.objects.filter(pk=hybrid.pk).update(is_hybrid=True)
        bundle = _bundle(_variant(stock=0, name='D'))
        # The same account from two devices — one row; an anonymous user — a row per device.
        self.f1 = Favorite.objects.create(device_id='d1', user=user, product_variant=gone)
        self.f2 = Favorite.objects.create(device_id='d2', user=user, product_variant=gone)
        Favorite.objects.create(device_id='d3', product_variant=gone)
        Favorite.objects.create(device_id='d1', product_variant=in_stock)
        Favorite.objects.create(device_id='d1', product_variant=hybrid)
        Favorite.objects.create(device_id='d1', bundle=bundle)
        StockNotification.objects.create(email='a@example.com', variant=in_stock)
        return gone, in_stock, bundle

    def test_likes_shown_only_while_unavailable_subs_always(self):
        gone, in_stock, bundle = self._setup()
        items = self._items()
        self.assertEqual(set(items), {f'v{gone.pk}', f'v{in_stock.pk}', f'b{bundle.pk}'})
        likes = items[f'v{gone.pk}']['likes']
        self.assertEqual(sorted(len(r['ids']) for r in likes), [1, 2])
        self.assertEqual(sorted(r['who'] or '' for r in likes), ['', 'fan@example.com'])
        s = items[f'v{in_stock.pk}']
        self.assertEqual((len(s['subs']), s['likes']), (1, []))   # a like on something that is in stock is not demand
        self.assertEqual(items[f'b{bundle.pk}']['kind'], 'bundle')
        self.assertEqual(list(items)[0], f'v{in_stock.pk}')  # the most recent interaction (the subscription) first

    def test_like_older_than_ttl_not_shown(self):
        from datetime import timedelta
        from django.utils import timezone
        from catalog.models import Favorite
        from catalog.notifications import LIKE_TTL_DAYS
        v = _variant(stock=0, name='Waiting for a long time')
        f = Favorite.objects.create(device_id='d8', product_variant=v)
        self.assertIn(f'v{v.pk}', self._items())
        Favorite.objects.filter(pk=f.pk).update(created_at=timezone.now() - timedelta(days=LIKE_TTL_DAYS + 1))
        self.assertNotIn(f'v{v.pk}', self._items())
        self.assertTrue(Favorite.objects.filter(pk=f.pk).exists())   # does not disappear from the DB

    def test_likes_on_hidden_item_not_shown(self):
        from catalog.models import Favorite
        hidden = _variant(stock=0, name='Withdrawn from sale')
        ProductVariant.objects.filter(pk=hidden.pk).update(is_active=False)
        Favorite.objects.create(device_id='d9', product_variant=hidden)
        self.assertNotIn(f'v{hidden.pk}', self._items())

    def test_inbox_counts_likes_done_and_delete(self):
        from django.contrib.auth import get_user_model
        from catalog.models import Favorite, NotificationState
        from catalog.notifications import get_notification_counts, unavailable_likes
        gone, _, _ = self._setup()
        # 1 subscription + 2 like rows on A + 1 on the bundle
        self.assertEqual(get_notification_counts()['stock'], 4)
        self.client.force_login(get_user_model().objects.create(email='m@example.com', is_staff=True, is_superuser=True))

        # "Done" on a like row (the state is kept on the row's latest like) removes it from
        # the counter.
        head = next(r for r in unavailable_likes()[f'v{gone.pk}'] if r['who'])['ids'][0]
        r = self.client.post('/admin/notifications/set-step/',
                             {'section': 'stock_like', 'id': head, 'step': 1}, content_type='application/json')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(get_notification_counts()['stock'], 3)

        # "Delete" removes the person's likes from all devices together with the state.
        r = self.client.post('/admin/notifications/stock-action/',
                             {'action': 'delete_likes', 'ids': [self.f1.pk, self.f2.pk]}, content_type='application/json')
        self.assertEqual(r.status_code, 200)
        self.assertFalse(Favorite.objects.filter(pk__in=[self.f1.pk, self.f2.pk]).exists())
        self.assertFalse(NotificationState.objects.filter(section='stock_like').exists())
        self.assertEqual(get_notification_counts()['stock'], 3)


class LowStockThresholdOverrideTests(TestCase):
    """Manual "Low" threshold (low_stock_threshold_override) — saves packagings without
    enough sales history, where the Croston calculation doesn't work at all."""

    def test_manual_override_without_sales_history_is_included(self):
        from catalog.notifications import low_stock_variants
        v = _variant(stock=5)
        v.low_stock_threshold_override = 10
        v.save(update_fields=['low_stock_threshold_override'])

        hits = low_stock_variants()

        self.assertEqual([h[0].pk for h in hits], [v.pk])
        self.assertIsNone(hits[0][1])          # daily_rate was not computed
        self.assertEqual(hits[0][2], 10)       # effective threshold = override

    def test_manual_override_excludes_when_stock_above_threshold(self):
        from catalog.notifications import low_stock_variants
        v = _variant(stock=20)
        v.low_stock_threshold_override = 10
        v.save(update_fields=['low_stock_threshold_override'])

        self.assertEqual(low_stock_variants(), [])

    def test_clearing_override_falls_back_to_history_gate(self):
        from catalog.notifications import low_stock_variants
        v = _variant(stock=5)
        v.low_stock_threshold_override = 10
        v.save(update_fields=['low_stock_threshold_override'])
        self.assertEqual(len(low_stock_variants()), 1)

        v.low_stock_threshold_override = None
        v.save(update_fields=['low_stock_threshold_override'])
        # With no sales in history the auto-calculation stays silent — the product disappears from
        # the list.
        self.assertEqual(low_stock_variants(), [])

    def test_save_variant_field_sets_and_clears_override(self):
        from django.contrib.auth import get_user_model
        v = _variant(stock=5)
        self.client.force_login(get_user_model().objects.create(email='m2@example.com', is_staff=True, is_superuser=True))

        r = self.client.post('/admin/catalog/save-variant-field/',
                             {'id': v.pk, 'field': 'low_stock_threshold', 'value': '12'},
                             content_type='application/json')
        self.assertEqual(r.status_code, 200)
        v.refresh_from_db()
        self.assertEqual(v.low_stock_threshold_override, 12)

        r = self.client.post('/admin/catalog/save-variant-field/',
                             {'id': v.pk, 'field': 'low_stock_threshold', 'value': ''},
                             content_type='application/json')
        self.assertEqual(r.status_code, 200)
        v.refresh_from_db()
        self.assertIsNone(v.low_stock_threshold_override)


class ProductPriceRangeFilterTests(TestCase):
    """/api/products/?price_min&price_max: both bounds on ONE active variant.

    Previously these were two separate .filter() calls on variants: the minimum could be met by one
    variant and the maximum by another (26% false matches
    for 150..250 in production), plus extra JOINs (4–8 s per request)."""

    def _product(self, name, prices, active=True):
        p = Product.objects.create(name=name)
        for i, price in enumerate(prices):
            ProductVariant.objects.create(product=p, name=f'{name}-{i}', price=Decimal(price), stock=5, is_active=active)
        return p

    def _names(self, **params):
        resp = self.client.get('/api/products/', params)
        self.assertEqual(resp.status_code, 200, resp.content)
        return {row['name'] for row in resp.json()['results']}

    def test_both_bounds_must_hold_on_the_same_variant(self):
        self._product('Mismatch', ['65', '130', '280', '540'])    # 130 < 150, and 280 > 250: no variant hits
        self._product('Match', ['65', '200'])
        self.assertEqual(self._names(price_min='150', price_max='250'), {'Match'})

    def test_single_bounds(self):
        self._product('Cheap', ['50'])
        self._product('Expensive', ['500'])
        self.assertEqual(self._names(price_min='100'), {'Expensive'})
        self.assertEqual(self._names(price_max='100'), {'Cheap'})

    def test_invalid_bound_is_ignored(self):
        self._product('One', ['50'])
        self._product('Two', ['500'])
        self.assertEqual(self._names(price_min='abc'), {'One', 'Two'})

    def test_inactive_variant_price_does_not_count(self):
        p = self._product('Mixed', ['500'])
        ProductVariant.objects.create(product=p, name='withdrawn', price=Decimal('200'), stock=5, is_active=False)
        self.assertEqual(self._names(price_min='150', price_max='250'), set())
