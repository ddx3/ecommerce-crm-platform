"""Purchasing calculation: the code computes the numbers. Here we check each component separately."""
from datetime import datetime, timedelta, timezone as dt_tz
from decimal import Decimal

from django.test import TestCase, override_settings
from django.utils import timezone

from analyst import purchasing as P
from analyst.models import PlannedPurchase

NOW = datetime(2026, 10, 19, 12, 0, tzinfo=dt_tz.utc)


def _product(name='Item'):
    from catalog.models import Category, Product
    product = Product.objects.create(name=f'{name} {Product.objects.count()}')
    product.categories.add(Category.objects.create(name=f'Cat {Category.objects.count()}', slug=f'cat-{Category.objects.count()}'))
    return product


def _variant(product=None, stock=10, price='100', **kw):
    from catalog.models import ProductVariant
    return ProductVariant.objects.create(product=product or _product(), price=Decimal(price), stock=stock, **kw)


def _order(variant, qty, when, imported=False, method='cod'):
    """An order with a single item. Live ones go through the stock write-off signal (stock_applied=True)."""
    from orders.models import Order, OrderItem
    order = Order.objects.create(
        customer_name='Test', customer_phone='+380000000000', delivery_type='pickup',
        payment_method=method, total_price=Decimal('100'), is_imported=imported,
        payment_status='paid' if imported else 'pending')
    Order.objects.filter(pk=order.pk).update(created_at=when)
    OrderItem.objects.create(order=order, product_variant=variant, quantity=qty, price=Decimal('100'))
    return order


def _imported_bounds():
    """Sentinel imported orders set the history bounds (needed for valid past-year windows)."""
    from orders.models import Order
    for when in (datetime(2024, 1, 5, tzinfo=dt_tz.utc), datetime(2026, 6, 15, tzinfo=dt_tz.utc)):
        o = Order.objects.create(customer_name='x', customer_phone='+380500000001', delivery_type='pickup',
                                 payment_method='cod', is_imported=True)
        Order.objects.filter(pk=o.pk).update(created_at=when)


def _find(items, key):
    return next(i for i in items if i.key == key)


@override_settings(ANALYST_LEAD_DAYS=14, ANALYST_COVER_DAYS=45, ANALYST_MIN_REVENUE=0, ANALYST_MIN_SALES_MONTHS=0)
class EngineTests(TestCase):
    def test_empty_shop(self):
        ctx, items = P.build(NOW)
        self.assertEqual(items, [])
        self.assertEqual((ctx.lead_days, ctx.cover_days, ctx.horizon_days), (14, 45, 59))

    def test_excludes_drop_hybrid_and_fallback(self):
        _variant(is_dropshipping=True)
        _variant(is_hybrid=True)
        _variant(is_supplier_fallback=True)
        own = _variant()
        _, items = P.build(NOW)
        self.assertEqual([i.variant_ids for i in items], [[own.pk]])

    def test_recent_rate_and_cover(self):
        v = _variant(stock=100)
        for days_ago in (5, 10, 20, 30, 40, 50):
            _order(v, 2, NOW - timedelta(days=days_ago))
        # stock_applied is set by the signal; stock decreased by 12
        v.refresh_from_db()
        _, items = P.build(NOW)
        item = _find(items, f'var:{v.pk}')
        self.assertEqual(item.recent_60, 12)
        self.assertAlmostEqual(item.rate_per_day, 0.2, places=3)
        self.assertEqual(item.forecast_basis, 'recent')
        self.assertAlmostEqual(item.forecast, 0.2 * 59, places=1)
        self.assertEqual(item.stock, v.stock)

    def test_out_of_stock_with_waiting_people_is_a_candidate_even_without_sales(self):
        from catalog.models import StockNotification
        v = _variant(stock=0)
        for i in range(3):
            StockNotification.objects.create(email=f'a{i}@example.com', variant=v)
        StockNotification.objects.create(email='done@example.com', variant=v, is_notified=True)   # already notified
        _, items = P.build(NOW)
        item = _find(items, f'var:{v.pk}')
        self.assertEqual(item.waiting_subs, 3)
        self.assertEqual(item.urgency, 'out')
        self.assertIn(item, P.candidates(items))

    def test_out_of_stock_without_demand_is_not_a_candidate(self):
        v = _variant(stock=0)
        _, items = P.build(NOW)
        item = _find(items, f'var:{v.pk}')
        self.assertEqual(item.urgency, 'dead')
        self.assertEqual(P.candidates(items), [])

    def test_bundle_subscription_points_to_the_missing_component(self):
        from catalog.models import Bundle, BundleItem, StockNotification
        have, missing = _variant(stock=5), _variant(stock=0)
        bundle = Bundle.objects.create(name='Set', price=Decimal('300'))
        BundleItem.objects.create(bundle=bundle, product_variant=have, quantity=1)
        BundleItem.objects.create(bundle=bundle, product_variant=missing, quantity=1)
        StockNotification.objects.create(email='b@example.com', bundle=bundle)
        _, items = P.build(NOW)
        self.assertEqual(_find(items, f'var:{missing.pk}').waiting_subs, 1)
        self.assertEqual(_find(items, f'var:{have.pk}').waiting_subs, 0)

    def test_likes_on_unavailable_item_count_as_waiting(self):
        from catalog.models import Favorite
        v = _variant(stock=0)
        Favorite.objects.create(device_id='d1', product_variant=v)
        Favorite.objects.create(device_id='d2', product_variant=v)
        _, items = P.build(NOW)
        self.assertEqual(_find(items, f'var:{v.pk}').waiting_likes, 2)

    def test_open_planned_purchase_reduces_need_and_closed_does_not(self):
        v = _variant(stock=0)
        _imported_bounds()
        for year in (2024, 2025):      # past-year windows: 30 pcs in total in the next horizon
            _order(v, 30, datetime(year, 11, 5, tzinfo=dt_tz.utc), imported=True)
            _order(v, 5, datetime(year, 9, 20, tzinfo=dt_tz.utc), imported=True)
        _, base = P.build(NOW)
        need0 = _find(base, f'var:{v.pk}').need
        self.assertGreater(need0, 0)
        PlannedPurchase.objects.create(key=f'var:{v.pk}', label='x', quantity=Decimal('10'), unit='pcs')
        PlannedPurchase.objects.create(key=f'var:{v.pk}', label='x', quantity=Decimal('999'), unit='pcs',
                                       status=PlannedPurchase.RECEIVED)
        _, after = P.build(NOW)
        item = _find(after, f'var:{v.pk}')
        self.assertEqual(item.open_po, 10)
        self.assertAlmostEqual(item.need, max(need0 - 10, 0), places=1)

    def test_seasonality_from_previous_years(self):
        v = _variant(stock=100)
        _imported_bounds()
        for year in (2024, 2025):
            _order(v, 6, datetime(year, 9, 10, tzinfo=dt_tz.utc), imported=True)     # the "past" window
            _order(v, 24, datetime(year, 11, 10, tzinfo=dt_tz.utc), imported=True)   # the "next" window
        for days_ago in (10, 30, 50):                                                # this year on par with last year
            _order(v, 2, NOW - timedelta(days=days_ago))
        _, items = P.build(NOW)
        item = _find(items, f'var:{v.pk}')
        self.assertEqual(item.forecast_basis, 'item_seasonal')
        self.assertEqual(item.history_years, 2)
        self.assertEqual(item.seasonal_next, 24)
        self.assertEqual(item.seasonal_past, 6)
        self.assertEqual(item.trend_basis, 'item')
        self.assertAlmostEqual(item.trend, 1.0)                  # 6 / 6
        self.assertAlmostEqual(item.forecast, 24.0, places=1)

    def test_item_out_of_stock_uses_shop_level_trend_not_zero_sales(self):
        """Not in stock → the last 60 days are empty because of absence, not because of demand."""
        gone, active = _variant(stock=0), _variant(stock=200)
        _imported_bounds()
        for year in (2024, 2025):
            for v in (gone, active):
                _order(v, 20, datetime(year, 9, 10, tzinfo=dt_tz.utc), imported=True)
                _order(v, 20, datetime(year, 11, 10, tzinfo=dt_tz.utc), imported=True)
        for days_ago in range(1, 40):                 # another product sells at roughly half of last year's level this year
            _order(active, 1, NOW - timedelta(days=days_ago))
        ctx, items = P.build(NOW)
        item = _find(items, f'var:{gone.pk}')
        self.assertEqual(item.trend_basis, 'shop')
        self.assertIsNotNone(ctx.shop_trend)
        self.assertAlmostEqual(item.forecast, 20 * item.trend, delta=0.3)   # the trend in the object is rounded to hundredths
        self.assertLess(item.forecast, 20)

    def test_no_history_uses_recent_with_shop_season_when_known(self):
        _imported_bounds()
        other = _variant(stock=500)
        for year in (2024, 2025):                      # the shop as a whole: the next 59 days are much quieter
            for i in range(10):
                _order(other, 10, datetime(year, 9, 1 + i, tzinfo=dt_tz.utc), imported=True)
            _order(other, 10, datetime(year, 11, 10, tzinfo=dt_tz.utc), imported=True)
        fresh = _variant(stock=10)                     # new item with no history from last year
        for days_ago in (5, 15, 25, 35):
            _order(fresh, 3, NOW - timedelta(days=days_ago))
        ctx, items = P.build(NOW)
        item = _find(items, f'var:{fresh.pk}')
        self.assertEqual(item.forecast_basis, 'recent_shop_season')
        self.assertLess(item.forecast, item.recent_60 / 60 * 59)


@override_settings(ANALYST_LEAD_DAYS=14, ANALYST_COVER_DAYS=45, ANALYST_MIN_REVENUE=0, ANALYST_MIN_SALES_MONTHS=0)
class BagTests(TestCase):
    def _bag(self, quantity='10000', waste='0'):
        from catalog.models import BulkSource, ProductVariant
        product = _product('Seeds')
        bag = BulkSource.objects.create(product=product, name='Bag', quantity=Decimal(quantity), unit='g',
                                        waste_percent=Decimal(waste))
        small = ProductVariant.objects.create(product=product, price=Decimal('50'), unit_size=100, bulk_source=bag)
        big = ProductVariant.objects.create(product=product, price=Decimal('400'), unit_size=1000, bulk_source=bag)
        return bag, small, big

    def test_variants_of_one_bag_are_one_purchase_item_in_grams(self):
        bag, small, big = self._bag(quantity='20000', waste='10')
        _order(small, 5, NOW - timedelta(days=10))     # 5 × 100 g × 1.10 = 550 g
        _order(big, 2, NOW - timedelta(days=20))       # 2 × 1000 g × 1.10 = 2200 g
        _, items = P.build(NOW)
        item = _find(items, f'bag:{bag.pk}')
        self.assertEqual(sorted(item.variant_ids), sorted([small.pk, big.pk]))
        self.assertEqual(item.unit, 'g')
        self.assertAlmostEqual(item.recent_60, 2750.0, places=0)
        self.assertEqual(len([i for i in items if i.key.startswith('var:')]), 0)

    def test_negative_bag_means_promised_supply_and_is_out(self):
        bag, small, _big = self._bag(quantity='-300')
        _, items = P.build(NOW)
        self.assertEqual(_find(items, f'bag:{bag.pk}').urgency, 'dead')    # no demand — not a candidate
        from catalog.models import Favorite
        Favorite.objects.create(device_id='d', product_variant=small)
        _, items = P.build(NOW)
        self.assertEqual(_find(items, f'bag:{bag.pk}').urgency, 'out')

    def test_recommendation_is_rounded_up_in_grams(self):
        value = P._round_up(1234, 'g')
        self.assertEqual(value, 1500.0)
        self.assertEqual(P._round_up(12_300, 'g'), 13_000.0)
        self.assertEqual(P._round_up(2.1, 'pcs'), 3.0)
        self.assertEqual(P._round_up(0, 'g'), 0.0)
        self.assertEqual(P.human_qty(12_500, 'g'), '12.5 kg')
        self.assertEqual(P.human_qty(800, 'g'), '800 g')


@override_settings(ANALYST_LEAD_DAYS=14, ANALYST_COVER_DAYS=45, ANALYST_MIN_REVENUE=0, ANALYST_MIN_SALES_MONTHS=0)
class DeadStockAndUrgencyTests(TestCase):
    def test_dead_stock_is_listed_and_new_stock_item_is_not_a_candidate(self):
        dead = _variant(stock=50)                       # sitting in stock, no sales
        live = _variant(stock=50)
        for days_ago in (3, 9, 15):
            _order(live, 1, NOW - timedelta(days=days_ago))
        _, items = P.build(NOW)
        self.assertIn(dead.pk, [vid for i in P.dead_stock(items) for vid in i.variant_ids])
        self.assertNotIn(live.pk, [vid for i in P.dead_stock(items) for vid in i.variant_ids])

    def test_urgent_when_cover_is_shorter_than_lead_time(self):
        v = _variant(stock=20)
        for days_ago in range(1, 40, 2):                 # sells ~2 pcs/day, 20 pieces will last ~10 days
            _order(v, 4, NOW - timedelta(days=days_ago))
        v.refresh_from_db()
        _, items = P.build(NOW)
        item = _find(items, f'var:{v.pk}')
        self.assertIn(item.urgency, ('urgent', 'out'))
        self.assertGreater(item.need, 0)
        self.assertIn(item, P.candidates(items))

    def test_monthly_sales_combines_history_and_live(self):
        v = _variant(stock=50)
        _imported_bounds()
        _order(v, 7, datetime(2025, 3, 10, tzinfo=dt_tz.utc), imported=True)
        _order(v, 2, NOW - timedelta(days=5))
        months = dict(P.monthly_sales([v.pk], NOW))
        self.assertEqual(months['2025-03'], 7)
        self.assertEqual(months[(NOW - timedelta(days=5)).strftime('%Y-%m')], 2)
