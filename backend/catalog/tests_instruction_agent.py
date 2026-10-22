# -*- coding: utf-8 -*-
"""Instruction markup agent (catalog/instruction_agent.py).

The model is not called here: the session logic (tools) is tested directly, and the
`normalize_agent` run uses a scripted 'agent' that simply calls the tools.
"""
from decimal import Decimal
from unittest import mock

from django.test import SimpleTestCase, TestCase, override_settings

from catalog import instruction_agent as ia
from catalog import instruction_llm as il
from catalog.instruction_resolve import Comp
from catalog.models import Bundle, Product, ProductVariant

# LANGUAGE DATA: the dose parser (instruction_llm._DOSE_RE), the pack-size parser (Comp.pack) and the
# contents-list label (_COMP_HEAD_RE) only understand Ukrainian unit abbreviations (мл = ml, г = g,
# л = l) and the Ukrainian label «СКЛАД КОМПЛЕКТУ» (bundle contents), so the dose formulas, the pack
# sizes and that label stay in Ukrainian below ("води" = of water); everything else is English.
SRC = (
    '<p>BUNDLE «TEST»</p>'
    '<h3>Treatment No. 1</h3>'
    '<p>Growfast 25 мл + Leafguard 3 г = 10 л води</p>'
    '<p>Treat in the morning or in the evening, in dry windless weather, when there is no dew on the leaves, '
    'no sooner than two weeks before harvest and not in direct sun.</p>'
    '<p>СКЛАД КОМПЛЕКТУ:</p>'
    '<p>Growfast — 25 мл</p>'
    '<p>Leafguard — 3 г</p>'
)


def _variant(product_name, variant_name):
    p = Product.objects.create(name=product_name)
    return ProductVariant.objects.create(product=p, name=variant_name, price=Decimal('10'), stock=5)


class SplitAndRulesTests(SimpleTestCase):
    def test_split_blocks_top_level_only_and_skips_empty(self):
        blocks = ia.split_blocks('<h3>A</h3>\n<p>B</p><p> </p><p><br></p>loose text<ul><li>1</li><li>2</li></ul>')
        self.assertEqual(blocks, ['<h3>A</h3>', '<p>B</p>', '<p>loose text</p>', '<ul><li>1</li><li>2</li></ul>'])

    def test_rules_come_from_the_script_prompt_without_its_json_format(self):
        rules = ia._rules()
        for title in ia._RULE_SECTIONS:
            self.assertIn(title, rules)
        self.assertNotIn('RESPONSE FORMAT', rules)
        self.assertNotIn('{{', rules)
        self.assertLess(len(rules), 14000)     # goes into the first message, not the system prompt (Windows cmdline limit)


class SessionTestCase(TestCase):
    def setUp(self):
        self.mega = _variant('Growfast', 'Growfast, 25 мл')
        self.ardis = _variant('Leafguard', 'Leafguard, 3 г')
        self.comps = [Comp(self.mega, 1), Comp(self.ardis, 1)]
        self.s = ia.Session('Bundle «Test»', SRC, self.comps)

    def _all_ok(self):
        """Correct pass: the title and the composition list are dropped, the rest stays as is, markup, finish."""
        self.s.remove([{'i': 0, 'reason': 'sheet_title'}, *({'i': i, 'reason': 'composition_list'} for i in (4, 5, 6))])
        self.s.keep([1, 2, 3])
        self.s.mark([{'surface': 'Growfast', 'product': self.mega.product_id, 'kind': 'component', 'doses': ['25 мл']},
                     {'surface': 'Leafguard', 'product': self.ardis.product_id, 'kind': 'component', 'doses': ['3 г']}])
        return self.s.finish()


class WriteBlockTests(SessionTestCase):
    def test_ok_and_progress(self):
        r = self.s.write([{'i': 1, 'html': '<h3>Treatment No. 1 — September</h3>', 'change': 'clarified'}])
        self.assertTrue(r['results'][0]['ok'])
        self.assertEqual(r['processed'], 1)
        self.assertEqual(self.s.changes, ['clarified'])

    def test_invented_dose_is_rejected_and_not_stored(self):
        r = self.s.write([{'i': 2, 'html': '<p>Growfast 250 мл + Leafguard 3 г = 10 л води</p>'}])
        self.assertFalse(r['results'][0]['ok'])
        self.assertIn('250 мл', r['results'][0]['problem'])
        self.assertNotIn(2, self.s.out)

    def test_forbidden_tags_attributes_and_empty_result(self):
        r = self.s.write([{'i': 1, 'html': '<h2>Treatment No. 1</h2>'}, {'i': 1, 'html': '<p class="x">Treatment No. 1</p>'},
                          {'i': 1, 'html': '<p> </p>'}, {'i': 99, 'html': '<p>x</p>'}])
        self.assertEqual([x['ok'] for x in r['results']], [False] * 4)
        self.assertIn('h2', r['results'][0]['problem'])
        self.assertIn('attributes', r['results'][1]['problem'])
        self.assertIn('remove_blocks', r['results'][2]['problem'])

    def test_unit_fix_to_pack_and_merged_range_are_allowed_like_in_the_script(self):
        r = self.s.write([{'i': 2, 'html': '<p>Growfast 25 г + Leafguard 3 г = 10 л води</p>'}])     # 25 ml → 25 g? the packaging is «25 мл» (25 ml)
        # Growfast packaging «25 мл» (25 ml): «25 г» (25 g) is a different unit for the number, NOT a
        # match with the packaging → invented dose
        self.assertFalse(r['results'][0]['ok'])
        s2 = ia.Session('K', '<p>Growfast 25 г + Leafguard 3 г = 10 л води. Then 5 г -10 г later</p>', self.comps)
        r2 = s2.write([{'i': 0, 'html': '<p>Growfast 25 мл + Leafguard 3 г = 10 л води. Then 5–10 г later</p>'}])
        self.assertTrue(r2['results'][0]['ok'], r2)       # 25 g → 25 ml matches the packaging; «5 г -10 г» (5 g -10 g) → «5–10 г» (5–10 g)

    def test_lost_dose_is_only_a_warning_at_block_level(self):
        r = self.s.write([{'i': 2, 'html': '<p>Growfast + Leafguard 3 г = 10 л води</p>'}])
        self.assertTrue(r['results'][0]['ok'])
        self.assertIn('25 мл', r['results'][0]['note'])


class RemoveAndMarkTests(SessionTestCase):
    def test_remove_refuses_unique_doses_and_bad_reason(self):
        r = self.s.remove([{'i': 2, 'reason': 'empty'}, {'i': 1, 'reason': 'whatever'}, {'i': 1, 'reason': 'empty'}])
        self.assertEqual([x['ok'] for x in r['results']], [False, False, True])
        self.assertIn('doses that are nowhere else', r['results'][0]['problem'])

    def test_remove_allows_composition_list_whose_doses_live_in_the_program(self):
        r = self.s.remove([{'i': i, 'reason': 'composition_list'} for i in (4, 5, 6)])
        self.assertTrue(all(x['ok'] for x in r['results']), r)

    def test_mark_rules(self):
        other = _variant('Another', 'Another, 1 л')
        r = self.s.mark([
            {'surface': 'Growfast', 'product': self.mega.product_id, 'kind': 'component', 'doses': ['25 мл']},
            {'surface': 'Leafguard', 'product': other.product_id, 'kind': 'component'},        # not from the composition
            {'surface': 'Leafguard', 'product': None, 'kind': 'component'},                    # component without a product
            {'surface': 'Malachite', 'product': None, 'kind': 'external'},                   # no such line in the text
            {'surface': 'Leafguard', 'product': 99999999, 'kind': 'external'},                 # not in the catalog
            {'surface': 'Leafguard', 'product': other.product_id, 'kind': 'external'},         # ok: external, not from the composition
            {'surface': 'Leafguard', 'kind': 'wat'},
        ])
        self.assertEqual([x['ok'] for x in r['results']], [True, False, False, False, False, True, False])
        self.assertEqual({m['kind'] for m in self.s.mentions.values()}, {'component', 'external'})


class FinishTests(SessionTestCase):
    def test_pending_blocks_block_finish(self):
        r = self.s.finish()
        self.assertFalse(r['ok'])
        self.assertIn('not processed', r['problem'])

    def test_full_run_passes_and_reports_warnings(self):
        r = self._all_ok()
        self.assertTrue(r['ok'], r)
        self.assertEqual(self.s.result['html'].count('<h3>'), 1)
        self.assertNotIn('BUNDLE «TEST»', self.s.result['html'])
        self.assertNotIn('Growfast — 25 мл', self.s.result['html'])
        self.assertEqual(len(self.s.result['mentions']), 2)
        self.assertEqual(len([c for c in self.s.result['changes'] if c.startswith('removed')]), 4)

    def test_lost_program_dose_is_rejected_by_the_real_validator(self):
        self.s.remove([{'i': 0, 'reason': 'sheet_title'}, *({'i': i, 'reason': 'composition_list'} for i in (4, 5, 6))])
        self.s.keep([1, 3])
        # 25 ml disappeared from the treatment program although the same figure is in the contents
        # list
        # (packaging) — exactly this hole the validator used to forgive with a warning
        self.s.write([{'i': 2, 'html': '<p>Growfast + Leafguard 3 г = 10 л води</p>'}])
        r = self.s.finish()
        self.assertFalse(r['ok'])
        self.assertIn('lost doses', r['rejected'])
        self.assertIn('25 мл', r['rejected'])
        self.assertIsNone(self.s.result)

    def test_any_edit_after_successful_finish_invalidates_it(self):
        self.assertTrue(self._all_ok()['ok'])
        self.assertIsNotNone(self.s.result)
        self.s.write([{'i': 1, 'html': '<h3>Treatment No. 1 (September)</h3>'}])
        self.assertIsNone(self.s.result)

    def test_unmarked_component_is_reported_as_warning(self):
        self.s.remove([{'i': 0, 'reason': 'sheet_title'}, *({'i': i, 'reason': 'composition_list'} for i in (4, 5, 6))])
        self.s.keep([1, 2, 3])
        r = self.s.finish()
        self.assertTrue(r['ok'])
        self.assertTrue(any('was not found in the text' in w for w in r['remarks']))


class SpaceOutOperatorsTests(SimpleTestCase):
    """Mechanical splitting of '+'/'=' before the model: formulas — yes, the sign of a number — no."""

    def test_glued_formula_is_spaced_out(self):
        self.assertEqual(il._space_out_operators('<p>Product A 25 g+ Product B 20 ml= 10 ml</p>'),
                         '<p>Product A 25 g + Product B 20 ml = 10 ml</p>')
        self.assertEqual(il._space_out_operators('<p>Product A 3 ml+Product B 5 g=10 l</p>'),
                         '<p>Product A 3 ml + Product B 5 g = 10 l</p>')
        self.assertEqual(il._space_out_operators('<p>3 ml+5 ml</p>'), '<p>3 ml + 5 ml</p>')

    def test_sign_of_a_number_is_left_alone(self):
        for text in ('<p>Temperature <strong>+15…+25 °C</strong>.</p>', '<p>(+12°C) and above</p>',
                     '<p>from +12°C</p>', '<p>up to +25 °C</p>', '<p>NPK 15-9-12+2MgO</p>'):
            self.assertEqual(il._space_out_operators(text), text)

    def test_idempotent_and_tags_untouched(self):
        ok = '<p bis="a+b=c">Product A 2 ml + Product B 3 g = 8–10 l of water</p>'
        self.assertEqual(il._space_out_operators(ok), ok)
        glued = il._space_out_operators('<p>Product A 2 ml+Product B 3 g=8 l</p>')
        self.assertEqual(il._space_out_operators(glued), glued)


class ValidatorLossTests(SessionTestCase):
    """validate() is the filter shared by the script and the agent: a dose must not vanish from the
treatment program."""
    OK = ('<h3>Treatment No. 1</h3><p>Growfast 25 мл + Leafguard 3 г = 10 л води</p>'
          '<p>Treat in the morning or in the evening, in dry windless weather, when there is no dew on the leaves, '
          'no sooner than two weeks before harvest and not in direct sun.</p>')

    def test_removing_title_and_composition_list_is_fine(self):
        il.validate(SRC, self.OK, [], self.comps)

    def test_dose_equal_to_a_pack_size_cannot_vanish_from_the_program(self):
        lost = self.OK.replace('Growfast 25 мл', 'Growfast')
        with self.assertRaises(il.Rejected) as cm:
            il.validate(SRC, lost, [], self.comps)
        self.assertIn('25 мл', str(cm.exception))

    def test_invented_dose_is_still_rejected(self):
        with self.assertRaises(il.Rejected):
            il.validate(SRC, self.OK.replace('3 г', '30 г'), [], self.comps)


class NormalizeAgentTests(SessionTestCase):
    """normalize_agent glue: a scripted 'agent' instead of the model."""

    def _run(self, script, *, validate_effect=None):
        bundle = Bundle(name='Bundle «Test»', instruction_html=SRC)

        def fake_drive(session, options, prompt, timeout):
            script(session)
            return {'turns': 4, 'tool_calls': 6, 'tokens': 1234, 'subtype': 'success'}

        patches = [mock.patch.object(ia, '_options', return_value=None), mock.patch.object(ia, '_drive', fake_drive),
                   mock.patch('core.llm._run_sync', side_effect=lambda x: x), mock.patch('core.limits.enforce_llm'),
                   mock.patch('core.limits.record_llm_tokens')]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        if validate_effect is not None:
            p = mock.patch.object(il, 'validate', side_effect=validate_effect)
            p.start()
            self.addCleanup(p.stop)
        return ia.normalize_agent(bundle, self.comps)

    def test_returns_the_same_shape_as_the_script_plus_stats(self):
        def script(s):
            s.remove([{'i': 0, 'reason': 'sheet_title'}, *({'i': i, 'reason': 'composition_list'} for i in (4, 5, 6))])
            s.keep([1, 2, 3])
            s.mark([{'surface': 'Growfast', 'product': self.mega.product_id, 'kind': 'component'},
                    {'surface': 'Leafguard', 'product': self.ardis.product_id, 'kind': 'component'}])
            s.finish(['no doubts'])
        res = self._run(script)
        self.assertEqual(set(res), {'html', 'mentions', 'changes', 'warnings', 'stats'})
        self.assertIn('no doubts', res['warnings'])
        self.assertEqual(res['stats']['tokens'], 1234)
        self.assertNotIn('BUNDLE «TEST»', res['html'])

    def test_agent_that_never_finishes_is_rejected(self):
        with self.assertRaises(il.Rejected) as cm:
            self._run(lambda s: s.keep([1]))
        self.assertIn('did not finish', str(cm.exception))

    def test_agent_rejection_reason_is_passed_on(self):
        def script(s):
            s.keep([0, 1, 3]); s.remove([{'i': i, 'reason': 'composition_list'} for i in (4, 5, 6)])
            s.write([{'i': 2, 'html': '<p>Growfast + Leafguard 3 г = 10 л води</p>'}])
            s.finish()
        with self.assertRaises(il.Rejected) as cm:
            self._run(script)
        self.assertIn('lost doses', str(cm.exception))

    def test_result_is_validated_again_outside_the_agent(self):
        calls = []

        def validate(src, out, mentions, comps):
            calls.append(1)
            if len(calls) > 1:
                raise il.Rejected('the second check rejected')
            return []

        def script(s):
            s.keep(list(range(7))); s.finish()
        with self.assertRaises(il.Rejected) as cm:
            self._run(script, validate_effect=validate)
        self.assertEqual(len(calls), 2)
        self.assertIn('second', str(cm.exception))

    def test_empty_instruction_is_rejected_before_the_agent(self):
        with self.assertRaises(il.Rejected):
            ia.normalize_agent(Bundle(name='K', instruction_html='  '), self.comps)


class BackgroundModeTests(SessionTestCase):
    """Scheduler (resolve_pending_instructions) without a human: auto by default — script, with the
    agent
    only when the script is rejected; the mode comes from INSTRUCTION_BACKGROUND_MODE."""
    FAKE = {'html': '<p>script</p>', 'mentions': [], 'changes': [], 'warnings': []}
    AGENT = {'html': '<p>agent</p>', 'mentions': [], 'changes': [], 'warnings': []}

    def setUp(self):
        super().setUp()
        from catalog.models import BundleInstructionOverride, BundleItem
        self.Ov = BundleInstructionOverride
        self.bundle = Bundle.objects.create(name='Bundle', instruction_html='<p>text</p>')
        BundleItem.objects.create(bundle=self.bundle, product_variant=self.mega, quantity=1)
        self.ov = BundleInstructionOverride.objects.create(
            bundle=self.bundle, signature='s1', status=BundleInstructionOverride.PENDING,
            composition=[{'sku': c.sku, 'name': c.name, 'qty': c.quantity} for c in [Comp(self.mega, 1)]])

    def _run(self):
        from io import StringIO
        from django.core.management import call_command
        call_command('resolve_pending_instructions', stdout=StringIO())
        self.ov.refresh_from_db()

    def test_default_is_auto(self):
        from django.conf import settings
        self.assertEqual((settings.INSTRUCTION_MODE, settings.INSTRUCTION_BACKGROUND_MODE), ('agent', 'auto'))

    @override_settings(INSTRUCTION_BACKGROUND_MODE='auto')
    def test_auto_script_passes_agent_untouched(self):
        with mock.patch.object(il, 'normalize', return_value=dict(self.FAKE)), \
                mock.patch.object(ia, 'normalize_agent') as agent:
            self._run()
        self.assertEqual(self.ov.status, self.Ov.READY)
        agent.assert_not_called()

    @override_settings(INSTRUCTION_BACKGROUND_MODE='auto')
    def test_auto_rejected_script_is_finished_by_the_agent(self):
        with mock.patch.object(il, 'normalize', side_effect=il.Rejected('lost a dose')), \
                mock.patch.object(ia, 'normalize_agent', return_value=dict(self.AGENT)) as agent:
            self._run()
        agent.assert_called_once()
        self.assertEqual(self.ov.status, self.Ov.READY)
        self.assertEqual(self.ov.instruction_html, '<p>agent</p>')

    @override_settings(INSTRUCTION_BACKGROUND_MODE='auto')
    def test_auto_both_fail_row_stays_pending(self):
        with mock.patch.object(il, 'normalize', side_effect=il.Rejected('script')), \
                mock.patch.object(ia, 'normalize_agent', side_effect=il.Rejected('agent')):
            self._run()
        self.assertEqual(self.ov.status, self.Ov.PENDING)

    @override_settings(INSTRUCTION_BACKGROUND_MODE='auto')
    def test_auto_infrastructure_failure_does_not_start_the_agent(self):
        with mock.patch.object(il, 'normalize', side_effect=RuntimeError('rate limit')), \
                mock.patch.object(ia, 'normalize_agent') as agent:
            self._run()
        self.assertEqual(self.ov.status, self.Ov.PENDING)
        agent.assert_not_called()

    @override_settings(INSTRUCTION_BACKGROUND_MODE='agent')
    def test_agent_mode_skips_the_script(self):
        with mock.patch.object(il, 'normalize') as script, \
                mock.patch.object(ia, 'normalize_agent', return_value=dict(self.AGENT)):
            self._run()
        script.assert_not_called()
        self.assertEqual(self.ov.status, self.Ov.READY)


@override_settings(ALLOWED_HOSTS=['testserver'])
class ViewSwitchTests(SessionTestCase):
    URL = '/admin/catalog/instruction-normalize/'

    def setUp(self):
        super().setUp()
        from django.contrib.auth import get_user_model
        self.client.force_login(get_user_model().objects.create(email='ag@example.com', is_staff=True, is_superuser=True))

    def _post(self):
        import json
        return self.client.post(self.URL, data=json.dumps({
            'name': 'K', 'text': '<p>Growfast 25 мл.</p>', 'items': [{'variant_id': self.mega.pk}]}),
            content_type='application/json')

    FAKE = {'html': '<p>Growfast 25 мл.</p>', 'mentions': [], 'changes': [], 'warnings': []}

    def test_agent_mode_uses_only_the_agent(self):
        with override_settings(INSTRUCTION_MODE='agent'), \
                mock.patch.object(ia, 'normalize_agent', return_value=dict(self.FAKE)) as agent, \
                mock.patch.object(il, 'normalize') as script:
            resp = self._post()
        self.assertEqual(resp.status_code, 200, resp.content)
        agent.assert_called_once()
        script.assert_not_called()
        self.assertTrue(resp.json()['ok'])

    def test_script_mode_is_the_default_and_uses_only_the_script(self):
        for mode in ('script', 'something-unknown'):
            with override_settings(INSTRUCTION_MODE=mode), \
                    mock.patch.object(ia, 'normalize_agent') as agent, \
                    mock.patch.object(il, 'normalize', return_value=dict(self.FAKE)) as script:
                resp = self._post()
            self.assertEqual(resp.status_code, 200, resp.content)
            script.assert_called_once()
            agent.assert_not_called()

    def test_agent_rejection_is_a_normal_not_ok_result(self):
        with override_settings(INSTRUCTION_MODE='agent'), \
                mock.patch.object(ia, 'normalize_agent', side_effect=il.Rejected('the model lost doses')):
            resp = self._post()
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(resp.json()['ok'])
        self.assertIn('lost', resp.json()['rejected'])

    def test_auto_mode_keeps_the_cheap_script_when_it_passes(self):
        with override_settings(INSTRUCTION_MODE='auto'), \
                mock.patch.object(ia, 'normalize_agent') as agent, \
                mock.patch.object(il, 'normalize', return_value=dict(self.FAKE)):
            resp = self._post()
        self.assertTrue(resp.json()['ok'])
        agent.assert_not_called()

    def test_auto_mode_hands_a_rejected_script_result_to_the_agent(self):
        agent_res = dict(self.FAKE, html='<p>fixed by the agent</p>')
        with override_settings(INSTRUCTION_MODE='auto'), \
                mock.patch.object(il, 'normalize', side_effect=il.Rejected('the model lost doses: 10 г')) as script, \
                mock.patch.object(ia, 'normalize_agent', return_value=agent_res) as agent:
            resp = self._post()
        self.assertEqual(resp.status_code, 200, resp.content)
        script.assert_called_once()
        agent.assert_called_once()
        self.assertEqual(agent.call_args.kwargs['timeout'], ia.AUTO_TIMEOUT)
        self.assertEqual(agent.call_args.args[0].instruction_html, '<p>Growfast 25 мл.</p>')   # the original, not the script's result
        body = resp.json()
        self.assertTrue(body['ok'])
        self.assertEqual(body['html'], '<p>fixed by the agent</p>')

    def test_auto_mode_reports_the_agents_rejection_when_both_fail(self):
        with override_settings(INSTRUCTION_MODE='auto'), \
                mock.patch.object(il, 'normalize', side_effect=il.Rejected('script: lost')), \
                mock.patch.object(ia, 'normalize_agent', side_effect=il.Rejected('agent: failed')):
            resp = self._post()
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(resp.json()['ok'])
        self.assertIn('agent: failed', resp.json()['rejected'])

    def test_auto_mode_does_not_escalate_infrastructure_failures(self):
        with override_settings(INSTRUCTION_MODE='auto'), \
                mock.patch.object(il, 'normalize', side_effect=RuntimeError('Timeout 280s')), \
                mock.patch.object(ia, 'normalize_agent') as agent:
            resp = self._post()
        self.assertEqual(resp.status_code, 500)
        self.assertIn('Timeout', resp.json()['error'])
        agent.assert_not_called()
