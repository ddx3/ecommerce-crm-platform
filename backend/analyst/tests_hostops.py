"""Server actions from the Django side: channel → real ops/shop_helper.py (with stubbed commands)."""
import json
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings

from analyst import actions, hostops
from analyst.checks import health
from analyst.models import ActionRun, Finding
from analyst.tests_ops import HELPER_PATH, HelperEnv

OWNER = 'boss@example.com'


def _finding(check_name, **kw):
    defaults = dict(key=f'{check_name}:{Finding.objects.count()}', check_name=check_name,
                    section=Finding.SECTION_HEALTH, severity=Finding.CRITICAL, title='Finding', data={})
    defaults.update(kw)
    return Finding.objects.create(**defaults)


@unittest.skipUnless(HELPER_PATH.exists(), 'ops/shop_helper.py is not available')
class HostFixerTests(HelperEnv, TestCase):
    def setUp(self):
        super().setUp()
        self.enterContext(override_settings(ANALYST_OPS_DIR=str(self.queue)))
        self.helper._clock = lambda: datetime.now(timezone.utc)
        real_submit = hostops._submit

        def submit_and_process(payload):
            rid = real_submit(payload)
            self.helper.process_queue()           # the 'service' responds immediately
            return rid
        self.real_submit = real_submit
        self.enterContext(mock.patch.object(hostops, '_submit', submit_and_process))
        self.helper.write_status()

    # ── availability ──
    def test_buttons_exist_only_when_channel_is_configured(self):
        self.assertEqual({f.key for f in actions.fixers_for('disk_usage')}, {'host_disk_cleanup'})
        with override_settings(ANALYST_OPS_DIR=''):
            self.assertEqual(actions.fixers_for('disk_usage'), [])
            with self.assertRaises(actions.PlanError):
                actions.make_plan(_finding('disk_usage'), 'host_disk_cleanup', {}, OWNER)

    def test_auto_mode_has_no_buttons_but_keeps_the_automatic_actions(self):
        with override_settings(ANALYST_AUTOFIX='auto'):
            self.assertEqual(actions.fixers_for('disk_usage'), [])
            self.assertEqual({f.key for f in actions.auto_fixers_for('disk_usage')}, {'host_disk_cleanup'})
        with override_settings(ANALYST_AUTOFIX='auto', ANALYST_OPS_DIR=''):
            self.assertEqual(actions.auto_fixers_for('disk_usage'), [])

    # ── main scenarios ──
    def test_disk_cleanup_plan_then_apply(self):
        finding = _finding('disk_usage')
        run = actions.make_plan(finding, 'host_disk_cleanup', {}, OWNER)
        self.assertEqual(self.destructive(), [], 'the plan changes nothing on the server')
        self.assertFalse(run.reversible)
        self.assertTrue(run.params['host_plan_id'])
        self.assertTrue(all(c.get('host') for c in run.changes))
        self.assertTrue(any('I am not touching the rollback images' in c['text'] for c in run.changes))
        actions.apply_run(run)
        run.refresh_from_db()
        finding.refresh_from_db()
        self.assertEqual((run.status, finding.status), (ActionRun.APPLIED, Finding.DONE))
        self.assertTrue(any(c['text'].startswith('Result:') for c in run.changes))
        self.assertTrue(any(c.startswith('docker builder prune') for c in self.commands()))
        with self.assertRaises(actions.ApplyError):
            actions.revert_run(run)

    def test_rollback_images_are_opt_in_with_validated_params(self):
        finding = _finding('disk_usage')
        for bad in ({'rollback_images': 2}, {'keep_rollback': 0}, {'keep_rollback': 9}):
            with self.subTest(bad=bad), self.assertRaises(actions.PlanError):
                actions.make_plan(finding, 'host_disk_cleanup', bad, OWNER)
        run = actions.make_plan(finding, 'host_disk_cleanup', {}, OWNER)
        self.assertEqual(run.params['rollback_images'], 0)

    def test_backup_now(self):
        self.fake_backup_tools()
        run = actions.make_plan(_finding('backup_freshness'), 'host_backup_now', {}, OWNER)
        actions.apply_run(run)
        self.assertEqual(len(list(self.backups.glob('db_auto_*.dump'))), 1)

    def test_restart_scheduler(self):
        run = actions.make_plan(_finding('scheduler_heartbeat'), 'host_restart_scheduler', {}, OWNER)
        self.assertEqual(self.destructive(), [])
        actions.apply_run(run)
        self.assertIn(['docker', 'compose', '-f', str(self.project / 'docker-compose.yml'), 'restart', 'scheduler'],
                      self.calls)

    # ── failures ──
    def test_apply_failure_is_reported_and_marks_run_failed(self):
        self.set('docker', 'compose', '-f', str(self.project / 'docker-compose.yml'), 'exec', '-T', 'db', 'sh',
                 code=1, err='connection refused')
        run = actions.make_plan(_finding('backup_freshness'), 'host_backup_now', {}, OWNER)
        with self.assertRaises(actions.ApplyError) as ctx:
            actions.apply_run(run)
        self.assertIn('pg_dump', str(ctx.exception))
        run.refresh_from_db()
        self.assertEqual(run.status, ActionRun.FAILED)

    def test_helper_down_blocks_planning_with_clear_message(self):
        for name in ('status.json', 'results/status.json'):
            (self.queue / name).unlink(missing_ok=True)
        with self.assertRaises(actions.PlanError) as ctx:
            actions.make_plan(_finding('disk_usage'), 'host_disk_cleanup', {}, OWNER)
        self.assertIn('shop-helper', str(ctx.exception))

    def test_stale_heartbeat_counts_as_down(self):
        stale = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
        (self.queue / 'results' / 'status.json').write_text(json.dumps({'heartbeat': stale}), encoding='utf-8')
        with self.assertRaises(actions.PlanError):
            actions.make_plan(_finding('disk_usage'), 'host_disk_cleanup', {}, OWNER)

    def test_no_answer_in_time(self):
        self.enterContext(mock.patch.object(hostops, '_submit', lambda payload: 'abcdef0123456789'))
        self.enterContext(mock.patch.object(hostops, 'PLAN_WAIT', 0.05))
        self.enterContext(mock.patch.object(hostops, 'POLL', 0.01))
        with self.assertRaises(actions.PlanError) as ctx:
            actions.make_plan(_finding('disk_usage'), 'host_disk_cleanup', {}, OWNER)
        self.assertIn('did not respond', str(ctx.exception))

    def test_request_file_is_written_atomically_and_only_whitelisted_keys(self):
        seen = []

        def spy(payload):
            rid = self.real_submit(payload)
            seen.append(json.loads((self.queue / 'requests' / f'{rid}.json').read_text(encoding='utf-8')))
            self.helper.process_queue()
            return rid
        self.enterContext(mock.patch.object(hostops, '_submit', spy))
        actions.make_plan(_finding('disk_usage'), 'host_disk_cleanup', {}, OWNER)
        self.assertEqual(set(seen[0]), {'id', 'mode', 'action', 'params'})
        self.assertEqual(list((self.queue / 'requests').glob('*.tmp')), [])


@unittest.skipUnless(HELPER_PATH.exists(), 'ops/shop_helper.py is not available')
@override_settings(ANALYST_OWNER_EMAIL=OWNER)
class HostEndpointTests(HelperEnv, TestCase):
    def setUp(self):
        super().setUp()
        self.enterContext(override_settings(ANALYST_OPS_DIR=str(self.queue)))
        self.helper._clock = lambda: datetime.now(timezone.utc)
        real_submit = hostops._submit

        def submit_and_process(payload):
            rid = real_submit(payload)
            self.helper.process_queue()
            return rid
        self.enterContext(mock.patch.object(hostops, '_submit', submit_and_process))
        self.helper.write_status()
        self.owner = get_user_model().objects.create_user(email=OWNER, password='x', is_staff=True, is_superuser=True)
        self.client.force_login(self.owner)

    def _post(self, url, data):
        return self.client.post(url, data=json.dumps(data), content_type='application/json')

    def test_page_offers_server_buttons(self):
        _finding('disk_usage', title='Disk is 94% full')
        page = self.client.get('/admin/analyst/').content.decode()
        self.assertIn('data-fix="host_disk_cleanup"', page)

    def test_plan_all_skips_actions_that_need_a_decision(self):
        _finding('disk_usage')               # has parameters (rollback images) — not in bulk
        _finding('backup_freshness')
        data = self._post('/admin/analyst/fix/plan-all/', {}).json()
        self.assertEqual([r['title'] for r in data['runs']], ['Back up the database now'])

    def test_full_flow(self):
        finding = _finding('disk_usage')
        plan = self._post('/admin/analyst/fix/plan/', {'finding_id': finding.pk, 'action': 'host_disk_cleanup',
                                                       'params': {'rollback_images': 0}}).json()
        self.assertIn('Disk:', plan['changes'][0])
        res = self._post('/admin/analyst/fix/apply/', {'run_ids': [plan['run_id']]}).json()
        self.assertTrue(res['results'][0]['ok'], res)
        run = ActionRun.objects.get(pk=plan['run_id'])
        self.assertEqual((run.status, run.title), (ActionRun.APPLIED, 'Free up space on the server'))
        self.assertTrue(any(c['text'].startswith('Result:') for c in run.changes))

    def test_helper_error_is_422_for_the_ui(self):
        for name in ('status.json', 'results/status.json'):
            (self.queue / name).unlink(missing_ok=True)
        finding = _finding('disk_usage')
        resp = self._post('/admin/analyst/fix/plan/', {'finding_id': finding.pk, 'action': 'host_disk_cleanup'})
        self.assertEqual(resp.status_code, 422)
        self.assertIn('shop-helper', resp.json()['error'])


@override_settings(ANALYST_OPS_DIR='')
class OpsHelperHealthTests(TestCase):
    def test_disabled_without_channel(self):
        self.assertEqual(health.ops_helper(datetime.now(timezone.utc)), [])

    def test_states(self):
        import tempfile
        from pathlib import Path
        now = datetime.now(timezone.utc)
        with tempfile.TemporaryDirectory() as tmp, override_settings(ANALYST_OPS_DIR=tmp):
            status = Path(tmp) / 'status.json'
            self.assertEqual([i.key for i in health.ops_helper(now)], ['ops_helper:down'])       # no file
            status.write_text(json.dumps({'heartbeat': now.isoformat(),
                                          'last_auto_backup': {'ok': True, 'file': 'x'}}), encoding='utf-8')
            self.assertEqual(health.ops_helper(now), [])
            status.write_text(json.dumps({'heartbeat': (now - timedelta(minutes=30)).isoformat()}), encoding='utf-8')
            self.assertEqual([i.key for i in health.ops_helper(now)], ['ops_helper:down'])
            status.write_text(json.dumps({'heartbeat': now.isoformat(),
                                          'last_auto_backup': {'ok': False, 'error': 'disk full'}}), encoding='utf-8')
            items = health.ops_helper(now)
            self.assertEqual([i.key for i in items], ['ops_helper:autobackup_failed'])
            self.assertEqual(items[0].severity, Finding.CRITICAL)
