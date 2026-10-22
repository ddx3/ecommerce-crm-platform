"""In plain language: the agent's explanation without jargon, the line of numbers without service
terms."""
from datetime import timedelta
from decimal import Decimal

from django.test import TestCase, override_settings

from analyst import advice, agent
from analyst import purchasing as P
from analyst.tests_purchasing import NOW, _find, _order, _product

GATES = dict(ANALYST_LEAD_DAYS=14, ANALYST_COVER_DAYS=45, ANALYST_MIN_REVENUE=0, ANALYST_MIN_SALES_MONTHS=0,
             ANALYST_OWNER_EMAIL='boss@example.com')


def _setup():
    from catalog.models import ProductVariant
    other = ProductVariant.objects.create(product=_product('Orchid Feed'), name='Orchid Feed, 500 ml', price=Decimal('300'), stock=30)
    main = ProductVariant.objects.create(product=other.product, name='Orchid Feed, 40 g', price=Decimal('80'), stock=40)
    _order(other, 3, NOW - timedelta(days=5))
    for d in (2, 6, 10, 14, 18):
        _order(main, 3, NOW - timedelta(days=d))
    return main, other


@override_settings(**GATES)
class PlainCardTests(TestCase):
    def setUp(self):
        self.main, self.other = _setup()
        self.ctx, self.items = P.build(NOW)
        self.item = _find(self.items, f'var:{self.main.pk}')

    def test_facts_line_has_no_service_words_and_keeps_product_names(self):
        facts = advice._facts(self.item, self.ctx)
        self.assertTrue(facts.startswith('In stock:'))
        self.assertIn('we expect to sell', facts)
        self.assertIn('enough for', facts)
        self.assertIn('"Orchid Feed, 500 ml" — 27 pcs in stock', facts)             # the name was not degraded to lowercase by capitalize()
        for jargon in ('forecast for', '×', 'correction', 'history', 'factory-made / own stock', 'lead', 'shop_index'):
            self.assertNotIn(jargon, facts)

    def test_out_of_stock_facts_say_it_plainly(self):
        from catalog.models import ProductVariant
        ProductVariant.objects.filter(pk=self.main.pk).update(stock=0)
        facts = advice._facts(_find(P.build(NOW)[1], f'var:{self.main.pk}'), self.ctx)
        self.assertTrue(facts.startswith('Out of stock'))

    def test_sibling_line_for_the_owner_is_plain_and_for_the_agent_is_detailed(self):
        s = self.item.siblings[0]
        plain, detailed = P.sibling_line(s, kind=False), P.sibling_line(s)
        self.assertEqual(plain, '"Orchid Feed, 500 ml" — 27 pcs in stock, sold in 60 days: 3')
        self.assertIn('(factory-made / own stock)', detailed)

    def test_quantities_are_rounded_for_people(self):
        self.assertEqual(P.human_qty(8259, 'g'), '8.3 kg')
        self.assertEqual(P.human_qty(19277.4, 'g'), '19.3 kg')
        self.assertEqual(P.human_qty(12500, 'g'), '12.5 kg')
        self.assertEqual(P.human_qty(1500, 'ml'), '1.5 l')
        self.assertEqual(P.human_qty(800.4, 'g'), '800 g')
        self.assertEqual(P.human_qty(2.34, 'pcs'), '2.3 pcs')

    def test_rule_card_does_not_state_a_delivery_time(self):
        for c in advice.rule_cards(self.ctx, [self.item]):
            self.assertFalse(any(w in c.body.lower() for w in ('deliver', 'supply')))

    def test_rule_card_reason_no_longer_advertises_the_method(self):
        card = advice.rule_cards(self.ctx, [self.item])
        for c in card:
            self.assertNotIn('without AI', c.body)


@override_settings(**GATES)
class AgentSpeaksPlainTests(TestCase):
    def setUp(self):
        self.main, _ = _setup()
        self.session = agent.Session(*P.build(NOW))
        self.key = f'var:{self.main.pk}'

    def test_technical_words_in_the_reason_are_rejected(self):
        for bad in ('Coverage 10 days at lead 14, 50 orders, top_order_share 0.06.',
                    'Look at orders_60 and sales_months_12 — all good.',
                    'shop_index 0.3, so we buy.'):
            res = self.session.submit(self.key, 'buy', 5, bad, 'normal')
            self.assertFalse(res['ok'], bad)
            self.assertIn('plain words', res['problem'])

    def test_plain_reason_is_accepted(self):
        # LANGUAGE DATA: the prompt makes the agent write reasons in Ukrainian; the delivery-claim filter must pass this
        res = self.session.submit(self.key, 'buy', 5, 'Лишилось на 10 днів. Продається щодня, зараз сезон.', 'normal')
        self.assertTrue(res['ok'], res)

    def test_delivery_time_claims_are_rejected_because_the_owner_never_set_it(self):
        # LANGUAGE DATA: LEAD_CLAIM matches the Ukrainian wording of a delivery-time claim, so the inputs stay Ukrainian
        for bad in ('Лишилось на 10 днів, а поставка йде 14. Продається щодня.',
                    'Запасу вистачить менше ніж на час поставки, беремо зараз.',
                    'Закінчиться за 10 днів, а на доставку треба 14 днів.',
                    'Термін поставки два тижні, тому замовляємо.'):
            res = self.session.submit(self.key, 'buy', 5, bad, 'normal')
            self.assertFalse(res['ok'], bad)
            self.assertIn('delivery or shipping time', res['problem'])

    def test_promises_to_waiting_customers_are_not_mistaken_for_delivery_claims(self):
        # LANGUAGE DATA: Ukrainian wording that LEAD_CLAIM must not match
        res = self.session.submit(self.key, 'buy', 5, 'Закінчилось, а поставку вже обіцяно трьом покупцям, що чекають.', 'high')
        self.assertTrue(res['ok'], res)

    def test_shop_context_tells_the_agent_the_lead_time_is_an_assumption(self):
        self.assertIn('an assumption', self.session.shop_context()['lead_days_note'])

    def test_latin_brand_names_are_fine(self):
        # LANGUAGE DATA: Latin brand names inside a Ukrainian reason (the agent writes Ukrainian)
        res = self.session.submit(self.key, 'buy', 5, 'Osmocote і Stocker розбирають щотижня, лишилось на тиждень.', 'normal')
        self.assertTrue(res['ok'], res)

    def test_prompt_demands_short_plain_reasons_with_an_example(self):
        text = agent.SYSTEM_PROMPT
        self.assertIn('in plain words', text)
        self.assertIn('10 days left. It sells every day', text)
        self.assertIn('The owner did NOT specify a delivery lead time', text)
        self.assertEqual(agent.MAX_REASON, 400)
