"""Purchasing agent: validation of model responses, cards, integration into the daily run, "Ordered"
tracking."""
import asyncio
import json
from datetime import timedelta
from decimal import Decimal
from unittest import mock

from django.core.cache import cache
from django.test import TestCase, override_settings

from analyst import actions, advice, agent, purchasing as P, runner
from analyst.models import ActionRun, AnalystRun, Finding, PlannedPurchase
from analyst.tests_purchasing import NOW, _order, _variant

OWNER = 'boss@example.com'


def _waiting_item(stock=0, subs=2):
    """An item that has run out and has people waiting for it is a typical candidate."""
    from catalog.models import StockNotification
    v = _variant(stock=stock)
    for n in range(subs):
        StockNotification.objects.create(email=f'w{v.pk}-{n}@example.com', variant=v)
    return v


def _session(now=NOW):
    ctx, items = P.build(now)
    return agent.Session(ctx, items), items


@override_settings(ANALYST_LEAD_DAYS=14, ANALYST_COVER_DAYS=45, ANALYST_MIN_REVENUE=0, ANALYST_MIN_SALES_MONTHS=0)
class SessionTests(TestCase):
    def setUp(self):
        self.v = _waiting_item(subs=3)
        self.key = f'var:{self.v.pk}'

    def test_candidates_and_context_are_readable_and_have_no_personal_data(self):
        session, _ = _session()
        listing = session.list_candidates(5)
        self.assertEqual(listing['candidates'][0]['key'], self.key)
        self.assertEqual(listing['candidates'][0]['waiting_subs'], 3)
        blob = json.dumps(listing, ensure_ascii=False)
        self.assertNotIn('@example.com', blob)          # subscriber addresses are not passed to the agent
        self.assertEqual(session.shop_context()['horizon_days'], 59)

    def test_unknown_key_and_bad_arguments_are_rejected_with_a_reason(self):
        session, _ = _session()
        self.assertFalse(session.submit('var:999999', 'buy', 5, 'a reason that is long enough', 'high')['ok'])
        bad = session.submit(self.key, 'order', 5, 'short', 'urgent')
        self.assertFalse(bad['ok'])
        for fragment in ('action', 'urgency', 'reason'):
            self.assertIn(fragment, bad['problem'])
        self.assertFalse(session.submit(self.key, 'buy', 0, 'a reason that is long enough', 'normal')['ok'])
        self.assertFalse(session.submit(self.key, 'buy', 'abc', 'a reason that is long enough', 'normal')['ok'])
        self.assertEqual(session.recs, {})

    def test_quantity_is_capped_at_one_and_a_half_times_the_calculated_need(self):
        from datetime import datetime, timezone as dt_tz
        from analyst.tests_purchasing import _imported_bounds
        seasonal = _variant(stock=0)
        _imported_bounds()
        for year in (2024, 2025):
            _order(seasonal, 30, datetime(year, 11, 5, tzinfo=dt_tz.utc), imported=True)
        session, items = _session()
        key = f'var:{seasonal.pk}'
        item = next(i for i in items if i.key == key)
        self.assertEqual(item.recommended, 30.0)
        result = session.submit(key, 'buy', 300, 'The season fits, so we buy some extra.', 'high')
        self.assertTrue(result['ok'])
        self.assertIn('warning', result)
        self.assertEqual(session.recs[key]['qty'], 45.0)
        self.assertIn('capped', session.recs[key]['cap_note'])

    def test_waiting_only_item_has_a_small_cap_not_zero(self):
        session, items = _session()
        item = next(i for i in items if i.key == self.key)
        self.assertEqual(item.recommended, 0.0)                      # no sales, only 3 waiting
        result = session.submit(self.key, 'buy', 50, 'Three are waiting, we buy the minimum.', 'normal')
        self.assertTrue(result['ok'])
        self.assertEqual(session.recs[self.key]['qty'], 3.0)

    def test_wait_has_no_quantity_and_resubmitting_replaces_the_decision(self):
        session, _ = _session()
        session.submit(self.key, 'buy', 3, 'Buyers are waiting, the season fits.', 'high')
        session.submit(self.key, 'wait', None, 'The season is over, come back in January.', 'low')
        self.assertEqual(len(session.recs), 1)
        self.assertEqual((session.recs[self.key]['action'], session.recs[self.key]['qty']), ('wait', 0.0))

    def test_cannot_buy_a_dead_position(self):
        dead = _variant(stock=0)
        session, items = _session()
        self.assertEqual(next(i for i in items if i.variant_ids == [dead.pk]).urgency, 'dead')
        result = session.submit(f'var:{dead.pk}', 'buy', 5, 'I want to buy for no reason at all.', 'normal')
        self.assertFalse(result['ok'])

    def test_recommendation_count_is_limited(self):
        for _ in range(agent.MAX_RECOMMENDATIONS + 2):
            _waiting_item(subs=1)
        session, items = _session()
        results = [session.submit(i.key, 'buy', 1, 'Buyers are waiting, the season fits.', 'normal') for i in items
                   if i.urgency == 'out']
        self.assertEqual(sum(r['ok'] for r in results), agent.MAX_RECOMMENDATIONS)
        self.assertFalse(results[-1]['ok'])

    def test_finish_requires_a_summary(self):
        session, _ = _session()
        self.assertFalse(session.finish('')['ok'])
        self.assertTrue(session.finish('The season is declining, only items with waiting buyers are urgent.')['ok'])
        self.assertIn('declining', session.digest)

    def test_item_detail_includes_monthly_sales(self):
        _order(self.v, 1, NOW - timedelta(days=3))
        session, _ = _session()
        detail = session.item_detail(self.key)
        self.assertIn('monthly_sales', detail)
        self.assertFalse(session.item_detail('var:0')['ok'])


class ToolWiringTests(TestCase):
    def test_tools_exist_and_return_json_text(self):
        v = _waiting_item()
        session, _ = _session()
        tools = {t.name: t for t in agent.build_tools(session)}
        self.assertEqual(set(tools), set(agent.TOOL_NAMES))
        out = asyncio.run(tools['list_candidates'].handler({'limit': 3}))
        payload = json.loads(out['content'][0]['text'])
        self.assertEqual(payload['candidates'][0]['key'], f'var:{v.pk}')

    def test_a_failing_tool_reports_an_error_instead_of_crashing_the_run(self):
        session, _ = _session()
        tools = {t.name: t for t in agent.build_tools(session)}
        with mock.patch.object(session, 'shop_context', side_effect=RuntimeError('boom')):
            out = asyncio.run(tools['shop_context'].handler({}))
        self.assertTrue(out.get('is_error'))
        self.assertIn('boom', out['content'][0]['text'])

    def test_options_forbid_builtin_tools(self):
        session, _ = _session()
        opts = agent._options(session, api_key=None, model='sonnet')
        self.assertEqual(opts.tools, [])
        self.assertEqual(opts.permission_mode, 'dontAsk')
        self.assertTrue(all(t.startswith('mcp__analyst__') for t in opts.allowed_tools))
        # subscription, not the paid API: the key is explicitly empty (not merely absent: the SDK
        # mixes in os.environ).
        # Condition without values in the message: on failure the output must not print the
        # environment with secrets.
        self.assertTrue(opts.env.get('ANTHROPIC_API_KEY') == '', 'ANTHROPIC_API_KEY must be an empty string')


@override_settings(ANALYST_LEAD_DAYS=14, ANALYST_COVER_DAYS=45, ANALYST_MIN_REVENUE=0, ANALYST_MIN_SALES_MONTHS=0)
class AgentRunTests(TestCase):
    def setUp(self):
        cache.clear()
        self.v = _waiting_item(subs=2)

    def _fake_drive(self, actions_fn, tokens=4321):
        async def fake(session, options, prompt, timeout, stats=None):
            actions_fn(session)
            stats = stats if stats is not None else {}
            stats.update({'turns': 4, 'tool_calls': 3, 'tokens': tokens, 'subtype': 'success'})
            return stats
        return mock.patch.object(agent, '_drive', fake)

    def test_run_returns_the_session_and_records_tokens_in_its_own_budget(self):
        ctx, items = P.build(NOW)

        def script(session):
            session.submit(f'var:{self.v.pk}', 'buy', 2, 'Two are waiting, the season fits, so we buy.', 'high')
            session.finish('One urgent item.')
        with self._fake_drive(script):
            session, stats = agent.run(ctx, items)
        self.assertEqual(list(session.recs), [f'var:{self.v.pk}'])
        self.assertEqual(cache.get('limit:analyst:llm:tokens:day'), 4321)
        self.assertIsNone(cache.get('limit:llm:tokens:day'), 'the shared budget of the CRM buttons is not touched')

    def test_run_without_any_answer_is_an_error(self):
        ctx, items = P.build(NOW)
        with self._fake_drive(lambda s: None):
            with self.assertRaises(RuntimeError):
                agent.run(ctx, items)

    @override_settings(ANALYST_LLM_TOKENS_PER_DAY=100)
    def test_daily_token_budget_stops_the_agent(self):
        from core.limits import LimitExceeded
        cache.set('limit:analyst:llm:tokens:day', 500, 3600)
        ctx, items = P.build(NOW)
        with self._fake_drive(lambda s: None):
            with self.assertRaises(LimitExceeded):
                agent.run(ctx, items)


@override_settings(ANALYST_LEAD_DAYS=14, ANALYST_COVER_DAYS=45, ANALYST_MIN_REVENUE=0, ANALYST_MIN_SALES_MONTHS=0)
class AdviceTests(TestCase):
    def setUp(self):
        cache.clear()
        self.v = _waiting_item(subs=1)

    def test_rule_cards_work_without_the_agent(self):
        result = advice.generate(NOW, use_agent=False)
        self.assertEqual(result.source, 'rules')
        card = result.items[0]
        self.assertEqual(card.check, advice.CHECK_ADVICE)
        self.assertEqual(card.key, f'purchase:var:{self.v.pk}')
        self.assertTrue(card.title.startswith('Order'))
        self.assertIn('1 buyer is waiting', card.body)              # correct singular noun and verb
        self.assertEqual(card.data['action'], 'buy')
        self.assertEqual(card.data['source'], 'rules')
        self.assertEqual(card.severity, Finding.WARN)         # waiting, but no advertising and no promised delivery

    def test_rule_card_turns_red_only_when_stock_ends_before_the_delivery(self):
        fast = _variant(stock=27)
        for days_ago in range(1, 13):
            _order(fast, 2, NOW - timedelta(days=days_ago))        # 24 pcs in 12 days, 3 left: about 7 days' worth
        cards = {c.key: c for c in advice.generate(NOW, use_agent=False).items}
        self.assertEqual(cards[f'purchase:var:{fast.pk}'].severity, Finding.CRITICAL)
        self.assertEqual(cards[f'purchase:var:{self.v.pk}'].severity, Finding.WARN)   # only waiting, stock is not running out
        self.assertNotIn('advert', ' '.join(c.body.lower() for c in cards.values()))

    def test_plural_forms(self):
        f = ("buyer", "buyers")
        self.assertEqual([advice.plural(n, f) for n in (1, 2, 5, 11, 12, 21, 22, 25, 111)],
                         ['buyer', 'buyers', 'buyers', 'buyers', 'buyers', 'buyers', 'buyers', 'buyers', 'buyers'])

    def test_agent_cards_show_only_what_to_buy_now_without_wait_cards_or_digest(self):
        other = _waiting_item(subs=1)            # the rules would have proposed both, the agent picked one

        def fake_run(ctx, items):
            session = agent.Session(ctx, items)
            session.submit(f'var:{self.v.pk}', 'buy', 2, 'The season fits, people are waiting, so we buy.', 'high')
            session.submit(f'var:{other.pk}', 'wait', None, 'The season is over, come back in January.', 'low')
            session.finish('We take one item and postpone the other until January.')
            return session, {'tokens': 1}
        with mock.patch.object(agent, 'run', fake_run):
            result = advice.generate(NOW, use_agent=True)
        self.assertEqual(result.source, 'agent')
        by_key = {c.key: c for c in result.items}
        self.assertEqual(by_key[f'purchase:var:{self.v.pk}'].data['action'], 'buy')
        self.assertEqual(by_key[f'purchase:var:{self.v.pk}'].data['source'], 'agent')
        self.assertNotIn(f'purchase:var:{other.pk}', by_key, '"do not order yet" is not shown to the owner')
        self.assertFalse(any(c.check == advice.CHECK_DIGEST for c in result.items), 'the daily summary is not shown')
        self.assertEqual(len(result.items), 1)
        # facts on the card come from the calculation, not from the model's text
        self.assertIn('Out of stock', by_key[f'purchase:var:{self.v.pk}'].body)

    def test_agent_failure_falls_back_to_rules_and_reports_it(self):
        with mock.patch.object(agent, 'run', side_effect=RuntimeError('limit')):
            result = advice.generate(NOW, use_agent=True)
        self.assertEqual(result.source, 'rules')
        self.assertIn('limit', result.error)
        self.assertTrue(result.items)


@override_settings(ANALYST_LEAD_DAYS=14, ANALYST_COVER_DAYS=45, ANALYST_MIN_REVENUE=0, ANALYST_MIN_SALES_MONTHS=0, ANALYST_AGENT_ENABLED=False)
class DailyRunIntegrationTests(TestCase):
    def setUp(self):
        cache.clear()

    def _run(self, **kw):
        with mock.patch('analyst.runner.collect', return_value=runner.Outcome()), \
             mock.patch('analyst.snapshots.take_stock_snapshot', return_value=0):
            return runner.run_daily(force=True, **kw)

    def test_purchase_cards_are_created_and_closed_when_the_need_disappears(self):
        v = _waiting_item(subs=1)
        run = self._run()
        self.assertEqual(run.status, AnalystRun.OK, run.error)
        card = Finding.objects.get(key=f'purchase:var:{v.pk}')
        self.assertEqual((card.section, card.check_name, card.status), (Finding.SECTION_PURCHASE, 'purchase_advice', Finding.NEW))
        # the product arrived — the card closes itself
        type(v).objects.filter(pk=v.pk).update(stock=50)
        self._run()
        card.refresh_from_db()
        self.assertIsNotNone(card.resolved_at)

    def test_failed_advice_keeps_old_cards_and_reports_the_problem(self):
        v = _waiting_item(subs=1)
        self._run()
        with mock.patch('analyst.advice.generate', side_effect=RuntimeError('db')):
            run = self._run()
        self.assertEqual(run.status, AnalystRun.OK)
        card = Finding.objects.get(key=f'purchase:var:{v.pk}')
        self.assertIsNone(card.resolved_at, 'a failure of the advice does not close the old cards')
        self.assertTrue(Finding.objects.filter(key='advice:failed', resolved_at__isnull=True).exists())

    def test_agent_failure_is_reported_but_rule_cards_remain(self):
        v = _waiting_item(subs=1)
        with mock.patch.object(agent, 'run', side_effect=RuntimeError('subscription limit')):
            self._run(use_agent=True)
        self.assertTrue(Finding.objects.filter(key=f'purchase:var:{v.pk}', resolved_at__isnull=True).exists())
        self.assertTrue(Finding.objects.filter(key='advice:agent_failed', resolved_at__isnull=True).exists())


@override_settings(ANALYST_OWNER_EMAIL=OWNER, ANALYST_LEAD_DAYS=14, ANALYST_COVER_DAYS=45, ANALYST_MIN_REVENUE=0, ANALYST_MIN_SALES_MONTHS=0)
class TickMeansOrderedTests(TestCase):
    """✓ on an "Order …" card records a purchase; there is no separate "Ordered"
button."""

    def setUp(self):
        cache.clear()
        self.v = _waiting_item(subs=2)
        card = next(i for i in advice.generate(NOW, use_agent=False).items if i.key == f'purchase:var:{self.v.pk}')
        self.finding = Finding.objects.create(
            key=card.key, check_name=card.check, section=card.section, severity=card.severity,
            title=card.title, body=card.body, data=card.data)
        from django.contrib.auth import get_user_model
        self.user = get_user_model().objects.create_user(email=OWNER, password='x', is_staff=True, is_superuser=True)
        self.client.force_login(self.user)

    def _act(self, action, **kw):
        return self.client.post('/admin/analyst/action/', data={'id': self.finding.pk, 'action': action, **kw},
                                content_type='application/json')

    def test_tick_records_the_purchase_and_the_next_calculation_subtracts_it(self):
        before = next(i for i in P.build(NOW)[1] if i.key == f'var:{self.v.pk}')
        self.assertEqual(self._act('done').status_code, 200)
        po = PlannedPurchase.objects.get()
        self.assertEqual((po.key, po.status, po.finding_id), (f'var:{self.v.pk}', PlannedPurchase.OPEN, self.finding.pk))
        self.assertEqual(po.created_by, OWNER)
        self.assertIsNone(po.expected_at)                       # we do not invent the arrival date: the delivery time is unknown
        after = next(i for i in P.build(NOW)[1] if i.key == f'var:{self.v.pk}')
        self.assertEqual(after.open_po, float(po.quantity))
        self.assertLessEqual(after.need, before.need)

    def test_ticking_twice_does_not_duplicate_and_untick_cancels(self):
        self._act('done')
        self._act('done')
        self.assertEqual(PlannedPurchase.objects.count(), 1)
        self._act('reopen')
        self.assertEqual(PlannedPurchase.objects.get().status, PlannedPurchase.CANCELLED)

    def test_untick_does_not_touch_a_purchase_entered_by_hand(self):
        PlannedPurchase.objects.create(key=f'var:{self.v.pk}', label='by hand', quantity=1, unit='pcs',
                                       finding=self.finding, note='I ordered it myself')
        self._act('reopen')
        self.assertEqual(PlannedPurchase.objects.get().status, PlannedPurchase.OPEN)

    def test_wait_card_is_not_recorded_as_an_order(self):
        Finding.objects.filter(pk=self.finding.pk).update(data=dict(self.finding.data, action='wait', qty=0))
        self._act('done')
        self.assertEqual(PlannedPurchase.objects.count(), 0)

    def test_bulk_tick_covers_every_selected_purchase_card(self):
        v2 = _waiting_item(subs=1)
        card2 = next(i for i in advice.generate(NOW, use_agent=False).items if i.key == f'purchase:var:{v2.pk}')
        f2 = Finding.objects.create(key=card2.key, check_name=card2.check, section=card2.section,
                                    severity=card2.severity, title=card2.title, data=card2.data)
        self.client.post('/admin/analyst/action/', data={'ids': [self.finding.pk, f2.pk], 'action': 'done'},
                         content_type='application/json')
        self.assertEqual(PlannedPurchase.objects.filter(status=PlannedPurchase.OPEN).count(), 2)

    def test_ordered_item_with_waiting_people_does_not_come_back_as_a_card(self):
        self._act('done')
        cards = advice.generate(NOW, use_agent=False).items
        self.assertNotIn(f'purchase:var:{self.v.pk}', [c.key for c in cards])

    def test_page_has_no_order_button_on_purchase_cards(self):
        page = self.client.get('/admin/analyst/').content.decode()
        self.assertNotIn('data-fix="purchase_ordered"', page)
        self.assertNotIn('id="ar-fix-all"', page)
