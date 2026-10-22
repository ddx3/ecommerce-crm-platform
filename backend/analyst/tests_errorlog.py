"""Collecting code errors from logs: cleanup, noise, fingerprint, recording and cards."""
import logging
import queue
import time
from datetime import timedelta
from types import SimpleNamespace
from unittest import mock

from django.test import SimpleTestCase, TestCase
from django.utils import timezone

from analyst import errorlog
from analyst.checks import code_errors as check_module
from analyst.models import ErrorEvent, Finding


def _boom(kind=ValueError, text='bad data'):
    raise kind(text)


def _caught(kind=ValueError, text='bad data'):
    try:
        _boom(kind, text)
    except Exception as exc:
        import sys
        return sys.exc_info(), exc


def _describe(kind=ValueError, text='bad data', request=None):
    (et, ex, tb), _ = _caught(kind, text)
    return errorlog.describe(et, ex, tb, request=request)


def _request(path_info='', route=''):
    return SimpleNamespace(path_info=path_info, path=path_info,
                           resolver_match=SimpleNamespace(route=route) if route else None)


class DescribeTests(SimpleTestCase):
    def test_a_bug_in_our_code_is_described_with_place_and_clean_traceback(self):
        info = _describe()
        self.assertEqual(info['kind'], 'code')
        self.assertEqual(info['where_path'], 'backend/analyst/tests_errorlog.py')
        self.assertEqual(info['where_func'], '_boom')
        self.assertIn('ValueError: ', info['traceback'])
        self.assertIn('raise kind(text)', info['traceback'])
        self.assertNotIn('site-packages', info['traceback'])
        self.assertNotIn(':\\', info['traceback'])                                 # no local disk paths

    def test_noise_is_dropped(self):
        from django.core.exceptions import DisallowedHost, PermissionDenied, SuspiciousOperation
        from django.http import Http404
        for kind in (DisallowedHost, SuspiciousOperation, PermissionDenied, Http404, BrokenPipeError, ConnectionResetError):
            self.assertIsNone(_describe(kind), kind.__name__)

    def test_environment_failures_are_external_not_code(self):
        for kind in (ConnectionRefusedError, TimeoutError, MemoryError):
            info = _describe(kind, 'x')
            self.assertEqual(info['kind'], 'external', kind.__name__)
            self.assertEqual(info['where_path'], '')
        import requests
        self.assertEqual(_describe(requests.exceptions.ConnectionError, 'no network')['kind'], 'external')

    def test_disk_and_resource_errors_are_external_not_code(self):
        import errno
        for number in (errno.ENOSPC, errno.EMFILE, errno.ENOMEM, errno.EIO, errno.ENETUNREACH, errno.EROFS):
            (et, ex, tb), _ = _caught(OSError, number)
            exc = OSError(number, 'environment failure')
            self.assertEqual(errorlog.describe(OSError, exc, tb)['kind'], 'external', errno.errorcode[number])

    def test_only_logic_errors_are_code_the_rest_are_other(self):
        for kind in (AttributeError, TypeError, KeyError, ValueError, ZeroDivisionError, IndexError):
            self.assertEqual(_describe(kind, 'x')['kind'], 'code', kind.__name__)
        for kind in (RuntimeError, PermissionError, NotImplementedError):
            self.assertEqual(_describe(kind, 'x')['kind'], 'other', kind.__name__)
        from django.db.utils import ProgrammingError
        self.assertEqual(_describe(ProgrammingError, 'column does not exist')['kind'], 'other')

    def test_library_prefixes_need_the_dot(self):
        """`socket`/`ssl` without a dot would also catch other packages (socketio, sslify)."""
        self.assertNotIn('socket', errorlog.EXTERNAL_MODULES)
        self.assertTrue(all(p.endswith('.') for p in errorlog.EXTERNAL_MODULES))

    def test_fingerprint_ignores_message_and_line_but_not_place_or_type(self):
        a = _describe(ValueError, 'one')
        b = _describe(ValueError, 'another text')
        self.assertEqual(a['fingerprint'], b['fingerprint'])
        self.assertNotEqual(a['fingerprint'], _describe(KeyError, 'one')['fingerprint'])

    def test_same_method_name_in_two_classes_gets_two_fingerprints(self):
        class First:
            def to_representation(self):
                raise AttributeError('x')

        class Second:
            def to_representation(self):
                raise AttributeError('y')

        prints = []
        for cls in (First, Second):
            try:
                cls().to_representation()
            except AttributeError as exc:
                import sys
                prints.append(errorlog.describe(*sys.exc_info())['fingerprint'])
        self.assertNotEqual(prints[0], prints[1])

    def test_message_keeps_code_names_and_drops_customer_values(self):
        info = _describe(ValueError, "invalid literal for int() with base 10: 'Maple St. 12, apt. 5, Kyiv'")
        self.assertIn('int()', info['message'])
        self.assertNotIn('Maple', info['message'])
        self.assertNotIn('Maple', info['traceback'])
        keyed = _describe(KeyError, 'text')                                       # 'text' is in the test code (a variable name)
        self.assertIn('text', keyed['message'])

    def test_personal_data_never_survives(self):
        samples = [
            "Field 'id' expected a number but got 'Jane Marie Doe'",
            'DETAIL:  Failing row contains (4821, Jane Doe, +380000000000, 000000000, jane@example.com)',
            'Key (full_name)=(Mary Smith) already exists.',
            "time data 'Jane Doe' does not match format '%d.%m.%Y'",
            'customer jane.doe@example.com tel +380 00 000 00 00 token abcdefghijklmnopqrstuvwxyz123456',
            'phone_0000000000 and 000/000/00/00 and 000000000',
            'unterminated "name Mary Smith ' + 'x' * 300,
        ]
        forbidden = ('Doe', 'Jane', 'Mary', 'Smith', 'Maple', 'jane', '0000000000', '000000000', '000 00 00',
                     '00/00', 'abcdefghijklmnopqrstuvwxyz123456')
        for text in samples:
            info = _describe(ValueError, text)
            blob = info['message'] + info['traceback']
            for word in forbidden:
                self.assertNotIn(word, blob, f'{word!r} leaked from {text[:40]!r}')

    def test_postgres_detail_is_cut_off(self):
        info = _describe(ValueError, 'null value violates not-null constraint\nDETAIL:  Failing row contains (1, secret)')
        self.assertIn('not-null constraint', info['message'])
        self.assertNotIn('secret', info['message'])

    def test_request_path_is_the_route_template_when_known(self):
        info = _describe(request=_request('/api/products/super-fertilizer/reviews/', 'api/products/<slug:slug>/reviews/'))
        self.assertEqual(info['request_path'], 'api/products/<slug:slug>/reviews/')

    def test_request_path_without_a_route_hides_ids_and_query_data(self):
        info = _describe(request=_request('/api/search/?q=jane@example.com&token=abc'))
        self.assertEqual(info['request_path'], '/api/search/<…>')
        info = _describe(request=_request('/api/orders/1234567890/confirm/'))
        self.assertNotIn('1234567890', info['request_path'])
        info = _describe(request=_request('/reset/MTIz/c1x9z-' + 'a' * 40 + '/'))
        self.assertNotIn('a' * 20, info['request_path'])

    def test_message_is_truncated_and_control_characters_removed(self):
        info = _describe(ValueError, 'a\x00b\x1b' + 'ab ' * 200)
        self.assertLessEqual(len(info['message']), 200)
        self.assertNotIn('\x00', info['message'])
        self.assertNotIn('\x1b', info['message'])


class CostTests(SimpleTestCase):
    """A single 500 with a long client-supplied value must not hang the worker (the regex used to be
quadratic)."""

    HUGE = 'a' * 200_000

    def test_the_request_thread_part_is_cheap_even_for_huge_messages(self):
        (et, ex, tb), _ = _caught(ValueError, "Field 'id' expected a number but got '" + self.HUGE + "'")
        started = time.perf_counter()
        raw = errorlog.capture(et, ex, tb, request=_request('/x/' + 'b' * 5000))
        self.assertLess(time.perf_counter() - started, 0.05)
        self.assertLessEqual(len(raw['message']), errorlog.RAW_MESSAGE)
        self.assertLessEqual(len(raw['request'][0]), 300)

    def test_cleaning_is_linear_for_pathological_input(self):
        for text in (self.HUGE, '"' * 100_000, "'a" * 50_000, 'x@' * 50_000, '0' * 100_000, '(' * 100_000):
            started = time.perf_counter()
            errorlog.shape_message(text)
            errorlog.scrub(text)
            self.assertLess(time.perf_counter() - started, 0.5, text[:12])

    def test_describe_of_a_huge_message_is_fast(self):
        started = time.perf_counter()
        info = _describe(ValueError, "got '" + self.HUGE + "'")
        self.assertLess(time.perf_counter() - started, 0.5)
        self.assertLessEqual(len(info['message']), 200)

    def test_handler_does_no_regex_work_in_the_calling_thread(self):
        handler = errorlog.ErrorEventHandler()
        logger = logging.getLogger('analyst-errorlog-cost')
        logger.propagate = False
        logger.addHandler(handler)
        self.addCleanup(logger.removeHandler, handler)
        with mock.patch.object(errorlog, '_queue', queue.Queue()) as q, mock.patch.object(errorlog, '_ensure_worker'), \
                mock.patch.object(errorlog, 'process') as process, mock.patch.object(errorlog, 'shape_message') as shape:
            try:
                _boom(ValueError, self.HUGE)
            except ValueError:
                logger.error('failure', exc_info=True)
            self.assertEqual(q.qsize(), 1)
            process.assert_not_called()
            shape.assert_not_called()


class HandlerTests(TestCase):
    def setUp(self):
        self.logger = logging.getLogger('analyst-errorlog-test')
        self.logger.propagate = False
        self.logger.setLevel(logging.DEBUG)
        self.handler = errorlog.ErrorEventHandler()
        self.logger.addHandler(self.handler)
        self.addCleanup(self.logger.removeHandler, self.handler)
        self.enterContext(mock.patch.object(errorlog, 'SYNC', True))

    def _log(self, kind=ValueError, text='bad data'):
        try:
            _boom(kind, text)
        except Exception:
            self.logger.error('failure', exc_info=True)

    def test_error_with_traceback_is_stored_and_repeats_count(self):
        for _ in range(3):
            self._log()
        event = ErrorEvent.objects.get()
        self.assertEqual((event.kind, event.count, event.status), ('code', 3, ErrorEvent.OPEN))
        self.assertEqual(len(event.recent), 3)
        self.assertEqual(event.where_func, '_boom')

    def test_records_without_traceback_and_noise_are_ignored(self):
        self.logger.error('plain text without an exception')
        self._log(BrokenPipeError)
        self.assertEqual(ErrorEvent.objects.count(), 0)

    def test_old_repeats_are_forgotten_after_two_days(self):
        self._log()
        event = ErrorEvent.objects.get()
        event.recent = [int((timezone.now() - timedelta(days=3)).timestamp())]
        event.save()
        self._log()
        self.assertEqual(len(ErrorEvent.objects.get().recent), 1)

    def test_a_fixed_error_that_comes_back_reopens(self):
        self._log()
        ErrorEvent.objects.update(status=ErrorEvent.FIXED)
        self._log()
        self.assertEqual(ErrorEvent.objects.get().status, ErrorEvent.OPEN)

    def test_the_latest_traceback_replaces_the_first_one(self):
        """After code changes the line number and the text of the first failure become stale: the agent must
see the current ones."""
        self._log(text='first')
        ErrorEvent.objects.update(where_line=1, traceback='old traceback')
        self._log(text='second')
        event = ErrorEvent.objects.get()
        self.assertNotEqual(event.traceback, 'old traceback')
        self.assertNotEqual(event.where_line, 1)

    def test_a_concurrent_first_insert_is_retried_not_lost(self):
        """Two processes create the same fingerprint at once: the loser retries instead of losing the batch."""
        from django.db import IntegrityError
        info = _describe()
        real_save, calls = ErrorEvent.save, {'n': 0}

        def flaky_save(self, *args, **kwargs):
            calls['n'] += 1
            if calls['n'] == 1:
                raise IntegrityError('duplicate key value violates unique constraint')
            return real_save(self, *args, **kwargs)

        with mock.patch.object(ErrorEvent, 'save', flaky_save):
            errorlog.persist(info, hits=2)
        self.assertEqual(calls['n'], 2)
        self.assertEqual((ErrorEvent.objects.count(), ErrorEvent.objects.get().count), (1, 2))

    def test_an_existing_event_is_updated_not_duplicated(self):
        info = _describe()
        ErrorEvent.objects.create(fingerprint=info['fingerprint'], exc_type='x', count=1, recent=[])
        errorlog.persist(info, hits=2)
        self.assertEqual((ErrorEvent.objects.count(), ErrorEvent.objects.get().count), (1, 3))

    def test_handler_never_raises_even_when_the_database_write_fails(self):
        with mock.patch.object(errorlog, 'persist', side_effect=RuntimeError('db down')):
            self._log()                                                    # must not raise

    def test_async_mode_only_queues(self):
        with mock.patch.object(errorlog, 'SYNC', False), mock.patch.object(errorlog, '_ensure_worker') as worker, \
                mock.patch.object(errorlog, '_queue', queue.Queue()) as q:
            self._log()
            self.assertEqual(q.qsize(), 1)
            worker.assert_called_once()
        self.assertEqual(ErrorEvent.objects.count(), 0)

    def test_drain_groups_repeats_by_fingerprint(self):
        q = queue.Queue()
        (et, ex, tb), _ = _caught(ValueError)
        (et2, ex2, tb2), _ = _caught(KeyError)
        a, b = errorlog.capture(et, ex, tb), errorlog.capture(et2, ex2, tb2)
        for raw in (a, a, b):
            q.put_nowait(raw)
        with mock.patch.object(errorlog, '_queue', q):
            first = q.get_nowait()
            groups = errorlog._drain(first)
        hits = sorted(hits for _info, hits in groups.values())
        self.assertEqual(hits, [1, 2])

    def test_old_events_are_purged(self):
        self._log()
        ErrorEvent.objects.update(last_seen=timezone.now() - timedelta(days=61))
        self.assertEqual(errorlog.purge(), 1)
        self._log()
        ErrorEvent.objects.update(last_seen=timezone.now() - timedelta(days=61), status=ErrorEvent.FIXING)
        self.assertEqual(errorlog.purge(), 0)                              # the one being fixed is left alone


class CardTests(TestCase):
    def _event(self, hits, kind='code', **kw):
        now = timezone.now()
        defaults = dict(fingerprint=f'fp{ErrorEvent.objects.count()}', kind=kind, exc_type='builtins.ValueError',
                        message='bad data', where_path='backend/catalog/views.py', where_func='product_list',
                        count=hits, recent=[int(now.timestamp()) - i for i in range(hits)], last_seen=now)
        defaults.update(kw)
        return ErrorEvent.objects.create(**defaults)

    def test_card_appears_only_after_repeats(self):
        self._event(2)
        self.assertEqual(check_module.code_errors(timezone.now()), [])
        ErrorEvent.objects.all().delete()
        self._event(3)
        (item,) = check_module.code_errors(timezone.now())
        self.assertEqual(item.severity, Finding.WARN)
        self.assertIn('ValueError in catalog/views.py (product_list)', item.title)
        self.assertIn('3 times in 24 h', item.body)
        self.assertNotIn('does not fix such errors itself', item.body)
        self.assertTrue(item.data['no_auto'])

    def test_other_errors_get_a_card_that_says_they_are_not_auto_fixed(self):
        self._event(4, kind='other', exc_type='builtins.RuntimeError')
        (item,) = check_module.code_errors(timezone.now())
        self.assertIn('the Analyst does not fix such errors itself', item.body)

    def test_many_repeats_are_critical(self):
        self._event(60)
        (item,) = check_module.code_errors(timezone.now())
        self.assertEqual(item.severity, Finding.CRITICAL)

    def test_external_failures_need_more_repeats_and_are_informational(self):
        self._event(5, kind='external', exc_type='requests.exceptions.ConnectionError', where_path='')
        self.assertEqual(check_module.code_errors(timezone.now()), [])
        ErrorEvent.objects.all().delete()
        self._event(12, kind='external', exc_type='requests.exceptions.ConnectionError', where_path='')
        (item,) = check_module.code_errors(timezone.now())
        self.assertEqual(item.severity, Finding.INFO)
        self.assertIn('External service failure', item.title)

    def test_ignored_and_stale_errors_are_not_shown(self):
        self._event(5, status=ErrorEvent.IGNORED)
        self._event(5, last_seen=timezone.now() - timedelta(days=2), recent=[int(timezone.now().timestamp()) - 3 * 86400] * 5)
        self.assertEqual(check_module.code_errors(timezone.now()), [])

    def test_check_is_part_of_the_guard_cycle(self):
        from analyst.guard import GUARD_CHECKS
        self.assertIn('code_errors', GUARD_CHECKS)
