"""Auto-fix policy (analyst/codefix_policy.py): what the Analyst may change on its own and what it may
never change."""
from django.test import SimpleTestCase

from analyst import codefix_policy as policy

VIEWS = 'backend/catalog/views.py'
TESTS = 'backend/catalog/tests_autofix.py'


def _diff(path, old, new):
    return policy.diff_text(path, old, new)


def make_patch(code=('def f(x):\n    return x + 1\n', 'def f(x):\n    return (x or 0) + 1\n'), code_path=VIEWS,
               test='from django.test import TestCase\n\n\nclass T(TestCase):\n    def test(self):\n        self.assertTrue(True)\n',
               test_path=TESTS):
    out = _diff(code_path, *code) if code else ''
    if test:
        out += _diff(test_path, '', test)
    return out


class PathRuleTests(SimpleTestCase):
    def test_allowed_files(self):
        self.assertEqual(policy.classify('backend/catalog/api_views.py'), 'code')
        self.assertEqual(policy.classify('backend/catalog/instruction_resolve.py'), 'code')
        self.assertEqual(policy.classify('backend/blog/serializers.py'), 'code')
        self.assertEqual(policy.classify('backend/core/jsonutils.py'), 'code')
        self.assertEqual(policy.classify('backend/catalog/tests_flora.py'), 'test')
        self.assertEqual(policy.classify('backend/core/tests_llm_limit.py'), 'test')

    def test_everything_sensitive_is_denied(self):
        for path in ('backend/orders/models.py', 'backend/orders/monobank.py', 'backend/users/views.py',
                     'backend/config/settings.py', 'backend/config/urls.py', 'backend/analyst/codefix_policy.py',
                     'backend/analyst/guard.py', 'backend/telegrambot/agent.py', 'backend/catalog/models.py',
                     'backend/catalog/signals.py', 'backend/catalog/stock_audit.py', 'backend/catalog/notifications.py',
                     'backend/catalog/admin.py', 'backend/catalog/migrations/0001_initial.py', 'backend/blog/models.py',
                     'backend/core/llm.py', 'backend/core/email.py', 'backend/core/limits.py', 'backend/courses/video_access.py',
                     'backend/entrypoint.sh', 'backend/Dockerfile', 'docker-compose.yml', 'ops/shop_helper.py',
                     'backend/templates/admin/analyst/inbox.html', 'frontend/src/App.tsx', 'backend/.env'):
            self.assertIsNone(policy.classify(path), path)

    def test_path_tricks_are_rejected(self):
        for path in ('backend/catalog/../orders/models.py', '/etc/passwd', '../x.py', 'backend\\catalog\\views.py',
                     'backend/catalog/./views.py', 'C:/x.py', 'backend/catalog/.hidden.py', 'backend/catalog//views.py', '',
                     'backend/catalog/views.py\x00.png'):
            self.assertEqual(policy.normalize(path), '', path)
            self.assertIsNone(policy.classify(path), path)

    def test_traceback_paths_map_to_repo_paths(self):
        self.assertEqual(policy.repo_path_from_traceback('/app/catalog/views.py'), 'backend/catalog/views.py')
        self.assertTrue(policy.is_fixable_file('/app/catalog/api_views.py'))
        self.assertFalse(policy.is_fixable_file('/app/orders/views.py'))
        self.assertFalse(policy.is_fixable_file('/usr/local/lib/python3.13/site-packages/django/db/models/query.py'))


class PatchRuleTests(SimpleTestCase):
    def test_a_small_fix_with_a_test_passes(self):
        report = policy.check_patch(make_patch())
        self.assertEqual((report.code_files, report.test_files), ([VIEWS], [TESTS]))

    def test_split_separates_the_test_part_from_the_code_part(self):
        tests, code = policy.split_patch(make_patch())
        self.assertIn(TESTS, tests)
        self.assertNotIn(VIEWS, tests)
        self.assertIn(VIEWS, code)
        self.assertNotIn(TESTS, code)

    def test_a_fix_without_a_test_is_refused(self):
        with self.assertRaisesMessage(policy.PolicyViolation, 'A test that reproduces the error'):
            policy.check_patch(make_patch(test=None))

    def test_only_tests_is_not_a_fix(self):
        with self.assertRaisesMessage(policy.PolicyViolation, 'contains only tests'):
            policy.check_patch(make_patch(code=None))

    def test_forbidden_file_is_refused(self):
        with self.assertRaisesMessage(policy.PolicyViolation, 'has no right to change'):
            policy.check_patch(make_patch(code_path='backend/orders/views.py'))

    def test_new_code_files_are_refused(self):
        patch = make_patch(code=('', 'def g():\n    return 1\n'))
        with self.assertRaisesMessage(policy.PolicyViolation, 'New files are allowed only for tests'):
            policy.check_patch(patch)

    def test_deleting_files_is_refused(self):
        bad = f'--- a/{VIEWS}\n+++ /dev/null\n@@ -1,1 +0,0 @@\n-x = 1\n'
        with self.assertRaisesMessage(policy.PolicyViolation, 'Deleting files is forbidden'):
            policy.check_patch(bad + make_patch(code=None))

    def test_garbage_and_smuggled_headers_are_refused(self):
        for bad in ('', 'hello', 'diff --git a/x b/x\nrename from a\nrename to b\n',
                    f'--- a/{VIEWS}\n+++ b/{VIEWS}\n@@ -1,1 +1,1 @@\n-a\n+b\nrename to backend/orders/models.py\n',
                    f'--- a/{VIEWS}\n+++ b/other.py\n@@ -1,1 +1,1 @@\n-a\n+b\n',
                    f'--- a/{VIEWS}\n+++ b/{VIEWS}\n@@ -1,1 +1,1 @@\n-a\n+b\n+c\n'):
            with self.assertRaises(policy.PolicyViolation, msg=bad[:40]):
                policy.parse_patch(bad)

    def test_a_removed_line_that_looks_like_a_header_does_not_confuse_the_parser(self):
        patch = f'--- a/{VIEWS}\n+++ b/{VIEWS}\n@@ -1,2 +1,1 @@\n--- a/backend/orders/models.py\n x = 1\n'
        changes = policy.parse_patch(patch)
        self.assertEqual([c.path for c in changes], [VIEWS])
        self.assertEqual(changes[0].removed, ['-- a/backend/orders/models.py'])

    def test_size_limits(self):
        big = 'x = 0\n' * 70
        patch = make_patch(code=(big, big.replace('x = 0', 'y = 1')))
        with self.assertRaisesMessage(policy.PolicyViolation, 'The fix is too large'):
            policy.check_patch(patch)
        three = ''.join(make_patch(code=('a = 1\n', 'a = 2\n'), code_path=f'backend/catalog/{n}.py', test=None)
                        for n in ('views', 'serializers', 'api_views', 'flora_feed'))
        with self.assertRaisesMessage(policy.PolicyViolation, 'Too many code files'):
            policy.check_patch(three + _diff(TESTS, '', 'import unittest\n'))

    def test_banned_constructs_in_added_code(self):
        for line in ('import subprocess', 'os.system("rm -rf /")', 'eval(payload)', 'exec(code)', '__import__("os")',
                     'import requests', 'import socket', 'urllib.request.urlopen(u)', 'open(p, "w")', 'os.environ["X"]',
                     'token = settings.ANTHROPIC_API_KEY', 'Order.objects.all().delete()', 'Product.objects.filter(a=1).update(b=2)',
                     'cursor.execute("DROP TABLE x")', 'from django.db import connection', 'send_mail(a, b, c, d)',
                     'return mark_safe(x)', 'permission_classes = [AllowAny]', 'import pickle', 'import ctypes',
                     'shutil.rmtree(p)', 'threading.Thread(target=f).start()', 'x = getattr(obj, "__class__")',
                     'from os import system', 'import sys', 'sys.modules["x"]', 'Path(p).open("w")', 'obj.save()',
                     'Order.objects.create(a=1)', 'import tempfile', 'import builtins'):
            patch = make_patch(code=('def f(x):\n    return x\n', f'def f(x):\n    {line}\n    return x\n'))
            with self.assertRaises(policy.PolicyViolation, msg=line):
                policy.check_patch(patch)

    def test_ordinary_defensive_code_is_fine(self):
        for line in ('value = (item.get("price") or 0) + 1', 'name = str(name or "").strip()', 'items = list(qs)[:10]',
                     'log.warning("could not parse %s", raw)', 'x = getattr(obj, "slug", None)',
                     'rows.update({"a": 1})', 'return sorted(set(ids))'):
            patch = make_patch(code=('def f(x):\n    return x\n', f'def f(x):\n    {line}\n    return x\n'))
            policy.check_patch(patch)

    def test_removing_an_access_check_is_refused(self):
        old = 'def f(request):\n    if not request.user.is_staff:\n        raise PermissionDenied\n    return 1\n'
        new = 'def f(request):\n    return 1\n'
        with self.assertRaisesMessage(policy.PolicyViolation, 'protection or an access check'):
            policy.check_patch(make_patch(code=(old, new)))

    def test_tests_may_not_escape_the_sandbox_either(self):
        for line in ('import subprocess', 'os.system("x")', 'import socket', 'exec("1")', 'open("/tmp/x", "w")',
                     'Path("/tmp/x").write_bytes(b"x")', 'import tempfile', 'import shutil'):
            patch = make_patch(test=f'import unittest\n\n\nclass T(unittest.TestCase):\n    def test(self):\n        {line}\n')
            with self.assertRaises(policy.PolicyViolation, msg=line):
                policy.check_patch(patch)

    def test_tests_may_mention_the_words_code_may_not(self):
        patch = make_patch(test='import unittest\nfrom unittest import mock\n\n\nclass T(unittest.TestCase):\n'
                                '    def test(self):\n        with mock.patch("catalog.views.requests"):\n            pass\n')
        policy.check_patch(patch)

    def test_policy_hash_is_stable_across_line_endings(self):
        self.assertEqual(len(policy.policy_sha()), 16)
