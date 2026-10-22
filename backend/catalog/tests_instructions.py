# -*- coding: utf-8 -*-
"""Tests for the automatic LLM-resolve queue of bundle instructions.

Covers three pieces added with no existing tests:
  1. resolve_pending_instructions — the command that resolves the PENDING queue.
  2. _resync_instruction_after_swap — now sets a PENDING override in the two
     branches where it used to just leave the text to a human (ambiguous swap,
     unconfirmed dose).
  3. bundle_instruction_docx — downloading the .docx with a bundle's instruction.

The LLM is never actually called — only catalog.instruction_llm.normalize is
mocked as a whole (the normalization call itself is unit-tested elsewhere;
what matters here is the behavior around it: whether the result is applied,
whether it stays in the queue on failure).
"""
from decimal import Decimal
from io import StringIO
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase, override_settings

from catalog import instruction_llm as il
from catalog.instruction_resolve import Comp
from catalog.models import Bundle, BundleInstructionOverride, BundleItem, Product, ProductVariant


def _variant(product_name, variant_name, stock=10):
    p = Product.objects.create(name=product_name)
    return ProductVariant.objects.create(product=p, name=variant_name,
                                         price=Decimal('100'), stock=stock)


def _staff():
    return get_user_model().objects.create(email='m@example.com', is_staff=True, is_superuser=True)


@override_settings(INSTRUCTION_BACKGROUND_MODE='script')   # command logic; modes are in tests_instruction_agent
class ResolvePendingInstructionsCommandTests(TestCase):
    def setUp(self):
        self.variant = _variant('Fertilizer', 'Fertilizer, 10 ml')
        self.bundle = Bundle.objects.create(name='Set', instruction_html='<p>text</p>')
        BundleItem.objects.create(bundle=self.bundle, product_variant=self.variant, quantity=1)
        self.composition = [{'sku': c.sku, 'name': c.name, 'qty': c.quantity}
                            for c in [Comp(self.variant, 1)]]
        self.override = BundleInstructionOverride.objects.create(
            bundle=self.bundle, signature='sig1', status=BundleInstructionOverride.PENDING,
            composition=self.composition,
        )

    def _run(self, **opts):
        call_command('resolve_pending_instructions', stdout=StringIO(), **opts)

    def test_success_resolves_and_syncs_bundle_when_composition_matches(self):
        with mock.patch.object(il, 'normalize', return_value={
                'html': '<p>normalized</p>', 'mentions': [], 'changes': [], 'warnings': []}) as m:
            self._run()
        m.assert_called_once_with(self.bundle, None)

        self.override.refresh_from_db()
        self.assertEqual(self.override.status, BundleInstructionOverride.READY)
        self.assertTrue(self.override.auto_substituted)
        self.assertIsNone(self.override.resolved_by)
        self.assertIsNotNone(self.override.resolved_at)
        self.assertEqual(self.override.instruction_html, '<p>normalized</p>')
        self.assertEqual(self.override.base_stamp, self.bundle.instruction_stamp)

        self.bundle.refresh_from_db()
        self.assertEqual(self.bundle.instruction_html, '<p>normalized</p>')

    def test_success_does_not_touch_bundle_when_composition_differs(self):
        """Override for bundle CONTENTS that are not in the template right now (a one-off swap for a
        single order) — resolves itself, but we don't touch bundle.instruction_html:
        it's not a permanent template change."""
        other = _variant('Another', 'Another, 5 ml')
        self.override.composition = [{'sku': other.sku, 'name': other.name, 'qty': 1}]
        self.override.save(update_fields=['composition'])
        original_html = self.bundle.instruction_html

        with mock.patch.object(il, 'normalize', return_value={
                'html': '<p>normalized</p>', 'mentions': [], 'changes': [], 'warnings': []}):
            self._run()

        self.override.refresh_from_db()
        self.assertEqual(self.override.status, BundleInstructionOverride.READY)
        self.bundle.refresh_from_db()
        self.assertEqual(self.bundle.instruction_html, original_html)

    def test_rejected_stays_pending(self):
        with mock.patch.object(il, 'normalize', side_effect=il.Rejected('invented dose')):
            self._run()
        self.override.refresh_from_db()
        self.assertEqual(self.override.status, BundleInstructionOverride.PENDING)
        self.assertEqual(self.override.instruction_html, '')

    def test_generic_failure_stays_pending(self):
        """Rate limit / network failure — not Rejected, a plain exception. Also
        stays in the queue: retried on the command's next run."""
        with mock.patch.object(il, 'normalize', side_effect=RuntimeError('rate limit exceeded')):
            self._run()
        self.override.refresh_from_db()
        self.assertEqual(self.override.status, BundleInstructionOverride.PENDING)

    def test_dry_run_does_not_call_llm_or_change_anything(self):
        with mock.patch.object(il, 'normalize') as m:
            self._run(dry_run=True)
        m.assert_not_called()
        self.override.refresh_from_db()
        self.assertEqual(self.override.status, BundleInstructionOverride.PENDING)

    def test_limit_caps_batch_size(self):
        b2 = Bundle.objects.create(name='Set 2', instruction_html='<p>2</p>')
        BundleItem.objects.create(bundle=b2, product_variant=self.variant, quantity=1)
        ov2 = BundleInstructionOverride.objects.create(
            bundle=b2, signature='sig2', status=BundleInstructionOverride.PENDING,
            composition=self.composition,
        )
        with mock.patch.object(il, 'normalize', return_value={
                'html': '<p>ok</p>', 'mentions': [], 'changes': [], 'warnings': []}) as m:
            self._run(limit=1)
        self.assertEqual(m.call_count, 1)
        # Exactly one of the two got resolved, the other stayed in the queue.
        resolved = BundleInstructionOverride.objects.filter(status=BundleInstructionOverride.READY).count()
        pending = BundleInstructionOverride.objects.filter(status=BundleInstructionOverride.PENDING).count()
        self.assertEqual(resolved, 1)
        self.assertEqual(pending, 1)


class ResyncInstructionQueueingTests(TestCase):
    """_resync_instruction_after_swap: two branches where it used to just leave the text
    to a human must now set BundleInstructionOverride(PENDING)."""

    def test_ambiguous_swap_queues_override(self):
        troiset = _variant('Alpha', 'Alpha, 50 g')
        skor = _variant('Beta', 'Beta, 10 ml')
        falcon = _variant('Gamma', 'Gamma, 10 ml')

        html = '<p>Alpha 50 g per 10 l of water. Beta 10 ml per 10 l of water.</p>'
        bundle = Bundle.objects.create(name='Set', instruction_html=html)
        BundleItem.objects.create(bundle=bundle, product_variant=troiset, quantity=1)
        BundleItem.objects.create(bundle=bundle, product_variant=skor, quantity=1)

        comps = [Comp(troiset, 1), Comp(skor, 1)]
        mentions = [
            {'surface': 'Alpha', 'product': troiset.product_id, 'kind': 'component'},
            {'surface': 'Beta', 'product': skor.product_id, 'kind': 'component'},
        ]
        bundle.instruction_map = il.build_map(html, mentions, comps)
        bundle.save()

        # Simulate what BundleAdmin._save_bundle_items does: the contents were recreated
        # ENTIRELY as one new variant (2-for-1 — ambiguous).
        bundle.items.all().delete()
        BundleItem.objects.create(bundle=bundle, product_variant=falcon, quantity=1)

        from catalog.admin import _resync_instruction_after_swap
        note = _resync_instruction_after_swap(bundle)

        self.assertIsNotNone(note)
        ov = BundleInstructionOverride.objects.get(bundle=bundle)
        self.assertEqual(ov.status, BundleInstructionOverride.PENDING)
        # The text stays as it was — nothing to substitute automatically.
        bundle.refresh_from_db()
        self.assertEqual(bundle.instruction_html, html)

    def test_dose_uncertain_swap_queues_override(self):
        troiset = _variant('Alpha', 'Alpha, 50 г')
        falcon = _variant('Gamma', 'Gamma, 10 мл')

        # LANGUAGE DATA: "у кількості" (in the amount of) is a word that the strict dose-replacement regexp
        # doesn't match (only the loose "there is a number here" recognizer does), so the dose
        # stays unconfirmed: dose_ok=False.
        html = '<p>Застосовувати Alpha у кількості 50 г на 10 л води.</p>'
        bundle = Bundle.objects.create(name='Set', instruction_html=html)
        BundleItem.objects.create(bundle=bundle, product_variant=troiset, quantity=1)

        comps = [Comp(troiset, 1)]
        mentions = [{'surface': 'Alpha', 'product': troiset.product_id, 'kind': 'component'}]
        bundle.instruction_map = il.build_map(html, mentions, comps)
        bundle.save()

        bundle.items.all().delete()
        BundleItem.objects.create(bundle=bundle, product_variant=falcon, quantity=1)

        from catalog.admin import _resync_instruction_after_swap
        note = _resync_instruction_after_swap(bundle)

        self.assertIsNotNone(note)
        ov = BundleInstructionOverride.objects.get(bundle=bundle)
        self.assertEqual(ov.status, BundleInstructionOverride.PENDING)
        bundle.refresh_from_db()
        self.assertEqual(bundle.instruction_html, html)

    def test_unambiguous_swap_still_applies_directly_without_override(self):
        """Control: an unambiguous swap with a confirmed dose — behavior that already
        existed and must not regress into the queue."""
        # LANGUAGE DATA: the pack sizes and doses use the Ukrainian unit abbreviations (г, мл, л) that the dose code parses
        troiset = _variant('Alpha', 'Alpha, 50 г')
        falcon = _variant('Gamma', 'Gamma, 10 мл')

        html = '<p>Alpha, 50 г на 10 л води.</p>'
        bundle = Bundle.objects.create(name='Set', instruction_html=html)
        BundleItem.objects.create(bundle=bundle, product_variant=troiset, quantity=1)

        comps = [Comp(troiset, 1)]
        mentions = [{'surface': 'Alpha', 'product': troiset.product_id, 'kind': 'component'}]
        bundle.instruction_map = il.build_map(html, mentions, comps)
        bundle.save()

        bundle.items.all().delete()
        BundleItem.objects.create(bundle=bundle, product_variant=falcon, quantity=1)

        from catalog.admin import _resync_instruction_after_swap
        _resync_instruction_after_swap(bundle)

        self.assertFalse(BundleInstructionOverride.objects.filter(bundle=bundle).exists())
        bundle.refresh_from_db()
        self.assertIn('Gamma', bundle.instruction_html)


@override_settings(INSTRUCTION_MODE='script')   # this checks the request shape; the model is mocked, the agent is not started
class BundleInstructionNormalizeViewTests(TestCase):
    """The "Run through AI" button works BEFORE the bundle's first save: the name, text and
    contents
    come from the form, there is no bundle in the DB."""
    URL = '/admin/catalog/instruction-normalize/'

    def setUp(self):
        self.client.force_login(_staff())
        self.mega = _variant('Delta', 'Delta, 25 ml')
        self.ardis = _variant('Epsilon', 'Epsilon, 3 g')

    def _post(self, **payload):
        import json
        return self.client.post(self.URL, data=json.dumps(payload), content_type='application/json')

    def _model(self, html):
        return {'html': html, 'mentions': [{'surface': 'Delta', 'product': self.mega.product_id, 'kind': 'component'}],
                'changes': [], 'warnings': []}

    def test_works_without_saved_bundle_and_uses_form_composition(self):
        src = '<p>Delta 25 ml per 10 l of water.</p>'
        before = Bundle.objects.count()
        with mock.patch('core.llm.text_to_json', return_value=self._model(src)) as llm:
            resp = self._post(name='New Set', text=src, items=[
                {'variant_id': self.mega.pk, 'qty': 2}, {'variant_id': self.ardis.pk, 'qty': 1}])
        self.assertEqual(resp.status_code, 200, resp.content)
        body = resp.json()
        self.assertTrue(body['ok'])
        self.assertEqual(body['map']['components'], [self.mega.product_id, self.ardis.product_id])
        prompt = llm.call_args.args[0]
        self.assertIn('New Set', prompt)
        self.assertIn(f'id={self.mega.product_id} | Delta', prompt)
        self.assertIn('in the bundle: 2 pcs', prompt)
        self.assertIn(src, prompt)
        self.assertEqual(Bundle.objects.count(), before)            # nothing saved

    def test_empty_name_and_unknown_items_are_tolerated(self):
        src = '<p>Delta 25 ml.</p>'
        with mock.patch('core.llm.text_to_json', return_value=self._model(src)):
            resp = self._post(name='', text=src, items=[
                {'variant_id': self.mega.pk}, {'variant_id': 999999, 'qty': 1}, {'qty': 3}, 'garbage', {'variant_id': 'x'}])
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(resp.json()['map']['components'], [self.mega.product_id])

    def test_empty_text_is_rejected_before_the_model(self):
        with mock.patch('core.llm.text_to_json') as llm:
            resp = self._post(name='K', text='  ', items=[{'variant_id': self.mega.pk}])
        self.assertEqual(resp.status_code, 400)
        llm.assert_not_called()

    def test_changed_dose_is_rejected_not_applied(self):
        # LANGUAGE DATA: doses are written with the Ukrainian unit abbreviations (мл, л) that the dose check parses
        with mock.patch('core.llm.text_to_json', return_value=self._model('<p>Delta 250 мл на 10 л води.</p>')):
            resp = self._post(name='K', text='<p>Delta 25 мл на 10 л води.</p>', items=[{'variant_id': self.mega.pk}])
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(resp.json()['ok'])

    def test_get_and_anonymous_are_refused(self):
        self.assertEqual(self.client.get(self.URL).status_code, 405)
        self.client.logout()
        self.assertEqual(self._post(text='x').status_code, 302)


class BundleInstructionDocxViewTests(TestCase):
    def setUp(self):
        self.client.force_login(_staff())
        self.variant = _variant('Delta', 'Delta, 15 ml')
        self.bundle = Bundle.objects.create(
            name='Test Set',
            instruction_html='<p><strong>Description</strong> of use.</p>',
        )
        BundleItem.objects.create(bundle=self.bundle, product_variant=self.variant, quantity=1)

    def _url(self, bundle_id):
        return f'/admin/catalog/bundle/{bundle_id}/instruction-docx/'

    def test_download_success(self):
        resp = self.client.get(self._url(self.bundle.pk))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(
            resp['Content-Type'],
            'application/vnd.openxmlformats-officedocument.wordprocessingml.document')
        self.assertIn('attachment', resp['Content-Disposition'])
        self.assertTrue(resp.content[:2] == b'PK')  # zip signature of .docx

    def test_download_404_when_instruction_empty(self):
        self.bundle.instruction_html = ''
        self.bundle.save()
        resp = self.client.get(self._url(self.bundle.pk))
        self.assertEqual(resp.status_code, 404)

    def test_download_404_unknown_bundle(self):
        resp = self.client.get(self._url(999999))
        self.assertEqual(resp.status_code, 404)

    def test_download_requires_staff(self):
        self.client.logout()
        resp = self.client.get(self._url(self.bundle.pk))
        self.assertEqual(resp.status_code, 302)
