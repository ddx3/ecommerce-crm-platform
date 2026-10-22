"""Regressions for the incident of 29.07–02.08.2026: the scheduler silently did not work for 4 days.

Postgres dropped the connection, and `run_scheduler` — a long-lived while-loop with no
HTTP requests — held the dead socket forever: EVERY task failed with
InterfaceError('connection already closed'), the process stayed 'alive', so
`restart: unless-stopped` did not kick in. Bank-transfer payments stopped being
auto-confirmed, and nobody noticed for 1020 cycles in a row.

Three properties are pinned down here, without which this will happen again:
  1. every task starts with close_old_connections() (that is where Django
     applies CONN_MAX_AGE/CONN_HEALTH_CHECKS, because it calls it only
     on the request_started/request_finished signals, which a command doesn't have);
  2. N fully dead cycles in a row kill the process so that Docker brings it up
     again with a clean connection;
  3. a Mono statement that hit the 500-operation limit is paged back, rather than
     silently losing the oldest tail.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone as dt_timezone
from decimal import Decimal
from unittest import mock

from django.test import SimpleTestCase, TestCase
from django.utils import timezone

from orders import monobank_personal
from orders.management.commands import run_scheduler


class SchedulerConnectionHygieneTests(SimpleTestCase):
    """Every sub-command must start by dropping dead connections."""

    def setUp(self):
        # The analyst heartbeat writes to the DB, and SimpleTestCase forbids DB access.
        patcher = mock.patch.object(run_scheduler.Command, '_beat')
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_run_job_closes_old_connections_before_running(self):
        cmd = run_scheduler.Command()
        calls: list[str] = []
        with mock.patch.object(run_scheduler, 'close_old_connections',
                               side_effect=lambda: calls.append('close')), \
             mock.patch.object(run_scheduler, 'call_command',
                               side_effect=lambda name: calls.append(f'run:{name}')):
            self.assertTrue(cmd._run_job('poll_monobank'))
        # The order is critical: reset first, then work.
        self.assertEqual(calls, ['close', 'run:poll_monobank'])

    def test_run_job_still_closes_connections_when_job_raises(self):
        """This is exactly the path that was broken: a task fails — the next one must heal."""
        cmd = run_scheduler.Command()
        closed: list[int] = []
        with mock.patch.object(run_scheduler, 'close_old_connections',
                               side_effect=lambda: closed.append(1)), \
             mock.patch.object(run_scheduler, 'call_command',
                               side_effect=RuntimeError('connection already closed')):
            self.assertFalse(cmd._run_job('match_bank_transactions'))
            self.assertFalse(cmd._run_job('match_bank_transactions'))
        self.assertEqual(len(closed), 2)

    def test_rare_cadence_jobs_go_through_the_same_hygiene(self):
        """recompute_khit/heal_stock_drift etc. must not have their own bare call_command."""
        cmd = run_scheduler.Command()
        with mock.patch.object(cmd, '_run_job') as run_job:
            cmd._run_khit()
            cmd._run_np_warmup()
            cmd._run_dropsync()
            cmd._run_drift()
            cmd._run_seo()
        self.assertEqual(
            [c.args[0] for c in run_job.call_args_list],
            ['recompute_khit', 'warmup_np_cache', 'sync_dropshipping',
             'heal_stock_drift', 'seo_apply_content_meta'],
        )


class _LoopDone(Exception):
    """Signal 'scenario finished' — exit the infinite loop."""


class SchedulerDeadCycleTests(SimpleTestCase):
    """Dead cycles must kill the process, not go on forever."""

    def setUp(self):
        patcher = mock.patch.object(run_scheduler.Command, '_beat')
        patcher.start()
        self.addCleanup(patcher.stop)

    def _cycle_with(self, *, all_fail: bool):
        cmd = run_scheduler.Command()
        with mock.patch.object(run_scheduler, 'close_old_connections'), \
             mock.patch.object(run_scheduler, 'call_command',
                               side_effect=(RuntimeError('dead') if all_fail else None)), \
             mock.patch.object(cmd, '_jobs', return_value=['a', 'b']):
            return cmd._run_cycle()

    def test_cycle_reports_failure_when_every_job_dies(self):
        self.assertFalse(self._cycle_with(all_fail=True))

    def test_cycle_reports_success_when_jobs_work(self):
        self.assertTrue(self._cycle_with(all_fail=False))

    def test_process_exits_after_max_dead_cycles(self):
        """Exit → Docker restart → fresh connection. Without this — 4 days of silence."""
        cmd = run_scheduler.Command()
        with mock.patch.object(cmd, '_run_cycle', return_value=False), \
             mock.patch.object(cmd, '_dropsync_age', return_value=None), \
             mock.patch.object(cmd, '_run_khit'), \
             mock.patch.object(cmd, '_run_np_warmup'), \
             mock.patch.object(cmd, '_run_dropsync'), \
             mock.patch.object(cmd, '_run_drift'), \
             mock.patch.object(cmd, '_run_seo'), \
             mock.patch.object(cmd, '_run_instructions'), \
             mock.patch.object(run_scheduler.time, 'sleep'):
            with self.assertRaises(SystemExit):
                cmd.handle(interval=1, khit_interval=999, np_interval=999,
                           dropsync_interval=999, drift_interval=999,
                           seo_interval=999,
                           instructions_interval=999, once=False)

    def test_successful_cycle_resets_the_dead_counter(self):
        """Single failures must not accumulate until a restart.

        Sequence fail,fail,OK,fail,fail with MAX_DEAD_CYCLES=3:
          with counter reset  → 1,2,→0,1,2 — no exit;
          without reset       → 1,2,  ,3   — SystemExit on the 4th cycle.
        So SystemExit here = a reset regression.
        """
        self.assertEqual(run_scheduler.MAX_DEAD_CYCLES, 3)
        cmd = run_scheduler.Command()
        # Interrupt the loop on the 6th sleep, when all scenario cycles have run.
        sleeps = [None] * 5 + [_LoopDone()]
        with mock.patch.object(cmd, '_run_cycle',
                               side_effect=[False, False, True, False, False, True]), \
             mock.patch.object(cmd, '_dropsync_age', return_value=None), \
             mock.patch.object(cmd, '_run_khit'), \
             mock.patch.object(cmd, '_run_np_warmup'), \
             mock.patch.object(cmd, '_run_dropsync'), \
             mock.patch.object(cmd, '_run_drift'), \
             mock.patch.object(cmd, '_run_seo'), \
             mock.patch.object(cmd, '_run_instructions'), \
             mock.patch.object(run_scheduler.time, 'sleep', side_effect=sleeps):
            with self.assertRaises(_LoopDone):
                cmd.handle(interval=1, khit_interval=999, np_interval=999,
                           dropsync_interval=999, drift_interval=999,
                           seo_interval=999,
                           instructions_interval=999, once=False)


class DropsyncSeedingTests(SimpleTestCase):
    """The sync with bradas is counted from the LAST SUCCESSFUL one, not from process start.

    Incident 18–21.10.2026: the counter started 'now', and there were several redeploys a day, so in
    77 h there was not a single
    run: prices and stock levels of 307 dropshipping variants went stale, and the task heartbeat did
    not exist, so the Analyst stayed silent."""

    def _dropsync_calls_in_first_iteration(self, age):
        cmd = run_scheduler.Command()
        with mock.patch.object(cmd, '_dropsync_age', return_value=age), \
             mock.patch.object(cmd, '_run_cycle', return_value=True), \
             mock.patch.object(cmd, '_run_khit'), \
             mock.patch.object(cmd, '_run_np_warmup'), \
             mock.patch.object(cmd, '_run_dropsync') as dropsync, \
             mock.patch.object(cmd, '_run_drift'), \
             mock.patch.object(cmd, '_run_seo'), \
             mock.patch.object(cmd, '_run_instructions'), \
             mock.patch.object(run_scheduler.time, 'sleep', side_effect=_LoopDone):
            with self.assertRaises(_LoopDone):
                cmd.handle(interval=1, khit_interval=999, np_interval=999,
                           dropsync_interval=86400, drift_interval=999,
                           seo_interval=999, instructions_interval=999, once=False)
        return dropsync.call_count

    def test_overdue_sync_runs_right_after_start(self):
        self.assertEqual(self._dropsync_calls_in_first_iteration(3 * 86400), 1)

    def test_sync_exactly_one_interval_old_is_due(self):
        self.assertEqual(self._dropsync_calls_in_first_iteration(86400), 1)

    def test_fresh_sync_is_not_repeated_on_redeploy(self):
        self.assertEqual(self._dropsync_calls_in_first_iteration(3600), 0)

    def test_unknown_age_keeps_the_old_wait_of_one_interval(self):
        self.assertEqual(self._dropsync_calls_in_first_iteration(None), 0)


class DropsyncAgeTests(TestCase):
    """`_dropsync_age`: the freshest of the task heartbeat and last_synced_at of dropshipping variants."""

    def test_unknown_without_heartbeat_and_synced_variants(self):
        self.assertIsNone(run_scheduler.Command._dropsync_age())

    def test_heartbeat_gives_the_age(self):
        from analyst.models import JobHeartbeat
        JobHeartbeat.objects.create(name='sync_dropshipping', last_ok_at=timezone.now() - timedelta(hours=2))
        self.assertAlmostEqual(run_scheduler.Command._dropsync_age(), 7200, delta=30)

    def test_failed_beat_without_success_does_not_count(self):
        from analyst.models import JobHeartbeat
        JobHeartbeat.objects.create(name='sync_dropshipping', last_fail_at=timezone.now(), last_error='boom')
        self.assertIsNone(run_scheduler.Command._dropsync_age())

    def test_freshest_of_heartbeat_and_variant_sync_wins(self):
        from analyst.models import JobHeartbeat
        from catalog.models import Product, ProductVariant
        JobHeartbeat.objects.create(name='sync_dropshipping', last_ok_at=timezone.now() - timedelta(hours=30))
        product = Product.objects.create(name='Dropship')
        ProductVariant.objects.create(product=product, name='d', price=Decimal('10'), stock=3, is_dropshipping=True,
                                      last_synced_at=timezone.now() - timedelta(hours=5))
        ProductVariant.objects.create(product=product, name='own', price=Decimal('10'), stock=3,
                                      last_synced_at=timezone.now() - timedelta(minutes=1))   # not dropshipping — not counted
        self.assertAlmostEqual(run_scheduler.Command._dropsync_age(), 5 * 3600, delta=30)


def _item(ts: int, ident: str) -> dict:
    return {'id': ident, 'time': ts, 'amount': 100, 'description': 'From: Test'}


class MonoStatementPagingTests(SimpleTestCase):
    """The 500-operation limit must not silently swallow the oldest transactions."""

    FRM = datetime(2026, 7, 29, 0, 0, tzinfo=dt_timezone.utc)
    TO = datetime(2026, 8, 2, 0, 0, tzinfo=dt_timezone.utc)

    def test_short_page_returns_complete_without_extra_requests(self):
        page = [_item(1785000000, 'a'), _item(1784000000, 'b')]
        with mock.patch.object(monobank_personal, 'get_statement',
                               return_value=page) as gs:
            items, complete = monobank_personal.get_statement_paged(
                'acc', frm=self.FRM, to=self.TO)
        self.assertTrue(complete)
        self.assertEqual(len(items), 2)
        self.assertEqual(gs.call_count, 1)

    def test_full_page_triggers_backward_paging(self):
        base = int(self.TO.timestamp())
        full = [_item(base - i * 60, f'p1-{i}')
                for i in range(monobank_personal.MAX_STATEMENT_ITEMS)]
        tail = [_item(int(self.FRM.timestamp()) + 30, 'tail')]
        with mock.patch.object(monobank_personal, 'get_statement',
                               side_effect=[full, tail]) as gs:
            items, complete = monobank_personal.get_statement_paged(
                'acc', frm=self.FRM, to=self.TO)
        self.assertTrue(complete)
        self.assertEqual(gs.call_count, 2)
        # The tail that the old implementation would have lost forever is now in place.
        self.assertIn('tail', {it['id'] for it in items})
        # The second page narrows back from the oldest operation of the first.
        second_to = gs.call_args_list[1].kwargs['to']
        self.assertLess(second_to, self.TO)
        self.assertEqual(gs.call_args_list[1].kwargs['frm'], self.FRM)

    def test_incomplete_when_pages_exhausted(self):
        """If paging back failed — say so, rather than pretending success."""
        base = int(self.TO.timestamp())
        pages = [[_item(base - (p * 10_000) - i * 60, f'p{p}-{i}')
                  for i in range(monobank_personal.MAX_STATEMENT_ITEMS)]
                 for p in range(4)]
        with mock.patch.object(monobank_personal, 'get_statement', side_effect=pages):
            items, complete = monobank_personal.get_statement_paged(
                'acc', frm=self.FRM, to=self.TO, max_pages=4)
        self.assertFalse(complete)
        self.assertEqual(len(items), 4 * monobank_personal.MAX_STATEMENT_ITEMS)

    def test_overlapping_pages_are_deduped(self):
        base = int(self.TO.timestamp())
        full = [_item(base - i * 60, f'dup-{i}')
                for i in range(monobank_personal.MAX_STATEMENT_ITEMS)]
        # The second page returns a duplicate of the first page's last item.
        overlap = [full[-1], _item(int(self.FRM.timestamp()) + 30, 'fresh')]
        with mock.patch.object(monobank_personal, 'get_statement',
                               side_effect=[full, overlap]):
            items, complete = monobank_personal.get_statement_paged(
                'acc', frm=self.FRM, to=self.TO)
        self.assertTrue(complete)
        ids = [it['id'] for it in items]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(len(ids), monobank_personal.MAX_STATEMENT_ITEMS + 1)
