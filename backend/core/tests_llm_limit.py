# -*- coding: utf-8 -*-
"""Claude subscription limit: the CLI refuses with the text "You've hit your limit", not with an
exception.

We don't exhaust the real limit in tests: SDK messages are assembled by hand and fed into
the same read loops (`_query_text`, the instruction agent).
"""
import asyncio
import json
from datetime import datetime, timedelta, timezone as dt_tz
from unittest import mock

from django.test import SimpleTestCase, TestCase, override_settings

from claude_agent_sdk import AssistantMessage, RateLimitEvent, RateLimitInfo, ResultMessage
from core import llm
from core.llm import SubscriptionLimit, _LimitWatch


def _event(status='rejected', resets_at=None):
    return RateLimitEvent(rate_limit_info=RateLimitInfo(status=status, resets_at=resets_at,
                                                        rate_limit_type='five_hour'),
                          uuid='u', session_id='s')


def _assistant(error=None, text=''):
    return AssistantMessage(content=[], model='sonnet', error=error)


def _result(is_error=False, result='', status=None):
    return ResultMessage(subtype='success', duration_ms=1, duration_api_ms=1, is_error=is_error,
                         num_turns=1, session_id='s', result=result, api_error_status=status)


def _fake_query(messages):
    async def query(**kwargs):
        for m in messages:
            yield m
    return query


class LimitWatchTests(SimpleTestCase):
    def test_warning_and_normal_messages_do_not_raise(self):
        w = _LimitWatch()
        for m in (_event('allowed'), _event('allowed_warning', 1), _assistant(), _result(result='ok')):
            w.feed(m)

    def test_assistant_rate_limit_error_raises_with_reset_time_from_the_event(self):
        resets = int((datetime.now(dt_tz.utc) + timedelta(hours=2)).timestamp())
        w = _LimitWatch()
        w.feed(_event('rejected', resets))
        with self.assertRaises(SubscriptionLimit) as cm:
            w.feed(_assistant(error='rate_limit'))
        self.assertEqual(cm.exception.resets_at, resets)
        self.assertIn('It resets', str(cm.exception))
        self.assertIn('then press the button again', str(cm.exception))

    def test_without_reset_time_the_text_asks_to_press_again_later(self):
        with self.assertRaises(SubscriptionLimit) as cm:
            _LimitWatch().feed(_assistant(error='rate_limit'))
        self.assertIsNone(cm.exception.resets_at)
        self.assertIn('pressing the button again', str(cm.exception))

    def test_far_reset_shows_the_date(self):
        resets = int((datetime.now(dt_tz.utc) + timedelta(days=3)).timestamp())
        self.assertRegex(str(SubscriptionLimit(resets)), r'\d\d\.\d\d at \d\d:\d\d')

    def test_result_429_and_limit_text_raise_but_other_errors_do_not(self):
        with self.assertRaises(SubscriptionLimit):
            _LimitWatch().feed(_result(is_error=True, status=429))
        with self.assertRaises(SubscriptionLimit):
            _LimitWatch().feed(_result(is_error=True, result="You've hit your limit · resets 5pm"))
        _LimitWatch().feed(_result(is_error=True, result='Internal server error', status=500))
        _LimitWatch().feed(_result(is_error=False, result="You've hit your limit"))   # not an error — it's the response text

    def test_is_recognised_as_rate_limit_for_the_paid_reserve(self):
        self.assertTrue(llm._is_rate_limit(SubscriptionLimit()))


class StreamTests(SimpleTestCase):
    MESSAGES = [_event('rejected', 1_900_000_000), _assistant(error='rate_limit'),
                _result(is_error=True, result="You've hit your limit", status=429)]

    def test_text_query_raises_the_limit_instead_of_parsing_the_refusal_as_json(self):
        with mock.patch('claude_agent_sdk.query', _fake_query(self.MESSAGES)):
            with self.assertRaises(SubscriptionLimit):
                asyncio.run(llm._query_text('p', system='s', model='sonnet', api_key=None, timeout=5))

    def test_text_to_json_does_not_retry_a_limit_three_times(self):
        calls = []

        def refuse(coro):
            coro.close()
            calls.append(1)
            raise SubscriptionLimit()

        with override_settings(ANTHROPIC_API_KEY='', LLM_PREFER_API=False), \
                mock.patch('core.limits.enforce_llm'), \
                mock.patch.object(llm, '_run_sync', side_effect=refuse):
            with self.assertRaises(SubscriptionLimit):
                llm.text_to_json('p')
        self.assertEqual(len(calls), 1)

    def test_agent_drive_raises_the_limit_instead_of_unfinished_review(self):
        from catalog import instruction_agent as ia
        with mock.patch('claude_agent_sdk.query', _fake_query(self.MESSAGES)):
            with self.assertRaises(SubscriptionLimit):
                asyncio.run(ia._drive(mock.Mock(), mock.Mock(), 'p', 5))


@override_settings(ALLOWED_HOSTS=['testserver'])
class ButtonAndSchedulerTests(TestCase):
    URL = '/admin/catalog/instruction-normalize/'

    def test_button_queues_the_job_instead_of_showing_an_error(self):
        # Queue details — catalog/tests_instruction_jobs.py
        from django.contrib.auth import get_user_model
        from catalog import instruction_agent as ia
        self.client.force_login(get_user_model().objects.create(email='lim@example.com', is_staff=True,
                                                                is_superuser=True))
        resets = int((datetime.now(dt_tz.utc) + timedelta(hours=1)).timestamp())
        with override_settings(INSTRUCTION_MODE='agent'), \
                mock.patch.object(ia, 'normalize_agent', side_effect=SubscriptionLimit(resets)):
            resp = self.client.post(self.URL, data=json.dumps({'name': 'K', 'text': '<p>Delta 25 ml.</p>',
                                                               'items': []}),
                                    content_type='application/json')
        self.assertEqual(resp.status_code, 202)
        self.assertTrue(resp.json()['queued'])
        self.assertNotIn('error', resp.json())

    def test_scheduler_stops_at_the_limit_and_keeps_every_row_pending(self):
        from io import StringIO
        from django.core.management import call_command
        from catalog import instruction_agent as ia
        from catalog.models import Bundle, BundleInstructionOverride as Ov
        rows = []
        for n in range(3):
            b = Bundle.objects.create(name=f'K{n}', instruction_html='<p>text</p>')
            rows.append(Ov.objects.create(bundle=b, signature=f's{n}', status=Ov.PENDING, composition=[]))
        with mock.patch('time.sleep'), \
                mock.patch.object(ia, 'normalize_by_mode', side_effect=SubscriptionLimit()) as run:
            call_command('resolve_pending_instructions', stdout=StringIO())
        self.assertEqual(run.call_count, 1)                    # the rest would hit the same limit
        for r in rows:
            r.refresh_from_db()
            self.assertEqual(r.status, Ov.PENDING)


class EnvForTests(SimpleTestCase):
    """'Subscription first': the API key must be EXPLICITLY empty in the environment of the child
    claude, because the SDK merges
    os.environ under options.env and would silently leave the key from the container environment
    (paid API)."""

    def test_subscription_mode_blanks_the_api_key_even_if_exported(self):
        with mock.patch.dict('os.environ', {'ANTHROPIC_API_KEY': 'sk-secret', 'PATH': '/bin'}):
            env = llm._env_for(None)
        self.assertEqual(env['ANTHROPIC_API_KEY'], '')
        self.assertEqual(env['PATH'], '/bin')

    def test_reserve_mode_passes_the_key(self):
        with mock.patch.dict('os.environ', {'ANTHROPIC_API_KEY': 'sk-secret'}):
            self.assertEqual(llm._env_for('sk-reserve')['ANTHROPIC_API_KEY'], 'sk-reserve')

    def test_the_sdk_merge_cannot_bring_the_key_back(self):
        """What the SDK does (subprocess_cli): {**os.environ, **options.env}. An empty value overrides the key."""
        with mock.patch.dict('os.environ', {'ANTHROPIC_API_KEY': 'sk-secret'}):
            merged = {**__import__('os').environ, **llm._env_for(None)}
        self.assertEqual(merged['ANTHROPIC_API_KEY'], '')
