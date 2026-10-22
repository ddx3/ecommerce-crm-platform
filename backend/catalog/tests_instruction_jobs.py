# -*- coding: utf-8 -*-
"""Queue for "Run through AI" for when the limit is exhausted
(catalog/instruction_jobs.py).

The model is not called: normalize_by_mode is patched and only the queue is checked —
that jobs get queued, that they finish on their own, and that they do NOT overwrite someone else's
edits.
"""
import json
from datetime import timedelta
from decimal import Decimal
from io import StringIO
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.utils import timezone

from catalog import instruction_agent as ia
from catalog import instruction_jobs as ij
from catalog import instruction_llm as il
from catalog.models import Bundle, BundleItem, InstructionJob, Product, ProductVariant
from core.limits import LimitExceeded
from core.llm import SubscriptionLimit

RESULT = {'html': '<p>Fertilizer 25 ml — done</p>', 'mentions': [], 'changes': [], 'warnings': []}


class QueueTestCase(TestCase):
    def setUp(self):
        p = Product.objects.create(name='Fertilizer')
        self.mega = ProductVariant.objects.create(product=p, name='Fertilizer, 25 ml', price=Decimal('10'), stock=5)
        self.bundle = Bundle.objects.create(name='Bundle', instruction_html='<p>old</p>')
        BundleItem.objects.create(bundle=self.bundle, product_variant=self.mega, quantity=1)
        self.items = [{'variant_id': self.mega.pk, 'qty': 1}]
        self.user = get_user_model().objects.create(email='q@example.com', is_staff=True, is_superuser=True)

    def job(self, **kw):
        return ij.enqueue(text=kw.pop('text', '<p>Fertilizer 25 ml</p>'), name='Bundle',
                          items=kw.pop('items', self.items), bundle_id=kw.pop('bundle_id', self.bundle.pk),
                          user=self.user)

    def run_queue(self, **kw):
        with mock.patch.object(ia, 'normalize_by_mode', **kw) as run:
            stats = ij.process(limit=5, sleep=lambda s: None)
        return stats, run


class EnqueueTests(QueueTestCase):
    def test_repeated_press_keeps_one_pending_job_per_bundle(self):
        a = ij.enqueue(text='<p>1</p>', name='B', items=self.items, bundle_id=self.bundle.pk, user=self.user)
        b = ij.enqueue(text='<p>2</p>', name='B', items=self.items, bundle_id=self.bundle.pk, user=self.user)
        self.assertEqual(a.pk, b.pk)
        self.assertEqual(InstructionJob.objects.count(), 1)
        self.assertEqual(InstructionJob.objects.get().text, '<p>2</p>')

    def test_unsaved_bundle_job_waits_for_link_and_only_the_owner_can_link(self):
        job = ij.enqueue(text='<p>1</p>', name='B', items=self.items, bundle_id=None, user=self.user)
        self.assertIsNone(job.bundle_id)
        other = get_user_model().objects.create(email='o@example.com', is_staff=True)
        ij.link(job.pk, self.bundle, other)
        job.refresh_from_db()
        self.assertIsNone(job.bundle_id)
        ij.link(job.pk, self.bundle, self.user)
        job.refresh_from_db()
        self.assertEqual(job.bundle_id, self.bundle.pk)

    def test_unlinked_jobs_are_never_processed_and_expire(self):
        job = ij.enqueue(text='<p>1</p>', name='B', items=self.items, bundle_id=None, user=self.user)
        stats, run = self.run_queue(return_value=dict(RESULT))
        run.assert_not_called()
        InstructionJob.objects.filter(pk=job.pk).update(created_at=timezone.now() - ij.WAIT - timedelta(hours=1))
        self.run_queue(return_value=dict(RESULT))
        job.refresh_from_db()
        self.assertEqual(job.status, InstructionJob.STALE)


@override_settings(ALLOWED_HOSTS=['testserver'])
class ButtonQueueTests(QueueTestCase):
    URL = '/admin/catalog/instruction-normalize/'

    def setUp(self):
        super().setUp()
        self.client.force_login(self.user)

    def post(self, **extra):
        body = {'name': 'B', 'text': '<p>Fertilizer 25 ml.</p>', 'items': self.items, 'bundle_id': self.bundle.pk}
        body.update(extra)
        return self.client.post(self.URL, data=json.dumps(body), content_type='application/json')

    def test_subscription_limit_queues_instead_of_failing(self):
        with override_settings(INSTRUCTION_MODE='agent'), \
                mock.patch.object(ia, 'normalize_agent', side_effect=SubscriptionLimit()):
            resp = self.post()
        self.assertEqual(resp.status_code, 202, resp.content)
        body = resp.json()
        self.assertTrue(body['queued'])
        self.assertTrue(body['linked'])
        self.assertIn('No need to press again', body['message'])
        job = InstructionJob.objects.get()
        self.assertEqual((job.bundle_id, job.status, job.created_by_id), (self.bundle.pk, 'pending', self.user.pk))
        self.assertEqual(job.items, self.items)

    def test_our_own_daily_budget_queues_too(self):
        with override_settings(INSTRUCTION_MODE='agent'), \
                mock.patch.object(ia, 'normalize_agent', side_effect=LimitExceeded('Daily limit')):
            resp = self.post()
        self.assertEqual(resp.status_code, 202)
        self.assertEqual(InstructionJob.objects.count(), 1)

    def test_unsaved_form_gets_a_job_id_to_send_back_with_the_save(self):
        with override_settings(INSTRUCTION_MODE='agent'), \
                mock.patch.object(ia, 'normalize_agent', side_effect=SubscriptionLimit()):
            resp = self.post(bundle_id=None)
        body = resp.json()
        self.assertEqual(resp.status_code, 202)
        self.assertFalse(body['linked'])
        self.assertIn('Save the bundle', body['message'])
        self.assertIsNone(InstructionJob.objects.get(pk=body['job_id']).bundle_id)

    def test_other_failures_are_still_errors_not_queued(self):
        with override_settings(INSTRUCTION_MODE='agent'), \
                mock.patch.object(ia, 'normalize_agent', side_effect=RuntimeError('Timeout 240s')):
            resp = self.post()
        self.assertEqual(resp.status_code, 500)
        self.assertEqual(InstructionJob.objects.count(), 0)

    def test_a_manual_success_cancels_the_old_pending_job(self):
        ij.enqueue(text='<p>1</p>', name='B', items=self.items, bundle_id=self.bundle.pk, user=self.user)
        with override_settings(INSTRUCTION_MODE='agent'), \
                mock.patch.object(ia, 'normalize_agent', return_value=dict(RESULT)):
            resp = self.post()
        self.assertTrue(resp.json()['ok'])
        self.assertEqual(InstructionJob.objects.get().status, InstructionJob.STALE)


class ProcessTests(QueueTestCase):
    def test_success_writes_text_markup_and_stamp_into_the_bundle(self):
        job = self.job()
        stats, run = self.run_queue(return_value=dict(RESULT))
        self.assertEqual(stats['done'], 1)
        run.assert_called_once()
        self.assertEqual(run.call_args.args[0].instruction_html, '<p>Fertilizer 25 ml</p>')   # text from the form, not from the DB
        job.refresh_from_db()
        self.bundle.refresh_from_db()
        self.assertEqual(job.status, InstructionJob.DONE)
        self.assertEqual(self.bundle.instruction_html, RESULT['html'])
        self.assertEqual(self.bundle.instruction_map.get('version'), 1)
        self.assertTrue(self.bundle.instruction_stamp)

    def test_saving_the_same_text_after_queueing_does_not_block_it(self):
        job = self.job(text='<p>Fertilizer   25 ml</p>')
        self.bundle.instruction_html = '<p>Fertilizer 25 ml</p>'      # same instruction, different markup
        self.bundle.save()
        stats, _ = self.run_queue(return_value=dict(RESULT))
        self.assertEqual(stats['done'], 1)

    def test_a_different_text_saved_after_queueing_is_never_overwritten(self):
        job = self.job()
        self.bundle.instruction_html = '<p>Someone rewrote the instruction</p>'
        self.bundle.save()
        stats, run = self.run_queue(return_value=dict(RESULT))
        run.assert_not_called()
        job.refresh_from_db()
        self.bundle.refresh_from_db()
        self.assertEqual(job.status, InstructionJob.STALE)
        self.assertIn('was changed', job.note)
        self.assertEqual(self.bundle.instruction_html, '<p>Someone rewrote the instruction</p>')

    def test_text_saved_while_the_model_was_working_is_not_overwritten_either(self):
        job = self.job()

        def model_then_manager_saves(*a, **kw):
            b = Bundle.objects.get(pk=self.bundle.pk)
            b.instruction_html = '<p>The manager saved their own first</p>'
            b.save()
            return dict(RESULT)

        stats, _ = self.run_queue(side_effect=model_then_manager_saves)
        job.refresh_from_db()
        self.bundle.refresh_from_db()
        self.assertEqual(job.status, InstructionJob.STALE)
        self.assertEqual(self.bundle.instruction_html, '<p>The manager saved their own first</p>')

    def test_unsaved_composition_waits_without_calling_the_model_then_expires(self):
        job = self.job(items=[{'variant_id': self.mega.pk, 'qty': 2}])     # 2 in the form, 1 saved
        stats, run = self.run_queue(return_value=dict(RESULT))
        run.assert_not_called()
        self.assertEqual(stats['waiting'], 1)
        job.refresh_from_db()
        self.assertEqual(job.status, InstructionJob.PENDING)
        # the manager saved the form with the bundle contents — now it matches and the queue gets
        # processed
        BundleItem.objects.filter(bundle=self.bundle).update(quantity=2)
        stats, run = self.run_queue(return_value=dict(RESULT))
        self.assertEqual(stats['done'], 1)

    def test_composition_never_saved_expires_after_a_few_days(self):
        job = self.job(items=[{'variant_id': self.mega.pk, 'qty': 2}])
        old = timezone.now() - ij.WAIT - timedelta(hours=1)
        InstructionJob.objects.filter(pk=job.pk).update(created_at=old)
        Bundle.objects.filter(pk=self.bundle.pk).update(instruction_updated_at=old - timedelta(days=1))
        self.run_queue(return_value=dict(RESULT))
        job.refresh_from_db()
        self.assertEqual(job.status, InstructionJob.STALE)
        self.assertIn('composition', job.note)

    def test_limit_stops_the_pass_and_leaves_the_job_waiting(self):
        job = self.job()
        stats, run = self.run_queue(side_effect=SubscriptionLimit())
        self.assertTrue(stats['stopped'])
        job.refresh_from_db()
        self.assertEqual((job.status, job.attempts), (InstructionJob.PENDING, 0))
        stats, run = self.run_queue(return_value=dict(RESULT))      # the limit has recovered — gets processed on its own
        self.assertEqual(stats['done'], 1)

    def test_own_budget_limit_also_just_waits(self):
        job = self.job()
        stats, _ = self.run_queue(side_effect=LimitExceeded('Daily AI token limit'))
        job.refresh_from_db()
        self.assertTrue(stats['stopped'])
        self.assertEqual(job.status, InstructionJob.PENDING)

    def test_validator_rejection_fails_the_job_with_the_reason(self):
        job = self.job()
        stats, _ = self.run_queue(side_effect=il.Rejected('the model lost the 10 g dose'))
        job.refresh_from_db()
        self.bundle.refresh_from_db()
        self.assertEqual(job.status, InstructionJob.FAILED)
        self.assertIn('10 g', job.note)
        self.assertEqual(self.bundle.instruction_html, '<p>old</p>')

    def test_ordinary_failures_retry_then_give_up(self):
        job = self.job()
        for _ in range(ij.MAX_ATTEMPTS):
            self.run_queue(side_effect=RuntimeError('network'))
            job.refresh_from_db()
        self.assertEqual(job.status, InstructionJob.FAILED)
        self.assertEqual(job.attempts, ij.MAX_ATTEMPTS)

    def test_one_pass_makes_at_most_limit_model_calls(self):
        for n in range(3):
            b = Bundle.objects.create(name=f'B{n}', instruction_html='<p>x</p>')
            BundleItem.objects.create(bundle=b, product_variant=self.mega, quantity=1)
            self.job(bundle_id=b.pk)
        with mock.patch.object(ia, 'normalize_by_mode', return_value=dict(RESULT)) as run:
            stats = ij.process(limit=2, sleep=lambda s: None)
        self.assertEqual(run.call_count, 2)
        self.assertEqual(InstructionJob.objects.filter(status=InstructionJob.PENDING).count(), 1)


class UiStateTests(QueueTestCase):
    def test_pending_is_shown_every_time_result_only_once(self):
        job = self.job()
        self.assertEqual(ij.ui_state(self.bundle.pk)['tone'], 'wait')
        self.assertEqual(ij.ui_state(self.bundle.pk)['tone'], 'wait')
        self.run_queue(return_value=dict(RESULT))
        self.assertEqual(ij.ui_state(self.bundle.pk)['tone'], 'ok')
        self.assertIsNone(ij.ui_state(self.bundle.pk))

    def test_failure_is_explained_once(self):
        self.job()
        self.run_queue(side_effect=il.Rejected('invented dose'))
        state = ij.ui_state(self.bundle.pk)
        self.assertEqual(state['tone'], 'warn')
        self.assertIn('invented dose', state['text'])
        self.assertIsNone(ij.ui_state(self.bundle.pk))

    def test_nothing_queued_nothing_shown(self):
        self.assertIsNone(ij.ui_state(self.bundle.pk))


class CommandTests(QueueTestCase):
    def test_scheduler_command_runs_the_queue(self):
        job = self.job()
        with mock.patch.object(ia, 'normalize_by_mode', return_value=dict(RESULT)):
            call_command('resolve_pending_instructions', stdout=StringIO(), pace=0.001)
        job.refresh_from_db()
        self.assertEqual(job.status, InstructionJob.DONE)

    def test_limit_in_the_queue_skips_the_rest_of_the_run(self):
        self.job()
        out = StringIO()
        with mock.patch.object(ia, 'normalize_by_mode', side_effect=SubscriptionLimit()) as run:
            call_command('resolve_pending_instructions', stdout=out, pace=0.001)
        self.assertEqual(run.call_count, 1)
        self.assertIn('the AI limit has not recovered yet', out.getvalue())
