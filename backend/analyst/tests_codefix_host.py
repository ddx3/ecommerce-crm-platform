"""Auto-fix on the host (ops/codefix_host.py): job signing, sandbox, verification, rollout, rollback,
recovery.
Git and docker are replaced by a “world” that REPRODUCES what matters for the conclusions (applies
patches to real files, serves output
by the mounted tree and labels, records the contents of the build layer) — the test touches nothing
on the machine; behavior with real docker
is verified on the VPS."""
import hashlib
import io
import json
import sys
import tarfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest import mock

from analyst.tests_codefix_policy import TESTS, VIEWS, make_patch
from analyst.tests_ops import GB, HELPER_PATH, HelperEnv

PID = 'abcdef0123'
HEAD = 'a' * 40
NEW_HEAD = 'b' * 40
KEY = b'k' * 64
OLD_VIEWS = 'def f(x):\n    return x + 1\n'
NEW_VIEWS = 'def f(x):\n    return (x or 0) + 1\n'
CANARY = 'backend/core/canary.py'
TREE = {VIEWS: OLD_VIEWS, 'backend/catalog/tests.py': 'y = 2\n', 'backend/manage.py': 'pass\n', CANARY: 'def g(x):\n    return x + 1\n',
        'backend/orders/tests.py': 'z = 3\n'}
NEW_TEST = 'catalog.tests_autofix.T.test'
FAIL_1 = (f'======\nFAIL: test ({NEW_TEST})\n------\nTraceback (most recent call last):\nAssertionError: boom\n\n'
          '------\nRan 1 test in 0.100s\n\nFAILED (failures=1)\n')
OK_1 = 'Ran 1 test in 0.100s\n\nOK\n'
OK_120 = 'Ran 120 tests in 9.000s\n\nOK\n'
IMAGE = {svc: 'sha256:' + str(i) * 64 for i, svc in enumerate(('web', 'scheduler', 'analyst'), 1)}
NEW_IMAGE = 'sha256:' + 'f' * 64


class World:
    """Server stub: git, docker, compose. Records the commands and answers according to the scenario."""

    def __init__(self, mod, policy, project, work):
        self.mod, self.policy, self.project, self.work = mod, policy, Path(project), Path(work)
        self.head, self.dirty = HEAD, ''
        self.tree = dict(TREE)
        self.cmds = []
        self.fail = {}
        self.out = {'a': (1, FAIL_1), 'b': (0, OK_1), 'c': (0, OK_120), 'd': (0, OK_1)}
        self.flags = {}                               # tag → {'timed_out': True, 'over_limit': True}
        self.states = 'web running\nscheduler running\nanalyst running\n'
        self.logs = ''
        self.broken_until_reset = False
        self.broken_always = False
        self.docker_up = True
        self.oom = False
        self.runs = []                                # (tag, snapshot tree, labels, arguments)
        self.builds = []                              # (Dockerfile, {path: contents})
        self.created = set()
        self.tags = {f'shop-{svc}:latest': image for svc, image in IMAGE.items()}
        self.container = dict(IMAGE)
        self.bad_states = ''
        self.bad_states_until_reset = False
        self.reset_keeps_untracked = False
        self.image_files = {rel[len('backend/'):]: text for rel, text in TREE.items()}
        self.log1 = (HEAD, 'autofix abcdef0123: fix')
        self.leaked = []
        self.up_calls = 0
        self.materialize()

    def R(self, code=0, out='', err='', **flags):
        return self.mod.Result(code, out, err, **flags)

    def materialize(self):
        for rel, text in self.tree.items():
            path = self.project / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding='utf-8', newline='\n')

    def __call__(self, args, cwd=None, stdout=None, max_output=None):
        a = list(args)
        self.cmds.append((a, cwd))
        if a[0] == 'git':
            return self.git(a, cwd)
        if a[0] == 'docker':
            return self.docker(a)
        return self.R()

    # ── git ──
    def git(self, a, cwd):
        sub = [x for x in a[1:] if not x.startswith('user.') and x not in ('-c',)]
        cmd = sub[0]
        if cmd == 'rev-parse':
            return self.R(out=self.head + '\n')
        if cmd == 'status':
            return self.R(out=self.dirty)
        if cmd == 'show':
            rel = sub[1].split(':', 1)[1]
            if rel in self.tree:
                return self.R(out=self.tree[rel])
            return self.R(128, err='fatal: path does not exist')
        if cmd == 'archive':
            path = a[a.index('-o') + 1]
            with tarfile.open(path, 'w') as tf:
                for name, text in self.tree.items():
                    data = text.encode()
                    info = tarfile.TarInfo(name)
                    info.size = len(data)
                    tf.addfile(info, io.BytesIO(data))
            return self.R()
        if cmd == 'apply':
            return self.apply(a, cwd)
        if cmd == 'commit':
            if self.fail.get('commit'):
                return self.R(1, err='nothing to commit')
            self.head = NEW_HEAD
            return self.R()
        if cmd == 'revert':
            if self.fail.get('revert'):
                return self.R(1, err='conflict')
            self.head = 'c' * 40
            return self.R()
        if cmd == 'reset':
            if self.fail.get('reset'):
                return self.R(1, err='index.lock')
            self.head = sub[-1]
            self.broken_until_reset = False
            self.bad_states_until_reset = False
            self.logs = ''
            if not self.reset_keeps_untracked:
                for rel in self.created:
                    (self.project / rel).unlink(missing_ok=True)
                self.created.clear()
            self.materialize()
            return self.R()
        if cmd == 'diff-tree':
            return self.R(out=f'{VIEWS}\n{TESTS}\nREADME.md\n')
        if cmd == 'clean':
            for rel in sub[sub.index('--') + 1:]:
                if rel in self.created:
                    (self.project / rel).unlink(missing_ok=True)
                    self.created.discard(rel)
            return self.R()
        if cmd == 'log':
            if '-1' in sub:
                return self.R(out=f'{self.log1[0]}\n{self.log1[1]}\n')
            return self.R(out='autofix abcdef0123: fix\nsome other commit\n')
        return self.R()

    def apply(self, a, cwd):
        if self.fail.get('apply'):
            return self.R(1, err='error: patch failed')
        text = Path(a[-1]).read_text(encoding='utf-8')
        check = '--check' in a
        base = Path(cwd)
        for change in self.policy.parse_patch(text):
            target = base / change.path
            if change.is_new and target.exists():
                return self.R(1, err=f'error: {change.path}: already exists in working directory')
            old = target.read_text(encoding='utf-8') if target.exists() else ''
            try:
                new = self.policy.apply_hunks(old, change.hunks)
            except self.policy.PolicyViolation:
                return self.R(1, err='error: patch does not apply')
            if not check:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(new, encoding='utf-8', newline='\n')
                if base == self.project and change.is_new:
                    self.created.add(change.path)
        if not check and base == self.project:
            self.applied_with_marker = (self.work / 'inflight.json').exists()
        return self.R()

    # ── docker ──
    def snapshot(self, tree: Path) -> dict:
        root = tree / 'backend'
        return {p.relative_to(root).as_posix(): p.read_text(encoding='utf-8') for p in root.rglob('*.py')} if root.exists() else {}

    def docker(self, a):
        if a[1] == 'compose':
            return self.compose(a)
        sub = a[1]
        if sub == 'version':
            return self.R(0 if self.docker_up else 1)
        if sub == 'run':
            return self.run(a)
        if sub == 'exec':
            return self.R(self.fail.get('pg_ready', 0), err='no pg' if self.fail.get('pg_ready') else '')
        if sub == 'inspect':
            fmt = a[a.index('--format') + 1]
            if 'OOMKilled' in fmt:
                return self.R(out='true' if self.oom else 'false')
            svc = a[-1][2:]
            return self.R(out=f'true {self.container[svc]}')
        if sub == 'image' and a[2] == 'inspect':
            return self.R(out=self.tags[a[-1]])
        if sub == 'tag':
            if self.fail.get('tag'):
                return self.R(1, err='no such image')
            source = a[2] if a[2].startswith('sha256:') else self.tags.get(a[2])
            if source is None:
                return self.R(1, err='Error response from daemon: No such image: ' + a[2])
            self.tags[a[3]] = source
            return self.R()
        if sub == 'build':
            if self.fail.get('build'):
                return self.R(1, err='build error')
            ctx = Path(a[-1])
            files = {p.relative_to(ctx / 'files').as_posix(): p.read_text(encoding='utf-8')
                     for p in (ctx / 'files').rglob('*') if p.is_file()}
            self.builds.append(((ctx / 'Dockerfile').read_text(encoding='utf-8'), files, a))
            self.tags[a[a.index('-t') + 1]] = NEW_IMAGE
            return self.R()
        if sub == 'ps':
            return self.R(out='\n'.join(self.leaked))
        if sub == 'images':
            return self.R(out='\n'.join(f'shop-{s}:pre-autofix-2026100{n}120000' for s in ('web', 'scheduler', 'analyst') for n in range(1, 6)))
        if sub == 'rm':
            return self.R()
        if sub == 'logs':
            return self.R(out='postgres log')
        return self.R()

    def run(self, a):
        joined = ' '.join(a)
        if '-d' in a[:3]:                                   # temporary database
            return self.R(self.fail.get('pg', 0), err='cannot start' if self.fail.get('pg') else '')
        entry = a[a.index('--entrypoint') + 1]
        if entry == 'cat':
            rel = a[-1][len('/app/'):]
            text = self.image_files.get(rel)
            return self.R(0, text) if text is not None else self.R(1, err='no such file')
        name = a[a.index('--name') + 1]
        tag = name.rsplit('-', 1)[1]
        mount = a[a.index('-v') + 1].split(':/app')[0]
        tree = Path(mount[:-len('/backend')])
        labels = a[a.index('--noinput') + 1:]
        self.runs.append((tag, self.snapshot(tree), labels, a))
        code, err = self.out[tag](tree, labels) if callable(self.out[tag]) else self.out[tag]
        return self.R(code, '', err, **self.flags.get(tag, {}))

    def compose(self, a):
        sub = a[4]
        if sub == 'ps':
            if '-q' in a:
                return self.R(out=f'c-{a[-1]}\n')
            return self.R(out=self.bad_states if self.bad_states_until_reset else self.states)
        if sub == 'logs':
            return self.R(out=self.logs)
        if sub == 'up':
            self.up_calls += 1
            if self.fail.get('up') or (self.fail.get('up_once') and self.up_calls == 1):
                return self.R(1, err='up error')
            for svc in ('web', 'scheduler', 'analyst'):
                if svc in a:
                    self.container[svc] = self.tags[f'shop-{svc}:latest']
            return self.R()
        return self.R()

    def lines(self):
        return [' '.join(str(x) for x in a) for a, _ in self.cmds]

    def index(self, fragment, start=0):
        return next(i for i, line in enumerate(self.lines()) if i >= start and fragment in line)

    def count(self, fragment):
        return sum(1 for line in self.lines() if fragment in line)


@unittest.skipUnless(HELPER_PATH.exists(), 'ops/shop_helper.py is not available')
class CodefixEnv(HelperEnv):
    def setUp(self):
        super().setUp()
        self.autofix = Path(self._tmp.name) / 'af'
        self.autofix.mkdir()
        (self.autofix / 'codefix.key').write_bytes(KEY)
        self.world = None
        outer = self

        class WorldRunner(self.mod.Runner):
            def run(self_inner, args, *, timeout=120, cwd=None, stdin=None, stdout=None, max_output=None):
                return outer.world(args, cwd=cwd, stdout=stdout, max_output=max_output)

        self.helper = self.mod.Helper(
            self.queue, self.project, self.backups, runner=WorldRunner(), clock=lambda: self.now,
            disk=lambda path: self.helper_disk(path), log_dir=self.logs, local_hour=lambda dt: self.hour,
            autofix_dir=self.autofix, site_url='https://shop.test', sync_jobs=True)
        self.cf = self.helper.codefix
        self.cfmod = sys.modules[type(self.cf).__module__]
        self.world = World(self.mod, self.cf.policy, self.project, self.autofix)
        self.clock = {'t': 0.0}
        self.kyiv_hour = 3
        self.http = {}
        self.cf._mono = lambda: self.clock['t']
        self.cf._sleep = lambda s: self.clock.__setitem__('t', self.clock['t'] + s)
        self.cf._meminfo = lambda: 4 * GB
        self.cf._kyiv_hour = lambda dt: self.kyiv_hour
        self.cf._http = self.fake_http

        def fake_backup(wait=True):
            self.world.cmds.append((['BACKUP'], None))
            return Path('db_auto_test.dump')
        self.backup = mock.MagicMock(side_effect=fake_backup)
        self.enterContext(mock.patch.object(self.helper, '_make_backup', self.backup))

    def helper_disk(self, path):
        from analyst.tests_ops import Usage
        return Usage(40 * GB, 40 * GB - self.free, self.free)

    def fake_http(self, url, timeout=10):
        path = url.replace('https://shop.test', '')
        if self.world.broken_until_reset or self.world.broken_always:
            return 500
        return self.http.get(path, 200)

    def put_patch(self, pid=PID, **kw):
        text = make_patch(**kw)
        (self.queue / 'artifacts').mkdir(parents=True, exist_ok=True)
        (self.queue / 'artifacts' / f'{pid}.patch').write_text(text, encoding='utf-8', newline='\n')
        return text

    def job(self, rid, action, auth=True, **params):
        req = {'mode': 'job', 'action': action, 'params': params}
        if auth:
            exp = int(self.now.timestamp()) + 600
            req['auth'] = {'exp': exp, 'mac': self.cfmod.sign(KEY, rid, action, params, exp)}
        return self.submit(rid, **req)

    def sha(self, pid=PID):
        """sha256 of the artifact in the queue — what the analyst puts in the signed request; zeros for a
missing file."""
        path = self.queue / 'artifacts' / f'{pid}.patch'
        return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else '0' * 64

    def validate(self, rid='cccccccc01', pid=PID):
        return self.job(rid, 'codefix_validate', patch_id=pid, patch_sha=self.sha(pid))

    def deploy(self, rid='dddddddd01', pid=PID, **extra):
        params = {'patch_id': pid, 'patch_sha': self.sha(pid), 'base_sha': HEAD, 'summary': 'Fixed the empty price'}
        params.update(extra)
        return self.job(rid, 'codefix_deploy', **params)

    def state(self):
        return json.loads((self.autofix / 'state.json').read_text(encoding='utf-8'))

    def ready(self):
        self.put_patch()
        self.assertEqual(self.validate()['verdict'], 'pass')
        self.world.cmds.clear()


class ParseTests(unittest.TestCase):
    def setUp(self):
        import importlib.util
        path = HELPER_PATH.with_name('codefix_host.py')
        spec = importlib.util.spec_from_file_location('codefix_host_for_parse', path)
        self.mod = importlib.util.module_from_spec(spec)
        sys.modules['codefix_host_for_parse'] = self.mod
        spec.loader.exec_module(self.mod)

    def test_a_failing_run_is_parsed(self):
        parsed = self.mod.parse_test_output(FAIL_1 + '\nERROR: setUpClass (catalog.tests.Big)\n'.replace('\nERROR', 'ERROR'))
        self.assertEqual(parsed['ran'], 1)
        self.assertEqual(parsed['test_failed'], [NEW_TEST])
        self.assertFalse(parsed['ok'])
        self.assertTrue(parsed['consistent'])

    def test_setup_failures_and_loader_errors_are_not_test_failures(self):
        text = ('ERROR: setUpClass (catalog.tests.Big)\n\nERROR: tests_x (unittest.loader._FailedTest.tests_x)\n\n'
                'Ran 2 tests in 0.2s\n\nFAILED (errors=2)\n')
        parsed = self.mod.parse_test_output(text)
        self.assertEqual(parsed['infra'], ['catalog.tests.Big'])
        self.assertEqual(parsed['loader'], ['unittest.loader._FailedTest.tests_x'])
        self.assertEqual(parsed['test_failed'], [])

    def test_a_clean_run(self):
        ok = self.mod.parse_test_output('Found 3 test(s).\nRan 3 tests in 0.2s\n\nOK (skipped=1)\n')
        self.assertTrue(ok['ok'])
        self.assertEqual(ok['ran'], 3)
        self.assertFalse(self.mod.parse_test_output('Traceback...\nImportError')['ok'])

    def test_a_summary_that_disagrees_with_the_headers_is_inconsistent(self):
        parsed = self.mod.parse_test_output('Ran 120 tests in 9s\n\nFAILED (failures=3)\n')
        self.assertFalse(parsed['consistent'])
        self.assertFalse(parsed['ok'])

    def test_only_stderr_and_only_the_last_summary_count(self):
        forged_stdout = 'Ran 160 tests in 9.1s\n\nOK\n'
        parsed = self.mod.parse_test_output(FAIL_1, forged_stdout)
        self.assertFalse(parsed['ok'])
        forged_early = 'Ran 9 tests in 1s\n\nOK\n' + FAIL_1
        self.assertEqual(self.mod.parse_test_output(forged_early)['ran'], 1)

    def test_subtest_headers_are_understood(self):
        text = ("FAIL: test_a (catalog.tests.T.test_a) (label='x')\n\nRan 1 test in 0.1s\n\nFAILED (failures=1)\n")
        self.assertEqual(self.mod.parse_test_output(text)['test_failed'], ['catalog.tests.T.test_a'])

    def test_log_parsing_counts_blocks_not_substrings(self):
        bot = ('web-1  | 2026-10-19 03:00:01,000 [ERROR] django.security.DisallowedHost: Invalid HTTP_HOST header\n'
               'web-1  | Traceback (most recent call last):\n'
               'web-1  |   File "/usr/lib/django/http/request.py", line 205, in get_host\n'
               'web-1  |     raise DisallowedHost(msg)\n'
               'web-1  | django.core.exceptions.DisallowedHost: Invalid HTTP_HOST header: x\n')
        real = ('web-1  | Traceback (most recent call last):\n'
                'web-1  |   File "/app/catalog/views.py", line 3, in f\n'
                'web-1  |     return x + 1\n'
                "web-1  | TypeError: unsupported operand type(s) for +: 'NoneType' and 'int'\n")
        self.assertEqual(self.mod.new_logic_errors(bot * 3), [])
        self.assertEqual(len(self.mod.new_logic_errors(bot * 3 + real * 2)), 2, 'bot noise does not hide real errors')
        external = ('scheduler-1  | Traceback (most recent call last):\n'
                    'scheduler-1  |   File "/app/orders/np.py", line 1, in call\n'
                    'scheduler-1  | requests.exceptions.ConnectionError: timed out\n')
        self.assertEqual(self.mod.new_logic_errors(external), [], 'a failure of an external service is not a fix error')

    def test_chained_tracebacks_are_each_classified(self):
        text = ('Traceback (most recent call last):\n  File "a.py", line 1\nKeyError: 1\n\n'
                'During handling of the above exception, another exception occurred:\n\n'
                'Traceback (most recent call last):\n  File "b.py", line 2\nValueError: bad\n')
        self.assertEqual(len(self.mod.new_logic_errors(text)), 2)

    def test_summary_is_made_safe_for_commit_messages(self):
        self.assertEqual(self.mod.sanitize_summary('a\nb\x00c  d'), 'a b c d')
        self.assertEqual(len(self.mod.sanitize_summary('é' * 500)), 160)


class AuthTests(CodefixEnv, unittest.TestCase):
    def test_a_signed_request_is_accepted(self):
        self.put_patch()
        result = self.validate()
        self.assertEqual(result['status'], 'done')

    def test_requests_without_a_valid_signature_run_nothing(self):
        self.put_patch()
        self.assertIn('is unsigned', self.job('eeeeeeee01', 'codefix_validate', auth=False, patch_id=PID)['error'])
        exp = int(self.now.timestamp()) + 600
        good = self.cfmod.sign(KEY, 'eeeeeeee02', 'codefix_validate', {'patch_id': PID}, exp)
        for rid, auth, params, why in (
                ('eeeeeeee02', {'exp': exp, 'mac': good[:-1] + ('0' if good[-1] != '0' else '1')}, {'patch_id': PID}, 'is wrong'),
                ('eeeeeeee03', {'exp': exp, 'mac': good}, {'patch_id': PID}, 'is wrong'),             # signature of someone else's request
                ('eeeeeeee02', {'exp': exp, 'mac': good}, {'patch_id': 'abcdef9999'}, 'is wrong'),    # other parameters
                ('eeeeeeee04', {'exp': int(self.now.timestamp()) - 5, 'mac': good}, {'patch_id': PID}, 'has expired'),
                ('eeeeeeee05', {'exp': exp + 99999, 'mac': good}, {'patch_id': PID}, 'invalid time'),
                ('eeeeeeee06', {'exp': 'x', 'mac': good}, {'patch_id': PID}, 'is unsigned'),
                ('eeeeeeee07', 'mac', {'patch_id': PID}, 'is unsigned')):
            with self.subTest(rid=rid, why=why):
                result = self.submit(rid, mode='job', action='codefix_validate', params=params, auth=auth)
                self.assertEqual(result['status'], 'failed')
                self.assertIn(why, result['error'])
        self.assertEqual(self.world.runs, [])
        self.assertEqual(self.world.count('git archive'), 0)

    def test_without_a_key_on_the_server_nothing_runs_even_when_signed(self):
        (self.autofix / 'codefix.key').unlink()
        helper = self.mod.Helper(self.queue, self.project, self.backups, runner=self.helper.runner, clock=lambda: self.now,
                                 autofix_dir=self.autofix, sync_jobs=True)
        self.assertIsNone(helper.codefix.key)
        self.helper = helper
        self.cf = helper.codefix
        self.put_patch()
        result = self.job('eeeeeeee08', 'codefix_validate', patch_id=PID)
        self.assertIn('no signing key', result['error'])

    def test_params_are_validated_strictly_even_with_a_valid_signature(self):
        self.put_patch()
        digest = self.sha()
        bad = [('codefix_validate', {}, 'The parameter "patch_id" is not specified'), ('codefix_validate', {'patch_id': 'XYZ', 'patch_sha': digest}, 'is invalid'),
               ('codefix_validate', {'patch_id': PID}, 'The parameter "patch_sha" is not specified'),
               ('codefix_validate', {'patch_id': PID, 'patch_sha': 'abc'}, 'sha256'),
               ('codefix_validate', {'patch_id': PID, 'patch_sha': digest.upper()}, 'sha256'),
               ('codefix_validate', {'patch_id': PID, 'patch_sha': digest, 'x': 1}, 'Unknown parameters'),
               ('codefix_deploy', {'patch_id': PID, 'patch_sha': digest, 'base_sha': 'abc', 'summary': 's'}, 'full commit sha'),
               ('codefix_deploy', {'patch_id': PID, 'base_sha': HEAD, 'summary': 's'}, 'The parameter "patch_sha" is not specified'),
               ('codefix_deploy', {'patch_id': PID, 'patch_sha': digest, 'base_sha': HEAD, 'summary': 's', 'by_owner': 'yes'}, 'true/false'),
               ('codefix_deploy', {'patch_id': PID, 'patch_sha': digest, 'base_sha': HEAD, 'summary': 's', 'urgent': True}, 'Unknown parameters'),
               ('codefix_revert', {'commit': 'HEAD~1', 'summary': 's'}, 'sha'), ('codefix_revert', {'commit': HEAD}, 'The parameter "summary" is not specified')]
        for n, (action, params, why) in enumerate(bad):
            with self.subTest(action=action, params=params):
                result = self.job(f'eeeeeeee{20 + n}', action, **params)
                self.assertEqual(result['status'], 'failed')
                self.assertIn(why, result['error'])

    def test_an_artifact_that_differs_from_the_signed_sha_is_not_touched(self):
        """A DB record pointing to a different artifact (an old rejected patch) does not pass: the signed
sha256 does not match."""
        self.put_patch()                                     # the artifact the request refers to
        other = make_patch(code=(OLD_VIEWS, 'def f(x):\n    return (x or 1) + 1\n'))
        (self.queue / 'artifacts' / f'{PID}.patch').write_text(other, encoding='utf-8', newline='\n')
        signed_for_another = hashlib.sha256(b'a different patch').hexdigest()
        for action, extra in (('codefix_validate', {}), ('codefix_deploy', {'base_sha': HEAD, 'summary': 's'})):
            with self.subTest(action=action):
                result = self.job(f'eeeeeeee{70 + len(extra)}', action, patch_id=PID, patch_sha=signed_for_another, **extra)
                self.assertEqual(result['status'], 'failed')
                self.assertIn('does not match the signed one', result['error'])
        self.assertEqual(self.world.runs, [], 'the sandbox was not started')
        self.assertEqual(self.world.count('docker build'), 0, 'the layer was not built')
        self.assertEqual(self.world.count('git apply'), 0)

    def test_unknown_job_and_disabled_codefix(self):
        self.assertEqual(self.job('eeeeeeee90', 'rm_rf')['status'], 'failed')
        plain = self.mod.Helper(self.queue, self.project, self.backups, runner=self.helper.runner)
        self.assertIsNone(plain.codefix)

    def test_the_key_fingerprint_is_published_but_not_the_key(self):
        self.helper.write_status()
        status = json.loads((self.queue / 'results' / 'status.json').read_text(encoding='utf-8'))
        self.assertEqual(status['key_fp'], hashlib.sha256(KEY).hexdigest()[:8])
        self.assertTrue(status['key_ok'])
        self.assertNotIn(KEY.decode(), json.dumps(status))


class ValidateTests(CodefixEnv, unittest.TestCase):
    def test_a_good_fix_passes_in_the_sandbox(self):
        text = self.put_patch()
        result = self.validate()
        self.assertEqual((result['status'], result['verdict']), ('done', 'pass'), result)
        self.assertEqual(result['head'], HEAD)
        self.assertEqual(result['patch_sha'], hashlib.sha256(text.encode()).hexdigest())
        self.assertEqual(result['tests_expected'], 1)
        self.assertEqual(result['phases']['baseline']['failed'], [NEW_TEST])
        self.assertTrue(result['phases']['patched']['ok'])
        self.assertEqual([t for t, *_ in self.world.runs], ['a', 'b', 'c'])
        validated = self.state()['validated']
        self.assertEqual(next(iter(validated.values()))['head'], HEAD)

    def test_each_phase_runs_on_the_right_tree_with_the_right_labels(self):
        self.put_patch()
        self.validate()
        by_tag = {tag: (tree, labels) for tag, tree, labels, _ in self.world.runs}
        base_tree, base_labels = by_tag['a']
        self.assertEqual(base_labels, ['catalog.tests_autofix'])
        self.assertEqual(base_tree['catalog/views.py'], OLD_VIEWS, 'without the fix: the old code')
        self.assertIn('catalog/tests_autofix.py', base_tree, 'but the new tests are already there')
        full_tree, full_labels = by_tag['b']
        self.assertEqual(full_tree['catalog/views.py'], NEW_VIEWS)
        self.assertIn('catalog/tests_autofix.py', full_tree)
        self.assertEqual(full_labels, ['catalog.tests_autofix'])
        regress_tree, regress_labels = by_tag['c']
        self.assertEqual(regress_tree['catalog/views.py'], NEW_VIEWS, 'the existing tests run on the fixed code')
        self.assertNotIn('catalog/tests_autofix.py', regress_tree, 'but WITHOUT the new ones (a hostile test does not share a process with the regression run)')
        self.assertEqual(regress_labels, ['catalog'])

    def test_a_core_fix_checks_every_app_that_depends_on_core(self):
        patch = make_patch(code=('def g(x):\n    return x + 1\n', 'def g(x):\n    return (x or 0) + 1\n'), code_path='backend/core/jsonutils.py',
                           test_path='backend/core/tests_autofix.py')
        self.world.tree['backend/core/jsonutils.py'] = 'def g(x):\n    return x + 1\n'
        self.world.materialize()
        self.world.image_files['core/jsonutils.py'] = 'def g(x):\n    return x + 1\n'
        (self.queue / 'artifacts').mkdir(exist_ok=True)
        (self.queue / 'artifacts' / f'{PID}.patch').write_text(patch, encoding='utf-8', newline='\n')
        self.world.out['a'] = (1, FAIL_1.replace('catalog.', 'core.'))
        result = self.validate()
        self.assertEqual(result.get('verdict'), 'pass', result)
        regress = [labels for tag, _t, labels, _ in self.world.runs if tag == 'c'][0]
        self.assertEqual(regress, ['blog', 'catalog', 'core', 'courses', 'orders', 'users'])

    def test_the_sandbox_has_no_secrets_no_internet_no_writes_and_is_cleaned_up(self):
        self.put_patch()
        self.validate()
        dbs = [' '.join(a) for a, _ in self.world.cmds if a[:3] == ['docker', 'run', '-d']]
        self.assertEqual(len(dbs), 3, 'each phase gets its own fresh database')
        for joined in dbs:
            for needed in ('--network none', '--read-only', '--cap-drop ALL', '--oom-score-adj 1000', '--memory 320m', '--label shop-autofix'):
                self.assertIn(needed, joined)
        for tag, _tree, _labels, args in self.world.runs:
            joined = ' '.join(args)
            for needed in ('--network container:cf-pg-cccccccc01', '--memory 800m', '--pids-limit', '--read-only', '--cap-drop ALL',
                           '--security-opt no-new-privileges', '--oom-score-adj 1000', '--cpu-shares', '--log-driver none',
                           '--ulimit fsize=', '--label shop-autofix', '--entrypoint timeout', ':/app:ro', 'DB_HOST=127.0.0.1'):
                self.assertIn(needed, joined, (tag, needed))
            for forbidden in ('--privileged', '--cap-add', ' --pid ', '--pid=', '--device', 'docker.sock', '--env-file', ' --rm ',
                              '--network bridge', 'host.docker.internal'):
                self.assertNotIn(forbidden, joined, (tag, forbidden))
            mounts = [args[i + 1] for i, x in enumerate(args) if x == '-v']
            self.assertEqual(len(mounts), 1, 'a single mount: the code, read-only')
            self.assertNotIn(str(self.project), joined, 'the live code is not mounted')
            for secret in ('TOKEN=', 'API_KEY', 'MONOBANK', 'NOVA', 'EMAIL_HOST_PASSWORD', 'CLAUDE_CODE', 'SECRET_KEY=real'):
                self.assertNotIn(secret, joined)
            self.assertIn(IMAGE['web'], args, 'the image is pinned by the id of the running container, not by a tag')
        for tag in 'abc':
            self.assertGreaterEqual(self.world.count(f'docker rm -f cf-run-cccccccc01-{tag}'), 1)
            self.assertGreaterEqual(self.world.count(f'docker rm -f cf-pg-cccccccc01-{tag}'), 1)
        self.assertTrue(any('ps -aq --filter label=shop-autofix' in line for line in self.world.lines()), 'sweeping by label')
        self.assertFalse((self.autofix / 'wt' / 'cccccccc01').exists())

    def test_new_test_that_passes_without_the_fix_proves_nothing(self):
        self.world.out['a'] = (0, OK_1)
        self.put_patch()
        result = self.validate()
        self.assertEqual(result['verdict'], 'fail')
        self.assertIn('PASS even without the fix', result['reason'])
        self.assertNotIn('b', [t for t, *_ in self.world.runs])

    def test_a_new_test_that_only_fails_to_import_does_not_count_as_reproduction(self):
        self.world.out['a'] = (1, 'ERROR: tests_autofix (unittest.loader._FailedTest.tests_autofix)\n\nRan 1 test in 0.1s\n\n'
                                  'FAILED (errors=1)\n')
        self.put_patch()
        result = self.validate()
        self.assertEqual(result['verdict'], 'fail')
        self.assertIn('fail on import or setup', result['reason'])

    def test_a_test_that_dies_in_setup_does_not_count_as_reproduction(self):
        self.world.out['a'] = (1, 'ERROR: setUpClass (catalog.tests_autofix.T)\n\nRan 1 test in 0.1s\n\nFAILED (errors=1)\n')
        self.put_patch()
        self.assertIn('fail on import or setup', self.validate()['reason'])

    def test_the_baseline_must_run_every_new_test(self):
        self.world.out['a'] = (1, FAIL_1.replace('Ran 1 test', 'Ran 0 tests'))
        self.put_patch()
        result = self.validate()
        self.assertEqual(result['verdict'], 'fail')
        self.assertIn('0 of 1', result['reason'])

    def test_new_test_that_fails_after_the_fix(self):
        self.world.out['b'] = (1, FAIL_1)
        self.put_patch()
        result = self.validate()
        self.assertEqual(result['verdict'], 'fail')
        self.assertIn('fails even with the fix', result['reason'])

    def test_a_run_that_exits_nonzero_without_fail_lines_is_not_a_pass(self):
        self.world.out['b'] = (1, OK_1)
        self.put_patch()
        self.assertEqual(self.validate()['verdict'], 'fail')
        self.world.out['b'] = (0, OK_1)
        self.world.out['c'] = (1, 'Ran 120 tests in 9s\n\nFAILED (failures=3)\n')
        self.assertEqual(self.validate('cccccccc02')['verdict'], 'fail')

    def test_a_forged_summary_is_not_trusted(self):
        self.world.out['c'] = (1, 'Ran 120 tests in 9s\n\nFAILED (failures=3)\n')
        self.put_patch()
        result = self.validate()
        self.assertEqual(result['verdict'], 'fail')
        self.assertIn('does not match the runner summary', result['reason'])

    def test_breaking_an_existing_test_fails_but_an_old_failure_does_not(self):
        old_failure = 'FAIL: test_old (catalog.tests.Old.test_old)\n\nRan 120 tests in 9s\n\nFAILED (failures=1)\n'
        self.world.out['c'] = (1, old_failure)
        self.world.out['d'] = (1, 'FAIL: test_old (catalog.tests.Old.test_old)\n\nRan 1 test in 1s\n\nFAILED (failures=1)\n')
        self.put_patch()
        self.assertEqual(self.validate('cccccccc02')['verdict'], 'pass', 'it was failing before the fix too')
        self.world.out['d'] = (0, 'Ran 1 test in 1s\n\nOK\n')
        result = self.validate('cccccccc03')
        self.assertEqual(result['verdict'], 'fail')
        self.assertIn('breaks', result['reason'])
        d_runs = [(tree, labels) for tag, tree, labels, _ in self.world.runs if tag == 'd']
        self.assertEqual(d_runs[-1][1], ['catalog.tests.Old.test_old'])
        self.assertEqual(d_runs[-1][0]['catalog/views.py'], OLD_VIEWS, 'the comparison runs on the untouched base')

    def test_an_import_error_after_the_fix_is_a_new_failure(self):
        self.world.out['c'] = (1, 'ERROR: catalog.tests (unittest.loader._FailedTest.catalog.tests)\n\nRan 1 test in 1s\n\n'
                                  'FAILED (errors=1)\n')
        self.world.out['d'] = (0, 'Ran 1 test in 1s\n\nOK\n')
        self.put_patch()
        result = self.validate()
        self.assertEqual(result['verdict'], 'fail')

    def test_timeout_oom_and_flood_are_failures_not_waits(self):
        self.put_patch()
        for n, (flags, oom, word) in enumerate((({'timed_out': True}, False, 'did not finish within'), ({}, True, 'memory'),
                                                ({'over_limit': True}, False, 'prints too much text'))):
            self.world.flags['a'] = flags
            self.world.oom = oom
            self.world.out['a'] = (137, '')
            result = self.validate(f'cccccccc2{n}')
            with self.subTest(word=word):
                self.assertEqual((result['status'], result['verdict']), ('done', 'fail'))
                self.assertIn(word, result['reason'])

    def test_docker_that_cannot_start_the_sandbox_means_wait_not_fail(self):
        self.put_patch()
        self.world.out['a'] = (125, '')
        result = self.validate()
        self.assertEqual((result['status'], result['code']), ('failed', 'wait'))
        self.world.out['a'] = (1, FAIL_1)
        self.world.fail['pg'] = 125
        result = self.validate('cccccccc02')
        self.assertEqual((result['status'], result['code']), ('failed', 'wait'))
        self.assertGreaterEqual(self.world.count('docker rm -f cf-pg-cccccccc02'), 1, 'even a failed start is cleaned up')

    def test_policy_is_enforced_again_on_the_host(self):
        self.put_patch(code_path='backend/orders/views.py')
        result = self.validate()
        self.assertEqual(result['status'], 'failed')
        self.assertIn('The policy rejected the patch', result['error'])
        self.assertEqual(result['code'], 'policy')
        self.assertEqual(self.world.runs, [])

    def test_the_structural_layer_runs_on_the_host_too(self):
        bad = NEW_VIEWS.replace('    return', '    APIView.check_permissions = None\n    return')
        self.put_patch(code=(OLD_VIEWS, bad))
        result = self.validate()
        self.assertEqual(result['status'], 'failed')
        self.assertIn('R2', result['error'])
        self.assertEqual(self.world.runs, [])

    def test_a_patch_that_does_not_match_git_is_refused(self):
        self.put_patch(code=('def f(x):\n    return x + 2\n', NEW_VIEWS))
        result = self.validate()
        self.assertEqual(result['status'], 'failed')
        self.assertIn('The policy rejected the patch', result['error'])

    def test_preconditions(self):
        self.put_patch()
        self.world.dirty = ' M backend/catalog/views.py\n'
        self.assertIn('uncommitted changes', self.validate('cccccccc04')['error'])
        self.world.dirty = ''
        self.cf._meminfo = lambda: 300 * 2 ** 20
        self.assertIn('free memory', self.validate('cccccccc05')['error'])
        self.cf._meminfo = lambda: 4 * GB
        self.free = 1 * GB
        self.assertIn('disk space', self.validate('cccccccc06')['error'])
        self.free = 4 * GB
        self.world.docker_up = False
        self.assertIn('Docker', self.validate('cccccccc07')['error'])
        self.world.docker_up = True
        self.assertIn('was not found in the queue', self.validate('cccccccc08', pid='deadbeef99')['error'])

    def test_a_running_image_that_differs_from_git_is_refused(self):
        self.put_patch()
        self.world.image_files['catalog/views.py'] = 'def f(x):\n    return x + 100\n'
        result = self.validate()
        self.assertEqual((result['status'], result['code']), ('failed', 'stale'))
        self.assertIn('does not match git', result['error'])
        self.assertEqual(self.world.runs, [])

    def test_patch_that_does_not_apply_is_a_failure_and_leaves_no_trace(self):
        self.put_patch()
        self.world.fail['apply'] = 1
        result = self.validate()
        self.assertEqual((result['status'], result['code']), ('failed', 'policy'))
        self.assertIn('does not apply', result['error'])
        self.assertFalse((self.autofix / 'wt' / 'cccccccc01').exists())
        self.assertEqual([t for t, *_ in self.world.runs], [])

    def test_only_one_job_at_a_time(self):
        self.put_patch()
        self.cf._running = object()
        result = self.validate()
        self.assertEqual(result['status'], 'failed')
        self.assertIn('already running', result['error'])

    def test_a_symlinked_artifact_is_not_read_and_nothing_leaks_into_the_result(self):
        import os
        from analyst.tests_safeio import CAN_SYMLINK
        if not CAN_SYMLINK:
            self.skipTest('symbolic links are not available')
        secret = self.queue.parent / 'secret.env'
        secret.write_text('DB_PASSWORD=hunter2\n')
        (self.queue / 'artifacts').mkdir(exist_ok=True)
        os.symlink(secret, self.queue / 'artifacts' / f'{PID}.patch')
        result = self.validate()
        self.assertIn('was not found in the queue', result['error'])
        self.assertNotIn('hunter2', json.dumps(result))

    def test_an_oversized_artifact_is_not_read(self):
        (self.queue / 'artifacts').mkdir(exist_ok=True)
        (self.queue / 'artifacts' / f'{PID}.patch').write_text('x' * (self.cf.policy.MAX_PATCH_BYTES + 5000))
        self.assertIn('was not found in the queue', self.validate()['error'])


class DeployTests(CodefixEnv, unittest.TestCase):
    def test_unvalidated_patch_is_never_deployed(self):
        self.put_patch()
        result = self.deploy()
        self.assertEqual(result['status'], 'failed')
        self.assertIn('has not passed the sandbox check', result['error'])
        self.assertFalse([l for l in self.world.lines() if 'docker build' in l or ' up ' in l])

    def test_a_fix_validated_on_another_commit_or_too_long_ago_is_refused(self):
        self.ready()
        self.world.head = 'c' * 40
        self.assertIn('changed after the check', self.deploy('dddddddd02')['error'])
        self.world.head = HEAD
        self.now += self.cfmod.VALIDATION_TTL + timedelta(minutes=1)
        self.assertIn('The check is out of date', self.deploy('dddddddd03')['error'])

    def test_a_swapped_patch_under_the_validated_id_is_not_deployed(self):
        self.ready()
        self.put_patch(code=(OLD_VIEWS, NEW_VIEWS.replace('0', '1')))
        self.assertIn('has not passed the sandbox check', self.deploy()['error'])

    def test_dirty_tree_and_wrong_base_are_refused(self):
        self.ready()
        self.assertIn('changed after the check', self.deploy('dddddddd04', base_sha='c' * 40)['error'])
        self.world.dirty = ' M x\n'
        self.assertIn('uncommitted changes', self.deploy('dddddddd05')['error'])

    def test_successful_deploy_runs_the_steps_in_order_and_records_it(self):
        self.ready()
        result = self.deploy()
        self.assertEqual((result['status'], result['outcome']), ('done', 'deployed'), result)
        self.assertEqual(result['commit'], NEW_HEAD)
        w = self.world
        order = [w.index('--entrypoint cat'), w.index('BACKUP'), w.index('docker tag ' + IMAGE['web'] + ' shop-web:pre-autofix-'),
                 w.index('git apply --check'), w.index('git add'), w.index('git -c user.name'), w.index('docker build'),
                 w.index('docker tag shop-web:latest shop-scheduler:latest'), w.index(' up -d --no-build --no-deps')]
        self.assertEqual(order, sorted(order), w.lines())
        self.assertTrue(w.applied_with_marker, 'the inflight journal exists BEFORE the tree is changed')
        self.assertEqual(self.backup.call_count, 1)
        up = [l for l in w.lines() if ' up ' in l][-1]
        for needed in ('--no-build', '--no-deps', 'web scheduler analyst'):
            self.assertIn(needed, up)
        state = self.state()
        self.assertEqual(len(state['attempts']), 1)
        self.assertEqual(state['applied'][0]['commit'], NEW_HEAD)
        self.assertEqual(state['applied'][0]['files'], [VIEWS, TESTS])
        self.assertFalse((self.autofix / 'inflight.json').exists())
        self.assertTrue((self.autofix / 'applied' / f'{PID}.patch').exists())
        self.assertEqual(list(self.autofix.glob('*.patch')), [], 'the temporary copy of the patch for git apply is not left behind')

    def test_the_image_is_a_thin_layer_over_the_running_one_built_without_network(self):
        self.ready()
        self.deploy()
        dockerfile, files, args = self.world.builds[0]
        stamp = next(l for l in self.world.lines() if 'docker tag sha256:' in l).rsplit('pre-autofix-', 1)[1]
        self.assertIn(f'FROM shop-web:pre-autofix-{stamp}', dockerfile)
        self.assertIn('COPY files/ /app/', dockerfile)
        self.assertIn(f'LABEL shop.autofix.commit={NEW_HEAD}', dockerfile)
        self.assertEqual(sorted(files), ['catalog/tests_autofix.py', 'catalog/views.py'])
        self.assertEqual(files['catalog/views.py'], NEW_VIEWS)
        self.assertIn('--network', args)
        self.assertEqual(args[args.index('--network') + 1], 'none')
        self.assertFalse(any('compose' in line and ' build' in line for line in self.world.lines()), 'compose build is not called')
        self.assertFalse((self.autofix / 'build' / 'dddddddd01').exists(), 'the build context is removed')

    def test_rollback_tags_come_from_the_running_containers_not_from_latest(self):
        self.ready()
        self.world.container = {s: 'sha256:' + c * 64 for s, c in zip(('web', 'scheduler', 'analyst'), 'abc')}
        self.world.tags = {f'shop-{s}:latest': image for s, image in self.world.container.items()}
        self.deploy()
        tags = [l for l in self.world.lines() if l.startswith('docker tag sha256:')]
        self.assertEqual(len(tags), 3)
        self.assertTrue(tags[0].startswith('docker tag sha256:' + 'a' * 64))

    def test_a_latest_tag_that_is_not_the_running_image_is_refused(self):
        self.ready()
        self.world.tags['shop-web:latest'] = 'sha256:' + '9' * 64
        result = self.deploy()
        self.assertEqual((result['status'], result['code']), ('failed', 'stale'))
        self.assertEqual(self.world.count('docker build'), 0)

    def test_a_running_image_that_differs_from_git_stops_the_deploy(self):
        self.ready()
        self.world.image_files['catalog/views.py'] = 'def f(x):\n    return 7\n'
        self.assertEqual(self.deploy()['code'], 'stale')
        self.assertEqual(self.backup.call_count, 0)

    def test_every_attempt_counts_even_a_failed_one(self):
        self.ready()
        self.world.broken_always = True
        first = self.deploy('dddddddd10')
        self.assertEqual(first['outcome'], 'rollback_unhealthy')
        self.world.broken_always = False
        self.assertEqual(len(self.state()['attempts']), 1)
        self.put_patch()
        self.assertEqual(self.validate('cccccccc11')['verdict'], 'pass')
        too_soon = self.deploy('dddddddd11')
        self.assertEqual((too_soon['status'], too_soon['code']), ('failed', 'wait'))
        self.assertIn('3 hours', too_soon['error'])
        self.now += timedelta(hours=3, minutes=1)
        self.assertEqual(self.validate('cccccccc12')['verdict'], 'pass')
        self.assertEqual(self.deploy('dddddddd12')['outcome'], 'deployed')
        self.now += timedelta(hours=4)
        self.assertEqual(self.validate('cccccccc13')['verdict'], 'pass')
        third = self.deploy('dddddddd13', base_sha=NEW_HEAD)
        self.assertIn('Limit', third['error'])

    def test_a_validation_is_good_for_one_attempt_only(self):
        self.ready()
        self.world.broken_always = True
        self.deploy('dddddddd20')
        self.world.broken_always = False
        self.now += timedelta(hours=4)
        again = self.deploy('dddddddd21')
        self.assertIn('has not passed the sandbox check', again['error'])

    def test_the_night_window_applies_to_everything_but_the_owner(self):
        self.ready()
        self.kyiv_hour = 14
        blocked = self.deploy('dddddddd30')
        self.assertEqual((blocked['status'], blocked['code']), ('failed', 'wait'))
        self.assertIn('only at night', blocked['error'])
        self.assertEqual(self.deploy('dddddddd31', by_owner=True)['outcome'], 'deployed')

    def test_a_storm_of_errors_cannot_buy_a_daytime_deploy(self):
        self.ready()
        self.kyiv_hour = 14
        result = self.deploy('dddddddd32', urgent=True)
        self.assertEqual(result['status'], 'failed')
        self.assertIn('Unknown parameters', result['error'])

    def test_the_drill_flag_is_only_for_the_canary(self):
        self.ready()
        result = self.deploy('dddddddd33', drill=True)
        self.assertEqual(result['status'], 'failed')
        self.assertIn('training canary', result['error'])
        canary_old, canary_new = TREE[CANARY], 'def g(x):\n    return (x or 0) + 1\n'
        self.put_patch(code=(canary_old, canary_new), code_path=CANARY, test_path='backend/core/tests_autofix_canary.py')
        self.world.image_files['core/canary.py'] = canary_old
        self.world.out['a'] = (1, FAIL_1.replace('catalog.', 'core.'))
        self.kyiv_hour = 14
        self.assertEqual(self.validate('cccccccc40')['verdict'], 'pass')
        self.assertEqual(self.deploy('dddddddd34', drill=True)['outcome'], 'deployed', 'the drill bypasses the window and the limits')

    def test_pause_flag_stops_new_deploys(self):
        self.ready()
        (self.autofix / 'PAUSE').write_text('')
        result = self.deploy()
        self.assertIn('are paused', result['error'])
        self.assertEqual(self.world.count('docker build'), 0)
        result = self.validate('cccccccc50')
        self.assertIn('are paused', result['error'])

    def test_a_busy_deploy_lock_postpones_the_deploy(self):
        self.ready()
        busy = self.cf.err('the lock is busy', 'wait')
        with mock.patch.object(self.cfmod.DeployLock, '__enter__', side_effect=busy):
            result = self.deploy()
        self.assertEqual((result['status'], result['code']), ('failed', 'wait'))
        self.assertEqual(self.world.count('docker build'), 0)

    def test_failed_health_check_rolls_everything_back(self):
        self.ready()
        self.world.broken_until_reset = True
        result = self.deploy()
        self.assertEqual((result['status'], result['outcome']), ('done', 'rolled_back'), result)
        w = self.world
        self.assertGreater(w.count('git reset --hard ' + HEAD), 0)
        self.assertEqual(w.count('docker tag shop-web:pre-autofix-'), 1)
        self.assertEqual(w.count('--no-deps'), 2, 'brought up twice: the new version and the rollback')
        self.assertFalse((self.autofix / 'inflight.json').exists())
        self.assertTrue((self.autofix / 'rejected' / f'{PID}.patch').exists())
        self.assertEqual(self.state()['applied'], [])

    def test_a_site_that_cannot_be_confirmed_even_after_rollback_is_reported_and_keeps_the_marker(self):
        self.ready()
        self.world.broken_always = True
        result = self.deploy()
        self.assertEqual(result['outcome'], 'rollback_unhealthy')
        self.assertTrue((self.autofix / 'inflight.json').exists(), 'the marker stays: the helper will retry on start')
        self.assertTrue(result['rollback_problems'])

    def test_a_service_that_is_not_running_means_rollback(self):
        self.ready()
        self.world.bad_states = 'web running\nscheduler exited\nanalyst running\n'
        real_compose = self.world.compose

        def break_on_first_up(a):
            if a[4] == 'up' and self.world.up_calls == 0:
                self.world.bad_states_until_reset = True
            return real_compose(a)
        self.world.compose = break_on_first_up
        result = self.deploy()
        self.assertEqual(result['outcome'], 'rolled_back')
        self.assertIn('scheduler', result['reason'])

    def test_new_logic_traceback_in_logs_after_deploy_means_rollback(self):
        self.ready()
        real_compose = self.world.compose

        def log_an_error_on_first_up(a):
            if a[4] == 'up' and self.world.up_calls == 0:
                self.world.logs = ('web-1  | Traceback (most recent call last):\n'
                                   'web-1  |   File "/app/catalog/views.py", line 3, in f\n'
                                   "web-1  | TypeError: bad\n")
            return real_compose(a)
        self.world.compose = log_an_error_on_first_up
        result = self.deploy()
        self.assertEqual(result['outcome'], 'rolled_back')
        self.assertIn('new code errors', result['reason'])

    def test_bot_noise_and_external_failures_in_logs_do_not_count(self):
        self.ready()
        self.world.logs = ('web-1  | Traceback (most recent call last):\n'
                           'web-1  |     raise DisallowedHost(msg)\n'
                           'web-1  | django.core.exceptions.DisallowedHost: Invalid HTTP_HOST header\n'
                           'scheduler-1  | Traceback (most recent call last):\n'
                           'scheduler-1  | requests.exceptions.ConnectionError: timeout\n') * 3
        self.assertEqual(self.deploy()['outcome'], 'deployed')

    def test_a_failed_up_is_rolled_back_with_a_second_up(self):
        self.ready()
        self.world.fail['up_once'] = 1
        result = self.deploy()
        self.assertEqual(result['outcome'], 'rolled_back')
        self.assertEqual(self.world.count('--no-deps'), 2)

    def test_build_failure_restores_code_without_restarting_containers(self):
        self.ready()
        self.world.fail['build'] = 1
        result = self.deploy()
        self.assertEqual(result['status'], 'failed')
        self.assertIn('The layer build failed', result['error'])
        self.assertEqual(self.world.count('--no-deps'), 0, 'the containers were not touched')
        self.assertGreater(self.world.count('git reset --hard ' + HEAD), 0)
        self.assertGreater(self.world.count('docker tag shop-web:pre-autofix-'), 0, 'the tags are restored')
        self.assertFalse((self.autofix / 'inflight.json').exists())
        self.assertEqual((self.project / VIEWS).read_text(encoding='utf-8'), OLD_VIEWS, 'the tree is restored')
        self.assertFalse((self.project / TESTS).exists(), 'the new test is removed too')

    def test_patch_that_does_not_apply_to_the_tree_changes_nothing(self):
        self.ready()
        self.world.fail['apply'] = 1
        result = self.deploy()
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(self.world.count('docker build'), 0)
        self.assertFalse((self.autofix / 'inflight.json').exists())

    def test_new_test_files_that_did_not_reach_a_commit_are_cleaned_so_the_next_attempt_can_apply(self):
        self.ready()
        self.world.reset_keeps_untracked = True           # git reset --hard does not touch untracked files
        self.world.fail['commit'] = 1
        result = self.deploy()
        self.assertEqual(result['status'], 'failed')
        self.assertFalse((self.project / TESTS).exists(), 'the untracked new test is removed via git clean')
        self.assertEqual(self.world.count('git clean -f -- ' + TESTS), 1)

    def test_a_failed_rollback_does_not_hide_the_original_error_or_skip_cleanup(self):
        self.ready()
        self.world.fail['build'] = 1
        self.world.fail['reset'] = 1
        result = self.deploy()
        self.assertEqual(result['status'], 'failed')
        self.assertIn('The layer build failed', result['error'])
        self.assertTrue((self.autofix / 'inflight.json').exists(), 'the rollback failed: the marker stays')
        self.assertTrue((self.autofix / 'rejected' / f'{PID}.patch').exists(), 'the rejected patch is kept despite the rollback failure')

    def test_a_crash_while_saving_the_bookkeeping_does_not_undo_a_healthy_deploy(self):
        self.ready()
        calls = {'n': 0}
        real = self.cf.save_state

        def flaky(data):
            calls['n'] += 1
            if calls['n'] >= 2:
                raise OSError('No space left on device')
            real(data)
        self.cf.save_state = flaky
        result = self.deploy()
        self.assertEqual((result['status'], result['outcome']), ('done', 'deployed'))
        self.assertFalse((self.autofix / 'inflight.json').exists(), 'the marker is removed BEFORE the bookkeeping: the next start will not roll anything back')

    def test_simulated_failure_exercises_the_real_rollback(self):
        self.ready()
        result = self.deploy(simulate_failure=True)
        self.assertEqual(result['outcome'], 'rolled_back')
        self.assertIn('deliberate', result['reason'])


class RevertTests(CodefixEnv, unittest.TestCase):
    def deployed(self):
        self.ready()
        self.assertEqual(self.deploy()['outcome'], 'deployed')
        self.world.cmds.clear()
        self.world.head = NEW_HEAD

    def test_revert_removes_the_commit_and_redeploys_a_thin_layer(self):
        self.deployed()
        self.now += timedelta(hours=1)
        result = self.job('ffffffff01', 'codefix_revert', commit=NEW_HEAD, summary='did not help')
        self.assertEqual((result['status'], result['outcome']), ('done', 'reverted'), result)
        self.assertEqual(self.world.count('git -c user.name=Analyst -c user.email=analyst@shop.local revert --no-edit ' + NEW_HEAD), 1)
        self.assertEqual(self.world.count('docker build'), 1)
        self.assertEqual(self.state()['applied'], [])
        self.assertFalse((self.autofix / 'inflight.json').exists())

    def test_an_old_record_without_a_file_list_takes_the_files_from_the_commit(self):
        self.deployed()
        data = self.state()
        for entry in data['applied']:
            entry.pop('files', None)
        (self.autofix / 'state.json').write_text(json.dumps(data), encoding='utf-8')
        self.now += timedelta(hours=1)
        result = self.job('ffffffff05', 'codefix_revert', commit=NEW_HEAD, summary='x')
        self.assertEqual(result['outcome'], 'reverted', result)
        self.assertEqual(sorted(self.world.builds[-1][1]), ['catalog/tests_autofix.py', 'catalog/views.py'], 'README.md is not backend: skipped')

    def test_only_our_own_commits_can_be_reverted(self):
        self.assertIn('not an Analyst auto-commit', self.job('ffffffff02', 'codefix_revert', commit='d' * 40, summary='x')['error'])
        self.assertEqual(self.world.count('git -c user.name=Analyst'), 0)

    def test_revert_that_breaks_the_site_is_undone(self):
        self.deployed()
        self.now += timedelta(hours=1)
        self.http['/admin/login/'] = 500
        result = self.job('ffffffff03', 'codefix_revert', commit=NEW_HEAD, summary='x')
        self.assertEqual(result['status'], 'failed')
        self.assertIn('unhealthy', result['error'])
        self.assertEqual(len(self.state()['applied']), 1, 'the record of the fix stays')

    def test_a_conflicting_revert_is_aborted(self):
        self.deployed()
        self.world.fail['revert'] = 1
        result = self.job('ffffffff04', 'codefix_revert', commit=NEW_HEAD, summary='x')
        self.assertEqual(result['status'], 'failed')
        self.assertIn('git revert failed', result['error'])
        self.assertEqual(self.world.count('git revert --abort'), 1)
        self.assertFalse((self.autofix / 'inflight.json').exists())


class RecoveryTests(CodefixEnv, unittest.TestCase):
    def marker(self, **over):
        info = {'job': 'dddddddd99', 'base_sha': HEAD, 'stamp': '20261019030000', 'at': self.now.isoformat(), 'attempts': 0}
        info.update(over)
        (self.autofix / 'inflight.json').write_text(json.dumps(info), encoding='utf-8')

    def test_a_crash_in_the_middle_is_recovered_on_start(self):
        self.marker()
        self.world.head = NEW_HEAD
        self.world.log1 = (HEAD, 'autofix abcdef0123: fix')
        for svc, image in IMAGE.items():
            self.world.tags[f'shop-{svc}:pre-autofix-20261019030000'] = image
        self.helper.startup()
        w = self.world
        self.assertGreater(w.count('git reset --hard ' + HEAD), 0)
        self.assertEqual(w.count('docker tag shop-web:pre-autofix-20261019030000 shop-web:latest'), 1)
        self.assertEqual(w.count('--no-deps'), 1)
        self.assertFalse((self.autofix / 'inflight.json').exists())
        self.assertTrue(self.state()['recovery']['ok'])

    def test_an_old_marker_is_dropped_without_rolling_anything_back(self):
        self.marker(at=(self.now - timedelta(days=5)).isoformat())
        self.world.head = 'e' * 40
        self.helper.startup()
        self.assertEqual(self.world.count('git reset'), 0)
        self.assertEqual(self.world.count('docker tag'), 0)
        self.assertEqual(self.world.count('--no-deps'), 0)
        self.assertFalse((self.autofix / 'inflight.json').exists())
        self.assertIsNone(self.state()['recovery']['ok'])

    def test_a_marker_whose_base_is_not_our_history_is_dropped(self):
        self.marker()
        self.world.head = 'e' * 40
        self.world.log1 = ('9' * 40, 'Release 12')
        self.helper.startup()
        self.assertEqual(self.world.count('git reset'), 0)
        self.assertFalse((self.autofix / 'inflight.json').exists())

    def test_a_failing_recovery_keeps_the_marker_and_gives_up_after_three_starts(self):
        self.marker()
        self.world.head = NEW_HEAD
        self.world.fail['up'] = 1
        for attempt in (1, 2, 3):
            self.helper.startup()
            info = json.loads((self.autofix / 'inflight.json').read_text(encoding='utf-8'))
            self.assertEqual(info['attempts'], attempt)
            self.assertFalse(self.state()['recovery']['ok'])
        self.helper.startup()
        self.assertFalse((self.autofix / 'inflight.json').exists(), 'the marker is removed after three attempts')
        self.assertIn('several attempts', self.state()['recovery']['error'])

    def test_running_results_are_marked_interrupted_on_start(self):
        results = self.queue / 'results'
        (results / 'aaaaaaaa77.json').write_text(json.dumps({'id': 'aaaaaaaa77', 'mode': 'job', 'status': 'running'}), encoding='utf-8')
        (results / 'aaaaaaaa78.json').write_text(json.dumps({'id': 'aaaaaaaa78', 'mode': 'job', 'status': 'done'}), encoding='utf-8')
        (results / 'aaaaaaaa79.json').write_text(json.dumps({'id': 'aaaaaaaa79', 'mode': 'plan', 'status': 'planned'}), encoding='utf-8')
        self.helper.startup()
        read = lambda n: json.loads((results / n).read_text(encoding='utf-8'))
        self.assertEqual((read('aaaaaaaa77.json')['status'], read('aaaaaaaa77.json')['code']), ('failed', 'interrupted'))
        self.assertEqual(read('aaaaaaaa78.json')['status'], 'done')
        self.assertEqual(read('aaaaaaaa79.json')['status'], 'planned')

    def test_leftover_sandbox_objects_are_swept_on_start(self):
        self.world.leaked = ['abc123def456']
        self.helper.startup()
        self.assertGreaterEqual(self.world.count('docker rm -f abc123def456'), 1)


class StatusTests(CodefixEnv, unittest.TestCase):
    def test_job_result_is_written_as_running_first_then_final(self):
        self.put_patch()
        seen = []
        real = self.cf._publish

        def spy(job, status='running', **extra):
            seen.append(status)
            return real(job, status, **extra)
        self.cf._publish = spy
        self.validate()
        self.assertEqual(seen[0], 'running')
        self.assertEqual(seen[-1], 'done')
        final = json.loads((self.queue / 'results' / 'cccccccc01.json').read_text(encoding='utf-8'))
        self.assertEqual(final['status'], 'done')
        self.assertEqual(final['id'], 'cccccccc01')
        self.assertEqual(final['action'], 'codefix_validate')

    def test_status_fields(self):
        fields = self.cf.status_fields()
        self.assertEqual(set(fields) >= {'policy_sha', 'head', 'autofix_ahead', 'key_ok', 'key_fp', 'paused', 'recovery', 'job'}, True)
        (self.autofix / 'PAUSE').write_text('')
        self.assertTrue(self.cf.status_fields()['paused'])

    def test_manual_actions_are_refused_while_a_deploy_runs(self):
        self.cf._running = self.cfmod.Job('abcdefab12', 'codefix_deploy', {})
        plan = self.plan('aaaaaaaa61', 'restart_service', service='scheduler')
        self.cf._running = None
        self.cf._running = self.cfmod.Job('abcdefab13', 'codefix_deploy', {})
        refused = self.apply('bbbbbbbb61', plan['id'])
        self.assertEqual(refused['status'], 'failed')
        self.assertIn('deployment', refused['error'])
        self.cf._running = None

    def test_the_daily_auto_backup_waits_while_a_deploy_runs(self):
        self.hour = 4
        self.cf._running = self.cfmod.Job('abcdefab14', 'codefix_deploy', {})
        self.helper.maybe_auto_backup()
        self.assertEqual(self.backup.call_count, 0)
        self.cf._running = None
