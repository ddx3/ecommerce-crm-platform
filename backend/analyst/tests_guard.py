"""Watchdog: frequent health checks and automatic server maintenance (ANALYST_AUTOFIX = ask | auto)."""
import io
import unittest
from datetime import datetime, timedelta, timezone as dt_timezone
from unittest import mock

from django.core.management import call_command
from django.test import TestCase, override_settings
from django.utils import timezone

from analyst import actions, guard, hostops, runner
from analyst.checks import Item
from analyst.models import ActionRun, Finding
from analyst.tests_ops import HELPER_PATH, HelperEnv

DISK = dict(key='disk:1', severity=Finding.CRITICAL, title='Disk is 94% full', check='disk_usage',
            section=Finding.SECTION_HEALTH)
BACKUP = dict(key='backup:stale', severity=Finding.CRITICAL, title='The last database backup is 8 days old',
              check='backup_freshness', section=Finding.SECTION_HEALTH)
SCHED = dict(key='scheduler:silent', severity=Finding.CRITICAL, title='The scheduler has been silent for 6 h',
             check='scheduler_heartbeat', section=Finding.SECTION_HEALTH)


def _outcome(*items):
    return runner.Outcome(items=[Item(**i) for i in items])


@unittest.skipUnless(HELPER_PATH.exists(), 'ops/shop_helper.py is not available')
class GuardTests(HelperEnv, TestCase):
    def setUp(self):
        super().setUp()
        self.enterContext(override_settings(ANALYST_OPS_DIR=str(self.queue), ANALYST_AUTOFIX='auto'))
        self.helper._clock = lambda: datetime.now(dt_timezone.utc)
        real_submit = hostops._submit

        def submit_and_process(payload):
            rid = real_submit(payload)
            self.helper.process_queue()
            return rid
        self.enterContext(mock.patch.object(hostops, '_submit', submit_and_process))
        self.helper.write_status()
        self.fake_backup_tools()

    def cycle(self, *items, now=None):
        """One watchdog cycle with stubbed checks (they return only the findings passed in)."""
        with mock.patch.object(guard, 'collect', return_value=_outcome(*items)):
            return guard.run_guard(now)

    # ── modes ──
    def test_ask_mode_only_reports(self):
        with override_settings(ANALYST_AUTOFIX='ask'):
            res = self.cycle(DISK)
        self.assertEqual((res.fresh, len(res.applied)), (1, 0))
        self.assertEqual(ActionRun.objects.count(), 0)
        self.assertEqual(self.destructive(), [])

    def test_invalid_mode_falls_back_to_ask(self):
        with override_settings(ANALYST_AUTOFIX='yolo'):
            self.assertEqual(guard.autofix_mode(), 'ask')
            self.cycle(DISK)
        self.assertEqual(ActionRun.objects.count(), 0)

    def test_auto_executes_reports_and_hides_buttons(self):
        res = self.cycle(DISK)
        run = ActionRun.objects.get()
        self.assertEqual((run.action, run.status, run.created_by), ('host_disk_cleanup', ActionRun.APPLIED, guard.AUTO_USER))
        self.assertEqual(len(res.applied), 1)
        self.assertTrue(any(c.startswith('docker builder prune') for c in self.commands()))
        card = Finding.objects.get(key=f'auto:{run.pk}')
        self.assertEqual((card.check_name, card.severity, card.status), (guard.CHECK_AUTO, Finding.INFO, Finding.NEW))
        self.assertIn('The Analyst did it itself', card.title)
        self.assertEqual(actions.fixers_for('disk_usage'), [], 'in auto mode there are no buttons')

    def test_auto_never_removes_rollback_images(self):
        self.cycle(DISK)
        run = ActionRun.objects.get()
        self.assertEqual(run.params['rollback_images'], 0)
        self.assertFalse([c for c in self.commands() if ' rmi ' in f' {c} ' and 'pre-' in c])

    def test_disk_below_critical_is_only_reported(self):
        self.cycle(dict(DISK, severity=Finding.WARN, title='Disk is 85% full'))
        self.assertEqual(ActionRun.objects.count(), 0)

    def test_backup_and_restart_run_in_the_same_cycle(self):
        self.cycle(BACKUP, SCHED)
        self.assertEqual({r.action for r in ActionRun.objects.all()}, {'host_backup_now', 'host_restart_scheduler'})

    def test_same_fix_is_done_once_per_cycle(self):
        second = dict(SCHED, key='job_failing:x', severity=Finding.WARN, title='Job x is failing')
        self.cycle(SCHED, second)
        self.assertEqual(ActionRun.objects.filter(action='host_restart_scheduler').count(), 1)

    # ── safeguards ──
    def test_owner_tick_is_respected(self):
        self.cycle(DISK)
        Finding.objects.filter(key='disk:1').update(status=Finding.DONE)
        ActionRun.objects.all().delete()                      # the owner closed it with ✓ before any Analyst actions
        self.cycle(DISK)
        self.assertEqual(ActionRun.objects.count(), 0)

    def test_no_repeat_inside_cooldown_then_stuck_card_instead_of_second_attempt(self):
        self.cycle(DISK)
        self.cycle(DISK)                                      # immediately: a pause, nothing new
        self.assertEqual(ActionRun.objects.count(), 1)
        self.assertFalse(Finding.objects.filter(check_name=guard.CHECK_STUCK).exists())
        res = self.cycle(DISK, now=timezone.now() + timedelta(hours=7))
        self.assertEqual(ActionRun.objects.count(), 1, 'the action must not be repeated in a loop')
        stuck = Finding.objects.get(check_name=guard.CHECK_STUCK)
        self.assertEqual((res.stuck, stuck.severity, stuck.status), (1, Finding.CRITICAL, Finding.NEW))
        self.assertIn('the problem did not go away', stuck.body)

    def test_stuck_card_outlives_the_24h_window_while_the_problem_lasts(self):
        self.cycle(DISK)
        self.cycle(DISK, now=timezone.now() + timedelta(hours=7))
        res = self.cycle(DISK, now=timezone.now() + timedelta(hours=30))
        self.assertEqual((res.stuck, ActionRun.objects.count()), (1, 1), 'the card stays, no new attempts')
        self.assertIsNone(Finding.objects.get(check_name=guard.CHECK_STUCK).resolved_at)

    def test_off_mode_shows_cards_without_buttons_or_actions(self):
        with override_settings(ANALYST_AUTOFIX='off'):
            self.cycle(DISK)
            self.assertEqual(actions.fixers_for('disk_usage'), [])
        self.assertEqual(ActionRun.objects.count(), 0)

    def test_stuck_card_closes_when_the_problem_goes_away(self):
        self.cycle(DISK)
        self.cycle(DISK, now=timezone.now() + timedelta(hours=7))
        self.cycle(now=timezone.now() + timedelta(hours=8))   # disk is back to normal
        self.assertIsNotNone(Finding.objects.get(check_name=guard.CHECK_STUCK).resolved_at)
        self.assertIsNotNone(Finding.objects.get(key='disk:1').resolved_at)

    def test_daily_cap_for_a_recurring_problem(self):
        for hours in (7, 9, 11):           # three restarts per day, each one long outside the pause
            ActionRun.objects.create(action='host_restart_scheduler', title='x', created_by=guard.AUTO_USER,
                                     status=ActionRun.APPLIED, created_at=timezone.now() - timedelta(hours=hours))
        res = self.cycle(SCHED)
        self.assertEqual(ActionRun.objects.filter(finding__isnull=False).count(), 0)
        self.assertEqual(res.stuck, 1)
        self.assertIn('in 24 hours', Finding.objects.get(check_name=guard.CHECK_STUCK).body)

    def test_helper_failure_is_reported_then_becomes_a_stuck_card(self):
        self.set('docker', 'compose', '-f', str(self.project / 'docker-compose.yml'), 'exec', '-T', 'db', 'sh',
                 code=1, err='connection refused')
        self.cycle(BACKUP)
        run = ActionRun.objects.get()
        self.assertEqual(run.status, ActionRun.FAILED)
        failed = Finding.objects.get(key=f'auto:{run.pk}')
        self.assertEqual((failed.severity, failed.check_name), (Finding.WARN, guard.CHECK_AUTO))
        self.assertIn('The Analyst could not run', failed.title)
        self.cycle(BACKUP, now=timezone.now() + timedelta(hours=7))
        self.assertEqual(ActionRun.objects.count(), 1)
        self.assertIn('failed', Finding.objects.get(check_name=guard.CHECK_STUCK).body)

    def test_read_only_queue_is_reported_not_crashed(self):
        """While compose mounts /ops read-only (ask mode), auto must not fail with OSError."""
        with mock.patch.object(hostops.Path, 'write_text', side_effect=OSError(30, 'Read-only file system')):
            res = self.cycle(DISK)
            with self.assertRaises(actions.PlanError) as ctx:
                actions.make_plan(Finding.objects.get(key='disk:1'), 'host_disk_cleanup', {}, 'boss@example.com')
        self.assertIn('read-only', str(ctx.exception))
        self.assertEqual((ActionRun.objects.count(), len(res.applied)), (0, 0))

    def test_helper_down_does_not_crash_or_spam_runs(self):
        for name in ('status.json', 'results/status.json'):
            (self.queue / name).unlink(missing_ok=True)
        res = self.cycle(DISK)
        self.assertEqual((ActionRun.objects.count(), len(res.applied)), (0, 0))

    def test_findings_marked_no_auto_are_only_shown(self):
        for item in (dict(SCHED, key='scheduler:no_heartbeat', severity=Finding.WARN, data={'no_auto': True}),
                     dict(SCHED, key='job_failing:x', severity=Finding.WARN, data={'no_auto': True}),
                     dict(BACKUP, key='backup:no_dir', severity=Finding.WARN, data={'no_auto': True})):
            self.cycle(item)
        self.assertEqual(ActionRun.objects.count(), 0)
        self.assertEqual(Finding.objects.filter(check_name=guard.CHECK_STUCK).count(), 0)

    def test_same_fix_for_another_finding_waits_out_the_cooldown(self):
        """Two different 'scheduler is silent' findings in a row count as one restart, not two
with a 5-minute pause."""
        self.cycle(SCHED)
        self.cycle(dict(SCHED, key='job_stale:other', severity=Finding.WARN, title='Job other is silent'))
        self.assertEqual(ActionRun.objects.filter(action='host_restart_scheduler').count(), 1)

    def test_automatic_actions_are_server_side_only(self):
        """Owner's principle: automation only for the technical side. The Analyst does not change shop data
itself."""
        self.assertTrue(actions.FIXERS)
        for key, fixer in actions.FIXERS.items():
            if fixer.auto:
                self.assertTrue(fixer.needs_ops, f'{key}: an automatic fix must go through the helper on the server')
                self.assertTrue(key.startswith('host_'), key)
        self.assertEqual({k for k, f in actions.FIXERS.items() if f.auto},
                         {'host_disk_cleanup', 'host_backup_now', 'host_restart_scheduler'})

    def test_real_checks_flag_what_automation_must_not_touch(self):
        from analyst.checks import health
        from analyst.models import JobHeartbeat
        now = timezone.now()
        with override_settings(DEBUG=False):
            [item] = health.scheduler_heartbeat(now)                      # the heartbeat table is empty
            self.assertEqual((item.key, item.data.get('no_auto')), ('scheduler:no_heartbeat', True))
            JobHeartbeat.objects.create(name='poll_monobank', last_ok_at=now, last_fail_at=now + timedelta(seconds=1),
                                        last_error='boom')
            items = health.scheduler_heartbeat(now + timedelta(minutes=1))
            self.assertEqual([(i.key, i.data.get('no_auto')) for i in items], [('job_failing:poll_monobank', True)])
        with override_settings(DEBUG=False, ANALYST_BACKUP_DIR='/no/such/dir'):
            [item] = health.backup_freshness(now)
            self.assertEqual((item.key, item.data.get('no_auto')), ('backup:no_dir', True))

    # ── what the watchdog must not touch ──
    def test_guard_does_not_close_cards_owned_by_the_daily_run(self):
        daily = Finding.objects.create(key='advice:agent_failed', check_name=runner.RUNNER_CHECK,
                                       section=Finding.SECTION_HEALTH, severity=Finding.WARN, title='The agent did not run')
        purchase = Finding.objects.create(key='purchase:var:1', check_name='purchase_advice',
                                          section=Finding.SECTION_PURCHASE, severity=Finding.WARN, title='Order')
        ssl = Finding.objects.create(key='ssl:expiring', check_name='ssl_expiry', section=Finding.SECTION_HEALTH,
                                     severity=Finding.WARN, title='SSL is expiring')
        self.cycle()
        for f in (daily, purchase, ssl):
            f.refresh_from_db()
            self.assertIsNone(f.resolved_at, f.title)

    def test_guard_closes_its_own_vanished_findings(self):
        self.cycle(dict(DISK, severity=Finding.WARN))
        self.cycle()
        self.assertIsNotNone(Finding.objects.get(key='disk:1').resolved_at)

    def test_auto_card_stays_until_owner_ticks_it(self):
        self.cycle(DISK)
        self.cycle()                                          # the problem disappeared
        card = Finding.objects.get(check_name=guard.CHECK_AUTO)
        self.assertIsNone(card.resolved_at)

    # ── command ──
    def test_management_command_runs_one_cycle(self):
        out = io.StringIO()
        with mock.patch.object(guard, 'collect', return_value=_outcome(DISK)):
            call_command('run_analyst', '--guard', stdout=out)
        self.assertIn('Mode: auto', out.getvalue())
        self.assertIn('executed 1', out.getvalue())
