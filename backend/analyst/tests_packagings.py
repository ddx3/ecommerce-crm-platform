"""Packagings of one product: twins in the catalog are merged, neighboring packagings are visible in
the calculation and to the agent.

Real case: "Plant Booster, 250 g" was entered twice — an old Horoshop card
(stock 0) and a new own packaging
from a bag (stock 85, 63 sales in 90 days). The Analyst advised restocking the old card without
seeing its twin."""
from datetime import timedelta
from decimal import Decimal

from django.test import TestCase, override_settings

from analyst import advice, agent
from analyst import purchasing as P
from analyst.tests_purchasing import NOW, _find, _order


def _product(name):
    from catalog.models import Product
    return Product.objects.create(name=name)


def _legacy(name='Plant Booster', variant_name='250 g', stock=0, **kw):
    """Old card: a packaging without a bag, stock in pieces."""
    from catalog.models import ProductVariant
    return ProductVariant.objects.create(product=_product(name), name=variant_name, price=Decimal('200'), stock=stock, **kw)


def _bagged(name='Plant Booster', variant_name='250 g', quantity='5000', unit_size=250, **kw):
    """New own packaging from a bag: product and packaging name as on the old card."""
    from catalog.models import BulkSource, ProductVariant
    product = _product(name)
    bag = BulkSource.objects.create(product=product, name='Plant Booster Bag', quantity=Decimal(quantity), unit='g',
                                    waste_percent=Decimal('0'))
    variant = ProductVariant.objects.create(product=product, name=variant_name, price=Decimal('250'),
                                            unit_size=unit_size, bulk_source=bag, **kw)
    return bag, variant


@override_settings(ANALYST_LEAD_DAYS=14, ANALYST_COVER_DAYS=45, ANALYST_MIN_REVENUE=0, ANALYST_MIN_SALES_MONTHS=0)
class TwinMergeTests(TestCase):
    def test_duplicate_card_is_merged_into_the_bag_item(self):
        legacy = _legacy(stock=0)
        bag, own = _bagged()
        _, items = P.build(NOW)
        self.assertEqual([i.key for i in items], [f'bag:{bag.pk}'])
        self.assertEqual(sorted(items[0].variant_ids), sorted([legacy.pk, own.pk]))
        self.assertIn('bulk bag', items[0].label)          # the name comes from the packaging from the bag

    def test_demand_of_both_cards_is_pooled_in_bag_units(self):
        legacy = _legacy(stock=50)
        bag, own = _bagged(quantity='20000')
        _order(own, 2, NOW - timedelta(days=10))           # 2 × 250 g
        _order(legacy, 3, NOW - timedelta(days=12))        # 3 pcs of the old card = 3 × 250 g (as for the twin)
        _, items = P.build(NOW)
        item = _find(items, f'bag:{bag.pk}')
        self.assertAlmostEqual(item.recent_60, 1250.0, places=0)

    def test_twin_stock_counts_as_supply_in_bag_units(self):
        legacy = _legacy(stock=4)
        bag, _own = _bagged(quantity='3000')
        _, items = P.build(NOW)
        self.assertAlmostEqual(_find(items, f'bag:{bag.pk}').stock, 3000 + 4 * 250, places=0)

    def test_two_plain_duplicates_are_pooled_into_one_item(self):
        first = _legacy(stock=3)
        second = _legacy(stock=2)
        _, items = P.build(NOW)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].stock, 5)
        self.assertEqual(sorted(items[0].variant_ids), sorted([first.pk, second.pk]))

    def test_same_names_on_different_bags_are_not_merged(self):
        a_bag, a = _bagged()
        b_bag, b = _bagged()
        _, items = P.build(NOW)
        self.assertEqual(sorted(i.key for i in items), sorted([f'bag:{a_bag.pk}', f'bag:{b_bag.pk}']))

    def test_different_pack_names_are_not_twins(self):
        _legacy(variant_name='250 g')
        _legacy(variant_name='20 g')
        _, items = P.build(NOW)
        self.assertEqual(len(items), 2)


@override_settings(ANALYST_LEAD_DAYS=14, ANALYST_COVER_DAYS=45, ANALYST_MIN_REVENUE=0, ANALYST_MIN_SALES_MONTHS=0)
class SiblingContextTests(TestCase):
    def test_twin_in_dropshipping_covers_the_own_card(self):
        own = _legacy(stock=0)
        _order(own, 2, NOW - timedelta(days=5))
        own.refresh_from_db()
        from catalog.models import ProductVariant
        ProductVariant.objects.create(product=_product('Plant Booster'), name='250 g', price=Decimal('200'), stock=0,
                                      is_dropshipping=True, is_active=True)
        _, items = P.build(NOW)
        item = _find(items, f'var:{own.pk}')
        self.assertIn('the same item', item.covered_by)
        self.assertEqual((item.recommended, item.urgency), (0.0, 'ok'))
        self.assertNotIn(item, P.candidates(items))

    def test_other_pack_size_in_stock_is_context_not_a_block(self):
        from catalog.models import ProductVariant
        product = _product('Fertilizer Test')
        out = ProductVariant.objects.create(product=product, name='Fertilizer Test, 250 g', price=Decimal('200'), stock=0)
        in_stock = ProductVariant.objects.create(product=product, name='Fertilizer Test, 20 g', price=Decimal('30'), stock=40)
        _order(in_stock, 6, NOW - timedelta(days=7))
        _order(out, 2, NOW - timedelta(days=40))
        _, items = P.build(NOW)
        item = _find(items, f'var:{out.pk}')
        self.assertIsNone(item.covered_by)
        self.assertIn(item, P.candidates(items), 'another pack size does not cancel the need, but it is visible')
        self.assertEqual([s['label'] for s in item.siblings], ['Fertilizer Test, 20 g'])
        self.assertTrue(item.siblings[0]['available'])
        self.assertEqual(item.siblings[0]['sold_60'], 6.0)
        self.assertTrue(any('Other pack sizes' in n for n in item.notes))

    def test_siblings_include_hybrid_and_dropship_but_not_hidden_dead_ones(self):
        from catalog.models import ProductVariant
        product = _product('Product Test')
        me = ProductVariant.objects.create(product=product, name='A', price=Decimal('10'), stock=0)
        ProductVariant.objects.create(product=product, name='B', price=Decimal('10'), stock=0, is_hybrid=True)
        ProductVariant.objects.create(product=product, name='C', price=Decimal('10'), stock=0, is_dropshipping=True)
        ProductVariant.objects.create(product=product, name='D', price=Decimal('10'), stock=0, is_active=False)
        _order(me, 1, NOW - timedelta(days=3))
        _, items = P.build(NOW)
        item = _find(items, f'var:{me.pk}')
        self.assertEqual(sorted(s['label'] for s in item.siblings), ['B', 'C'])
        self.assertTrue(all(s['available'] for s in item.siblings))


@override_settings(ANALYST_LEAD_DAYS=14, ANALYST_COVER_DAYS=45, ANALYST_MIN_REVENUE=0, ANALYST_MIN_SALES_MONTHS=0)
class AgentAndCardsSeeSiblingsTests(TestCase):
    def setUp(self):
        from catalog.models import ProductVariant
        product = _product('Fertilizer Test')
        self.out = ProductVariant.objects.create(product=product, name='Fertilizer Test, 250 g', price=Decimal('200'), stock=0)
        self.alt = ProductVariant.objects.create(product=product, name='Fertilizer Test, 20 g', price=Decimal('30'), stock=40)
        _order(self.alt, 6, NOW - timedelta(days=7))
        _order(self.out, 2, NOW - timedelta(days=40))
        self.ctx, self.items = P.build(NOW)
        self.session = agent.Session(self.ctx, self.items)
        self.key = f'var:{self.out.pk}'

    def test_agent_gets_other_packagings_in_the_listing_and_the_detail(self):
        row = next(r for r in self.session.list_candidates()['candidates'] if r['key'] == self.key)
        self.assertTrue(any('Fertilizer Test, 20 g' in line for line in row['other_packagings']))
        self.assertIsNone(row['covered_by'])
        detail = self.session.item_detail(self.key)
        self.assertEqual(detail['other_packagings_detail'][0]['label'], 'Fertilizer Test, 20 g')

    def test_prompt_forces_the_agent_to_check_other_packagings(self):
        self.assertIn('other_packagings', agent.SYSTEM_PROMPT)
        self.assertIn('covered_by', agent.SYSTEM_PROMPT)

    def test_covered_item_cannot_be_recommended_even_if_the_model_insists(self):
        from catalog.models import ProductVariant
        own = _legacy('Plant Booster', stock=0)
        _order(own, 2, NOW - timedelta(days=5))
        ProductVariant.objects.create(product=_product('Plant Booster'), name='250 g', price=Decimal('200'), stock=0,
                                      is_dropshipping=True, is_active=True)
        ctx, items = P.build(NOW)
        session = agent.Session(ctx, items)
        res = session.submit(f'var:{own.pk}', 'buy', 5, 'Out of stock, we need to restock at the sales pace.')
        self.assertFalse(res['ok'])
        self.assertIn('forbidden', res['problem'])

    def test_rule_card_mentions_available_other_packagings(self):
        cards = advice.rule_cards(self.ctx, self.items)
        card = next(c for c in cards if c.key == f'purchase:{self.key}')
        self.assertIn('other pack sizes', card.body)
        self.assertIn('Fertilizer Test, 20 g', card.body)
        self.assertIn('Other pack sizes of this product are in stock', card.body)

    def test_stored_item_dict_carries_siblings(self):
        item = _find(self.items, self.key)
        d = item.as_dict()
        self.assertTrue(d['siblings'] and d['covered_by'] is None)
