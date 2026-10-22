"""Auto-fix policy: every forbidden construct has a witness line, the limit boundaries are exact, plus
the rules for tests and paths."""
import hashlib
from pathlib import Path

from django.test import SimpleTestCase

from analyst import codefix_policy as policy
from analyst.tests_codefix_policy import TESTS, VIEWS, _diff, make_patch

BODY = 'import unittest\n\n\nclass T(unittest.TestCase):\n    def test(self):\n        pass\n'


class WitnessTableTests(SimpleTestCase):
    """Removing a 'redundant' alternative from a regular expression will not go unnoticed: each token has a
line that must be caught."""
    CODE_WITNESSES = (
        'subprocess.run(a)', 'pty.spawn(a)', 'os.system(a)', 'os.popen(a)', 'os.fork()', 'eval(a)', 'exec(a)', 'compile(a, b, c)',
        '__import__(a)', 'x.__class__', 'x.__subclasses__()', 'import socket', 'urllib.request.urlopen(a)', 'import urllib3',
        'import requests', 'import httpx', 'import aiohttp', 'import http.client', 'import smtplib', 'import ftplib', 'import telnetlib',
        'import paramiko', 'import websockets', 'import pickle', 'import marshal', 'import ctypes', 'import cffi', 'import importlib',
        'import shelve', 'import dill', 'import builtins', 'import signal', 'import inspect', 'import gc', 'import tempfile',
        'import pkgutil', 'import runpy', 'import zipimport', 'import os', 'import sys', 'sys.modules', 'import threading',
        'import multiprocessing', 'import concurrent.futures', 'asyncio.create_subprocess_shell(a)', 'shutil.copy(a, b)',
        'p.unlink()', 'p.rmdir()', 'p.rmtree()', 'p.chmod(1)', 'p.chown(1)', 'p.write_text(a)', 'p.write_bytes(a)', 'p.read_text()',
        'p.read_bytes()', 'os.remove(a)', 'os.environ', 'os.getenv(a)', 'open(a)', "x = '/proc/self/environ'", "x = '/etc/passwd'",
        'SECRET = 1', 'api_key = 1', 'APIKEY = 1', 'token = 1', 'password = 1', 'passwd = 1', 'private_key = 1', 'credential = 1',
        'authorization = 1', 'bearer = 1', 'DATABASES', 'x = ".env"', 'q.delete()', 'q.raw(a)', 'q.extra(a)', 'c.execute(a)',
        'c.executemany(a)', 'M.objects.bulk_create(a)', 'M.objects.bulk_update(a)', 'M.objects.update_or_create(a)',
        'M.objects.get_or_create(a)', 'M.objects.create(a)', 'o.save()', 'q.truncate()', 'connection.cursor()',
        'from django.db import connection', 'TRUNCATE TABLE x', 'DROP TABLE x', 'M.objects.filter(a=1).update(b=2)', 'M.objects.update(b=2)',
        'send_mail(a)', 'send_mass_mail(a)', 'EmailMessage(a)', 'EmailMultiAlternatives(a)', 'mark_safe(a)', 'x | safe', 'SafeString(a)',
        '@csrf_exempt', 'AllowAny', 'permission_classes', 'IsAdminUser', 'IsAuthenticated', '@login_required', '@staff_member_required',
        '@user_passes_test(a)', 'x.is_staff', 'x.is_superuser', 'x.has_perm(a)', 'authentication_classes', 'throttle_classes',
        'owner_only', 'logging.disable(5)', 'l.setLevel(5)', 'l.disabled = 1', 'warnings.filterwarnings(a)', 'warnings.simplefilter(a)',
        'global X', 'nonlocal X', 'except:', 'except Exception:', 'except BaseException as e:')
    TEST_WITNESSES = ('subprocess.run(a)', 'os.system(a)', 'eval(a)', '__import__(a)', 'import socket', 'import ctypes', 'import cffi',
                      'import pickle', 'import marshal', 'import tempfile', 'import importlib', 'import builtins', 'import shutil',
                      'os.remove(a)', 'os.environ', 'os.getenv(a)', 'p.unlink()', 'p.write_text(a)', 'p.write_bytes(a)', 'x.open("w")',
                      'open(p, "w")', 'open(p, mode="wb")', 'raise SystemExit(0)', 'sys.exit(0)', 'sys.stdout', 'sys.stderr',
                      'print(a)', 'TextTestResult', 'TextTestRunner')
    PROTECTED_WITNESSES = (
        'permission_classes = []', 'x = authenticate(a)', 'authorize(a)', '@login_required', '@staff_member_required', '@user_passes_test(a)',
        'csrf = 1', 'if not u.is_staff:', 'if u.is_superuser:', 'u.has_perm(a)', 'throttle_scope = 1', 'owner_only(a)', 'authentication_classes',
        'qs.filter(is_active=True)', 'is_approved = True', 'if not x.is_published:', 'is_visible = 1', 'is_public = 1', 'is_preview = 1',
        'raise PermissionDenied', 'raise Http404', 'raise SuspiciousOperation', 'return Response(s, status=403)', 'status=401',
        'HTTP_403_FORBIDDEN', 'HTTP_404_NOT_FOUND', 'hmac.compare_digest(a, b)', 'escape(a)', 'strip_tags(a)', 'bleach.clean(a)',
        'sanitize(a)', 'log.exception("x")', 'logger.error("x")', 'logging.critical("x")', 'log.warning("x")')

    def test_every_banned_code_construct_is_caught_by_the_text_layer(self):
        for line in self.CODE_WITNESSES:
            with self.subTest(line=line), self.assertRaises(policy.PolicyViolation):
                policy.check_added('code', 'backend/catalog/views.py', [line])

    def test_every_banned_test_construct_is_caught(self):
        for line in self.TEST_WITNESSES:
            with self.subTest(line=line), self.assertRaises(policy.PolicyViolation):
                policy.check_added('test', 'backend/catalog/tests_x.py', [line])

    def test_every_protected_line_cannot_be_removed(self):
        for line in self.PROTECTED_WITNESSES:
            with self.subTest(line=line):
                self.assertTrue(policy.PROTECTED_REMOVALS.search(line), line)

    def test_removing_a_protected_line_is_refused_end_to_end(self):
        old = 'def f(request):\n    if not request.user.is_staff:\n        raise PermissionDenied\n    return 1\n'
        new = 'def f(request):\n    return 1\n'
        with self.assertRaisesMessage(policy.PolicyViolation, 'with protection'):
            policy.check_patch(make_patch(code=(old, new)))


class LimitTests(SimpleTestCase):
    def test_the_limits_are_what_the_documentation_promises(self):
        self.assertEqual((policy.MAX_CODE_FILES, policy.MAX_CODE_LINES, policy.MAX_TEST_FILES, policy.MAX_TEST_LINES,
                          policy.MAX_PATCH_BYTES), (3, 60, 2, 200, 60_000))

    def test_code_lines_boundary(self):
        lines = [f'x{i} = {i}\n' for i in range(60)]
        old = ''.join(lines)
        edge = ''.join(f'y{i} = {i}\n' for i in range(30)) + ''.join(lines[30:])     # first 30 replaced: 30 removed + 30 added
        self.assertEqual(policy.check_patch(make_patch(code=(old, edge))).code_lines, 60)
        with self.assertRaisesMessage(policy.PolicyViolation, 'The fix is too large'):
            policy.check_patch(make_patch(code=(old, edge + 'z = 1\n')))

    def test_code_files_boundary(self):
        def files(n):
            return ''.join(make_patch(code=('a = 1\n', 'a = 2\n'), code_path=f'backend/catalog/{name}.py', test=None)
                           for name in ('views', 'serializers', 'api_views', 'flora_feed')[:n])
        policy.check_patch(files(3) + _diff(TESTS, '', BODY))
        with self.assertRaisesMessage(policy.PolicyViolation, 'Too many code files'):
            policy.check_patch(files(4) + _diff(TESTS, '', BODY))

    def test_test_file_and_test_line_boundaries(self):
        two = _diff('backend/catalog/tests_a.py', '', BODY) + _diff('backend/catalog/tests_b.py', '', BODY)
        code = _diff(VIEWS, 'a = 1\n', 'a = 2\n')
        policy.check_patch(code + two)
        with self.assertRaisesMessage(policy.PolicyViolation, 'Too many test files'):
            policy.check_patch(code + two + _diff('backend/catalog/tests_c.py', '', BODY))
        padding = 200 - len(BODY.splitlines())
        policy.check_patch(code + _diff(TESTS, '', BODY + ''.join(f'# {i}\n' for i in range(padding))))
        with self.assertRaisesMessage(policy.PolicyViolation, 'The tests are too large'):
            policy.check_patch(code + _diff(TESTS, '', BODY + ''.join(f'# {i}\n' for i in range(padding + 1))))

    def test_patch_size_boundary(self):
        huge = 'a = 1\n' + '# ' + 'x' * 70 + '\n'
        with self.assertRaisesMessage(policy.PolicyViolation, 'The patch is too large'):
            policy.parse_patch(make_patch(code=('a = 1\n', huge * 900)))

    def test_a_long_added_line_is_refused(self):
        with self.assertRaisesMessage(policy.PolicyViolation, 'is too long'):
            policy.check_patch(make_patch(code=('a = 1\n', 'a = "' + 'x' * policy.MAX_LINE_CHARS + '"\n')))


class RuleTests(SimpleTestCase):
    def test_existing_tests_cannot_be_modified_and_a_file_cannot_appear_twice(self):
        modify = _diff('backend/catalog/tests_old.py', 'a = 1\n', 'a = 2\n')
        with self.assertRaisesMessage(policy.PolicyViolation, 'Existing tests cannot be changed'):
            policy.check_patch(make_patch() + modify)
        twice = make_patch() + _diff(VIEWS, 'a = 1\n', 'a = 3\n')
        with self.assertRaisesMessage(policy.PolicyViolation, 'is listed twice'):
            policy.check_patch(twice)

    def test_a_path_that_merely_starts_like_an_allowed_one_is_not_allowed(self):
        for path in ('backend/catalog/views.pyc', 'backend/catalog/views.py.bak', 'backend/catalog/tests.py/evil.py',
                     'backend/catalog/xviews.py', 'backend/blog/models.py', 'backend/catalog/migrations/0001_initial.py'):
            self.assertIsNone(policy.classify(path), path)

    def test_the_hash_does_not_depend_on_line_endings_and_is_fixed_at_import(self):
        data = Path(policy.__file__).read_bytes()
        as_lf = hashlib.sha256(data.replace(b'\r\n', b'\n')).hexdigest()[:16]
        as_crlf = hashlib.sha256(data.replace(b'\r\n', b'\n').replace(b'\n', b'\r\n').replace(b'\r\n', b'\n')).hexdigest()[:16]
        self.assertEqual(as_lf, as_crlf)
        self.assertEqual(policy.policy_sha(), as_lf)

    def test_nfkc_and_hidden_characters_in_added_lines(self):
        for line in ('ｏｓ.system(a)', 'x = 1  # ​', 'x = 1  # ‮', 'x = 1\x0b', 'x = " "'):
            with self.subTest(line=repr(line)), self.assertRaises(policy.PolicyViolation):
                policy.check_added('code', 'backend/catalog/views.py', [line])
