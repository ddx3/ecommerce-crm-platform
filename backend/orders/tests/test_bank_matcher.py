"""Unit tests for bank_matcher (3 factors: number+amount, full name+amount, noise filter)."""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from django.test import TestCase
from django.utils import timezone

from orders import bank_matcher
from orders.models import (
    BankAccount, BankTransaction, Order, OrderItem, PaymentMatch,
)


def _mk_account(name: str = 'Mono Account') -> BankAccount:
    return BankAccount.objects.create(
        source='mono', external_id=f'acc-{name}',
        name=name, iban='UA903220010000026000310010000',
        currency_code=980, is_active=True,
    )


def _mk_order(*, customer_name: str, total: str = '760.00',
              created_offset_min: int = -10) -> Order:
    """created_offset_min: relative to now. -10 = created 10 minutes ago."""
    o = Order.objects.create(
        customer_name=customer_name,
        customer_phone='+380000000000',
        delivery_type='nova_poshta',
        delivery_address='Kyiv',
        payment_method='bank_transfer',
        payment_status='pending',
        total_price=Decimal(total),
        status='new',
    )
    # We shift created_at manually, because auto_now_add sets timezone.now().
    Order.objects.filter(pk=o.pk).update(
        created_at=timezone.now() + timedelta(minutes=created_offset_min)
    )
    o.refresh_from_db()
    return o


def _mk_tx(account: BankAccount, *, amount: str = '760.00',
           comment: str = '', counterparty: str = '',
           description: str = '', minutes_ago: int = 0) -> BankTransaction:
    return BankTransaction.objects.create(
        source='mono', account=account,
        external_id=f'tx-{counterparty}-{amount}-{minutes_ago}',
        transacted_at=timezone.now() - timedelta(minutes=minutes_ago),
        amount=Decimal(amount), currency_code=980,
        description=description, comment=comment,
        counterparty_name=counterparty, status='unmatched',
    )


class FactorOrderNumberTests(TestCase):
    """Factor #1: order number in the comment + amount."""

    def setUp(self):
        self.acc = _mk_account()

    def test_order_number_with_correct_amount__auto(self):
        order = _mk_order(customer_name='John Smith', total='760.00')
        tx = _mk_tx(self.acc, amount='760.00',
                    comment=f'for goods per order {order.order_number}',
                    counterparty='Unknown Person')

        result = bank_matcher.match_transaction(tx)

        self.assertEqual(result['auto_confirmed_order'], order.order_number)
        order.refresh_from_db()
        self.assertEqual(order.payment_status, 'paid')
        tx.refresh_from_db()
        self.assertEqual(tx.status, 'matched')

    def test_order_number_wrong_amount__manual(self):
        order = _mk_order(customer_name='John Smith', total='760.00')
        tx = _mk_tx(self.acc, amount='500.00',  # ← less
                    comment=f'order {order.order_number}',
                    counterparty='Somebody')

        result = bank_matcher.match_transaction(tx)

        self.assertIsNone(result['auto_confirmed_order'])
        self.assertIn(order.order_number, result['manual_queue_orders'])
        order.refresh_from_db()
        self.assertEqual(order.payment_status, 'pending')  # not paid
        # Manual queue → status manual_review.
        tx.refresh_from_db()
        self.assertEqual(tx.status, 'manual_review')
        # Reason contains amount mismatch.
        match = PaymentMatch.objects.get(order=order, transaction=tx)
        self.assertEqual(match.confidence, 50)
        self.assertFalse(match.is_active)
        self.assertEqual(match.reasons[0]['type'], 'order_number_amount_mismatch')


class FactorNameAndAmountTests(TestCase):
    """Factor #2: counterparty full name + exact amount."""

    def setUp(self):
        self.acc = _mk_account()

    def test_full_name_match__auto(self):
        order = _mk_order(customer_name='Jane Doe', total='760.00')
        tx = _mk_tx(self.acc, amount='760.00',
                    counterparty='Jane Doe', comment='Goods for the garden')

        result = bank_matcher.match_transaction(tx)

        self.assertEqual(result['auto_confirmed_order'], order.order_number)
        order.refresh_from_db()
        self.assertEqual(order.payment_status, 'paid')

    def test_name_swapped_words__auto(self):
        """Word order does not matter (banks sometimes swap surname/name)."""
        order = _mk_order(customer_name='John Smith', total='1000.00')
        tx = _mk_tx(self.acc, amount='1000.00', counterparty='Smith John')

        result = bank_matcher.match_transaction(tx)
        self.assertEqual(result['auto_confirmed_order'], order.order_number)

    def test_name_match_but_wrong_amount__no_match(self):
        order = _mk_order(customer_name='Jane Doe', total='760.00')
        tx = _mk_tx(self.acc, amount='500.00', counterparty='Jane Doe')

        result = bank_matcher.match_transaction(tx)

        self.assertIsNone(result['auto_confirmed_order'])
        self.assertEqual(result['manual_queue_orders'], [])
        order.refresh_from_db()
        self.assertEqual(order.payment_status, 'pending')

    def test_only_one_token_match__no_match(self):
        """Only one shared token (Olga, Olga — a frequent coincidence) → no match."""
        order = _mk_order(customer_name='Olga Brown', total='760.00')
        tx = _mk_tx(self.acc, amount='760.00', counterparty='Olga White')

        result = bank_matcher.match_transaction(tx)
        self.assertIsNone(result['auto_confirmed_order'])
        self.assertEqual(result['manual_queue_orders'], [])


class NoiseFilterTests(TestCase):
    """Transactions from acquiring and FUIB MoneyTransfer must be ignored."""

    def setUp(self):
        self.acc = _mk_account()

    def test_acquiring_transfer__skipped(self):
        # An order for the same amount exists — but it must not match.
        # LANGUAGE DATA: the bank name stays in Ukrainian, as it appears in the statement; the noise
        # filter (NOISE_COUNTERPARTIES) matches that Ukrainian text.
        _mk_order(customer_name='Somebody', total='12225.96')
        tx = _mk_tx(self.acc, amount='12225.96',
                    counterparty='АТ "УНІВЕРСАЛ БАНК"',
                    description='From: АТ "УНІВЕРСАЛ БАНК"',
                    comment='Coverage for completed transactions according to')

        result = bank_matcher.match_transaction(tx)

        self.assertTrue(result['skipped'])
        self.assertIn('noise', result['reason'])
        tx.refresh_from_db()
        self.assertEqual(tx.status, 'ignored')

    def test_fuib_moneytransfer__skipped(self):
        _mk_order(customer_name='Somebody', total='460.00')
        tx = _mk_tx(self.acc, amount='460.00',
                    counterparty='FUIB MoneyTransfer',
                    description='From: FUIB MoneyTransfer')

        result = bank_matcher.match_transaction(tx)

        self.assertTrue(result['skipped'])
        self.assertIn('noise', result['reason'])

    def test_debit_skipped(self):
        """Debits (negative amount) are ignored."""
        tx = _mk_tx(self.acc, amount='-500.00', counterparty='Shop')
        result = bank_matcher.match_transaction(tx)
        self.assertTrue(result['skipped'])
        self.assertEqual(result['reason'], 'not a credit')


class WindowTests(TestCase):
    """Candidates are only orders within a window around the transaction time."""

    def setUp(self):
        self.acc = _mk_account()

    def test_old_order_not_a_candidate(self):
        """An order created 5 days ago must not match."""
        order = _mk_order(customer_name='Jane Doe', total='760.00',
                          created_offset_min=-60 * 24 * 5)  # 5 days ago
        tx = _mk_tx(self.acc, amount='760.00', counterparty='Jane Doe')

        result = bank_matcher.match_transaction(tx)
        self.assertIsNone(result['auto_confirmed_order'])
        self.assertEqual(result['manual_queue_orders'], [])

    def test_order_just_created_matches(self):
        order = _mk_order(customer_name='Jane Doe', total='760.00',
                          created_offset_min=-5)
        tx = _mk_tx(self.acc, amount='760.00', counterparty='Jane Doe')

        result = bank_matcher.match_transaction(tx)
        self.assertEqual(result['auto_confirmed_order'], order.order_number)


class IdempotencyTests(TestCase):
    """Re-running the matcher has no side effects."""

    def setUp(self):
        self.acc = _mk_account()

    def test_paid_order_not_double_processed(self):
        order = _mk_order(customer_name='Jane Doe', total='760.00')
        tx = _mk_tx(self.acc, amount='760.00', counterparty='Jane Doe')

        # First run.
        bank_matcher.match_transaction(tx)
        order.refresh_from_db()
        self.assertEqual(order.payment_status, 'paid')

        # Second run — nothing breaks, the status stays.
        tx.refresh_from_db()
        result2 = bank_matcher.match_transaction(tx)
        self.assertTrue(result2['skipped'])
        order.refresh_from_db()
        self.assertEqual(order.payment_status, 'paid')
