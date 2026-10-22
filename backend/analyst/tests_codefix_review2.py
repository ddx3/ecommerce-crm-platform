"""Second round of review of the auto-fix policy: findings of an independent check (34 agents, 24
confirmed bypasses). Every rule has
a test for 'reject' and for 'allow'; fixtures are plain text, nothing is executed."""
from django.test import SimpleTestCase

from analyst import codefix_policy as policy

CATALOG = 'backend/catalog/api_views.py'
COURSES = 'backend/courses/api_views.py'
SERIALIZERS = 'backend/catalog/serializers.py'

FILE_OLD = '''"""API."""
import logging

from django.conf import settings
from rest_framework.response import Response
from rest_framework.decorators import api_view
from catalog.models import Product, Review
from .revert import apply_snapshot

log = logging.getLogger(__name__)
VISIBLE_STATES = ['published']
LIMIT = 10
THUMB_DIR = 'thumbs'


class ReviewCreateThrottle(object):
    scope = 'review_create'

    def allow_request(self, request, view):
        return True


class ReviewPagination(object):
    max_page_size = 100


def _picker():
    return ReviewCreateThrottle


def _run(fn, *args):
    return fn(*args)


def _clean(text):
    return str(text or '').strip()


def _can_edit(user):
    return bool(user)


@api_view(['GET'])
def product_list(request):
    qs = Product.objects.filter(is_active=True)
    rows = []
    for p in qs[:LIMIT]:
        rows.append({'id': p.pk, 'name': _clean(p.name)})
    return Response(rows)


def restore(obj):
    apply_snapshot(obj, {})
    return obj


def lesson(request, pk):
    obj = Product.objects.get(pk=pk)
    return Response({'id': obj.pk})


def shorten(text, limit=10):
    return text[:limit]
'''

SERIALIZER_OLD = '''"""Serializers."""
from rest_framework import serializers
from catalog.models import Review


class ReviewSerializer(serializers.ModelSerializer):
    class Meta:
        model = Review
        fields = ['id', 'rating', 'text']
        read_only_fields = ['id']

    def get_text(self, obj):
        return str(obj.text or '')
'''


class Base(SimpleTestCase):
    def blocked(self, old, new, rule='', path=CATALOG):
        with self.assertRaises(policy.PolicyViolation) as ctx:
            policy.check_structure(path, 'code', old, new)
        self.assertIn(rule, str(ctx.exception))

    def passes(self, old, new, path=CATALOG):
        policy.check_structure(path, 'code', old, new)


def edit(old, anchor, lines, before=True):
    """Inserts lines (without indentation) before/after the anchor line."""
    assert old.count(anchor) == 1, anchor
    text = ''.join(lines)
    return old.replace(anchor, text + anchor if before else anchor + text)


def in_shorten(*lines):
    """Lines into the body of the existing pure function `shorten` (before return)."""
    body = ''.join('    ' + ln + '\n' for ln in lines)
    return FILE_OLD.replace('    return text[:limit]\n', body + '    return text[:limit]\n')


class FreshTargetsTests(Base):
    """R2: writing to attributes and items is allowed only on FRESH local objects (and `self.x = …`)."""

    def test_laundering_a_foreign_class_through_any_route_is_refused(self):
        for lines in (['cands = (ReviewCreateThrottle,)', 'for cls in cands:', '    cls.rate = "1/s"'],
                      ['cands = [ReviewCreateThrottle]', 'for cls in cands:', '    cls.rate = "1/s"'],
                      ['cls = _picker()', 'cls.rate = "1/s"'],
                      ['box = {"c": ReviewCreateThrottle}', 'cls = box["c"]', 'cls.rate = "1/s"'],
                      ['box = {"c": ReviewCreateThrottle}', 'cls = box.get("c")', 'cls.rate = "1/s"'],
                      ['cls = settings', 'cls.ALLOWED_HOSTS = ["*"]'],
                      ['for cls in [ReviewCreateThrottle][:1]:', '    cls.rate = "1/s"'],
                      ['cls = ReviewCreateThrottle if limit else None', 'cls.rate = "1/s"'],
                      ['cls, other = ReviewCreateThrottle, 1', 'cls.rate = "1/s"']):
            self.blocked(FILE_OLD, in_shorten(*lines), 'R2')

    def test_values_read_out_of_parameters_attributes_and_calls_are_not_fresh(self):
        for lines in (['t.rate = 1'], ['cls = text.model', 'cls.rate = 1'], ['cls = _clean(text)', 'cls.rate = 1'],
                      ['limit.x = 1'], ['for v in {"a": ReviewCreateThrottle}.values():', '    v.rate = 1']):
            self.blocked(FILE_OLD, in_shorten(*lines).replace('def shorten(text, limit=10):', 'def shorten(text, limit=10, t=None):'), 'R2')

    def test_look_alike_helpers_cannot_pass_for_a_queryset(self):
        spoof = FILE_OLD.replace('def shorten(text, limit=10):', 'class Fake(object):\n    @staticmethod\n    def all():\n        return ReviewCreateThrottle\n\n\n'
                                 'def shorten(text, limit=10):')
        self.blocked(FILE_OLD, spoof.replace('    return text[:limit]\n', '    m = Fake.all()\n    m.rate = 1\n    return text[:limit]\n'), 'R2')

    def test_fresh_local_objects_may_be_written(self):
        for lines in (['out = {}', 'out["a"] = 1'], ['rows = [{"a": i} for i in range(3)]', 'rows[0]["b"] = 1'],
                      ['out = dict()', 'out["k"] = {}', 'out["k"]["x"] = 1'], ['item = Review()', 'item.text = "x"'],
                      ['counts = {}', 'counts["a"] = counts.get("a", 0) + 1'], ['buf = []', 'buf.append(1)', 'buf[0] = 2']):
            self.passes(FILE_OLD, in_shorten(*lines))
        # instances from the request — in a function that already uses this model
        base = FILE_OLD.replace("    obj = Product.objects.get(pk=pk)\n    return Response({'id': obj.pk})",
                                "    obj = Product.objects.get(pk=pk)\n__X__    return Response({'id': obj.pk})")
        for extra in ('    obj.label = "x"\n', '    for p in Product.objects.filter(pk=1):\n        p.label = "x"\n'):
            self.passes(FILE_OLD, base.replace('__X__', extra))

    def test_self_attribute_one_level_only(self):
        old = FILE_OLD.replace('    scope = \'review_create\'\n', "    scope = 'review_create'\n\n    def reset(self):\n        self.count = 0\n")
        # changing the Throttle class is frozen by name, so we test the write rule on an ordinary
        # class
        plain = old.replace('ReviewCreateThrottle(object)', 'Counter2(object)').replace('return ReviewCreateThrottle', 'return Counter2')
        self.passes(plain, plain.replace('        self.count = 0\n', '        self.count = 0\n        self.last = 1\n'))
        self.blocked(plain, plain.replace('        self.count = 0\n', '        self.count = 0\n        self.cfg.last = 1\n'), 'R2')
        self.blocked(plain, plain.replace('        self.count = 0\n', '        self.count = 0\n        self.cache["x"] = 1\n'), 'R2')


class MutatorTests(Base):
    def test_mutating_methods_need_a_fresh_local_collection(self):
        for line in ('settings.ALLOWED_HOSTS.append("*")', 'settings.CSRF_TRUSTED_ORIGINS.extend(["x"])', 'settings.REST_FRAMEWORK.pop("a")',
                     'VISIBLE_STATES.append("draft")', 'VISIBLE_STATES.insert(0, "draft")', 'settings.REST_FRAMEWORK.update({"a": 1})',
                     'settings.REST_FRAMEWORK.setdefault("a", 1)', 'text.sort()'):
            self.blocked(FILE_OLD, in_shorten(line), 'R11')

    def test_a_positional_update_is_no_longer_exempt(self):
        self.blocked(FILE_OLD, in_shorten('VISIBLE_STATES.update({"x"})'), 'R11')

    def test_local_collections_keep_working(self):
        self.passes(FILE_OLD, in_shorten('seen = set()', 'seen.add(1)', 'seen.discard(1)'))
        self.passes(FILE_OLD, in_shorten('merged = dict(a=1)', 'merged.update({"b": 2})', 'merged.setdefault("c", 3)'))


class VariableCallsTests(Base):
    def test_calling_a_variable_is_refused_whatever_it_holds(self):
        for lines in (['fn = {"a": text.strip}["a"]', 'fn()'], ['d = {"a": text.strip}', 'for fn in d.values():', '    fn()'],
                      ['fn = (lambda: text.restock_all)()', 'fn(100)'], ['fn = getattr(text, "restock")', 'fn()'],
                      ['fns = [text.restock]', 'fns[0]()'], ['x = [f() for f in (text.a, text.b)]']):
            self.blocked(FILE_OLD, in_shorten(*lines), 'R14')

    def test_lambdas_bound_to_variables_are_fine(self):
        self.passes(FILE_OLD, in_shorten('tidy = lambda s: str(s).strip()', 'out = tidy(text)'))

    def test_a_parameter_called_inside_a_lambda_is_refused(self):
        self.blocked(FILE_OLD, in_shorten('out = (lambda f: f())(text.restock)'), 'R14')

    def test_passing_a_foreign_method_to_a_helper_that_calls_its_argument_is_refused(self):
        self.blocked(FILE_OLD, in_shorten('_run(text.restock_all, 1)'), 'R14')
        self.blocked(FILE_OLD, in_shorten('_run(fn=text.restock_all)'), 'R14')
        self.passes(FILE_OLD, in_shorten('_run(len, text)', '_run(lambda s: s.strip(), text)'))

    def test_higher_order_builtins_in_every_position(self):
        for line in ('x = list(map(text.restock, [1]))', 'x = iter(text.restock, 0)', 'x = sorted([1], key=text.restock)',
                     'buf = []\n    buf.sort(key=text.restock)', 'import functools\n    x = functools.partial(text.restock, 1)',
                     'x = list(map(_clean_all(), [1]))'):
            self.blocked(FILE_OLD, in_shorten(*line.split('\n    ')), 'R14')


class ImportTimeTests(Base):
    def test_module_level_class_level_and_default_argument_calls_may_not_use_what_the_file_merely_calls_elsewhere(self):
        for new in (FILE_OLD.replace("LIMIT = 10\n", "LIMIT = 10\n_warm = apply_snapshot(None, {})\n"),
                    FILE_OLD.replace("class ReviewPagination(object):\n    max_page_size = 100\n",
                                     "class ReviewPagination(object):\n    max_page_size = 100\n    seed = apply_snapshot(None, {})\n"),
                    FILE_OLD.replace("def shorten(text, limit=10):", "def shorten(text, limit=apply_snapshot(None, {})):"),
                    FILE_OLD.replace("def shorten(text, limit=10):", "@apply_snapshot\ndef shorten(text, limit=10):")):
            self.blocked(FILE_OLD, new, 'R14')

    def test_safe_import_time_calls_stay_possible(self):
        self.passes(FILE_OLD, FILE_OLD.replace("LIMIT = 10\n", "LIMIT = 10\nNAMES = tuple(str(i) for i in range(3))\nCOUNT = len(NAMES)\n"))

    def test_the_same_call_inside_a_function_that_already_makes_it_is_unchanged(self):
        self.passes(FILE_OLD, FILE_OLD)


class AnnotationTests(Base):
    def test_annotations_run_at_import_and_are_checked_like_code(self):
        for new in (FILE_OLD.replace("def shorten(text, limit=10):", "def shorten(text: setattr(ReviewCreateThrottle, 'a', 1), limit=10):"),
                    FILE_OLD.replace("def shorten(text, limit=10):", "def shorten(text, limit=10) -> apply_snapshot(None, {}):"),
                    FILE_OLD.replace("LIMIT = 10\n", "LIMIT: apply_snapshot(None, {}) = 10\n"),
                    FILE_OLD.replace("def shorten(text, limit=10):", "def shorten(*args: apply_snapshot(None, {})):")):
            with self.assertRaises(policy.PolicyViolation):
                policy.check_structure(CATALOG, 'code', FILE_OLD, new)

    def test_plain_annotations_are_fine(self):
        self.passes(FILE_OLD, FILE_OLD.replace("def shorten(text, limit=10):", "def shorten(text: str, limit: int = 10) -> str:"))


class WriterTests(Base):
    def test_a_function_gaining_a_call_to_a_helper_that_writes_is_refused(self):
        self.blocked(FILE_OLD, in_shorten('apply_snapshot(None, {})'), 'R7h')
        self.blocked(FILE_OLD, in_shorten('restore(None)'), 'R7h')
        self.blocked(FILE_OLD, in_shorten('x = apply_snapshot'), 'R7h')
        self.blocked(FILE_OLD, in_shorten('x = list(map(apply_snapshot, [1], [{}]))'), 'R7')

    def test_the_existing_call_site_stays_untouched_and_may_not_be_edited(self):
        self.blocked(FILE_OLD, FILE_OLD.replace('    apply_snapshot(obj, {})\n    return obj', '    apply_snapshot(obj, {"a": 1})\n    return obj'), 'R7f')

    def test_a_brand_new_function_may_not_call_writers_either(self):
        self.blocked(FILE_OLD, FILE_OLD + "\n\ndef _again(obj):\n    return restore(obj)\n", 'R7h')

    def test_management_commands_files_and_network_count_as_writes(self):
        for body in ('from django.core.management import call_command\n    call_command("flush")', 'path.unlink()', 'path.write_text("x")',
                     'f = open("a.txt")'):
            old = FILE_OLD + "\n\ndef _job(path):\n    return 1\n"
            new = old.replace("def _job(path):\n    return 1\n", "def _job(path):\n    " + body + "\n    return 1\n")
            with self.assertRaises(policy.PolicyViolation, msg=body):
                policy.check_structure(CATALOG, 'code', old, new)

    def test_callers_of_writers_are_frozen_transitively(self):
        self.blocked(FILE_OLD, FILE_OLD.replace("    obj = Product.objects.get(pk=pk)\n    return Response({'id': obj.pk})",
                                                "    obj = Product.objects.get(pk=pk)\n    restore(obj)\n    return Response({'id': obj.pk})"), 'R7h')
        callers = FILE_OLD.replace("def lesson(request, pk):", "def lesson2(obj):\n    return restore(obj)\n\n\ndef lesson(request, pk):")
        self.blocked(callers, callers.replace("    return restore(obj)\n", "    x = 1\n    return restore(obj)\n"), 'R7f')

    def test_frozen_reason_knows_about_callers_and_names(self):
        callers = FILE_OLD.replace("def lesson(request, pk):", "def lesson2(obj):\n    return restore(obj)\n\n\ndef lesson(request, pk):")
        line = callers.splitlines().index('    return restore(obj)') + 1
        self.assertIn('writes', policy.frozen_reason(callers, line, CATALOG))
        self.assertIn('by its name', policy.frozen_reason(FILE_OLD, FILE_OLD.splitlines().index('    return bool(user)') + 1, CATALOG))
        self.assertEqual(policy.frozen_reason(FILE_OLD, FILE_OLD.splitlines().index('    return text[:limit]') + 1, CATALOG), '')


class NameFreezeTests(Base):
    def test_access_by_name_and_throttle_pagination_classes_are_frozen(self):
        self.blocked(FILE_OLD, FILE_OLD.replace('    return bool(user)', '    return True'), 'R7e')
        self.blocked(FILE_OLD, FILE_OLD.replace('    max_page_size = 100', '    max_page_size = 100000'), 'R7e')
        self.blocked(FILE_OLD, FILE_OLD.replace("        return True\n\n\nclass ReviewPagination", "        return False\n\n\nclass ReviewPagination"), 'R7e')

    def test_constants_named_after_visibility_and_paths_are_frozen(self):
        self.blocked(FILE_OLD, FILE_OLD.replace("['published']", "['published', 'draft']"), 'R7g')
        self.blocked(FILE_OLD, FILE_OLD.replace("THUMB_DIR = 'thumbs'", "THUMB_DIR = ''"), 'R7g')
        self.passes(FILE_OLD, FILE_OLD.replace('LIMIT = 10', 'LIMIT = 20'))

    def test_rebinding_a_name_the_file_already_defines_is_refused(self):
        self.blocked(FILE_OLD, FILE_OLD + "\n_can_edit = lambda user: True\n", 'R7l')
        self.blocked(FILE_OLD, FILE_OLD + "\nfrom django.utils.html import escape as _can_edit\n", 'R7l')
        self.blocked(FILE_OLD, FILE_OLD + "\n\ndef _clean(text):\n    return text\n", 'R7l')
        self.passes(FILE_OLD, FILE_OLD + "\n\ndef _brand_new(text):\n    return text\n")


class MetaContractTests(Base):
    def test_the_serializer_contract_cannot_widen(self):
        for new in (SERIALIZER_OLD.replace("['id', 'rating', 'text']", "['id', 'rating', 'text', 'user']"),
                    SERIALIZER_OLD.replace("['id', 'rating', 'text']", "['id', 'rating', 'text', 'user'][:0] + ['id', 'rating', 'text', 'user']"),
                    SERIALIZER_OLD.replace("        fields = ['id', 'rating', 'text']\n", "        exclude = ['text']\n"),
                    SERIALIZER_OLD.replace("        read_only_fields = ['id']\n", "        read_only_fields = []\n"),
                    SERIALIZER_OLD.replace("        read_only_fields = ['id']\n", "        read_only_fields = ['id']\n        depth = 1\n"),
                    SERIALIZER_OLD.replace("        read_only_fields = ['id']\n", "        read_only_fields = ['id']\n        extra_kwargs = {}\n"),
                    SERIALIZER_OLD.replace("['id', 'rating', 'text']", "'__all__'")):
            with self.assertRaises(policy.PolicyViolation):
                policy.check_structure(SERIALIZERS, 'code', SERIALIZER_OLD, new)

    def test_narrowing_and_reordering_are_allowed(self):
        self.passes(SERIALIZER_OLD, SERIALIZER_OLD.replace("['id', 'rating', 'text']", "['id', 'text']"), SERIALIZERS)
        self.passes(SERIALIZER_OLD, SERIALIZER_OLD.replace("['id', 'rating', 'text']", "['text', 'rating', 'id']"), SERIALIZERS)
        self.passes(SERIALIZER_OLD, SERIALIZER_OLD.replace("return str(obj.text or '')", "return str(obj.text or '').strip()"), SERIALIZERS)


VIS_OLD = '''"""API."""
from rest_framework.response import Response
from catalog.models import Course, Post


def _approved(qs):
    return qs.filter(is_approved=True)


def courses_list(request):
    qs = Course.objects.filter(is_active=True)
    return Response(list(qs.values_list('title', flat=True)))


def replies(obj):
    def collect(o):
        return [r for r in o.replies.filter(is_approved=True)]
    return collect(obj) + collect(obj)


def mine(request):
    return Response(list(Course.objects.filter(owner=request.user)))
'''


class VisibilityTests(Base):
    def test_a_function_that_filters_by_visibility_may_not_get_a_second_unfiltered_query(self):
        for new in (VIS_OLD.replace("    return Response(list(qs.values_list('title', flat=True)))",
                                    "    if request.GET.get('all'):\n        qs = Course.objects.all()\n    return Response(list(qs.values_list('title', flat=True)))"),
                    VIS_OLD.replace("    qs = Course.objects.filter(is_active=True)\n", "    qs = Course.objects.filter(is_active=True)\n    qs = Course.objects.all()\n"),
                    VIS_OLD.replace("    return Response(list(Course.objects.filter(owner=request.user)))",
                                    "    return Response(list(Course.objects.filter(owner=request.user)) + list(Course.objects.all()))")):
            self.blocked(VIS_OLD, new, 'R7', COURSES)

    def test_ownership_in_the_value_of_a_keyword_counts_too(self):
        old = "from catalog.models import Note\n\n\ndef notes(request):\n    return list(Note.objects.filter(author_name=str(request.user)))\n"
        new = old.replace("    return list(", "    if request.GET.get('all'):\n        return list(Note.objects.all())\n    return list(")
        self.blocked(old, new, 'R7', COURSES)

    def test_new_data_sources_are_refused(self):
        self.blocked(VIS_OLD, VIS_OLD.replace("    return Response(list(qs.values_list('title', flat=True)))",
                                              "    return Response(list(qs.values_list('title', flat=True)) + list(Post.objects.values_list('title', flat=True)))"),
                     'R7j', COURSES)
        self.blocked(VIS_OLD, VIS_OLD + "\n\ndef _more():\n    from catalog.models import Review\n    return Review.objects.all()\n", 'R7j', COURSES)

    def test_visibility_words_and_filtering_helpers_cannot_disappear(self):
        self.blocked(VIS_OLD, VIS_OLD.replace("o.replies.filter(is_approved=True)", "o.replies.all()"), 'R7k', COURSES)
        self.blocked(VIS_OLD, VIS_OLD.replace("    return collect(obj) + collect(obj)", "    return o_all(obj)").replace(
            "def replies(obj):", "def o_all(o):\n    return list(o.replies.all())\n\n\ndef replies(obj):"), 'R7', COURSES)
        self.blocked(VIS_OLD, VIS_OLD.replace("    return qs.filter(is_approved=True)", "    return qs.all()"), 'R7', COURSES)

    def test_harmless_edits_in_those_functions_pass(self):
        self.passes(VIS_OLD, VIS_OLD.replace("list(qs.values_list('title', flat=True))", "sorted(qs.values_list('title', flat=True))"), COURSES)


class EarlyReturnTests(Base):
    def test_returns_inside_loops_and_try_count_as_early_exits(self):
        old = "from catalog.models import Course\n\n\ndef pick(request):\n    qs = Course.objects.filter(is_active=True)\n    return list(qs)\n"
        for body in ("    for c in Course.objects.filter(is_active=True):\n        return [c]\n",
                     "    while qs:\n        return list(qs.exclude(pk=0))\n"):
            self.blocked(old, old.replace("    return list(qs)\n", body + "    return list(qs)\n"), 'R7', COURSES)


class OrmNamesTests(Base):
    OLD = ("from django.db.models import Count, F, Q\nfrom catalog.models import Course\n\n\ndef lst(request):\n"
           "    qs = Course.objects.filter(is_active=True)\n    return list(qs)\n")

    def test_field_names_must_be_literals_in_the_patch(self):
        for line in ("qs = qs.filter(**request.GET)", "qs = qs.order_by(request.GET.get('sort'))", "qs = qs.values_list(request.GET['f'])",
                     "qs = qs.annotate(n=Count(request.GET['f']))", "qs = qs.filter(Q(**request.GET))", "qs = qs.only(*request.GET.getlist('f'))"):
            self.blocked(self.OLD, self.OLD.replace("    return list(qs)", "    " + line + "\n    return list(qs)"), 'R14b', COURSES)

    def test_literal_field_names_are_fine(self):
        for line in ("qs = qs.filter(**{'is_active': True})", "qs = qs.order_by('-pk')", "qs = qs.values_list('title', flat=True)",
                     "qs = qs.annotate(n=Count('id'))", "qs = qs.filter(Q(title__icontains=request.GET.get('q', '')) | Q(pk=1))",
                     "qs = qs.order_by(F('pk').desc())", "qs = qs.annotate(x=F('pk') * 2)"):
            self.passes(self.OLD, self.OLD.replace("    return list(qs)", "    " + line + "\n    return list(qs)"), COURSES)


class DeadCodeTests(Base):
    def test_unreachable_statements_are_refused(self):
        self.blocked(FILE_OLD, in_shorten('return text', 'x = 1'), 'R18')
        self.blocked(FILE_OLD, FILE_OLD.replace("    return text[:limit]\n", "    return text[:limit]\n    x = 1\n"), 'R18')
        self.blocked(FILE_OLD, FILE_OLD.replace("def shorten(text, limit=10):\n", "def shorten(text, limit=10):\n    raise ValueError('x')\n    y = 2\n"), 'R18')
        self.passes(FILE_OLD, in_shorten('if not text:', '    return text'))


class ClassificationTests(SimpleTestCase):
    def test_the_agent_options_module_is_not_fixable_but_the_other_instruction_modules_are(self):
        self.assertIsNone(policy.classify('backend/catalog/instruction_agent.py'))
        for name in ('llm', 'resolve', 'match', 'jobs'):
            self.assertEqual(policy.classify(f'backend/catalog/instruction_{name}.py'), 'code')

    def test_business_secrets_are_words_nobody_adds(self):
        for expr in ('v.supplier_price', 'x["purchase_price"]', 'obj.wholesale', 'cost_price', 'margin'):
            old = "def f(v):\n    return 1\n"
            new = f"def f(v):\n    y = {expr}\n    return 1\n"
            with self.assertRaises(policy.PolicyViolation, msg=expr):
                policy.check_structure('backend/catalog/api_views.py', 'code', old, new)

    def test_error_responses_cannot_carry_data_but_validation_errors_may(self):
        old = ("from rest_framework.response import Response\nfrom catalog.models import Course\n\n\ndef f(request, c):\n"
               "    qs = Course.objects.filter(is_active=True)\n    return Response({'ok': True})\n")
        bad = old.replace("    return Response({'ok': True})", "    if request.GET.get('x'):\n        return Response(c.content, status=403)\n    return Response({'ok': True})")
        good = old.replace("    return Response({'ok': True})", "    if request.GET.get('x'):\n        return Response(request.errors, status=400)\n    return Response({'ok': True})")
        with self.assertRaises(policy.PolicyViolation):
            policy.check_structure('backend/catalog/api_views.py', 'code', old, bad)
        policy.check_structure('backend/catalog/api_views.py', 'code', old, good)
