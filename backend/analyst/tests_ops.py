"""VPS helper (ops/shop_helper.py): whitelist of actions, plan→apply protocol, backups.
External commands are stubbed — the test touches nothing on the machine."""
import importlib.util
import json
import os
import tempfile
import unittest
from collections import namedtuple
from datetime import datetime, timedelta, timezone
from pathlib import Path

from django.conf import settings

HELPER_PATH = Path(settings.BASE_DIR).parent / 'ops' / 'shop_helper.py'
Usage = namedtuple('Usage', 'total used free')
GB = 2 ** 30


def _load():
    spec = importlib.util.spec_from_file_location('shop_helper_under_test', HELPER_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class HelperEnv:
    """Scaffold: temporary folders, clock, disk, and a runner that records the commands called.
    A mixin so that both the helper's own tests and the Django-side channel tests can share it."""

    def setUp(self):
        super().setUp()
        self.mod = _load()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        self.queue, self.project, self.backups, self.logs = root / 'q', root / 'proj', root / 'bk', root / 'log'
        for p in (self.project, self.logs):
            p.mkdir()
        self.now = datetime(2026, 10, 19, 9, 0, tzinfo=timezone.utc)
        self.free = 4 * GB
        self.calls = []
        self.handlers = {}
        self.hour = 10
        runner = self

        class FakeRunner(self.mod.Runner):
            def run(self_inner, args, *, timeout=120, cwd=None, stdin=None, stdout=None, max_output=None):
                runner.calls.append(list(args))
                for prefix, handler in runner.handlers.items():
                    if tuple(args[:len(prefix)]) == prefix:
                        return handler(args, stdout)
                return self_inner._result(0)

            def _result(self_inner, code, out='', err=''):
                return runner.mod.Result(code, out, err)

        self.helper = self.mod.Helper(
            self.queue, self.project, self.backups, runner=FakeRunner(),
            clock=lambda: self.now, disk=lambda path: Usage(40 * GB, 40 * GB - self.free, self.free),
            log_dir=self.logs, local_hour=lambda dt: self.hour)

    # ── helpers ──
    def set(self, *prefix, out='', code=0, err='', effect=None):
        def handler(args, stdout):
            if effect:
                effect(args, stdout)
            return self.mod.Result(code, out, err)
        self.handlers[tuple(prefix)] = handler

    def submit(self, rid, **req):
        req.setdefault('id', rid)
        (self.queue / 'requests' / f'{rid}.json').write_text(json.dumps(req), encoding='utf-8')
        self.helper.process_queue()
        return json.loads((self.queue / 'results' / f'{rid}.json').read_text(encoding='utf-8'))

    def plan(self, rid, action, **params):
        return self.submit(rid, mode='plan', action=action, params=params)

    def apply(self, rid, plan_id, **extra):
        return self.submit(rid, mode='apply', plan_id=plan_id, **extra)

    def commands(self):
        return [' '.join(c) for c in self.calls]

    def destructive(self):
        words = ('prune', 'rmi', '--vacuum-size', 'restart', 'pg_dump')
        return [c for c in self.commands() if any(w in c for w in words)]

    def fake_backup_tools(self):
        """pg_dump writes a valid dump, pg_restore -l sees its contents."""
        def dump(args, stdout):
            stdout.write(b'PGDMP' + b'x' * 200_000)
        compose = ('docker', 'compose', '-f', str(self.project / 'docker-compose.yml'), 'exec', '-T', 'db')
        self.set(*compose, 'sh', effect=dump)
        self.set(*compose, 'pg_restore', out='\n'.join(f'; entry {i}' for i in range(40)))


@unittest.skipUnless(HELPER_PATH.exists(), 'ops/shop_helper.py is not available')
class HelperTestCase(HelperEnv, unittest.TestCase):
    pass


class ValidationTests(HelperTestCase):
    def test_params_schema(self):
        v = self.mod.validate_params
        self.assertEqual(v('disk_cleanup', {}), {'rollback_images': 0, 'keep_rollback': 2})
        self.assertEqual(v('disk_cleanup', {'rollback_images': 1, 'keep_rollback': 3}),
                         {'rollback_images': 1, 'keep_rollback': 3})
        self.assertEqual(v('restart_service', {'service': 'scheduler'}), {'service': 'scheduler'})
        for action, bad in (('disk_cleanup', {'rollback_images': 2}), ('disk_cleanup', {'keep_rollback': 0}),
                            ('disk_cleanup', {'keep_rollback': '2'}), ('disk_cleanup', {'rollback_images': True}),
                            ('disk_cleanup', {'extra': 1}), ('backup_now', {'x': 1}),
                            ('restart_service', {}), ('restart_service', {'service': 'web'}),
                            ('restart_service', {'service': 'scheduler; rm -rf /'}),
                            ('restart_service', {'service': ['scheduler']})):
            with self.subTest(action=action, bad=bad), self.assertRaises(self.mod.HelperError):
                v(action, bad)


class ProtocolTests(HelperTestCase):
    def test_plan_changes_nothing(self):
        self.set('docker', 'system', 'df', out=json.dumps(
            {'Type': 'Build Cache', 'Size': '9.8GB', 'Reclaimable': '3.8GB'}) + '\n')
        self.set('journalctl', '--disk-usage', out='Archived and active journals take up 786.9M in the file system.')
        result = self.plan('aaaaaaaa11', 'disk_cleanup')
        self.assertEqual(result['status'], 'planned')
        self.assertTrue(any('3.8GB' in line for line in result['lines']))
        self.assertTrue(any('786.9M' in line for line in result['lines']))
        self.assertEqual(self.destructive(), [], 'a plan must not run any state-changing commands')

    def test_unknown_action_and_bad_requests_are_refused_without_running_anything(self):
        for rid, req in (('aaaaaaaa01', {'mode': 'plan', 'action': 'rm_rf', 'params': {}}),
                         ('aaaaaaaa02', {'mode': 'bogus', 'action': 'backup_now'}),
                         ('aaaaaaaa03', {'mode': 'plan', 'action': 'restart_service',
                                         'params': {'service': 'scheduler; id'}}),
                         ('aaaaaaaa04', {'id': 'someone-else', 'mode': 'plan', 'action': 'backup_now'})):
            with self.subTest(rid=rid):
                result = self.submit(rid, **req)
                self.assertEqual(result['status'], 'failed')
                self.assertTrue(result['error'])
        self.assertEqual(self.calls, [])

    def test_garbage_files(self):
        req = self.queue / 'requests'
        (req / 'aaaaaaaa05.json').write_text('{not json', encoding='utf-8')
        (req / 'NOT-AN-ID.json').write_text('{}', encoding='utf-8')
        (req / '..evil.json').write_text('{}', encoding='utf-8')
        self.helper.process_queue()
        res = json.loads((self.queue / 'results' / 'aaaaaaaa05.json').read_text(encoding='utf-8'))
        self.assertEqual(res['status'], 'failed')
        self.assertEqual([p.name for p in req.glob('*.json')], ['..evil.json'],
                         'all requests were removed from the queue except the hidden file: the helper ignores it (prune_queue cleans it)')
        self.assertEqual(self.calls, [])

    def test_apply_needs_an_existing_fresh_unused_plan(self):
        self.assertEqual(self.apply('bbbbbbbb01', 'cccccccc01')['status'], 'failed')          # no plan
        self.assertEqual(self.apply('bbbbbbbb02', '../etc/passwd')['status'], 'failed')        # not an id
        self.assertEqual(self.apply('bbbbbbbb03', None)['status'], 'failed')
        plan = self.plan('dddddddd01', 'restart_service', service='scheduler')
        self.assertEqual(self.apply('bbbbbbbb04', plan['id'])['status'], 'done')
        again = self.apply('bbbbbbbb05', plan['id'])
        self.assertEqual((again['status'], 'already been applied' in again['error']), ('failed', True))

    def test_expired_plan_is_refused(self):
        plan = self.plan('dddddddd02', 'restart_service', service='scheduler')
        self.now += self.mod.PLAN_TTL + timedelta(minutes=1)
        result = self.apply('bbbbbbbb06', plan['id'])
        self.assertEqual(result['status'], 'failed')
        self.assertNotIn(['docker', 'compose', '-f', str(self.project / 'docker-compose.yml'),
                          'restart', 'scheduler'], self.calls)

    def test_apply_uses_the_plan_not_the_request(self):
        """Substituting the action/parameters in an apply request has no effect: only the stored plan is
executed."""
        plan = self.plan('dddddddd03', 'restart_service', service='scheduler')
        result = self.apply('bbbbbbbb07', plan['id'], action='restart_service', params={'service': 'analyst'})
        self.assertEqual(result['status'], 'done')
        restarts = [c for c in self.calls if 'restart' in c]
        self.assertEqual([c[-1] for c in restarts], ['scheduler'])

    def test_rate_limit_and_cooldown(self):
        for i in range(self.mod.MAX_APPLIES_PER_HOUR):
            p = self.plan(f'eeeeeeee{i:02d}', 'restart_service', service='scheduler')
            self.assertEqual(self.apply(f'ffffffff{i:02d}', p['id'])['status'], 'done', i)
            self.now += self.mod.COOLDOWN + timedelta(seconds=1)
        p = self.plan('eeeeeeee99', 'restart_service', service='scheduler')
        blocked = self.apply('ffffffff99', p['id'])
        self.assertEqual(blocked['status'], 'failed')
        self.assertIn('Too many', blocked['error'])

    def test_same_action_cooldown(self):
        p1 = self.plan('eeeeeeee50', 'restart_service', service='scheduler')
        self.apply('ffffffff50', p1['id'])
        p2 = self.plan('eeeeeeee51', 'restart_service', service='scheduler')
        blocked = self.apply('ffffffff51', p2['id'])
        self.assertEqual(blocked['status'], 'failed')
        self.assertIn('just been performed', blocked['error'])


class DiskCleanupTests(HelperTestCase):
    IMAGES = '\n'.join([
        'shop-web:latest\tsha256:web-live\t1.26GB\t2026-10-11 10:00:00 +0000 UTC',
        'shop-web:pre-release-20261004\tsha256:old-a\t4.6GB\t2026-10-03 10:00:00 +0000 UTC',
        'shop-scheduler:pre-release-20261004\tsha256:old-a2\t4.6GB\t2026-10-03 10:00:00 +0000 UTC',
        'shop-web:pre-bundles-20261004\tsha256:old-b\t4.6GB\t2026-10-04 12:00:00 +0000 UTC',
        'shop-web:pre-fix-20261015\tsha256:new-c\t1.2GB\t2026-10-15 09:00:00 +0000 UTC',
        'shop-web:pre-live-20260901\tsha256:in-use\t1.2GB\t2026-09-01 09:00:00 +0000 UTC',
        'postgres:18-alpine\tsha256:pg\t409MB\t2026-05-01 00:00:00 +0000 UTC',
        'shop-nginx:backup-thing\tsha256:odd\t70MB\t2026-10-01 00:00:00 +0000 UTC',
    ])

    def setUp(self):
        super().setUp()
        self.set('docker', 'images', '--no-trunc', out=self.IMAGES)
        self.set('docker', 'ps', '-a', '-q', out='c1\nc2\n')
        self.set('docker', 'inspect', '--format', '{{.Image}}', out='sha256:web-live\nsha256:in-use\n')
        self.tag_ids = {'shop-web:pre-release-20261004': 'sha256:old-a',
                        'shop-scheduler:pre-release-20261004': 'sha256:old-a2',
                        'shop-web:pre-bundles-20261004': 'sha256:old-b'}
        self.handlers[('docker', 'inspect', '--format', '{{.Id}}')] = \
            lambda args, stdout: self.mod.Result(0, self.tag_ids.get(args[-1], ''))

    def test_candidates_keep_newest_releases_and_used_images(self):
        tags = {c['tag'] for c in self.helper.rollback_candidates(keep=1)}
        # the newest release stays (pre-fix-20261015); in-use tags and non-rollback tags are not
        # touched
        self.assertEqual(tags, {'shop-web:pre-release-20261004', 'shop-scheduler:pre-release-20261004',
                                'shop-web:pre-bundles-20261004'})
        tags2 = {c['tag'] for c in self.helper.rollback_candidates(keep=2)}
        self.assertEqual(tags2, {'shop-web:pre-release-20261004', 'shop-scheduler:pre-release-20261004'})
        for never in ('shop-web:latest', 'postgres:18-alpine', 'shop-nginx:backup-thing', 'shop-web:pre-live-20260901'):
            self.assertNotIn(never, tags | tags2)

    def test_rollback_images_untouched_unless_asked(self):
        plan = self.plan('aaaaaaaa21', 'disk_cleanup')
        self.assertEqual(plan['data']['rollback_tags'], [])
        self.assertTrue(any('not touching' in line for line in plan['lines']))

    def test_apply_runs_only_whitelisted_steps_and_reports_freed_space(self):
        (self.logs / 'btmp').write_text('x' * 2_000_000)
        free_after = {'value': self.free}

        def prune(args, stdout):
            free_after['value'] += 3 * GB
        self.set('docker', 'builder', 'prune', out='Total:\t3.79GB', effect=prune)
        self.set('journalctl', '--vacuum-size=200M', out='Vacuuming done, freed 590.0M of archived journals')
        self.helper._disk = lambda path: Usage(40 * GB, 40 * GB - free_after['value'], free_after['value'])
        plan = self.plan('aaaaaaaa22', 'disk_cleanup', rollback_images=1, keep_rollback=1)
        self.assertEqual(len(plan['data']['rollback_tags']), 3)
        # Between the plan and apply the container started using image old-a — we do not delete it.
        self.set('docker', 'inspect', '--format', '{{.Image}}',
                 out='sha256:web-live\nsha256:in-use\nsha256:old-a\n')
        result = self.apply('bbbbbbbb22', plan['id'])
        self.assertEqual(result['status'], 'done', result)
        cmds = self.commands()
        self.assertTrue(any(c.startswith('docker builder prune -af --filter until=24h') for c in cmds))
        self.assertIn('journalctl --vacuum-size=200M', cmds)
        self.assertIn('docker image prune -f', cmds)
        rmi = [c for c in cmds if c.startswith('docker rmi')]
        self.assertEqual(sorted(rmi), sorted(f'docker rmi {t}' for t in plan['data']['rollback_tags']
                                             if t != 'shop-web:pre-release-20261004'),
                         'an image that became "used" between the plan and apply is skipped')
        self.assertEqual((self.logs / 'btmp').read_text(), '', 'btmp is cleared')
        self.assertTrue(any('Freed 3.0 GB' in line for line in result['lines']), result['lines'])
        self.assertEqual(result['freed_bytes'], 3 * GB)
        for forbidden in ('rm -rf', 'docker system prune', 'docker volume', 'rmi -f', '--volumes'):
            self.assertFalse(any(forbidden in c for c in cmds), forbidden)

    def test_failed_steps_are_reported_but_all_failing_raises(self):
        self.set('docker', 'builder', 'prune', code=1, err='boom')
        self.set('docker', 'image', 'prune', code=1, err='boom')
        self.set('journalctl', '--vacuum-size=200M', code=1, err='boom')
        plan = self.plan('aaaaaaaa23', 'disk_cleanup')
        result = self.apply('bbbbbbbb23', plan['id'])
        self.assertEqual(result['status'], 'failed')
        self.assertIn('No cleanup step succeeded', result['error'])


class BackupTests(HelperTestCase):
    def setUp(self):
        super().setUp()
        self.valid_listing = '\n'.join(f'; entry {i}' for i in range(40))

        def dump(args, stdout):
            stdout.write(b'PGDMP' + b'x' * 200_000)
        self.set('docker', 'compose', '-f', str(self.project / 'docker-compose.yml'), 'exec', '-T', 'db', 'sh',
                 effect=dump)
        self.set('docker', 'compose', '-f', str(self.project / 'docker-compose.yml'), 'exec', '-T', 'db',
                 'pg_restore', out=self.valid_listing)

    def _backup(self, rid='aaaaaaaa31'):
        plan = self.plan(rid, 'backup_now')
        return self.apply('bbbbbbbb' + rid[-2:], plan['id'])

    def test_backup_is_created_verified_and_atomic(self):
        result = self._backup()
        self.assertEqual(result['status'], 'done', result)
        files = list(self.backups.glob('db_auto_*.dump'))
        self.assertEqual(len(files), 1)
        self.assertEqual(files[0].name, result['file'])
        self.assertEqual(list(self.backups.glob('.*.part')), [], 'no temporary files are left')

    def test_tiny_dump_is_rejected_and_leaves_nothing(self):
        self.set('docker', 'compose', '-f', str(self.project / 'docker-compose.yml'), 'exec', '-T', 'db', 'sh',
                 effect=lambda a, out: out.write(b'oops'))
        result = self._backup()
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(list(self.backups.iterdir()), [])

    def test_unreadable_dump_is_rejected(self):
        self.set('docker', 'compose', '-f', str(self.project / 'docker-compose.yml'), 'exec', '-T', 'db',
                 'pg_restore', out='', code=1, err='bad archive')
        result = self._backup()
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(list(self.backups.iterdir()), [])

    def test_pg_dump_failure(self):
        self.set('docker', 'compose', '-f', str(self.project / 'docker-compose.yml'), 'exec', '-T', 'db', 'sh',
                 code=1, err='connection refused')
        self.assertEqual(self._backup()['status'], 'failed')
        self.assertEqual(list(self.backups.iterdir()), [])

    def test_refuses_when_disk_is_nearly_full(self):
        self.free = 1_000_000
        result = self._backup()
        self.assertEqual(result['status'], 'failed')
        self.assertIn('Not enough space', result['error'])

    def test_retention_prunes_only_old_auto_backups_and_keeps_minimum(self):
        self.backups.mkdir(exist_ok=True)
        old = (self.now - timedelta(days=30)).timestamp()
        names = [f'db_auto_2026090{i}_0300.dump' for i in range(1, 6)]
        for n in names:
            p = self.backups / n
            p.write_bytes(b'x' * 100)
            os.utime(p, (old, old))
        manual = self.backups / 'db_before_release_20261004.dump'
        manual.write_bytes(b'x' * 100)
        os.utime(manual, (old, old))
        removed = self.helper._prune_backups()
        self.assertEqual(removed, 2, 'of 5 old ones we keep a minimum of 3')
        self.assertTrue(manual.exists(), 'manual dumps are not touched')

    def test_auto_backup_schedule(self):
        self.hour = 2
        self.helper.maybe_auto_backup()
        self.assertEqual(list(self.backups.glob('db_auto_*.dump')), [], 'before 03:00 we do not back up')
        self.hour = 4
        self.helper.maybe_auto_backup()
        self.assertEqual(len(list(self.backups.glob('db_auto_*.dump'))), 1)
        self.assertTrue(self.helper.last_auto_backup['ok'])
        before = len(self.calls)
        self.helper.maybe_auto_backup()
        self.assertEqual(len(self.calls), before, 'a fresh backup exists, so we do nothing')

    def test_auto_backup_failure_is_recorded_and_retried_hourly(self):
        self.set('docker', 'compose', '-f', str(self.project / 'docker-compose.yml'), 'exec', '-T', 'db', 'sh',
                 code=1, err='down')
        self.helper.maybe_auto_backup()
        self.assertFalse(self.helper.last_auto_backup['ok'])
        calls = len(self.calls)
        self.now += timedelta(minutes=30)
        self.helper.maybe_auto_backup()
        self.assertEqual(len(self.calls), calls, 'no retry earlier than after an hour')
        self.now += timedelta(minutes=45)
        self.helper.maybe_auto_backup()
        self.assertGreater(len(self.calls), calls)


class RestartAndStatusTests(HelperTestCase):
    def test_restart_command_is_exact(self):
        plan = self.plan('aaaaaaaa41', 'restart_service', service='analyst')
        self.assertEqual(self.destructive(), [])
        self.apply('bbbbbbbb41', plan['id'])
        self.assertIn(['docker', 'compose', '-f', str(self.project / 'docker-compose.yml'), 'restart', 'analyst'],
                      self.calls)

    def test_status_heartbeat(self):
        self.helper.write_status()
        status = json.loads((self.queue / 'results' / 'status.json').read_text(encoding='utf-8'))
        self.assertEqual(status['heartbeat'], self.now.isoformat())
        legacy = json.loads((self.queue / 'status.json').read_text(encoding='utf-8'))
        self.assertEqual(legacy['heartbeat'], status['heartbeat'], 'the old location stays for the duration of the transition')

    def test_status_survives_pruning(self):
        self.helper.write_status()
        stale = (self.now - timedelta(days=10)).timestamp()
        os.utime(self.queue / 'results' / 'status.json', (stale, stale))
        self.helper.prune_queue()
        self.assertTrue((self.queue / 'results' / 'status.json').exists())

    def test_old_results_are_pruned(self):
        old = self.queue / 'results' / 'old.json'
        old.write_text('{}')
        stale = (self.now - timedelta(days=10)).timestamp()
        os.utime(old, (stale, stale))
        self.helper.prune_queue()
        self.assertFalse(old.exists())
