"""Deeper purchasing checks: real demand (not a clearance of leftovers), weight of the purchase,
batches from invoices,
product context and alternatives for the agent."""
from datetime import timedelta
from decimal import Decimal

from django.test import TestCase, override_settings

from analyst import agent
from analyst import purchasing as P
from analyst.tests_packagings import _bagged
from analyst.tests_purchasing import NOW, _find, _order, _product, _variant

GATES = dict(ANALYST_LEAD_DAYS=14, ANALYST_COVER_DAYS=45, ANALYST_MIN_REVENUE=1000, ANALYST_MIN_SALES_MONTHS=2)


def _sold_out(variant, *days_ago, qty=1, price=None):
    """Sales on the given days ago, after which the product ran out."""
    from catalog.models import ProductVariant
    for d in days_ago:
        _order(variant, qty, NOW - timedelta(days=d))
    ProductVariant.objects.filter(pk=variant.pk).update(stock=0, **({'price': Decimal(price)} if price else {}))


def _confirmed_receipt(variant=None, bag_id=None, amount=5, days_ago=20):
    from orders.models import Invoice, InvoiceItem
    inv = Invoice.objects.create(photo='x.jpg', status='confirmed', invoice_date=(NOW - timedelta(days=days_ago)).date())
    kw = dict(applied_bag_id=bag_id, applied_bag_amount=Decimal(amount)) if bag_id else dict(matched_variant=variant, applied_delta=int(amount))
    return InvoiceItem.objects.create(invoice=inv, invoice_name='item', quantity=1, confirmed=True, **kw)


@override_settings(**GATES)
class DemandQualityTests(TestCase):
    def test_orders_months_and_lumpiness_are_counted(self):
        v = _variant(stock=100, price='1000')
        _order(v, 1, NOW - timedelta(days=5))
        _order(v, 1, NOW - timedelta(days=40))
        _order(v, 2, NOW - timedelta(days=70))          # outside the 60 days, but in a different month
        _, items = P.build(NOW)
        item = _find(items, f'var:{v.pk}')
        self.assertEqual(item.orders_60, 2)
        self.assertEqual(item.sales_months_12, 3)
        self.assertEqual(item.top_order_share, 0.5)

    def test_one_big_order_is_visible_as_lumpy_demand(self):
        v = _variant(stock=100, price='1000')
        _order(v, 5, NOW - timedelta(days=10))
        _, items = P.build(NOW)
        item = _find(items, f'var:{v.pk}')
        self.assertEqual((item.orders_60, item.top_order_share), (1, 1.0))

    def test_one_off_sales_of_leftovers_are_not_a_candidate(self):
        v = _variant(stock=10, price='5000')
        _sold_out(v, 5, 8, 12, price='5000')               # everything in one month, then it ran out
        _, items = P.build(NOW)
        item = _find(items, f'var:{v.pk}')
        self.assertIn('one-off sales', item.skip_reason)
        self.assertNotIn(item, P.candidates(items))
        self.assertIn(item, P.filtered_out(items))

    def test_steady_demand_over_months_passes(self):
        v = _variant(stock=10, price='5000')
        _sold_out(v, 5, 40, 70, price='5000')
        _, items = P.build(NOW)
        item = _find(items, f'var:{v.pk}')
        self.assertIsNone(item.skip_reason)
        self.assertIn(item, P.candidates(items))

    def test_small_money_is_not_worth_an_order(self):
        v = _variant(stock=10, price='100')
        _sold_out(v, 5, 40, 70, price='100')                # ≈3 pcs × 100 UAH over the horizon
        _, items = P.build(NOW)
        item = _find(items, f'var:{v.pk}')
        self.assertLess(item.est_revenue, 1000)
        self.assertIn('small revenue', item.skip_reason)
        self.assertNotIn(item, P.candidates(items))

    def test_two_waiting_people_override_the_noise_filter(self):
        from catalog.models import StockNotification
        v = _variant(stock=0, price='100')
        for i in range(2):
            StockNotification.objects.create(email=f'w{i}@example.com', variant=v)
        _, items = P.build(NOW)
        item = _find(items, f'var:{v.pk}')
        self.assertIsNone(item.skip_reason)
        self.assertIn(item, P.candidates(items))

    def test_a_single_like_is_not_enough(self):
        from catalog.models import StockNotification
        v = _variant(stock=0, price='100')
        StockNotification.objects.create(email='one@example.com', variant=v)
        _, items = P.build(NOW)
        item = _find(items, f'var:{v.pk}')
        self.assertIsNotNone(item.skip_reason)
        self.assertNotIn(item, P.candidates(items))

    def test_unit_price_and_revenue_for_a_bag_are_per_gram(self):
        bag, own = _bagged(quantity='20000')               # 250 g for 250 UAH = 1 UAH/g
        _order(own, 4, NOW - timedelta(days=10))
        _, items = P.build(NOW)
        item = _find(items, f'bag:{bag.pk}')
        self.assertAlmostEqual(item.unit_price, 1.0, places=2)
        self.assertAlmostEqual(item.est_revenue, round(item.forecast * 1.0), delta=1)


@override_settings(**GATES)
class PurchaseLotTests(TestCase):
    def test_lot_size_comes_from_confirmed_receipts(self):
        v = _variant(stock=10, price='5000')
        for amount, days in ((5, 90), (10, 50), (10, 20)):
            _confirmed_receipt(variant=v, amount=amount, days_ago=days)
        _, items = P.build(NOW)
        item = _find(items, f'var:{v.pk}')
        self.assertEqual(item.lot_size, 10.0)
        self.assertEqual([a for _d, a in item.receipts], [5.0, 10.0, 10.0])

    def test_reverted_invoices_are_ignored(self):
        v = _variant(stock=10)
        receipt = _confirmed_receipt(variant=v, amount=5)
        type(receipt.invoice).objects.filter(pk=receipt.invoice_id).update(status='reverted')
        _, items = P.build(NOW)
        self.assertIsNone(_find(items, f'var:{v.pk}').lot_size)

    def test_bag_receipts_are_in_bag_units(self):
        bag, own = _bagged(quantity='3000')
        _confirmed_receipt(bag_id=bag.pk, amount=5000)
        _, items = P.build(NOW)
        self.assertEqual(_find(items, f'bag:{bag.pk}').lot_size, 5000.0)

    def test_a_lot_that_covers_years_is_not_worth_ordering(self):
        v = _variant(stock=10, price='5000')
        _sold_out(v, 5, 40, 70, price='5000')
        _confirmed_receipt(variant=v, amount=200, days_ago=100)     # batch of 200 pcs with demand of ≈3 pcs over the horizon
        _, items = P.build(NOW)
        item = _find(items, f'var:{v.pk}')
        self.assertIn('supply lot', item.skip_reason)
        self.assertNotIn(item, P.candidates(items))

    def test_unknown_lot_is_reported_as_unknown_not_invented(self):
        v = _variant(stock=0)
        _, items = P.build(NOW)
        ctx = P.build(NOW)[0]
        item = _find(items, f'var:{v.pk}')
        self.assertIsNone(item.lot_size)
        detail = agent.Session(ctx, items).item_detail(f'var:{v.pk}')
        self.assertIn('unknown', detail['lot_note'])


@override_settings(**GATES)
class AgentContextTests(TestCase):
    def setUp(self):
        from catalog.models import Brand, Category, Product
        self.cat = Category.objects.create(name='Fertilizers Test', slug='fertilizers-test')
        brand = Brand.objects.create(name='BrandTest', slug='brand-test')
        self.product = Product.objects.create(name='Main product', brand=brand,
                                              description='<p>A <b>long-acting</b> fertilizer for conifers.</p>')
        self.product.categories.add(self.cat)
        from catalog.models import ProductVariant
        self.main = ProductVariant.objects.create(product=self.product, name='Main product, 250 g', price=Decimal('5000'), stock=10)
        _sold_out(self.main, 5, 40, 70, price='5000')
        alt_product = Product.objects.create(name='Other fertilizer')
        alt_product.categories.add(self.cat)
        self.alt = ProductVariant.objects.create(product=alt_product, name='Other fertilizer, 1 kg', price=Decimal('300'), stock=7)
        _order(self.alt, 3, NOW - timedelta(days=12))
        self.ctx, self.items = P.build(NOW)
        self.session = agent.Session(self.ctx, self.items)
        self.key = f'var:{self.main.pk}'

    def test_item_detail_gives_description_alternatives_recent_sales_and_demand_quality(self):
        d = self.session.item_detail(self.key)
        self.assertEqual(d['product']['brand'], 'BrandTest')
        self.assertIn('A long-acting fertilizer for conifers.', d['product']['description'])   # no HTML tags
        self.assertEqual(d['product']['categories'], ['Fertilizers Test'])
        self.assertEqual([a['label'] for a in d['alternatives']], ['Other fertilizer, 1 kg'])
        self.assertEqual(d['alternatives'][0]['sold_60'], 3.0)
        self.assertTrue(d['recent_sales'])
        self.assertTrue(all(len(x) == 2 for x in d['recent_sales']))            # date and quantity, no personal data
        for key in ('orders_60', 'sales_months_12', 'top_order_share', 'est_revenue_uah', 'unit_price_uah', 'receipts'):
            self.assertIn(key, d)

    def test_listing_tells_the_agent_that_code_already_filtered_noise(self):
        from catalog.models import ProductVariant
        noise = ProductVariant.objects.create(product=_product('Trifle'), name='Trifle', price=Decimal('50'), stock=10)
        _sold_out(noise, 5, 40, 70, price='50')
        session = agent.Session(*P.build(NOW))
        out = session.list_candidates()
        self.assertGreaterEqual(out['filtered_out_by_code'], 1)
        self.assertNotIn(f'var:{noise.pk}', [r['key'] for r in out['candidates']])

    def test_submit_refuses_a_buy_the_code_filtered_as_noise(self):
        from catalog.models import ProductVariant
        noise = ProductVariant.objects.create(product=_product('Trifle'), name='Trifle', price=Decimal('50'), stock=10)
        _sold_out(noise, 5, 40, 70, price='50')
        session = agent.Session(*P.build(NOW))
        res = session.submit(f'var:{noise.pk}', 'buy', 5, 'Out of stock, it was selling, so we are taking it.', 'normal')
        self.assertFalse(res['ok'])
        self.assertIn('not worth buying', res['problem'])

    def test_brief_carries_the_evidence_columns(self):
        row = agent.brief(_find(self.items, self.key), self.ctx)
        for key in ('orders_60', 'sales_months_12', 'top_order_share', 'est_revenue_uah', 'lot_size', 'last_restock'):
            self.assertIn(key, row)

    def test_prompt_is_a_skeptical_checklist(self):
        text = agent.SYSTEM_PROMPT
        for needle in ('by default do NOT buy', 'clearance of leftovers', 'alternatives', 'est_revenue_uah',
                       'lot_size', 'zero recommendations'):
            self.assertIn(needle, text)
        self.assertEqual(agent.MAX_RECOMMENDATIONS, 8)

    def test_agent_thinks_and_uses_the_stronger_model(self):
        with override_settings(ANALYST_THINKING_TOKENS=8000):
            self.assertEqual(agent._thinking(), {'type': 'enabled', 'budget_tokens': 8000})
        with override_settings(ANALYST_THINKING_TOKENS=0):
            self.assertEqual(agent._thinking(), {'type': 'disabled'})
        from django.conf import settings
        self.assertEqual(settings.ANALYST_MODEL, 'opus')

    def test_stored_card_data_keeps_the_new_fields(self):
        d = _find(self.items, self.key).as_dict()
        for key in ('orders_60', 'sales_months_12', 'est_revenue', 'lot_size', 'receipts', 'skip_reason'):
            self.assertIn(key, d)
