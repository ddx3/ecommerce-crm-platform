"""reconcile_pending_payments: auto-cancel only for abandoned Monobank orders in the "New"
status."""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from io import StringIO
from unittest import mock

from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from orders import monobank
from orders.models import Order


def _order(*, age_days=8.0, status='new', method='monobank', pay='pending', invoice='inv-1') -> Order:
    o = Order.objects.create(
        customer_name='Test', customer_phone='+380000000000',
        delivery_type='nova_poshta', delivery_address='Kyiv',
        payment_method=method, payment_status=pay, status=status,
        total_price=Decimal('100.00'), monobank_invoice_id=invoice,
    )
    # created_at is auto_now_add, so we shift it manually.
    Order.objects.filter(pk=o.pk).update(created_at=timezone.now() - timedelta(days=age_days))
    o.refresh_from_db()
    return o


def _run(mono_status='expired', amount=10000, *args):
    with mock.patch.object(
        monobank, 'get_invoice_status', return_value={'status': mono_status, 'amount': amount},
    ) as m:
        call_command('reconcile_pending_payments', *args, stdout=StringIO())
    return m


class AutoCancelTests(TestCase):
    def test_abandoned_order_cancelled_after_window_and_invoice_closed(self):
        o = _order(age_days=8)
        _run('expired')
        o.refresh_from_db()
        self.assertEqual(o.status, 'cancelled')
        self.assertEqual(o.monobank_invoice_id, '')

    def test_failure_and_reversed_statuses_also_cancel_after_window(self):
        for st in ('failure', 'reversed'):
            o = _order(age_days=8, invoice=f'inv-{st}')
            _run(st)
            o.refresh_from_db()
            self.assertEqual(o.status, 'cancelled', st)

    def test_not_cancelled_before_window(self):
        # 2 days — already outside the old 24 h window, but still within 7 days.
        o = _order(age_days=2)
        _run('expired')
        o.refresh_from_db()
        self.assertEqual(o.status, 'new')
        self.assertEqual(o.monobank_invoice_id, 'inv-1')

    def test_order_taken_into_work_by_manager_is_untouched(self):
        for st in ('processing', 'shipped', 'delivered'):
            o = _order(age_days=8, status=st, invoice=f'inv-{st}')
            _run('expired')
            o.refresh_from_db()
            self.assertEqual(o.status, st)
            self.assertEqual(o.monobank_invoice_id, f'inv-{st}')

    def test_other_payment_method_untouched(self):
        # The manager gave a Monobank link for an order paid by bank transfer — the invoice must not
        # cancel it.
        o = _order(age_days=8, method='bank_transfer')
        _run('expired')
        o.refresh_from_db()
        self.assertEqual(o.status, 'new')

    def test_manager_reopen_is_not_cancelled_again(self):
        o = _order(age_days=8)
        _run('expired')
        Order.objects.filter(pk=o.pk).update(status='new')  # the manager returned it to "New"
        m = _run('expired')
        o.refresh_from_db()
        self.assertEqual(o.status, 'new')
        m.assert_not_called()  # invoice closed — the order is not polled

    def test_live_invoice_is_not_cancelled(self):
        o = _order(age_days=8)
        _run('created')
        o.refresh_from_db()
        self.assertEqual(o.status, 'new')
        self.assertEqual(o.monobank_invoice_id, 'inv-1')

    def test_too_old_order_is_not_polled(self):
        o = _order(age_days=15)
        m = _run('expired')
        o.refresh_from_db()
        self.assertEqual(o.status, 'new')
        m.assert_not_called()

    def test_dry_run_changes_nothing(self):
        o = _order(age_days=8)
        _run('expired', 10000, '--dry-run')
        o.refresh_from_db()
        self.assertEqual(o.status, 'new')
        self.assertEqual(o.monobank_invoice_id, 'inv-1')


class PaidPathUnchangedTests(TestCase):
    def test_paid_invoice_marks_paid_and_keeps_manager_status(self):
        o = _order(age_days=1, status='processing')
        _run('success', 10000)
        o.refresh_from_db()
        self.assertEqual(o.payment_status, 'paid')
        self.assertEqual(o.status, 'processing')

    def test_amount_mismatch_does_not_mark_paid(self):
        o = _order(age_days=1)
        _run('success', 9999)
        o.refresh_from_db()
        self.assertEqual(o.payment_status, 'pending')
