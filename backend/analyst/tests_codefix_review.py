"""Regression tests for the findings of the independent policy review (stage B): every bypass the
reviewer proved in practice must be closed,
while typical harmless fixes must still pass."""
from django.test import SimpleTestCase

from analyst import codefix_policy as policy

CATALOG = 'backend/catalog/api_views.py'
COURSES = 'backend/courses/api_views.py'

API_OLD = '''"""API."""
from rest_framework.response import Response
from rest_framework.decorators import api_view


@api_view(['POST'])
def clear_variants(request, pk):
    from catalog.models import Product
    obj = Product.objects.get(pk=pk)
    return Response({'id': obj.id})
'''

COURSE_OLD = '''"""API."""
from rest_framework.response import Response
from rest_framework.decorators import api_view
from rest_framework import status


def _user_owns_course(user, course_id):
    if not user or not user.is_authenticated:
        return False
    return True


@api_view(['GET'])
def lesson_content(request, slug, lesson_id):
    owns = _user_owns_course(request.user, 1)
    if not owns:
        return Response({'detail': 'no'}, status=status.HTTP_403_FORBIDDEN)
    note = 'ok'
    return Response({'ok': True, 'note': note})
'''

THROTTLE_OLD = '''"""API."""
from rest_framework.response import Response
from rest_framework.decorators import api_view
from rest_framework.throttling import UserRateThrottle


class ReviewCreateThrottle(UserRateThrottle):
    scope = 'review_create'

    def allow_request(self, request, view):
        if request.method != 'POST':
            return True
        return super().allow_request(request, view)


@api_view(['POST'])
def create_review(request):
    return Response({'ok': True})
'''

TEST_BODY = ('from django.test import TestCase\n\n\nclass T(TestCase):\n    def test_x(self):\n        self.assertTrue(True)\n')


class Base(SimpleTestCase):
    def blocked(self, old, new, rule='', path=CATALOG):
        with self.assertRaises(policy.PolicyViolation) as ctx:
            policy.check_structure(path, 'code', old, new)
        self.assertIn(rule, str(ctx.exception))

    def passes(self, old, new, path=CATALOG):
        policy.check_structure(path, 'code', old, new)

    def full(self, old, new, path=CATALOG):
        """The same as the production pipeline does: the full patch (code + new test) through check_patch."""
        patch = policy.diff_text(path, old, new) + policy.diff_text(path.rsplit('/', 1)[0] + '/tests_autofix_probe.py', '', TEST_BODY)
        return policy.check_patch(patch, read_old=lambda p: old if p == path else None)


def with_body(lines, old=API_OLD):
    """Adds lines before `return` in `clear_variants`."""
    text = ''.join('    ' + line + '\n' for line in lines)
    return old.replace("    return Response({'id': obj.id})\n", text + "    return Response({'id': obj.id})\n")


class ManagerWritesTests(Base):
    def test_update_through_a_related_manager_is_refused(self):
        new = with_body(['obj.variants.update(stock=0, price=0)'])
        self.blocked(API_OLD, new, 'R11')
        with self.assertRaises(policy.PolicyViolation):
            self.full(API_OLD, new)

    def test_text_layer_sees_update_with_keyword_arguments(self):
        for line in ('    obj.variants.update(stock=0, price=0)', '    obj.variants.update(**fields)', '    obj.variants.update('):
            with self.assertRaises(policy.PolicyViolation, msg=line):
                policy.check_added('code', CATALOG, [line])
        policy.check_added('code', CATALOG, ['    merged.update(other)', '    if a == b:'])

    def test_other_relation_and_cache_writers_are_refused(self):
        for line in ('obj.categories.clear()', 'obj.categories.add(1)', 'obj.categories.set([1, 2])', 'obj.categories.remove(1)',
                     'request.session.update(a=1)', 'obj.variants.update(**{"stock": 0})'):
            self.blocked(API_OLD, with_body([line]), 'R11')

    def test_methods_cannot_be_smuggled_without_a_call(self):
        for lines in (['f = obj.variants.delete', 'f()'], ['f = obj.save'], ['for fn in (obj.delete,):', '    fn()'],
                      ["getattr(obj.variants, 'update')(stock=0)"], ["getattr(obj, 'delete')()"], ["f = getattr(obj, 'save')"]):
            self.blocked(API_OLD, with_body(lines), 'R')

    def test_private_orm_writers_and_signal_hooks_are_refused(self):
        for line in ('obj.variants._raw_delete("default")', 'post_save.connect(handler)', 'post_save.send(sender=None)',
                     'Product.add_to_class("x", 1)'):
            self.blocked(API_OLD, with_body([line]), 'R3')

    def test_plain_local_collections_are_fine(self):
        for lines in (['seen = set()', 'seen.add(pk)'], ['out = {}', 'out.update({"a": 1})'], ['names = []', 'names.append(1)', 'names.remove(1)'],
                      ['out = dict(request.data)', 'out.update(a=1)'], ['items = list(range(3))', 'items.clear()'],
                      ['out = {}', 'out.update(request.data)']):
            self.passes(API_OLD, with_body(lines))


SHOWN_OLD = '''"""API."""
from rest_framework.response import Response
from rest_framework.decorators import api_view
from rest_framework import status


def _is_published(course):
    return bool(course) and course.is_published


@api_view(['GET'])
def lesson_content(request, slug, lesson_id):
    shown = _is_published(slug)
    if not shown:
        return Response({'detail': 'no'}, status=status.HTTP_404_NOT_FOUND)
    note = 'ok'
    return Response({'ok': True, 'note': note})
'''


class GuardDataflowTests(Base):
    """VISIBILITY checks (published/active): not strictly frozen, but the data flow into them is guarded
(R7b-R7d)."""

    def test_a_value_that_feeds_a_visibility_check_cannot_be_changed(self):
        new = SHOWN_OLD.replace("    shown = _is_published(slug)\n",
                                "    shown = _is_published(slug) or request.GET.get('preview_key') == 'a1b2c3'\n")
        self.blocked(SHOWN_OLD, new, 'R7b', COURSES)
        with self.assertRaises(policy.PolicyViolation):
            self.full(SHOWN_OLD, new, COURSES)

    def test_a_new_assignment_to_a_checked_variable_before_the_check_is_refused(self):
        new = SHOWN_OLD.replace("    if not shown:", "    if request.GET.get('k'):\n        shown = True\n    if not shown:")
        self.blocked(SHOWN_OLD, new, 'R7b', COURSES)
        new = SHOWN_OLD.replace("    if not shown:", "    slug = None\n    if not shown:")
        self.blocked(SHOWN_OLD, new, 'R7b', COURSES)

    def test_an_existing_check_cannot_be_wrapped_in_a_new_condition(self):
        new = SHOWN_OLD.replace("    if not shown:\n        return Response({'detail': 'no'}, status=status.HTTP_404_NOT_FOUND)\n",
                                "    if not request.GET.get('preview'):\n        if not shown:\n"
                                "            return Response({'detail': 'no'}, status=status.HTTP_404_NOT_FOUND)\n")
        self.blocked(SHOWN_OLD, new, 'R7', COURSES)

    def test_a_new_early_exit_next_to_a_visibility_check_cannot_hand_out_data(self):
        new = SHOWN_OLD.replace("    shown = _is", "    if request.GET.get('k') == 'x':\n        return Response(slug)\n    shown = _is")
        self.blocked(SHOWN_OLD, new, 'R7c', COURSES)

    def test_a_helper_that_decides_the_check_is_frozen(self):
        new = SHOWN_OLD.replace("    return bool(course) and course.is_published\n", "    return bool(course) and course.is_published or course.pk\n")
        self.blocked(SHOWN_OLD, new, 'R7d', COURSES)
        lost = SHOWN_OLD.replace("    return bool(course) and course.is_published\n", "    return bool(course)\n")
        self.blocked(SHOWN_OLD, lost, 'R7', COURSES)                       # removing the visibility check itself is not allowed either (R7k)

    def test_a_check_statement_that_raises_by_itself_is_protected(self):
        old = SHOWN_OLD.replace("    shown = _is_published(slug)\n", "    _require_published(slug)\n    shown = True\n")
        self.blocked(old, old.replace("    _require_published(slug)\n", ''), 'R7', COURSES)
        self.blocked(old, old.replace("_require_published(slug)", "_require_published(lesson_id)"), 'R7', COURSES)

    def test_harmless_fixes_next_to_the_checks_still_pass(self):
        # an assignment AFTER the check does not affect it; new early constant exits and error
        # responses are allowed
        self.passes(SHOWN_OLD, SHOWN_OLD.replace("    note = 'ok'\n", "    note = str(slug or 'ok')\n"), COURSES)
        self.passes(SHOWN_OLD, SHOWN_OLD.replace("    shown = _is", "    if not slug:\n        return Response({'detail': 'no'}, status=404)\n    shown = _is"),
                    COURSES)
        self.passes(SHOWN_OLD, SHOWN_OLD.replace("    shown = _is", "    if lesson_id is None:\n        return None\n    shown = _is"), COURSES)
        self.passes(SHOWN_OLD, SHOWN_OLD.replace("    shown = _is", "    extra = (slug or '').strip()\n    shown = _is"), COURSES)

    def test_input_format_checks_are_not_treated_as_access_checks(self):
        old = API_OLD.replace("    obj = Product.objects.get(pk=pk)\n", "    email = request.data.get('email', '').strip()\n    if not email:\n"
                              "        return Response({'detail': 'email'}, status=400)\n    obj = Product.objects.get(pk=pk)\n")
        new = old.replace("request.data.get('email', '')", "(request.data.get('email') or '')")
        self.passes(old, new)


class StrongAccessFreezeTests(Base):
    """A function that itself checks access (user, permissions, ownership, payment...) is not auto-edited
at all (R7e)."""

    def test_any_change_inside_such_a_function_is_refused(self):
        for new in (COURSE_OLD.replace("    note = 'ok'\n", "    note = str(slug or 'ok')\n"),
                    COURSE_OLD.replace("    owns = _user_owns", "    if not slug:\n        return None\n    owns = _user_owns"),
                    COURSE_OLD.replace("    owns = _user_owns_course(request.user, 1)\n",
                                       "    owns = _user_owns_course(request.user, 1) or request.GET.get('preview_key') == 'a1b2c3'\n")):
            self.blocked(COURSE_OLD, new, 'R7e', COURSES)

    def test_helpers_used_by_such_a_check_are_frozen_too(self):
        new = COURSE_OLD.replace("        return False\n    return True\n", "        return False\n    return bool(course_id)\n")
        self.blocked(COURSE_OLD, new, 'R7e', COURSES)          # the helper itself checks the user

    def test_other_functions_in_the_same_file_stay_fixable(self):
        extra = "\n\ndef shorten(text, limit=10):\n    return text[:limit]\n"
        self.passes(COURSE_OLD + extra, COURSE_OLD + extra.replace('text[:limit]', "(text or '')[:limit]"), COURSES)

    def test_comments_and_blank_lines_do_not_count_as_a_change(self):
        self.passes(COURSE_OLD, COURSE_OLD.replace("    note = 'ok'\n", "    note = 'ok'   # just an explanation\n\n"), COURSES)

    def test_a_secret_or_password_check_counts_as_access_too(self):
        old = API_OLD.replace("    obj = Product.objects.get(pk=pk)\n", "    if request.data.get('secret_code') != 'x':\n"
                              "        return Response({'detail': 'no'}, status=403)\n    obj = Product.objects.get(pk=pk)\n")
        self.blocked(old, old.replace("Response({'id': obj.id})", "Response({'id': obj.id, 'n': 1})"), 'R7e')


class AliasTests(Base):
    def test_default_argument_aliases_a_foreign_class(self):
        new = THROTTLE_OLD.replace(
            "@api_view(['POST'])\ndef create_review(request):\n    return Response({'ok': True})\n",
            "def _ensure_warm(done=[], _cls=ReviewCreateThrottle):\n    if not done:\n"
            "        _cls.allow_request = lambda self, request, view: True\n        done.append(1)\n\n\n"
            "@api_view(['POST'])\ndef create_review(request):\n    _ensure_warm()\n    return Response({'ok': True})\n")
        self.blocked(THROTTLE_OLD, new, 'R2')
        with self.assertRaises(policy.PolicyViolation):
            self.full(THROTTLE_OLD, new)

    def test_keyword_only_and_lambda_defaults_too(self):
        for helper in ("def _h(*, c=ReviewCreateThrottle):\n    c.scope = 'x'\n    return 1\n",
                       "def _h(x, c=ReviewCreateThrottle.__bases__):\n    return 1\n"):
            self.blocked(THROTTLE_OLD, THROTTLE_OLD + '\n\n' + helper, 'R')
        self.blocked(THROTTLE_OLD, THROTTLE_OLD + "\n\ndef _h(x):\n    f = lambda c=ReviewCreateThrottle: c\n    return f()\n"
                     .replace('f = lambda c=ReviewCreateThrottle: c', 'f = (lambda c=ReviewCreateThrottle: setattr(c, "scope", 1))'), 'R3')

    def test_ordinary_defaults_do_not_trigger(self):
        self.passes(THROTTLE_OLD, THROTTLE_OLD + "\n\ndef _h(x, items=None, limit=10, tags=()):\n    out = list(items or [])\n    out.append(x)\n    return out[:limit]\n")

    def test_writes_into_attributes_of_parameters_are_refused(self):
        self.blocked(THROTTLE_OLD, THROTTLE_OLD + "\n\ndef _h(target):\n    target.scope = 'x'\n    return target\n", 'R2d')
        self.blocked(THROTTLE_OLD, THROTTLE_OLD + "\n\nclass K:\n    def m(self):\n        self.a.b = 1\n", 'R2d')
        self.passes(THROTTLE_OLD, THROTTLE_OLD + "\n\nclass K:\n    def m(self, v):\n        self.a = v\n        w = dict(v)\n        w['k'] = 1\n        return w\n")

    def test_storing_functions_into_attributes_is_refused(self):
        for tail in ("def _h(o):\n    p = o\n    p.m = lambda: 1\n    return p\n",
                     "def _f():\n    return 1\n\n\ndef _h():\n    d = {}\n    d['f'] = _f\n    return d\n",
                     "def _h(x):\n    r = {}\n    r['f'] = partial(print, x)\n    return r\n"):
            self.blocked(THROTTLE_OLD, THROTTLE_OLD + '\n\n' + tail, 'R')


class ImportTests(Base):
    OLD = API_OLD.replace('from rest_framework.decorators import api_view\n', 'from rest_framework.decorators import api_view\nfrom django.db import models\n')

    def test_names_imported_from_a_package_are_judged_by_their_full_name(self):
        for line in ('from django.db import connection', 'from django.db import connections', 'from django.db import transaction',
                     'from django.db.models import sql', 'from django.db.models import deletion', 'import django.db.models.deletion',
                     'from django.dispatch import receiver', 'from django.utils.crypto import get_random_string',
                     'from rest_framework.permissions import *', 'from django.db.models.query import QuerySet'):
            self.blocked(self.OLD, self.OLD + '\n\ndef _h():\n    ' + line + '\n    return 1\n', 'R4')

    def test_ordinary_imports_are_fine(self):
        for line in ('from django.db.models import Q, Count', 'from django.db.models.functions import Coalesce', 'import json',
                     'from django.utils.text import slugify', 'from rest_framework.response import Response'):
            self.passes(self.OLD, self.OLD + '\n\ndef _h():\n    ' + line + '\n    return 1\n')


class PersonalDataWordsTests(Base):
    def test_network_identifiers_count_as_personal_data(self):
        for expr in ("request.META['REMOTE_ADDR']", "request.META.get('HTTP_X_FORWARDED_FOR')", "obj.guest_name", "request.headers['user_agent']"):
            self.blocked(API_OLD, with_body([f'x = {expr}']), 'R8')


MODELS_OLD = '''"""API."""
from rest_framework.response import Response
from rest_framework.decorators import api_view
from catalog.models import Product


@api_view(['POST'])
def refresh(request, pk):
    obj = Product.objects.filter(pk=pk).first()
    if obj is None:
        return Response({'detail': 'no'}, status=404)
    obj.recalculate()
    return Response({'id': obj.id})
'''


class CallAllowlistTests(Base):
    """New calls only to safe builtins, methods of 'pure' types, modules from the list, and whatever the
file already called (R14)."""

    def with_lines(self, lines):
        text = ''.join('    ' + line + '\n' for line in lines)
        return MODELS_OLD.replace("    return Response({'id': obj.id})\n", text + "    return Response({'id': obj.id})\n")

    def test_unknown_methods_on_foreign_objects_are_refused(self):
        for line in ('obj.restock_all(100)', 'obj.dispatch()', 'obj.sync_prices()', 'Product.mark_all_sold()',
                     'obj.variants.first().set_stock_to_zero()'):
            self.blocked(MODELS_OLD, self.with_lines([line]), 'R14')

    def test_unknown_functions_are_refused(self):
        self.blocked(MODELS_OLD, self.with_lines(['restock(obj)']), 'R14')
        self.blocked(MODELS_OLD, self.with_lines(['handlers = {}', 'handlers["a"](obj)']), 'R14')
        self.blocked(MODELS_OLD, self.with_lines(['make_handler(obj)(1)']), 'R14')

    def test_a_method_taken_by_name_is_still_a_call_of_that_method(self):
        self.blocked(MODELS_OLD, self.with_lines(['fn = obj.restock_all', 'fn(100)']), 'R14')

    def test_what_the_file_already_calls_stays_allowed(self):
        self.passes(MODELS_OLD, self.with_lines(['obj.recalculate()']))
        self.passes(MODELS_OLD, self.with_lines(['Product.objects.filter(pk=pk).first()']))

    def test_safe_builtins_methods_and_listed_modules_are_allowed(self):
        for lines in (['name = str(obj.id).strip().lower()'], ['ids = sorted({1, 2}, reverse=True)', 'n = len(ids) + max(ids)'],
                      ['import re', 'x = re.sub(r"a", "b", "abc")'], ['import json', 'x = json.dumps({"a": 1})'],
                      ['from django.utils import timezone', 'x = timezone.now().isoformat()'],
                      ['from decimal import Decimal', 'x = Decimal("1.5").quantize(Decimal("1"))'],
                      ['x = Product.objects.filter(pk=pk).exclude(pk=0).order_by("pk").values_list("pk", flat=True)'],
                      ['x = obj.get_absolute_url()', 'y = obj.is_ready() or obj.has_images()'],
                      ['log = None', 'x = isinstance(obj, Product)', 'y = ", ".join(str(i) for i in range(3))']):
            self.passes(MODELS_OLD, self.with_lines(lines))

    def test_helpers_defined_by_the_patch_itself_are_allowed(self):
        new = MODELS_OLD.replace("@api_view(['POST'])\ndef refresh", "def _tidy(text):\n    return str(text or '').strip()\n\n\n@api_view(['POST'])\ndef refresh")
        new = new.replace("    obj.recalculate()\n", "    obj.recalculate()\n    note = _tidy(obj.id)\n")
        self.passes(MODELS_OLD, new)

    def test_decorators_are_calls_too(self):
        tail = "\n\n@register_hook\ndef _h(x):\n    return x\n"
        self.blocked(MODELS_OLD, MODELS_OLD + tail, 'R14')
        self.blocked(MODELS_OLD, MODELS_OLD + "\n\n@register_hook('a')\ndef _h(x):\n    return x\n", 'R14')
        self.passes(MODELS_OLD, MODELS_OLD + "\n\nclass K:\n    @property\n    def a(self):\n        return 1\n\n    @staticmethod\n    def b(x):\n        return x\n")


class LocalImportTests(Base):
    """From the project's own apps, new imports are only models, serializers and a couple of utility
modules: the rest are functions with side effects."""

    def test_modules_with_side_effects_cannot_be_pulled_in(self):
        for line in ('from catalog.stock_audit import write_off', 'from catalog.notifications import notify', 'from catalog import signals',
                     'from .stock_audit import write_off', 'from . import video_access', 'from courses.video_access import signed_url',
                     'from core.embeddings import embed_text', 'from catalog.revert import undo', 'import catalog.signals'):
            self.blocked(MODELS_OLD, MODELS_OLD + '\n\ndef _h():\n    ' + line + '\n    return 1\n', 'R4')

    def test_models_serializers_and_helpers_are_fine(self):
        for line in ('from catalog.models import ProductVariant', 'from .models import ProductVariant', 'from catalog import models',
                     'from . import serializers', 'from courses.models import Course', 'from core.jsonutils import js_json',
                     'from catalog.serializers import ProductSerializer'):
            self.passes(MODELS_OLD, MODELS_OLD + '\n\ndef _h():\n    ' + line + '\n    return 1\n')

    def test_a_module_the_file_already_imports_stays_available(self):
        old = MODELS_OLD.replace('from catalog.models import Product\n', 'from catalog.models import Product\nfrom catalog.notifications import notify\n')
        self.passes(old, old + '\n\ndef _h():\n    from catalog.notifications import notify\n    return 1\n')
        self.blocked(old, old + '\n\ndef _h():\n    from catalog.stock_audit import write_off\n    return 1\n', 'R4')


class StrongAccessWordsListTests(Base):
    def test_token_and_signature_in_text_processing_are_not_access_words(self):
        old = API_OLD.replace("    obj = Product.objects.get(pk=pk)\n", "    token = request.data.get('q', '')\n    if not token:\n"
                              "        return Response({'detail': 'q'}, status=400)\n    obj = Product.objects.get(pk=pk)\n")
        self.passes(old, old.replace("    return Response({'id': obj.id})", "    note = 1\n    return Response({'id': obj.id})"))


WRITER_OLD = '''"""API."""
from rest_framework.response import Response
from rest_framework.decorators import api_view
from catalog.models import Note


@api_view(['POST'])
def add_note(request):
    text = request.data.get('text', '')
    note = Note(text=text)
    note.save()
    return Response({'id': note.pk})


def shorten(text, limit=10):
    return text[:limit]
'''

GLOBAL_OLD = '''"""API."""
from rest_framework.response import Response
from rest_framework.decorators import api_view

PUBLISHED_STATES = ('published',)


@api_view(['GET'])
def lesson(request, state):
    if state not in PUBLISHED_STATES:
        return Response({'detail': 'no'}, status=404)
    return Response({'ok': True})
'''


class WriterFreezeTests(Base):
    """A function that itself writes to the database or sends something is not auto-edited: any change
could alter WHAT gets written (R7f)."""

    def test_any_change_inside_a_writing_function_is_refused(self):
        for new in (WRITER_OLD.replace("request.data.get('text', '')", "(request.data.get('text') or '')"),
                    WRITER_OLD.replace("    note.save()\n", "    note.text = text.strip()\n    note.save()\n")):
            self.blocked(WRITER_OLD, new, 'R7f', CATALOG)

    def test_neighbouring_functions_stay_fixable(self):
        self.passes(WRITER_OLD, WRITER_OLD.replace("return text[:limit]", "return (text or '')[:limit]"), CATALOG)

    def test_frozen_reason_names_the_writer_and_the_access_check(self):
        line = WRITER_OLD.splitlines().index("    text = request.data.get('text', '')") + 1
        self.assertIn('writes to the database', policy.frozen_reason(WRITER_OLD, line))
        self.assertEqual(policy.frozen_reason(WRITER_OLD, WRITER_OLD.splitlines().index('    return text[:limit]') + 1), '')
        self.assertIn('checks access', policy.frozen_reason(COURSE_OLD, COURSE_OLD.splitlines().index("    note = 'ok'") + 1))
        self.assertEqual(policy.frozen_reason('def broken(:\n', 1), '')
        self.assertEqual(policy.frozen_reason(WRITER_OLD, 0), '')


class GlobalConstantTests(Base):
    def test_a_constant_that_feeds_an_access_check_cannot_change(self):
        self.blocked(GLOBAL_OLD, GLOBAL_OLD.replace("('published',)", "('published', 'draft')"), 'R7g', COURSES)
        self.blocked(GLOBAL_OLD, GLOBAL_OLD.replace("PUBLISHED_STATES = ('published',)\n", "PUBLISHED_STATES = ('published',)\nPUBLISHED_STATES = ()\n"),
                     'R7g', COURSES)

    def test_unrelated_constants_may_change(self):
        old = GLOBAL_OLD.replace("PUBLISHED_STATES", "LIMIT = 5\nPUBLISHED_STATES", 1)
        self.passes(old, old.replace("LIMIT = 5", "LIMIT = 6"), COURSES)


class HigherOrderAndLoopTests(Base):
    def lines(self, lines, imports=''):
        old = MODELS_OLD
        text = ''.join('    ' + line + '\n' for line in lines)
        return old.replace("    return Response({'id': obj.id})\n", text + "    return Response({'id': obj.id})\n")

    def test_methods_passed_to_higher_order_functions_are_calls_too(self):
        for line in ('x = list(map(obj.restock_all, [1]))', 'x = sorted([1], key=obj.novel_key)', 'x = list(filter(obj.novel_test, [1]))'):
            self.blocked(MODELS_OLD, self.lines([line]), 'R14')
        self.blocked(MODELS_OLD, self.lines(['x = list(map(restock, [1]))']), 'R14')
        self.passes(MODELS_OLD, self.lines(['x = list(map(str, [1]))', 'y = sorted([1], key=len)']))

    def test_methods_collected_into_loops_are_calls_too(self):
        self.blocked(MODELS_OLD, self.lines(['for f in (obj.novel_a, obj.novel_b):', '    f()']), 'R14')
        self.blocked(MODELS_OLD, self.lines(['fns = [obj.novel_a]', 'for f in fns:', '    f()']), 'R14')
        self.blocked(MODELS_OLD, self.lines(['x = [f() for f in (obj.novel_a,)]']), 'R14')
        self.blocked(MODELS_OLD, self.lines(['f = obj.novel_a if obj else obj.novel_b', 'f()']), 'R14')
        self.passes(MODELS_OLD, self.lines(['total = 0', 'for v in (obj.id, obj.pk):', '    total += v']))

    def test_names_that_sound_like_actions_are_not_readers(self):
        for name in ('get_or_create_cart', 'get_and_reset', 'is_valid_and_save', 'has_to_sync', 'get_and_approve'):
            self.blocked(MODELS_OLD, self.lines([f'obj.{name}()']), 'R14')
        for name in ('get_total', 'is_ready', 'has_images', 'can_buy', 'get_absolute_url'):
            self.passes(MODELS_OLD, self.lines([f'obj.{name}()']))

    def test_endless_loops_are_refused(self):
        self.blocked(MODELS_OLD, self.lines(['while True:', '    pass']), 'R15')
        self.blocked(MODELS_OLD, self.lines(['while 1:', '    break']), 'R15')
        self.passes(MODELS_OLD, self.lines(['items = [1, 2]', 'while items:', '    items.pop()']))


class RealFilesTests(SimpleTestCase):
    """The policy must not break real code: a harmless function can be appended to every allowed file in
the repository."""

    def test_a_harmless_helper_can_be_appended_to_every_allowed_file(self):
        from pathlib import Path
        root = Path(__file__).resolve().parents[1]
        checked = 0
        for path in sorted(root.rglob('*.py')):
            rel = 'backend/' + path.relative_to(root).as_posix()
            if policy.classify(rel) != 'code':
                continue
            src = path.read_bytes().decode('utf-8').replace('\r\n', '\n')
            if policy.file_problem(src):
                continue
            helper = '\n\ndef _zz_helper(value):\n    return str(value or "").strip()\n'
            with self.subTest(path=rel):
                policy.check_structure(rel, 'code', src, src)
                policy.check_structure(rel, 'code', src, src + helper)
            checked += 1
        self.assertGreater(checked, 5)


class SourceEncodingTests(Base):
    """`# coding: utf-7` on the first line makes Python decode the whole file with a different codec,
    while the analyzer sees the raw text:
    code hides in a 'comment' or a string. Any encoding declaration other than UTF-8 is forbidden."""

    def test_a_coding_cookie_is_refused_by_both_layers(self):
        for cookie in ('# coding: utf-7', '# -*- coding: utf-7 -*-', '# vim: set fileencoding=utf-7 :', '# coding=latin-1',
                       '# coding: unicode_escape', '# coding: utf-8'):
            new = cookie + '\n' + API_OLD
            with self.assertRaises(policy.PolicyViolation, msg=cookie):
                policy.check_added('code', CATALOG, [cookie])
            if 'utf-8' not in cookie:
                self.blocked(API_OLD, new, 'R16')
        with self.assertRaises(policy.PolicyViolation):
            policy.check_added('test', 'backend/catalog/tests_x.py', ['# coding: utf-7'])

    def test_the_decoded_view_differs_from_the_raw_text_so_it_must_not_pass(self):
        smuggled = '# coding: utf-7\n' + API_OLD.replace("{'id': obj.id}", "{'id': +ACI-x+ACI-}")
        self.assertIn('encoding', policy.encoding_problem(smuggled))
        with self.assertRaises(policy.PolicyViolation):
            policy.check_patch(policy.diff_text(CATALOG, API_OLD, smuggled) + policy.diff_text('backend/catalog/tests_autofix_probe.py', '', TEST_BODY),
                               read_old=lambda p: API_OLD if p == CATALOG else None)

    def test_ordinary_files_and_a_bom_are_fine(self):
        self.assertEqual(policy.encoding_problem(API_OLD), '')
        self.assertEqual(policy.encoding_problem(chr(0xFEFF) + API_OLD), '')
        self.assertEqual(policy.encoding_problem('# -*- coding: utf-8 -*-\nx = 1\n'), '')
