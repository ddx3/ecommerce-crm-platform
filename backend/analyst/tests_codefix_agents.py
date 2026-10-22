"""Working copy for large and “unusual” files, search without hangs, isolation of LLM agents, token
accounting and LLM failures."""
import asyncio
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from django.test import SimpleTestCase, override_settings

from analyst import codefix_policy as policy
from analyst.codefix import drafter, llm, reviewer, workspace
from analyst.codefix.workspace import Workspace, WorkspaceError

SMALL_TEST = 'import unittest\n\n\nclass T(unittest.TestCase):\n    def test(self):\n        pass\n'


def source_with(root: Path, files: dict) -> Path:
    src = root / 'src'
    src.mkdir(parents=True)
    for name, data in files.items():
        (src / name).parent.mkdir(parents=True, exist_ok=True)
        (src / name).write_bytes(data if isinstance(data, bytes) else data.encode('utf-8'))
    return src


class UnusualFilesTests(SimpleTestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def workspace(self, files):
        src = source_with(self.root, files)
        ws = Workspace(src, self.root / 'work').create()
        self.addCleanup(ws.cleanup)
        return ws

    def test_a_file_bigger_than_the_old_limit_can_be_edited(self):
        body = 'def f(x):\n    return x + 1\n\n\n' + ''.join(f'def g{i}(x):\n    return x * {i}\n\n\n' for i in range(7000))
        self.assertGreater(len(body), 150_000)
        ws = self.workspace({'catalog/views.py': body})
        ws.edit_file('backend/catalog/views.py', 'return x + 1', 'return (x or 0) + 1')
        patch = ws.diff()
        self.assertIn('+    return (x or 0) + 1', patch)
        self.assertLess(len(patch), 2000, 'only the changed section is in the patch')

    def test_a_file_with_a_bom_can_be_edited_and_the_patch_round_trips(self):
        bom = '﻿'
        ws = self.workspace({'catalog/widgets.py': bom + 'def f(x):\n    return x + 1\n'})
        ws.edit_file('backend/catalog/widgets.py', 'return x + 1', 'return (x or 0) + 1')
        ws.add_test('catalog', 'bom', SMALL_TEST)
        patch = ws.diff()
        report = policy.check_patch(patch, read_old=ws.live_text)
        self.assertEqual(report.code_files, ['backend/catalog/widgets.py'])
        changes = policy.parse_patch(patch)
        old = ws.live_text('backend/catalog/widgets.py')
        self.assertEqual(policy.apply_hunks(old, changes[0].hunks), (ws.dir / 'catalog' / 'widgets.py').read_bytes().decode('utf-8'))

    def test_a_file_with_line_separator_characters_gives_a_valid_patch(self):
        sep = chr(0x2028) + chr(0x2029) + chr(0x85)
        text = 'def f(x):\n    note = "a' + sep + 'b"  # ' + chr(0x0c) + ' form feed\n    return x + 1\n'
        ws = self.workspace({'core/jsonutils.py': text})
        ws.edit_file('backend/core/jsonutils.py', 'return x + 1', 'return (x or 0) + 1')
        patch = ws.diff()
        changes = policy.parse_patch(patch)
        self.assertEqual(policy.apply_hunks(text, changes[0].hunks), (ws.dir / 'core' / 'jsonutils.py').read_bytes().decode('utf-8'))
        self.assertIn(sep, patch, 'the patch context contains the characters as they are, not cut by splitlines')

    def test_a_lone_carriage_return_is_refused_in_added_text(self):
        ws = self.workspace({'catalog/views.py': 'def f(x):\n    return x + 1\n'})
        with self.assertRaisesMessage(WorkspaceError, 'The policy forbids it'):
            ws.edit_file('backend/catalog/views.py', 'return x + 1', 'return x\r+ 1')

    def test_files_that_cannot_be_patched_safely_are_refused_with_a_clear_reason(self):
        ws = self.workspace({'catalog/crlf.py': 'def f(x):\r\n    return x + 1\r\n', 'catalog/nonl.py': 'def f(x):\n    return x'})
        for name in ('crlf', 'nonl'):
            with self.assertRaisesMessage(WorkspaceError, 'give_up', msg=name):
                ws.edit_file(f'backend/catalog/{name}.py', 'return', 'return')

    def test_secret_names_are_refused_by_the_resolver_even_when_present_in_the_copy(self):
        ws = self.workspace({'catalog/views.py': 'x = 1\n'})
        (ws.dir / 'catalog' / 'my_secret_notes.txt').write_text('x')           # appeared only after the copy was made
        (ws.dir / '.env').write_text('SECRET_KEY=1')
        (ws.dir / 'media').mkdir()
        (ws.dir / 'media' / 'photo.jpg').write_bytes(b'\xff\xd8')
        for path in ('backend/catalog/my_secret_notes.txt', 'backend/media/photo.jpg'):
            with self.assertRaisesMessage(WorkspaceError, 'is not available', msg=path):
                ws.read_file(path)
        with self.assertRaises(WorkspaceError):                                  # hidden names are cut off by path normalization
            ws.read_file('backend/.env')

    def test_paths_outside_backend_are_refused_with_the_right_reason(self):
        ws = self.workspace({'catalog/views.py': 'x = 1\n'})
        for path in ('catalog/views.py', 'frontend/src/App.tsx', 'ops/shop_helper.py', '/etc/passwd', 'backend/../ops/x', 'backend2/x.py'):
            with self.assertRaisesMessage(WorkspaceError, 'start with backend/', msg=path):
                ws.read_file(path)

    def test_existing_tests_cannot_be_edited(self):
        ws = self.workspace({'catalog/tests.py': 'import unittest\n\n\nclass T(unittest.TestCase):\n    def test(self):\n        assert 1\n'})
        with self.assertRaisesMessage(WorkspaceError, 'Existing tests cannot be changed'):
            ws.edit_file('backend/catalog/tests.py', 'assert 1', 'assert 0')

    def test_the_diff_puts_code_before_tests(self):
        ws = self.workspace({'catalog/views.py': 'def f(x):\n    return x + 1\n', 'catalog/serializers.py': 'def s(x):\n    return x + 1\n'})
        ws.edit_file('backend/catalog/views.py', 'return x + 1', 'return (x or 0) + 1')
        ws.add_test('catalog', 'a', SMALL_TEST)
        ws.edit_file('backend/catalog/serializers.py', 'return x + 1', 'return (x or 0) + 1')
        patch = ws.diff()
        order = [patch.index(p) for p in ('b/backend/catalog/serializers.py', 'b/backend/catalog/views.py', 'b/backend/catalog/tests_autofix_a.py')]
        self.assertEqual(order, sorted(order))

    def test_the_structural_layer_runs_while_the_agent_edits(self):
        ws = self.workspace({'catalog/views.py': 'def f(x):\n    return x + 1\n'})
        with self.assertRaisesMessage(WorkspaceError, 'R2'):
            ws.edit_file('backend/catalog/views.py', 'return x + 1', 'Base.attr = 1\n    return x + 1')
        with self.assertRaisesMessage(WorkspaceError, 'R3'):
            ws.edit_file('backend/catalog/views.py', 'return x + 1', "return getattr(x, 'a' + 'b')")
        self.assertEqual(ws.diff(), '')

    def test_existing_guards_cannot_be_changed_by_the_agent(self):
        ws = self.workspace({'catalog/views.py': 'def f(x):\n    if x is None:\n        return 0\n    return x + 1\n'})
        with self.assertRaisesMessage(WorkspaceError, 'R7'):
            ws.edit_file('backend/catalog/views.py', 'if x is None:', 'if x is None and False:')


class GrepSafetyTests(SimpleTestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        src = source_with(Path(self._tmp.name), {'catalog/views.py': 'a' * 40 + '!\n' + 'def product_price():\n    pass\n'})
        self.ws = Workspace(src, Path(self._tmp.name) / 'work').create()
        self.addCleanup(self.ws.cleanup)

    def test_a_catastrophic_pattern_is_cut_off_instead_of_freezing_the_service(self):
        started = time.monotonic()
        with mock.patch.object(workspace, 'GREP_TIMEOUT', 1):
            with self.assertRaisesMessage(WorkspaceError, 'catastrophic'):
                self.ws.grep(r'(a+)+$', 'backend')
        self.assertLess(time.monotonic() - started, 6)

    def test_an_ordinary_search_works_and_reports_the_right_path_and_line(self):
        self.assertEqual(self.ws.grep(r'def product_', 'backend')['matches'], ['backend/catalog/views.py:2: def product_price():'])

    def test_bad_patterns_are_refused_quickly(self):
        for bad in ('(', '', 'x' * 400, None):
            with self.assertRaises(WorkspaceError, msg=str(bad)[:20]):
                self.ws.grep(bad)

    def test_overlong_lines_are_skipped(self):
        long_file = self.ws.dir / 'catalog' / 'long.py'
        long_file.write_text('x = 1  # ' + 'y' * 5000 + '\nneedle = 1\n', encoding='utf-8', newline='\n')
        self.assertEqual(self.ws.grep('needle', 'backend/catalog/long.py')['matches'], ['backend/catalog/long.py:2: needle = 1'])


class AgentIsolationTests(SimpleTestCase):
    """The only thing that takes Bash/Write/WebFetch away from the agent that writes code is the launch
parameters. Changing any one of them removes the isolation."""

    def options(self, api_key=None):
        return llm._options('sys', 'codefix', [], ['get_error', 'edit_file'], api_key=api_key, model='opus', max_turns=7)

    def test_no_builtin_tools_and_deny_by_default(self):
        opts = self.options()
        self.assertEqual(opts.tools, [])
        self.assertEqual(opts.permission_mode, 'dontAsk')
        self.assertTrue(opts.strict_mcp_config)
        self.assertIsNone(opts.setting_sources)
        self.assertEqual(opts.allowed_tools, ['mcp__codefix__get_error', 'mcp__codefix__edit_file'])
        self.assertEqual(opts.max_turns, 7)
        self.assertGreater(opts.max_budget_usd, 0)

    def test_the_subscription_branch_blanks_the_api_key_and_the_reserve_branch_sets_it(self):
        self.assertTrue(self.options().env.get('ANTHROPIC_API_KEY') == '')
        self.assertTrue(self.options('reserve-key').env.get('ANTHROPIC_API_KEY') == 'reserve-key')

    def test_reviewer_and_drafter_tool_sets_are_exactly_what_they_declare(self):
        from analyst.codefix.drafter import DraftSession, TOOL_NAMES
        from analyst.codefix.reviewer import ReviewSession
        names = [t.name for t in drafter.build_tools(DraftSession({}, mock.MagicMock()))]
        self.assertEqual(sorted(names), sorted(TOOL_NAMES))
        self.assertEqual([t.name for t in reviewer.build_tools(ReviewSession())], reviewer.TOOL_NAMES)

    def test_tool_handlers_are_wired_to_the_right_session_methods(self):
        session = mock.MagicMock()
        tools = {t.name: t for t in drafter.build_tools(session)}
        run = lambda coro: asyncio.run(coro)                                   # noqa: E731
        run(tools['edit_file'].handler({'path': 'backend/catalog/views.py', 'old': 'a', 'new': 'b'}))
        session.edit_file.assert_called_once_with('backend/catalog/views.py', 'a', 'b')
        run(tools['add_test'].handler({'app': 'catalog', 'name': 'x', 'content': 'c'}))
        session.add_test.assert_called_once_with('catalog', 'x', 'c')
        run(tools['submit_fix'].handler({'summary': 's'}))
        session.submit_fix.assert_called_once_with('s')
        run(tools['give_up'].handler({'reason': 'r'}))
        session.give_up.assert_called_once_with('r')


class RunSessionTests(SimpleTestCase):
    def run_session(self, drive, **settings_kw):
        async def fake_drive(session, options, prompt, timeout, stats=None):
            return await drive(stats if stats is not None else {})
        with mock.patch('analyst.agent._drive', fake_drive), mock.patch.object(llm, '_options', lambda *a, **k: object()), \
                override_settings(ANTHROPIC_API_KEY='', LLM_PREFER_API=False, **settings_kw):
            return llm.run_session('s', 'p', 'codefix', [], [], timeout=5, max_turns=3)

    def test_a_normal_session_returns_stats(self):
        async def drive(stats):
            stats.update(tokens=1234, turns=3, subtype='success', is_error=False)
            return stats
        with mock.patch('core.limits.record_analyst_llm_tokens') as record:
            stats = self.run_session(drive)
        self.assertEqual(stats['tokens'], 1234)
        record.assert_called_once_with(1234)

    def test_tokens_are_recorded_even_when_the_session_fails(self):
        async def drive(stats):
            stats['tokens'] = 777
            raise RuntimeError('timeout')
        with mock.patch('core.limits.record_analyst_llm_tokens') as record:
            with self.assertRaises(RuntimeError):
                self.run_session(drive)
        record.assert_called_once_with(777)

    def test_the_subscription_limit_becomes_a_deferral_with_the_reset_time(self):
        from core.llm import SubscriptionLimit

        async def drive(stats):
            raise SubscriptionLimit(int(time.time()) + 7200)
        with self.assertRaises(llm.LLMUnavailable) as ctx:
            self.run_session(drive)
        self.assertTrue(7200 <= ctx.exception.wait <= 7500, ctx.exception.wait)

    def test_an_api_error_reported_as_a_result_is_an_outage_not_a_refusal(self):
        async def drive(stats):
            stats.update(tokens=5, subtype='success', is_error=True, result_text='Invalid API key · Please run /login')
            return stats
        with self.assertRaisesMessage(llm.LLMUnavailable, 'Invalid API key'):
            self.run_session(drive)

    def test_running_out_of_turns_is_the_agents_own_failure_not_an_outage(self):
        async def drive(stats):
            stats.update(tokens=5, subtype='error_max_turns', is_error=True, result_text='')
            return stats
        self.assertEqual(self.run_session(drive)['subtype'], 'error_max_turns')

    def test_the_daily_token_budget_is_checked_first(self):
        from core.limits import LimitExceeded
        with mock.patch('core.limits.enforce_analyst_llm', side_effect=LimitExceeded('limit')):
            with self.assertRaises(llm.LLMUnavailable):
                self.run_session(lambda stats: None)

    def test_the_paid_reserve_is_used_only_after_a_rate_limit(self):
        from core.llm import SubscriptionLimit
        calls = []

        async def drive(stats):
            calls.append(1)
            if len(calls) == 1:
                raise SubscriptionLimit(None)
            stats.update(tokens=1, subtype='success', is_error=False)
            return stats
        with mock.patch('analyst.agent._drive', lambda session, options, prompt, timeout, stats=None: drive(stats)), \
                mock.patch.object(llm, '_options', lambda *a, **k: object()), \
                override_settings(ANTHROPIC_API_KEY='reserve', LLM_PREFER_API=False):
            stats = llm.run_session('s', 'p', 'codefix', [], [], timeout=5, max_turns=3)
        self.assertEqual(len(calls), 2)
        self.assertEqual(stats['subtype'], 'success')


class TokenWeightTests(SimpleTestCase):
    def test_cache_tokens_are_counted_not_ignored(self):
        """Previously only input+output were counted, but in a multi-turn session almost all input is cached —
the budget limited nothing."""
        from core.llm import _tokens_of
        message = mock.MagicMock(usage={'input_tokens': 100, 'output_tokens': 50, 'cache_creation_input_tokens': 200,
                                        'cache_read_input_tokens': 10_000})
        self.assertGreater(_tokens_of(message), 100 + 50 + 1000)
        self.assertEqual(_tokens_of(mock.MagicMock(usage=None)), 0)


class AsyncGeneratorLeakTests(SimpleTestCase):
    def test_a_generator_left_open_by_an_exception_in_the_loop_body_is_closed_by_run_sync(self):
        from core.llm import _run_sync
        closed = []

        async def stream():
            try:
                yield 1
                yield 2
            finally:
                closed.append(True)

        async def body():
            async for _ in stream():
                raise RuntimeError('limit hit in the middle of a session')
        with self.assertRaises(RuntimeError):
            _run_sync(body())
        self.assertEqual(closed, [True], 'the generator finally block (where the SDK kills the claude process) has run')

    def test_drive_closes_the_sdk_stream_and_keeps_partial_token_counts(self):
        import types
        from analyst import agent
        closed, sent = [], []

        class FakeAssistant:
            def __init__(self, mid, tokens):
                self.content, self.message_id, self.usage, self.error = [], mid, {'input_tokens': tokens, 'output_tokens': 0}, None

        async def fake_query(prompt, options):
            try:
                yield FakeAssistant('m1', 100)
                yield FakeAssistant('m1', 100)               # same message_id — not counted a second time
                yield FakeAssistant('m2', 50)
                raise RuntimeError('connection dropped')
            finally:
                closed.append(True)
        fake_sdk = types.SimpleNamespace(AssistantMessage=FakeAssistant, ResultMessage=type('R', (), {}), ToolUseBlock=type('T', (), {}),
                                         query=fake_query)
        stats = {}
        with mock.patch.dict('sys.modules', {'claude_agent_sdk': fake_sdk}), \
                mock.patch('core.llm._LimitWatch', lambda: types.SimpleNamespace(feed=lambda m: sent.append(m))):
            with self.assertRaises(RuntimeError):
                asyncio.run(agent._drive(None, None, 'p', 5, stats))
        self.assertEqual(closed, [True])
        self.assertEqual(stats['tokens'], 150)
        self.assertEqual(len(sent), 3)
