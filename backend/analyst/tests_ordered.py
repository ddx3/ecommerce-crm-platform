"""'Ordered …' rows on the Analyst page: what the owner marked with ✓ and is still in
transit.

Previously this was a separate admin section ('Ordered purchases'). Now the
list lives on the Analyst page as gray rows:
they appear after ✓ and disappear on their own when the stock level has grown (arrived) or via ↩."""
from __future__ import annotations

import json
from datetime import timedelta
from types import SimpleNamespace
from unittest import mock

from django.test import TestCase, override_settings
from django.utils import timezone

from analyst import advice, runner
from analyst.models import Finding, PlannedPurchase


def _po(key='bag:7', qty='1000', unit='g', stock_at_order=None, days_ago=0, status=PlannedPurchase.OPEN, finding=None,
        label='Bag X'):
    return PlannedPurchase.objects.create(
        key=key, label=label, quantity=qty, unit=unit, stock_at_order=stock_at_order, status=status, finding=finding,
        ordered_at=timezone.localdate() - timedelta(days=days_ago), note=advice.TICK_NOTE)


def _item(key='bag:7', stock=500.0, label='Bag X'):
    return SimpleNamespace(key=key, stock=stock, label=label, product_id=3)


class OrderedCardsTests(TestCase):
    def test_open_order_becomes_a_processed_row_with_a_link(self):
        po = _po(days_ago=2)
        rows = advice.ordered_cards([_item()], cards=[])
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual((row.key, row.check, row.severity), ('ordered:bag:7', advice.CHECK_ORDERED, Finding.INFO))
        self.assertEqual(row.title, 'Ordered 1 kg: Bag X')
        self.assertIn((timezone.localdate() - timedelta(days=2)).strftime('%d.%m.%Y'), row.body)
        self.assertTrue(row.data['processed'])
        self.assertEqual(row.data['po_ids'], [po.pk])
        self.assertEqual(row.link, '/admin/catalog/product/3/change/')

    def test_orders_of_one_item_are_summed_in_one_row(self):
        _po(qty='500')
        _po(qty='700')
        rows = advice.ordered_cards([_item()], cards=[])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].title, 'Ordered 1.2 kg: Bag X')

    def test_item_advised_again_keeps_its_normal_card_instead(self):
        _po()
        self.assertEqual(advice.ordered_cards([_item()], cards=[SimpleNamespace(key='purchase:bag:7')]), [])

    def test_closed_cancelled_and_forgotten_orders_are_not_shown(self):
        _po(status=PlannedPurchase.RECEIVED)
        _po(status=PlannedPurchase.CANCELLED)
        _po(days_ago=91)
        self.assertEqual(advice.ordered_cards([_item()], cards=[]), [])

    def test_item_missing_from_the_calculation_still_shows_its_order(self):
        _po(label='Old product')
        rows = advice.ordered_cards([], cards=[])
        self.assertEqual(rows[0].title, 'Ordered 1 kg: Old product')
        self.assertEqual(rows[0].link, '')


class SettleReceivedTests(TestCase):
    def test_stock_growth_by_half_of_the_order_means_arrived(self):
        po = _po(qty='1000', stock_at_order=200)
        self.assertEqual(advice.settle_received([_item(stock=700)]), 1)       # +500 = exactly half
        po.refresh_from_db()
        self.assertEqual(po.status, PlannedPurchase.RECEIVED)
        self.assertIn('arrived', po.note)

    def test_small_growth_or_a_fall_keeps_the_order_open(self):
        po = _po(qty='1000', stock_at_order=200)
        self.assertEqual(advice.settle_received([_item(stock=650)]), 0)
        self.assertEqual(advice.settle_received([_item(stock=50)]), 0)
        po.refresh_from_db()
        self.assertEqual(po.status, PlannedPurchase.OPEN)

    def test_order_without_baseline_or_item_is_left_alone(self):
        _po(key='bag:1', stock_at_order=None)
        _po(key='bag:2', stock_at_order=10)
        self.assertEqual(advice.settle_received([_item(key='bag:1', stock=99999)]), 0)     # no base stock level
        self.assertEqual(advice.settle_received([]), 0)                                      # the item is not in the calculation

    def test_generate_rebuilds_the_calculation_after_settling_and_skips_it_in_dry_run(self):
        _po(stock_at_order=200)
        ctx = SimpleNamespace(horizon_days=59)
        with mock.patch.object(advice.P, 'build', return_value=(ctx, [_item(stock=900)])) as build, \
             mock.patch.object(advice, 'rule_cards', return_value=[]):
            result = advice.generate(use_agent=False)
        self.assertEqual(build.call_count, 2)                    # the incoming shipment was closed → the calculation was rebuilt
        self.assertEqual(result.items, [])                       # a closed order is no longer shown
        _po(key='bag:9', stock_at_order=200)
        with mock.patch.object(advice.P, 'build', return_value=(ctx, [_item(key='bag:9', stock=900)])) as build, \
             mock.patch.object(advice, 'rule_cards', return_value=[]):
            result = advice.generate(use_agent=False, settle=False)
        self.assertEqual(build.call_count, 1)
        self.assertEqual(PlannedPurchase.objects.get(key='bag:9').status, PlannedPurchase.OPEN)   # dry-run writes nothing
        self.assertEqual([r.key for r in result.items], ['ordered:bag:9'])


class MarkOrderedBaselineTests(TestCase):
    def test_tick_remembers_the_stock_on_the_card(self):
        f = Finding.objects.create(
            key='purchase:bag:7', check_name=advice.CHECK_ADVICE, section=Finding.SECTION_PURCHASE, severity=Finding.WARN,
            title='Order 1 kg X',
            data={'action': 'buy', 'qty': 1000, 'unit': 'g', 'item': {'key': 'bag:7', 'label': 'X', 'stock': 123.5}})
        self.assertEqual(advice.mark_ordered([f]), 1)
        self.assertEqual(float(PlannedPurchase.objects.get(finding=f).stock_at_order), 123.5)


class RunnerProcessedRowsTests(TestCase):
    def _item(self):
        return advice.ordered_cards([_item()], cards=[])[0]

    def test_processed_row_is_created_done_and_not_counted_as_new(self):
        _po()
        now = timezone.now()
        fresh = runner.apply(runner.Outcome(items=[self._item()]), now, extra_ran=advice.PURCHASE_CHECKS)
        f = Finding.objects.get(key='ordered:bag:7')
        self.assertEqual((fresh, f.status), (0, Finding.DONE))
        from analyst import inbox
        self.assertEqual(inbox.new_count(), 0)                    # the badge does not count reference rows

    def test_row_stays_done_on_later_runs_and_disappears_when_the_order_closes(self):
        po = _po()
        now = timezone.now()
        runner.apply(runner.Outcome(items=[self._item()]), now, extra_ran=advice.PURCHASE_CHECKS)
        runner.apply(runner.Outcome(items=[self._item()]), now + timedelta(days=1), extra_ran=advice.PURCHASE_CHECKS)
        self.assertEqual(Finding.objects.get(key='ordered:bag:7').status, Finding.DONE)
        PlannedPurchase.objects.filter(pk=po.pk).update(status=PlannedPurchase.RECEIVED)
        runner.apply(runner.Outcome(items=[]), now + timedelta(days=2), extra_ran=advice.PURCHASE_CHECKS)
        self.assertIsNotNone(Finding.objects.get(key='ordered:bag:7').resolved_at)

    def test_resolved_row_comes_back_done_not_new(self):
        _po()
        now = timezone.now()
        runner.apply(runner.Outcome(items=[self._item()]), now, extra_ran=advice.PURCHASE_CHECKS)
        runner.apply(runner.Outcome(items=[]), now + timedelta(days=1), extra_ran=advice.PURCHASE_CHECKS)
        runner.apply(runner.Outcome(items=[self._item()]), now + timedelta(days=2), extra_ran=advice.PURCHASE_CHECKS)
        f = Finding.objects.get(key='ordered:bag:7')
        self.assertEqual((f.status, f.resolved_at), (Finding.DONE, None))


@override_settings(ANALYST_OWNER_EMAIL='boss@example.com')
class UndoOrderedRowTests(TestCase):
    def setUp(self):
        from django.contrib.auth import get_user_model
        self.owner = get_user_model().objects.create_user(
            email='boss@example.com', password='x', is_staff=True, is_superuser=True)
        self.client.force_login(self.owner)

    def _post(self, **data):
        return self.client.post('/admin/analyst/action/', data=json.dumps(data), content_type='application/json', secure=True)

    def test_undo_cancels_the_order_and_hides_the_row_at_once(self):
        po = _po()
        runner.apply(runner.Outcome(items=advice.ordered_cards([_item()], cards=[])), timezone.now(),
                     extra_ran=advice.PURCHASE_CHECKS)
        row = Finding.objects.get(key='ordered:bag:7')
        self.assertEqual(self._post(id=row.pk, action='reopen').status_code, 200)
        po.refresh_from_db()
        row.refresh_from_db()
        self.assertEqual(po.status, PlannedPurchase.CANCELLED)
        self.assertIsNotNone(row.resolved_at)
        self.assertNotContains(self.client.get('/admin/analyst/', secure=True), 'Ordered 1 kg')

    def test_ordered_row_is_visible_on_the_analyst_page(self):
        _po()
        runner.apply(runner.Outcome(items=advice.ordered_cards([_item()], cards=[])), timezone.now(),
                     extra_ran=advice.PURCHASE_CHECKS)
        self.assertContains(self.client.get('/admin/analyst/', secure=True), 'Ordered 1 kg: Bag X')
