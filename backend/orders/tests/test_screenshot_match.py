"""Tests of screenshot_match — everything is mocked, no real Gemini API."""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from orders import screenshot_match
from orders.models import BankAccount, BankTransaction, Order, PaymentMatch
from orders.screenshot_match import OCRResult, match_screenshot


def _mk_order_with_screenshot(*, total='760.00') -> Order:
    o = Order.objects.create(
        customer_name='Jane Doe',
        customer_phone='+380000000000',
        delivery_type='nova_poshta',
        delivery_address='Kyiv',
        payment_method='bank_transfer',
        payment_status='pending',
        total_price=Decimal(total),
        status='new',
    )
    # Simulate an uploaded screenshot (path is not used in the tests — OCR is mocked).
    o.payment_screenshot = 'payment_screenshots/fake.jpg'
    o.save(update_fields=['payment_screenshot'])
    return o


def _mk_tx(*, amount='760.00', minutes_ago=10) -> BankTransaction:
    acc, _ = BankAccount.objects.get_or_create(
        source='mono', external_id='acc1',
        defaults={'name': 'Mono Sole Proprietor', 'iban': 'UA85...', 'currency_code': 980, 'is_active': True},
    )
    return BankTransaction.objects.create(
        source='mono', account=acc,
        external_id=f'tx-{amount}-{minutes_ago}',
        transacted_at=timezone.now() - timedelta(minutes=minutes_ago),
        amount=Decimal(amount), currency_code=980,
        description='From: Jane Doe',
        counterparty_name='Jane Doe',
        comment='Garden goods',
        status='unmatched',
    )


def _ocr_result(*, amount=Decimal('760.00'), minutes_ago=10,
                is_receipt=True) -> OCRResult:
    return OCRResult(
        is_payment_receipt=is_receipt,
        amount=amount,
        currency='UAH',
        date=timezone.now() - timedelta(minutes=minutes_ago),
        sender_name='Jane Doe',
        recipient_name='John Smith',
        recipient_card_last4='0002',
        recipient_iban_tail=None,
        comment=None,
        confidence='high',
        raw={},
    )


class ScreenshotMatchTests(TestCase):
    @patch.object(screenshot_match, 'ocr_screenshot')
    def test_one_matching_tx__auto(self, mock_ocr):
        order = _mk_order_with_screenshot(total='760.00')
        tx = _mk_tx(amount='760.00', minutes_ago=15)
        mock_ocr.return_value = _ocr_result(amount=Decimal('760.00'), minutes_ago=15)

        res = match_screenshot(order)

        self.assertTrue(res.auto_confirmed)
        self.assertEqual(res.matched_transaction_id, tx.external_id)
        order.refresh_from_db()
        self.assertEqual(order.payment_status, 'paid')
        self.assertTrue(PaymentMatch.objects.filter(order=order, transaction=tx,
                                                     auto_confirmed=True).exists())

    @patch.object(screenshot_match, 'ocr_screenshot')
    def test_no_matching_tx__possible_fraud(self, mock_ocr):
        order = _mk_order_with_screenshot(total='760.00')
        # There are no transactions in the DB at all.
        mock_ocr.return_value = _ocr_result(amount=Decimal('760.00'))

        res = match_screenshot(order)

        self.assertFalse(res.auto_confirmed)
        self.assertEqual(res.candidates_count, 0)
        self.assertIn('NO matching transaction', res.reason)
        order.refresh_from_db()
        self.assertEqual(order.payment_status, 'pending')  # not paid

    @patch.object(screenshot_match, 'ocr_screenshot')
    def test_multiple_matching_txs__manual(self, mock_ocr):
        order = _mk_order_with_screenshot(total='760.00')
        # Two transactions with the same amount in the window.
        _mk_tx(amount='760.00', minutes_ago=10)
        _mk_tx(amount='760.00', minutes_ago=30)
        mock_ocr.return_value = _ocr_result(amount=Decimal('760.00'), minutes_ago=15)

        res = match_screenshot(order)

        self.assertFalse(res.auto_confirmed)
        self.assertEqual(res.candidates_count, 2)
        self.assertIn('multiple', res.reason)
        # Manual candidates are created.
        self.assertEqual(PaymentMatch.objects.filter(order=order).count(), 2)
        for m in PaymentMatch.objects.filter(order=order):
            self.assertFalse(m.is_active)

    @patch.object(screenshot_match, 'ocr_screenshot')
    def test_amount_mismatch_screenshot_vs_order__skip(self, mock_ocr):
        """The amount on the screenshot (500) != the order's total_price (760)."""
        order = _mk_order_with_screenshot(total='760.00')
        _mk_tx(amount='500.00', minutes_ago=10)
        mock_ocr.return_value = _ocr_result(amount=Decimal('500.00'))

        res = match_screenshot(order)

        self.assertFalse(res.auto_confirmed)
        self.assertIn('amount on screenshot', res.reason)
        order.refresh_from_db()
        self.assertEqual(order.payment_status, 'pending')

    @patch.object(screenshot_match, 'ocr_screenshot')
    def test_already_paid_skipped(self, mock_ocr):
        order = _mk_order_with_screenshot(total='760.00')
        order.payment_status = 'paid'
        order.save(update_fields=['payment_status'])

        res = match_screenshot(order)

        mock_ocr.assert_not_called()  # don't waste Gemini tokens.
        self.assertEqual(res.reason, 'order already paid')

    @patch.object(screenshot_match, 'ocr_screenshot')
    def test_not_a_receipt__skip(self, mock_ocr):
        order = _mk_order_with_screenshot(total='760.00')
        mock_ocr.return_value = _ocr_result(is_receipt=False)

        res = match_screenshot(order)

        self.assertFalse(res.auto_confirmed)
        self.assertIn('not a payment receipt', res.reason)
