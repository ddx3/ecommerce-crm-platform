"""Payer full-name matching: Mono transliteration and the full name inside Privat's payment purpose.

The "full name + amount" factor fired in prod 3 times out of 63 (5%) — effectively dead:
  - Mono returns the full name in Latin script in its own romanization,
    while in the order it is in Cyrillic → no shared token;
  - in Privat `counterparty_name` is a transit account
    ("Транз.рахунок платежi_ DN, DG," — transit payment account), and the real full name is in the
    payment purpose.

The payment purposes below follow real rows from the prod statement for 29.07–01.08.2026;
the full names are fictional. The Ukrainian names and bank wording below are language data (the
matcher works on Ukrainian text and Mono/Privat wording), so they stay Ukrainian.
"""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from django.test import TestCase
from django.utils import timezone

from orders import bank_matcher
from orders.models import BankAccount, BankTransaction, Order


def _account(source: str = 'mono') -> BankAccount:
    return BankAccount.objects.create(
        source=source, external_id=f'acc-{source}', name=f'{source} sole proprietor',
        iban='UA000000000000000000000000000', currency_code=980, is_active=True,
    )


def _order(*, customer_name: str, total: str, number: str) -> Order:
    return Order.objects.create(
        order_number=number, customer_name=customer_name,
        customer_email='x@example.com', customer_phone='+380670000001',
        total_price=Decimal(total), payment_method='bank_transfer',
        payment_status='pending', status='processing',
    )


def _tx(account: BankAccount, *, amount: str, counterparty: str = '',
        comment: str = '', description: str = '', ident: str = 'tx-1') -> BankTransaction:
    return BankTransaction.objects.create(
        source=account.source, external_id=ident, account=account,
        transacted_at=timezone.now(), amount=Decimal(amount), currency_code=980,
        description=description, comment=comment, counterparty_name=counterparty,
        status='unmatched', raw_data={},
    )


class SkeletonTests(TestCase):
    """Romanization-insensitive skeleton — the basis of comparison."""

    def test_cyrillic_and_mono_latin_collapse_to_the_same_skeleton(self):
        # Ukrainian names against Mono's Latin romanization: language data, must stay Cyrillic.
        for cyr, lat in [
            ('Войтовська', 'Voitovska'),
            ('Алла', 'Alla'),
            ('Сергєєва', 'Serhieieva'),
            ('Ірина', 'Iryna'),
            ('Кошель', 'Koshel'),
            ('Олена', 'Olena'),
        ]:
            with self.subTest(cyr=cyr):
                self.assertEqual(bank_matcher._skeleton(cyr), bank_matcher._skeleton(lat))

    def test_distinct_surnames_do_not_collapse(self):
        """Collapsing digraphs must not make different surnames identical."""
        # Ukrainian surnames (language data): the skeleton logic is built for Ukrainian spelling.
        distinct = ['Войтовська', 'Петренко', 'Бондар', 'Гавриленко', 'Макаренко', 'Остапчук']
        skeletons = [bank_matcher._skeleton(n) for n in distinct]
        self.assertEqual(len(set(skeletons)), len(distinct))


class MonoTransliteratedNameTests(TestCase):
    """Real case: order 12741 for 430 UAH and a transfer from a payer shown in Latin script."""

    def setUp(self):
        self.acc = _account('mono')

    def test_transliterated_counterparty_auto_confirms(self):
        order = _order(customer_name='Войтовська Алла Василівна',
                       total='430.00', number='12741')
        tx = _tx(self.acc, amount='430.00', counterparty='Voitovska Alla')
        result = bank_matcher.match_transaction(tx)
        self.assertEqual(result['auto_confirmed_order'], '12741')
        order.refresh_from_db()
        self.assertEqual(order.payment_status, 'paid')

    def test_wrong_amount_blocks_auto_confirm_even_with_matching_name(self):
        _order(customer_name='Войтовська Алла Василівна', total='430.00', number='12741')
        tx = _tx(self.acc, amount='431.00', counterparty='Voitovska Alla')
        result = bank_matcher.match_transaction(tx)
        self.assertIsNone(result['auto_confirmed_order'])

    def test_different_person_same_amount_is_not_matched(self):
        _order(customer_name='Войтовська Алла Василівна', total='430.00', number='12741')
        tx = _tx(self.acc, amount='430.00', counterparty='Petrenko Ivan')
        result = bank_matcher.match_transaction(tx)
        self.assertIsNone(result['auto_confirmed_order'])
        self.assertEqual(result['candidates'], [])


class PrivatNameInPurposeTests(TestCase):
    """In Privat the counterparty is a transit account, the full name is inside the payment purpose."""

    # Privat's own wording for the transit account (language data, matched by the code).
    TRANSIT = 'Транз.рахунок платежi_ DN, DG,'

    def setUp(self):
        self.acc = _account('privat')

    def test_name_inside_purpose_auto_confirms(self):
        order = _order(customer_name='Мельничук Вероніка Вікторівна',
                       total='595.00', number='12761')
        tx = _tx(self.acc, amount='595.00', counterparty=self.TRANSIT,
                 comment='Сплата за ....товар 12762, Мельничук Вероніка Вікторівна')
        result = bank_matcher.match_transaction(tx)
        # The number in the payment purpose is someone else's (12762); only the full name saves the
        # match.
        self.assertEqual(result['auto_confirmed_order'], '12761')
        order.refresh_from_db()
        self.assertEqual(order.payment_status, 'paid')

    def test_purpose_without_order_number_still_matches_by_name(self):
        _order(customer_name='Захарченко Ольга Вікторівна', total='870.00', number='12744')
        tx = _tx(self.acc, amount='870.00', counterparty=self.TRANSIT,
                 comment='Сплата за ...., Захарченко Ольга Вікторівна')
        result = bank_matcher.match_transaction(tx)
        self.assertEqual(result['auto_confirmed_order'], '12744')

    def test_transit_account_alone_never_matches(self):
        """The transit account name must not be taken for anyone's full name."""
        _order(customer_name='Транзитний Рахунок', total='500.00', number='12800')
        tx = _tx(self.acc, amount='500.00', counterparty=self.TRANSIT,
                 comment='4000 **** **** 0001 Зарахування коштів')
        result = bank_matcher.match_transaction(tx)
        self.assertIsNone(result['auto_confirmed_order'])

    def test_single_token_customer_name_is_not_enough(self):
        """Order 12757 is recorded as "Квітка" (Flower) — a single word is not enough for AUTO."""
        _order(customer_name='Квітка', total='286.00', number='12757')
        tx = _tx(self.acc, amount='286.00', counterparty=self.TRANSIT,
                 comment='За товари для саду, Квітка')
        result = bank_matcher.match_transaction(tx)
        self.assertIsNone(result['auto_confirmed_order'])

    def test_purpose_noise_words_do_not_fabricate_a_match(self):
        """The purpose "за товари для саду та городу" (for goods for the garden and vegetable plot; Ukrainian
noise words, so it stays Ukrainian) must not match someone's full name."""
        _order(customer_name='Садова Городня', total='333.00', number='12801')
        tx = _tx(self.acc, amount='333.00', counterparty=self.TRANSIT,
                 comment='за товари для саду та городу згідно замовлення')
        result = bank_matcher.match_transaction(tx)
        self.assertIsNone(result['auto_confirmed_order'])


class AmbiguousCandidateTests(TestCase):
    """A tie at the top must not silently pay someone else's order."""

    def setUp(self):
        self.acc = _account('mono')

    def test_two_equal_candidates_go_to_manual_review(self):
        _order(customer_name='Іваненко Іван Іванович', total='500.00', number='12900')
        _order(customer_name='Іваненко Іван Іванович', total='500.00', number='12901')
        tx = _tx(self.acc, amount='500.00', counterparty='Ivanenko Ivan')
        result = bank_matcher.match_transaction(tx)
        self.assertIsNone(result['auto_confirmed_order'])
        self.assertCountEqual(result['manual_queue_orders'], ['12900', '12901'])
        tx.refresh_from_db()
        self.assertEqual(tx.status, 'manual_review')
        self.assertEqual(
            Order.objects.filter(payment_status='paid').count(), 0,
            'neither of the two orders should have been paid automatically',
        )

    def test_unique_winner_still_auto_confirms(self):
        _order(customer_name='Іваненко Іван Іванович', total='500.00', number='12900')
        _order(customer_name='Петренко Петро Петрович', total='500.00', number='12901')
        tx = _tx(self.acc, amount='500.00', counterparty='Ivanenko Ivan')
        result = bank_matcher.match_transaction(tx)
        self.assertEqual(result['auto_confirmed_order'], '12900')


class RegressionOrderNumberFactorTests(TestCase):
    """The order number remains the main factor (95% of matches in production)."""

    def setUp(self):
        self.acc = _account('privat')

    def test_order_number_with_exact_amount_still_auto_confirms(self):
        _order(customer_name='Андрієнко Ірина Юріївна', total='1151.00', number='12721')
        tx = _tx(self.acc, amount='1151.00', counterparty='Транз.рахунок платежi_ DN, DG,',
                 comment='За товари для саду та городу #12721, Андрієнко Ірина Юріївна')
        result = bank_matcher.match_transaction(tx)
        self.assertEqual(result['auto_confirmed_order'], '12721')

    def test_foreign_order_number_does_not_steal_another_order(self):
        """The customer entered the number of a neighboring order — the amount and full name do not match."""
        _order(customer_name='Щербак Оксана Сергіївна', total='252.00', number='12740')
        tx = _tx(self.acc, amount='252.00', counterparty='Транз.рахунок платежi_ DN, DG,',
                 comment='за товари для саду та городу згідно замовлення 12739')
        result = bank_matcher.match_transaction(tx)
        self.assertIsNone(result['auto_confirmed_order'])


class NoiseFilterRegressionTests(TestCase):
    """Acquiring noise must still be ignored after widening the full-name search."""

    def test_acquiring_credit_is_ignored(self):
        acc = _account('mono')
        tx = _tx(acc, amount='2223.71', counterparty='АТ "УНІВЕРСАЛ БАНК"',
                 comment='Покриття за проведені трансакції згідно договору еквайрингу')
        result = bank_matcher.match_transaction(tx)
        self.assertTrue(result['skipped'])
        tx.refresh_from_db()
        self.assertEqual(tx.status, 'ignored')
