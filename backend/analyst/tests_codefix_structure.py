"""Structural (AST) layer of the auto-fix policy: what cannot be added or changed even when there are
no blacklisted words."""
from django.test import SimpleTestCase

from analyst import codefix_policy as policy

PATH = 'backend/courses/api_views.py'
TEST_PATH = 'backend/courses/tests_autofix_probe.py'

OLD = '''"""Course lessons."""
import logging

from django.http import JsonResponse
from rest_framework.decorators import api_view

log = logging.getLogger(__name__)
LIMIT = 10


def _clean(value):
    return str(value or '').strip()


@api_view(['GET'])
def lesson_content(request, pk):
    if not request.GET.get('lang'):
        return JsonResponse({'detail': 'no'}, status=401)
    if pk < 0:
        raise ValueError('pk')
    data = {'pk': pk}
    return JsonResponse(data)


class Lesson(object):
    def title(self, item):
        return _clean(item)
'''


def with_func(body, name='helper_x'):
    text = '\n'.join('    ' + line for line in body)
    return OLD.rstrip('\n') + f'\n\n\ndef {name}(x, request=None):\n{text}\n    return x\n'


def with_module(lines):
    return OLD.rstrip('\n') + '\n\n\n' + '\n'.join(lines) + '\n'


class Base(SimpleTestCase):
    def blocked(self, new, rule, old=OLD, kind='code', path=PATH):
        with self.assertRaises(policy.PolicyViolation) as ctx:
            policy.check_structure(path, kind, old, new)
        self.assertIn(rule, str(ctx.exception))

    def passes(self, new, old=OLD, kind='code', path=PATH):
        policy.check_structure(path, kind, old, new)


class PayloadTests(Base):
    def test_monkey_patching_foreign_objects(self):
        for body in (['from rest_framework.views import APIView', 'APIView.check_permissions = lambda self, request: None'],
                     ['v = APIView', 'v.check_permissions = lambda self, request: None'],
                     ['a, b = APIView, 1', 'a.check_permissions = None'],
                     ['for c in (APIView,):', '    c.check_permissions = None'],
                     ['AnonymousUser.is_active = True'],
                     ['log.disabled = True'],
                     ['LIMIT = 5', 'JsonResponse.status_code = 500']):
            self.blocked(with_func(body), 'R', )

    def test_module_level_side_effects(self):
        self.blocked(with_module(['APIView.check_permissions = lambda self, request: None']), 'R1')
        self.blocked(with_module(['print("x")']), 'R1')
        self.blocked(with_module(['for i in range(3):', '    LIMIT = i']), 'R1')

    def test_reflection_and_dynamic_resolution(self):
        for body in (["setattr(AnonymousUser, 'is_' + 'staff', True)"],
                     ["f = getattr(x, 'a' + 'b')"],
                     ["f = getattr(x, '__cla' + 'ss__')"],
                     ["f = getattr(x, name)"],
                     ["g = vars(x)"],
                     ["T = type('T', (), {})"],
                     ["c = type(x)"],
                     ["import functools", "functools.reduce(getattr, ['a'], x)"],
                     ["y = '{0.__class__}'.format(x)"],
                     ["y = x.__class__"],
                     ["y = x.__dict__"]):
            self.blocked(with_func(body), 'R3')

    def test_imports(self):
        self.blocked(with_func(['import types']), 'R4')
        self.blocked(with_func(['import os']), 'R')
        self.blocked(with_func(['from orders.models import Order']), 'R4')
        self.blocked(with_func(['from analyst import access']), 'R4')
        self.blocked(with_func(['from django.db import connection']), 'R4')
        self.blocked(with_func(['from core.llm import call_text']), 'R4')

    def test_dangerous_names_even_when_the_import_is_already_there(self):
        old = OLD.replace('import logging\n', 'import logging\nimport os\n')
        self.blocked(old.rstrip('\n') + '\n\n\ndef g(x):\n    y = os\n    return x\n', 'R10', old=old)
        self.blocked(old.rstrip('\n') + '\n\n\ndef g(x):\n    os.system("id")\n    return x\n', 'structural check', old=old)

    def test_import_string_is_refused_even_when_imported_earlier(self):
        old = OLD.replace('import logging\n', 'import logging\nfrom django.utils.module_loading import import_string\n')
        self.blocked(old.rstrip('\n') + '\n\n\ndef g(x):\n    import_string("o" + "s.system")("id")\n    return x\n', 'R3', old=old)

    def test_swallowing_errors(self):
        self.blocked(with_func(['try:', '    x = int(x)', 'except Exception:', '    x = 0']), 'R5')
        self.blocked(with_func(['try:', '    x = int(x)', 'except:', '    x = 0']), 'R5')
        self.blocked(with_func(['try:', '    x = int(x)', 'except ValueError:', '    pass']), 'R5')

    def test_silencing_logs(self):
        for body in (['import logging', 'logging.disable(50)'],
                     ['log.setLevel(60)'],
                     ['logging.getLogger("x").setLevel(60)']):
            self.blocked(with_func(body), 'R6')
        self.blocked(with_func(['logging.getLogger().handlers.clear()']), 'structural check')       # R6 and R11 at the same time
        old = OLD.replace('import logging\n', 'import logging\nimport warnings\n')
        self.blocked(old.rstrip('\n') + '\n\n\ndef g(x):\n    warnings.simplefilter("ignore")\n    return x\n', 'R6', old=old)

    def test_data_writes(self):
        self.blocked(with_func(['from courses.models import Course', 'Course.objects.filter(id=1).update(price=0)']), 'R11')
        self.blocked(with_func(['from courses.models import Course', 'q = Course.objects.all()', 'q.update(price=0)']), 'R11')
        for body in (['x.delete()'], ['x.save()'], ['x.objects.create(a=1)']):
            self.blocked(with_func(body), 'R3')

    def test_secrets_and_personal_data_words(self):
        self.blocked(with_func(['y = x.customer_email']), 'R8')
        self.blocked(with_func(["y = x['phone']"]), 'R8')
        self.blocked(with_func(["y = x['token']"]), 'R8')
        self.blocked(with_func(['password = 1']), 'R8')

    def test_hidden_identifiers_and_globals(self):
        self.blocked(with_func(['café = 1']), 'R9')   # any non-ASCII identifier is refused
        self.blocked(with_func(['ｏｓ.system("id")']), 'structural check')   # Python folds full-width letters to ASCII
        self.blocked(with_func(['global LIMIT', 'LIMIT = 1']), 'R12')

    def test_syntax_errors_and_cr_are_refused(self):
        self.blocked(OLD + '\ndef broken(:\n', 'does not compile')
        self.blocked(OLD.replace('\n', '\r\n'), 'CR')

    def test_deeply_nested_source_does_not_crash_the_gate(self):
        nested = with_func(['y = ' + '(' * 1500 + '1' + ')' * 1500])
        with self.assertRaises(policy.PolicyViolation):
            policy.check_structure(PATH, 'code', OLD, nested)


class BenignFixTests(Base):
    def test_typical_defensive_fixes_pass(self):
        for body in (['if x is None:', '    return None', 'x = str(x or "").strip()'],
                     ['try:', '    n = int(x)', 'except (TypeError, ValueError):', '    n = 0'],
                     ['import re', 'x = re.sub(r"\\s+", " ", str(x))'],
                     ['out = {}', 'out["a"] = 1', 'out["b"] = out.get("a", 0) + 1'],
                     ['rows = [{"a": i} for i in range(3)]', 'rows[0]["b"] = 1'],
                     ['data = dict(request.data) if request is not None else {}', 'data["x"] = 1'],
                     ['resp = list()', 'resp.append(1)', 'resp[0] = 2'],
                     ['from django.utils.text import slugify', 'x = slugify(str(x))'],
                     ['log.warning("could not parse %s", x)'],
                     ['x = getattr(x, "slug", None)']):
            self.passes(with_func(body))

    def test_adding_a_helper_and_editing_a_body_pass(self):
        self.passes(OLD.rstrip('\n') + '\n\n\ndef _tidy(s):\n    """doc"""\n    return " ".join(str(s or "").split())\n')
        self.passes(OLD.replace("    data = {'pk': pk}", "    data = {'pk': pk, 'ok': True}"))

    def test_adding_a_new_guard_is_fine(self):
        self.passes(OLD.replace("    data = {'pk': pk}", "    if pk > 1000:\n        raise ValueError('pk')\n    data = {'pk': pk}"))

    def test_constructs_that_were_already_in_the_file_stay_allowed(self):
        old = OLD.replace("return str(value or '').strip()", "return str(value.customer_email or '').strip()")
        self.passes(old.replace("value.customer_email or ''", "value.customer_email or '-'"), old=old)


class ExistingGuardsTests(Base):
    def test_removing_or_changing_a_decorator_is_refused(self):
        self.blocked(OLD.replace("@api_view(['GET'])\n", ''), 'R7')
        self.blocked(OLD.replace("['GET']", "['GET', 'POST']"), 'R7')

    def test_removing_or_editing_an_early_exit_check_is_refused(self):
        self.blocked(OLD.replace("    if pk < 0:\n        raise ValueError('pk')\n", ''), 'R7')
        self.blocked(OLD.replace('pk < 0', 'pk < 0 and False'), 'R7')
        self.blocked(OLD.replace("raise ValueError('pk')", "raise KeyError('pk')"), 'R7')
        self.blocked(OLD.replace('status=401', 'status=200'), 'R7')

    def test_removing_or_renaming_functions_is_refused(self):
        self.blocked(OLD.replace('def _clean(value)', 'def _clean2(value)').replace('_clean(item)', '_clean2(item)'), 'R7')
        self.blocked(OLD.replace('    def title(self, item):\n        return _clean(item)\n', '    pass\n'), 'R7')
        self.blocked(OLD.replace('class Lesson(object)', 'class Lesson()'), 'R7')


class TestFileRulesTests(Base):
    TEST_OLD = ''

    def new_test(self, body):
        return ('from django.contrib.auth import get_user_model\nfrom django.test import TestCase\n\n\n'
                'class T(TestCase):\n    def test_x(self):\n' + ''.join('        ' + ln + '\n' for ln in body))

    def check(self, body):
        policy.check_structure(TEST_PATH, 'test', '', self.new_test(body))

    def test_ordinary_django_test_code_is_fine(self):
        self.check(['User = get_user_model()', 'u = User.objects.create(username="a")', 'u.save()', 'u.delete()',
                    'r = self.client.get("/api/products/")', 'self.assertEqual(r.status_code, 200)'])

    def test_escapes_are_refused_in_tests_too(self):
        for body in (["setattr(self, 'x', 1)"], ['import_string("os.system")'], ['print("OK")'], ['open("/etc/passwd")'],
                     ['exec("1")'], ['y = self.__class__']):
            with self.assertRaises(policy.PolicyViolation, msg=body[0]):
                self.check(body)

    def test_module_level_side_effects_are_refused_in_tests(self):
        with self.assertRaisesMessage(policy.PolicyViolation, 'R1'):
            policy.check_structure(TEST_PATH, 'test', '', 'import logging\nlogging.disable(50)\n')

    def test_counting_test_methods(self):
        self.assertEqual(policy.count_tests(self.new_test(['pass'])), 1)
        self.assertEqual(policy.count_tests('class A:\n    def helper(self):\n        pass\n'), 0)


def _patch(code_old, code_new, test_body='        self.assertTrue(True)\n', code_path=PATH, test_path=TEST_PATH):
    test = ('from django.test import TestCase\n\n\nclass T(TestCase):\n    def test_x(self):\n' + test_body)
    return policy.diff_text(code_path, code_old, code_new) + policy.diff_text(test_path, '', test)


class PatchLevelTests(SimpleTestCase):
    def reader(self, text):
        return lambda path: text if path == PATH else None

    def test_a_good_patch_passes_the_full_check(self):
        new = OLD.replace("    data = {'pk': pk}", "    pk = int(pk or 0)\n    data = {'pk': pk}")
        report = policy.check_patch(_patch(OLD, new), read_old=self.reader(OLD))
        self.assertEqual(report.tests_expected, 1)
        self.assertEqual(report.code_files, [PATH])

    def test_a_patch_that_does_not_match_the_file_is_refused(self):
        new = OLD.replace("    data = {'pk': pk}", "    pk = int(pk or 0)\n    data = {'pk': pk}")
        other = OLD.replace("data = {'pk': pk}", "data = {'id': pk}")
        with self.assertRaisesMessage(policy.PolicyViolation, 'does not match'):
            policy.check_patch(_patch(OLD, new), read_old=self.reader(other))

    def test_a_patch_with_a_wrong_hunk_position_is_refused(self):
        patch = _patch(OLD, OLD.replace("    data = {'pk': pk}", "    pk = int(pk or 0)\n    data = {'pk': pk}"))
        with self.assertRaises(policy.PolicyViolation):
            policy.check_patch(patch.replace('@@ -', '@@ -9', 1), read_old=self.reader(OLD))

    def test_the_structural_layer_runs_inside_check_patch(self):
        new = OLD.replace("    data = {'pk': pk}", "    APIView.check_permissions = None\n    data = {'pk': pk}")
        with self.assertRaisesMessage(policy.PolicyViolation, 'R2'):
            policy.check_patch(_patch(OLD, new), read_old=self.reader(OLD))

    def test_hidden_characters_in_added_lines_are_refused(self):
        for bad in ('\u200b', '\u2028', '\x0b', '\x0c', '\u202e', '\ufeff', '\ue000'):
            new = OLD.replace("    data = {'pk': pk}", f"    data = {{'pk': pk}}  # a{bad}b")
            with self.assertRaises(policy.PolicyViolation, msg=repr(bad)):
                policy.check_patch(_patch(OLD, new), read_old=self.reader(OLD))

    def test_text_layer_sees_through_fullwidth_letters(self):
        new = OLD.replace("    data = {'pk': pk}", "    ｓｕｂｐｒｏｃｅｓｓ.run(['id'])\n    data = {'pk': pk}")
        with self.assertRaises(policy.PolicyViolation):
            policy.check_patch(_patch(OLD, new), read_old=self.reader(OLD))

    def test_a_test_file_without_test_methods_is_refused(self):
        patch = policy.diff_text(PATH, OLD, OLD.replace("{'pk': pk}", "{'pk': pk, 'a': 1}")) + policy.diff_text(TEST_PATH, '', 'x = 1\n')
        with self.assertRaisesMessage(policy.PolicyViolation, 'test*'):
            policy.check_patch(patch, read_old=self.reader(OLD))

    def test_files_with_windows_line_endings_are_not_fixable(self):
        crlf = OLD.replace('\n', '\r\n')
        new = OLD.replace("{'pk': pk}", "{'pk': pk, 'a': 1}")
        with self.assertRaisesMessage(policy.PolicyViolation, 'CR'):
            policy.check_patch(_patch(OLD, new), read_old=self.reader(crlf))
        self.assertIn('CR', policy.file_problem(crlf))
        self.assertIn('trailing newline', policy.file_problem('x = 1'))
        self.assertIn('NUL', policy.file_problem('x = 1\x00\n'))
        self.assertEqual(policy.file_problem(OLD), '')

    def test_check_applied_compares_the_real_files(self):
        new = OLD.replace("    data = {'pk': pk}", "    pk = int(pk or 0)\n    data = {'pk': pk}")
        patch = _patch(OLD, new)
        files = {PATH: new, TEST_PATH: 'class T: pass\n'}
        policy.check_applied(patch, self.reader(OLD), files.get)
        evil = new + '\nAPIView.check_permissions = None\n'
        with self.assertRaises(policy.PolicyViolation):
            policy.check_applied(patch, self.reader(OLD), {PATH: evil, TEST_PATH: ''}.get)
        with self.assertRaisesMessage(policy.PolicyViolation, 'does not exist'):
            policy.check_applied(patch, self.reader(OLD), {TEST_PATH: ''}.get)


class DiffTextTests(SimpleTestCase):
    def roundtrip(self, old, new):
        patch = policy.diff_text('x.py', old, new)
        changes = policy.parse_patch(patch)
        self.assertEqual(policy.apply_hunks(old, changes[0].hunks), new)
        return patch

    def test_lines_are_split_only_on_newline(self):
        old = 'a = 1\nb = "x\u2028y"\nc = "p\x0cq"\nd = "r\x85s"\n'
        new = old.replace('c = ', 'c2 = ')
        patch = self.roundtrip(old, new)
        self.assertIn('\u2028', patch)

    def test_new_file_and_edge_hunks(self):
        self.roundtrip('', 'a = 1\n')
        self.roundtrip('a = 1\nb = 2\nc = 3\n', 'a = 1\nb = 2\nc = 3\nd = 4\n')
        self.roundtrip('a = 1\nb = 2\nc = 3\n', 'z = 0\na = 1\nb = 2\nc = 3\n')
        self.roundtrip('a = 1\nb = 2\nc = 3\n', 'a = 1\nc = 3\n')

    def test_content_without_trailing_newline_is_refused(self):
        with self.assertRaises(policy.PolicyViolation):
            policy.diff_text('x.py', 'a = 1', 'a = 2\n')

    def test_a_line_that_looks_like_a_header_survives_the_roundtrip(self):
        old = 'a = 1\n-- b/x.py\n++ trick\n@@ -1 +1 @@\n'
        self.roundtrip(old, old.replace('a = 1', 'a = 2'))
