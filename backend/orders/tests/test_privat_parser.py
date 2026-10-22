"""Tests of the Privat24 for Business parser. No live API — on fake data
in the format the API returns."""
from __future__ import annotations

from decimal import Decimal

from django.test import TestCase

from orders import privatbank_business as privat
from orders.models import BankAccount, BankTransaction


# Sample item in the format Privat returns (based on historical docs).
SAMPLE_CREDIT = {
    'ID': 'TX-12345',
    'TRANTYPE': 'C',
    'SUM': '760.00',
    'CCY': 'UAH',
    'DATE_TIME_DAT_OD_TIM_P': '06.05.2026 14:30:21',
    'AUT_MY_ACC': 'UA903052990000026000011710000',
    'AUT_MY_NAM': 'Sole proprietor Jane Roe',
    'AUT_MY_CRF': '2806101020',
    'AUT_CNTR_NAM': 'Jane Doe',
    'AUT_CNTR_ACC': 'UA111234567890',
    'AUT_CNTR_CRF': '1234567890',
    'OSND': 'for garden goods per order 11776',
    'REF': 'ABC123',
}

SAMPLE_DEBIT = {
    'ID': 'TX-67890',
    'TRANTYPE': 'D',
    'SUM': '500.00',
    'CCY': 'UAH',
    'DATE_TIME_DAT_OD_TIM_P': '06.05.2026 09:00:00',
    'AUT_CNTR_NAM': 'Some shop',
    'OSND': 'Payment for goods',
    'REF': 'XYZ',
}


class PrivatParserTests(TestCase):

    def test_credit_parsed_with_positive_amount(self):
        items = list(privat.iter_statement_items([SAMPLE_CREDIT], iban='UA55...'))
        self.assertEqual(len(items), 1)
        it = items[0]
        self.assertEqual(it['amount'], Decimal('760.00'))
        self.assertGreater(it['amount'], 0)
        self.assertEqual(it['currency_code'], 980)
        self.assertEqual(it['counterparty_name'], 'Jane Doe')
        self.assertEqual(it['comment'], 'for garden goods per order 11776')
        self.assertIn('From: Jane Doe', it['description'])
        self.assertTrue(it['external_id'].startswith('UA55...:TX-12345'))

    def test_debit_parsed_with_negative_amount(self):
        items = list(privat.iter_statement_items([SAMPLE_DEBIT], iban='UA55...'))
        it = items[0]
        self.assertEqual(it['amount'], Decimal('-500.00'))
        self.assertLess(it['amount'], 0)
        self.assertIn('To:', it['description'])  # debit → "To:" instead of "From:"

    def test_amount_with_comma_separator(self):
        item = {**SAMPLE_CREDIT, 'SUM': '760,00'}
        items = list(privat.iter_statement_items([item], iban=''))
        self.assertEqual(items[0]['amount'], Decimal('760.00'))

    def test_external_id_prefixed_with_iban(self):
        items = list(privat.iter_statement_items([SAMPLE_CREDIT], iban='UA85ABC'))
        self.assertEqual(items[0]['external_id'], 'UA85ABC:TX-12345')

    def test_unknown_currency_defaults_to_uah(self):
        item = {**SAMPLE_CREDIT, 'CCY': None}
        items = list(privat.iter_statement_items([item], iban=''))
        self.assertEqual(items[0]['currency_code'], 980)


class PrivatTransactionStorageTests(TestCase):
    """Verifies that Privat transactions go into BankTransaction without conflicts with Mono."""

    def test_can_save_privat_alongside_mono(self):
        mono = BankAccount.objects.create(
            source='mono', external_id='m1', name='Mono', iban='UA90322001...',
            currency_code=980, is_active=True,
        )
        privat_acc = BankAccount.objects.create(
            source='privat', external_id='UA90305299...', name='Privat',
            iban='UA90305299...', currency_code=980, is_active=True,
        )

        # The same external_id in different sources must be allowed (unique per pair).
        BankTransaction.objects.create(
            source='mono', account=mono, external_id='shared-id',
            transacted_at='2026-05-06T10:00:00Z', amount=Decimal('100'),
            description='From: A', comment='', counterparty_name='A',
        )
        BankTransaction.objects.create(
            source='privat', account=privat_acc, external_id='shared-id',
            transacted_at='2026-05-06T10:00:00Z', amount=Decimal('100'),
            description='From: A', comment='', counterparty_name='A',
        )
        self.assertEqual(BankTransaction.objects.count(), 2)

    def test_dedup_within_same_source(self):
        privat_acc = BankAccount.objects.create(
            source='privat', external_id='UA55...', name='Privat',
            iban='UA55...', currency_code=980, is_active=True,
        )
        BankTransaction.objects.create(
            source='privat', account=privat_acc, external_id='dup',
            transacted_at='2026-05-06T10:00:00Z', amount=Decimal('100'),
        )
        from django.db import IntegrityError
        with self.assertRaises(IntegrityError):
            BankTransaction.objects.create(
                source='privat', account=privat_acc, external_id='dup',
                transacted_at='2026-05-06T11:00:00Z', amount=Decimal('200'),
            )


class PrivatMatcherIntegrationTests(TestCase):
    """A Privat transaction must be matched through the very same bank_matcher."""

    def test_privat_credit_matches_order_by_number(self):
        from orders import bank_matcher
        from orders.models import Order

        order = Order.objects.create(
            customer_name='Jane Doe', customer_phone='+380000000000',
            delivery_type='nova_poshta', delivery_address='Kyiv',
            payment_method='bank_transfer', payment_status='pending',
            total_price=Decimal('760.00'), status='new',
        )
        privat_acc = BankAccount.objects.create(
            source='privat', external_id='UA55...', name='Privat',
            iban='UA55...', currency_code=980, is_active=True,
        )
        # Take the Privat parser → BankTransaction
        sample = {**SAMPLE_CREDIT,
                  'OSND': f'for goods per order {order.order_number}'}
        from django.utils import timezone
        for parsed in privat.iter_statement_items([sample], iban='UA55...'):
            tx = BankTransaction.objects.create(
                source='privat', account=privat_acc,
                external_id=parsed['external_id'],
                transacted_at=timezone.now(),  # for the window — recent
                amount=parsed['amount'], currency_code=parsed['currency_code'],
                description=parsed['description'],
                comment=parsed['comment'],
                counterparty_name=parsed['counterparty_name'],
            )

        result = bank_matcher.match_transaction(tx)

        self.assertEqual(result['auto_confirmed_order'], order.order_number)
        order.refresh_from_db()
        self.assertEqual(order.payment_status, 'paid')
