"""Seams between the Django part and the helper: the real queue client (hostops) against the real
helper, threaded execution, the night window."""
import json
import threading
import unittest
from datetime import datetime, timezone
from unittest import mock

from django.test import override_settings

from analyst import hostops
from analyst.codefix import auth
from analyst.tests_codefix_host import HEAD, KEY, PID, CodefixEnv


class ContractTests(CodefixEnv, unittest.TestCase):
    """The format of the request, signature, artifact and result used by both sides is checked on the real
modules."""

    def setUp(self):
        super().setUp()
        self.enterContext(override_settings(ANALYST_OPS_DIR=str(self.queue), ANALYST_CODEFIX_KEY_FILE=str(self.autofix / 'codefix.key')))
        self.enterContext(mock.patch.object(hostops, 'helper_alive', lambda *a: True))
        self.enterContext(mock.patch('analyst.codefix.auth.time.time', return_value=self.now.timestamp()))

    def test_a_job_submitted_by_hostops_is_accepted_and_answered_in_the_format_the_pipeline_reads(self):
        text = self.put_patch()
        hostops.put_artifact(PID, text)
        rid = hostops.submit_job('codefix_validate', {'patch_id': PID, 'patch_sha': auth.patch_sha(text)})
        request = json.loads((self.queue / 'requests' / f'{rid}.json').read_text(encoding='utf-8'))
        self.assertEqual(set(request), {'mode', 'action', 'params', 'id', 'auth'})
        self.helper.process_queue()
        result = hostops.job_result(rid)
        self.assertEqual((result['status'], result['id'], result['action'], result['verdict']), ('done', rid, 'codefix_validate', 'pass'))
        self.assertEqual(result['patch_sha'], auth.patch_sha(text))
        self.assertEqual(result['head'], HEAD)

    def test_the_status_published_by_the_helper_is_what_the_pipeline_checks(self):
        from analyst import codefix_policy
        from analyst.codefix import pipeline
        self.helper.write_status()
        status = hostops.status()
        self.assertEqual(status['policy_sha'], codefix_policy.policy_sha(), 'the host loaded the same policy as Radnyk')
        self.assertEqual(status['key_fp'], auth.key_fingerprint())
        with mock.patch.object(hostops, 'helper_alive', lambda *a: True):
            ready, why = pipeline.ops_ready()
        self.assertTrue(ready, why)

    def test_a_request_signed_with_another_key_is_refused_by_the_real_helper(self):
        self.enterContext(override_settings(ANALYST_CODEFIX_KEY_FILE=self._other_key()))
        text = self.put_patch()
        rid = hostops.submit_job('codefix_validate', {'patch_id': PID, 'patch_sha': auth.patch_sha(text)})
        self.helper.process_queue()
        result = hostops.job_result(rid)
        self.assertEqual(result['status'], 'failed')
        self.assertIn('signature is wrong', result['error'])

    def _other_key(self):
        path = self.autofix / 'other.key'
        path.write_bytes(b'z' * 64)
        return str(path)

    def test_the_artifact_written_by_hostops_has_exactly_the_bytes_the_helper_hashes(self):
        text = self.put_patch()
        hostops.put_artifact(PID, text)
        body, digest = self.cf.artifact(PID)
        self.assertEqual(body, text)
        self.assertEqual(digest, auth.patch_sha(text))


class ThreadedExecutionTests(CodefixEnv, unittest.TestCase):
    def test_a_second_job_while_the_first_runs_is_told_to_wait_and_the_first_finishes(self):
        helper = self.mod.Helper(self.queue, self.project, self.backups, runner=self.helper.runner, clock=lambda: self.now,
                                 disk=lambda path: self.helper_disk(path), log_dir=self.logs, autofix_dir=self.autofix, sync_jobs=False)
        cf = helper.codefix
        started, release = threading.Event(), threading.Event()

        def slow(job):
            started.set()
            release.wait(10)
            job.lines.append('done')
        cf._do_validate = slow
        self.helper, self.cf = helper, cf
        first = self.job('aaaaaaaa81', 'codefix_validate', patch_id=PID, patch_sha='0' * 64)
        self.assertEqual(first['status'], 'running')
        self.assertTrue(started.wait(5))
        second = self.job('aaaaaaaa82', 'codefix_validate', patch_id=PID, patch_sha='0' * 64)
        self.assertEqual((second['status'], second['code']), ('failed', 'wait'))
        release.set()
        for _ in range(100):
            done = json.loads((self.queue / 'results' / 'aaaaaaaa81.json').read_text(encoding='utf-8'))
            if done['status'] != 'running':
                break
            threading.Event().wait(0.05)
        self.assertEqual(done['status'], 'done')
        self.assertIsNone(cf._running)


class NightWindowTests(unittest.TestCase):
    def test_kyiv_hour_follows_summer_and_winter_time(self):
        import importlib.util
        import sys
        from analyst.tests_ops import HELPER_PATH
        try:
            from zoneinfo import ZoneInfo
            ZoneInfo('Europe/Kyiv')
        except Exception:
            self.skipTest('no time zone database')
        spec = importlib.util.spec_from_file_location('codefix_host_for_window', HELPER_PATH.with_name('codefix_host.py'))
        host = importlib.util.module_from_spec(spec)
        sys.modules['codefix_host_for_window'] = host
        spec.loader.exec_module(host)
        hour = host.Codefix._local_kyiv_hour
        self.assertEqual(hour(datetime(2026, 7, 1, 21, 30, tzinfo=timezone.utc)), 0)        # summer: UTC+3
        self.assertEqual(hour(datetime(2026, 12, 1, 22, 30, tzinfo=timezone.utc)), 0)       # winter: UTC+2
        self.assertEqual(hour(datetime(2026, 7, 1, 4, 0, tzinfo=timezone.utc)), 7)
