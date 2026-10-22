"""Safe handling of the queue by the helper (ops/safeio.py + ops/shop_helper.py): a container that can
write to requests/ and artifacts/
cannot make the root helper read other people's files, or write or delete them.
Links and FIFOs are checked only where the OS allows them (skipped on Windows without privileges —
on the VPS they run)."""
import importlib.util
import json
import os
import sys
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

from analyst.tests_ops import HELPER_PATH, HelperEnv

SAFEIO_PATH = HELPER_PATH.with_name('safeio.py')


def _load_safeio():
    spec = importlib.util.spec_from_file_location('safeio_under_test', SAFEIO_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules['safeio_under_test'] = module
    spec.loader.exec_module(module)
    return module


def can_symlink() -> bool:
    with tempfile.TemporaryDirectory() as tmp:
        try:
            os.symlink(Path(tmp) / 'a', Path(tmp) / 'b')
            return True
        except (OSError, NotImplementedError):
            return False


CAN_SYMLINK = can_symlink()
CAN_FIFO = hasattr(os, 'mkfifo')


@unittest.skipUnless(SAFEIO_PATH.exists(), 'ops/safeio.py is unavailable')
class SafeIOTests(unittest.TestCase):
    def setUp(self):
        self.io = _load_safeio()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def test_read_regular_reads_only_ordinary_files_of_bounded_size(self):
        (self.root / 'ok.txt').write_bytes(b'hello')
        (self.root / 'big.txt').write_bytes(b'x' * 100)
        self.assertEqual(self.io.read_regular(self.root / 'ok.txt', 10), b'hello')
        self.assertIsNone(self.io.read_regular(self.root / 'big.txt', 99))
        self.assertEqual(len(self.io.read_regular(self.root / 'big.txt', 100)), 100)
        self.assertIsNone(self.io.read_regular(self.root / 'missing', 10))
        self.assertIsNone(self.io.read_regular(self.root, 10), 'a directory is not a file')

    @unittest.skipUnless(CAN_SYMLINK, 'symbolic links are unavailable')
    def test_read_regular_does_not_follow_links(self):
        secret = self.root / 'secret.env'
        secret.write_text('DB_PASSWORD=hunter2')
        os.symlink(secret, self.root / 'patch.patch')
        self.assertIsNone(self.io.read_regular(self.root / 'patch.patch', 1000))

    @unittest.skipUnless(CAN_FIFO, 'FIFOs are unavailable')
    def test_read_regular_never_blocks_on_a_fifo(self):
        os.mkfifo(self.root / 'pipe.json')
        self.assertIsNone(self.io.read_regular(self.root / 'pipe.json', 1000))

    def test_write_atomic_replaces_and_leaves_no_temp_files(self):
        target = self.root / 'result.json'
        self.io.write_atomic(target, b'one')
        self.io.write_atomic(target, b'two')
        self.assertEqual(target.read_bytes(), b'two')
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), ['result.json'])

    @unittest.skipUnless(CAN_SYMLINK, 'symbolic links are unavailable')
    def test_write_atomic_replaces_a_link_instead_of_writing_through_it(self):
        victim = self.root / 'victim.txt'
        victim.write_text('ORIGINAL')
        os.symlink(victim, self.root / 'status.json')
        self.io.write_atomic(self.root / 'status.json', b'{"ok": true}')
        self.assertEqual(victim.read_text(), 'ORIGINAL', 'the link target file was not changed')
        self.assertFalse((self.root / 'status.json').is_symlink())
        self.assertEqual((self.root / 'status.json').read_bytes(), b'{"ok": true}')

    def test_scan_separates_files_from_junk(self):
        (self.root / 'a.json').write_text('{}')
        (self.root / 'b.txt').write_text('x')
        (self.root / '.hidden.json').write_text('{}')
        (self.root / 'sub').mkdir()
        regular, junk = self.io.scan(self.root, '.json')
        self.assertEqual((regular, junk), (['a.json'], []))

    @unittest.skipUnless(CAN_SYMLINK, 'symbolic links are unavailable')
    def test_scan_marks_links_as_junk_even_when_they_point_to_directories(self):
        os.symlink(self.root, self.root / 'loop.json')
        regular, junk = self.io.scan(self.root, '.json')
        self.assertEqual((regular, junk), ([], ['loop.json']))

    def test_prune_old_removes_stale_files_and_keeps_fresh_and_protected(self):
        old = self.root / 'old.json'
        fresh = self.root / 'fresh.json'
        status = self.root / 'status.json'
        for p in (old, fresh, status):
            p.write_text('{}')
        os.utime(old, (1, 1))
        os.utime(status, (1, 1))
        removed = self.io.prune_old(self.root, 1000, keep=('status.json',))
        self.assertEqual(removed, 1)
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), ['fresh.json', 'status.json'])

    @unittest.skipUnless(CAN_SYMLINK, 'symbolic links are unavailable')
    def test_prune_old_does_not_walk_into_a_linked_directory(self):
        outside = self.root / 'outside'
        outside.mkdir()
        (outside / 'precious.dump').write_text('data')
        os.utime(outside / 'precious.dump', (1, 1))
        queue = self.root / 'queue'
        queue.mkdir()
        os.symlink(outside, queue / 'results')
        self.assertEqual(self.io.prune_old(queue / 'results', 1000), 0, 'results is a link, not a directory')
        self.assertTrue((outside / 'precious.dump').exists())


@unittest.skipUnless(HELPER_PATH.exists(), 'ops/shop_helper.py is unavailable')
class QueueHardeningTests(HelperEnv, unittest.TestCase):
    def test_a_planted_link_is_removed_without_being_read(self):
        if not CAN_SYMLINK:
            self.skipTest('symbolic links are unavailable')
        victim = self.queue.parent / 'victim.env'
        victim.write_text('SECRET=1')
        link = self.queue / 'requests' / 'aaaaaaaa91.json'
        os.symlink(victim, link)
        self.helper.process_queue()
        self.assertFalse(os.path.lexists(link), 'the link was removed')
        self.assertEqual(victim.read_text(), 'SECRET=1')
        self.assertFalse((self.queue / 'results' / 'aaaaaaaa91.json').exists(), 'there is no reply to the link')

    def test_a_fifo_does_not_hang_the_loop(self):
        if not CAN_FIFO:
            self.skipTest('FIFOs are unavailable')
        fifo = self.queue / 'requests' / 'aaaaaaaa92.json'
        os.mkfifo(fifo)
        self.assertEqual(self.helper.process_queue(), 0)
        self.assertFalse(os.path.lexists(fifo))

    def test_an_oversized_request_is_refused(self):
        (self.queue / 'requests' / 'aaaaaaaa93.json').write_text('x' * (self.mod.MAX_REQUEST_BYTES + 1))
        self.helper.process_queue()
        result = json.loads((self.queue / 'results' / 'aaaaaaaa93.json').read_text(encoding='utf-8'))
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(self.calls, [])

    def test_a_request_id_is_handled_only_once(self):
        first = self.plan('aaaaaaaa94', 'backup_now')
        self.assertEqual(first['status'], 'planned')
        replay = self.queue / 'requests' / 'aaaaaaaa94.json'
        replay.write_text(json.dumps({'id': 'aaaaaaaa94', 'mode': 'plan', 'action': 'backup_now', 'params': {}}), encoding='utf-8')
        self.helper.process_queue()
        self.assertFalse(replay.exists(), 'the replay was removed')
        again = json.loads((self.queue / 'results' / 'aaaaaaaa94.json').read_text(encoding='utf-8'))
        self.assertEqual(again['created'], first['created'], 'the result was not overwritten')

    def test_the_helper_refuses_to_start_when_a_queue_directory_is_a_link(self):
        if not CAN_SYMLINK:
            self.skipTest('symbolic links are unavailable')
        import shutil
        shutil.rmtree(self.queue / 'results')
        elsewhere = self.queue.parent / 'elsewhere'
        elsewhere.mkdir()
        os.symlink(elsewhere, self.queue / 'results')
        with self.assertRaises(RuntimeError):
            self.mod.Helper(self.queue, self.project, self.backups, runner=self.helper.runner, clock=lambda: self.now,
                            log_dir=self.logs)

    def test_prune_ignores_a_linked_results_directory(self):
        if not CAN_SYMLINK:
            self.skipTest('symbolic links are unavailable')
        import shutil
        outside = self.queue.parent / 'outside'
        outside.mkdir()
        victim = outside / 'db_auto_20260101_0300.dump'
        victim.write_text('dump')
        stale = (self.now - timedelta(days=30)).timestamp()
        os.utime(victim, (stale, stale))
        shutil.rmtree(self.queue / 'results')
        os.symlink(outside, self.queue / 'results')
        self.helper.prune_queue()
        self.assertTrue(victim.exists(), 'the file outside the queue was not deleted')

    def test_stale_junk_in_requests_is_pruned(self):
        junk = self.queue / 'requests' / '.leftover.tmp'
        junk.write_text('x')
        stale = (self.now - timedelta(days=10)).timestamp()
        os.utime(junk, (stale, stale))
        self.helper.prune_queue()
        self.assertFalse(junk.exists())


@unittest.skipUnless(HELPER_PATH.exists(), 'ops/shop_helper.py is unavailable')
class BoundedRunnerTests(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location('shop_helper_for_runner', HELPER_PATH)
        self.mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.mod)

    def run_py(self, code, **kw):
        return self.mod.Runner().run([sys.executable, '-c', code], **kw)

    def test_output_is_bounded_and_the_process_is_killed_when_over_the_limit(self):
        result = self.run_py('import sys\nwhile True:\n    sys.stdout.write("x" * 65536)\n    sys.stdout.flush()',
                             timeout=30, max_output=2_000_000)
        self.assertTrue(result.over_limit)
        self.assertLessEqual(len(result.out), self.mod.TAIL_BYTES)

    def test_a_chatty_but_finite_process_keeps_the_tail_with_the_summary(self):
        code = 'import sys\nfor i in range(20000):\n    sys.stderr.write("line %d\\n" % i)\nsys.stderr.write("Ran 1 test in 0.1s\\n\\nOK\\n")'
        result = self.run_py(code, timeout=30, max_output=50_000_000)
        self.assertFalse(result.over_limit)
        self.assertTrue(result.err.rstrip().endswith('OK'))

    def test_timeout_is_reported(self):
        result = self.run_py('import time\ntime.sleep(60)', timeout=1, max_output=1_000_000)
        self.assertTrue(result.timed_out)
