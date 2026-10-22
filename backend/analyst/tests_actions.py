"""Fix mechanism: plan → "Apply" → rollback and all safeguards.

The Analyst currently has no real fixes for shop data (only server actions, which tests_hostops
checks),
so the general mechanism is tested on test actions by replacing the FIXERS registry.
"""
import json
from datetime import timedelta
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone

from analyst import actions
from analyst.actions import Fixer, Param, Plan
from analyst.models import ActionRun, Finding, PlannedPurchase

OWNER = 'boss@example.com'
CHECK = 'test_check'


def _finding(check_name=CHECK, data=None, **kw):
    defaults = dict(key=f'{check_name}:{Finding.objects.count()}', check_name=check_name,
                    section=Finding.SECTION_HEALTH, severity=Finding.WARN, title='Finding', data=data or {})
    defaults.update(kw)
    return Finding.objects.create(**defaults)


def _purchase(note='was', **kw):
    defaults = dict(key='var:1', label='Item', quantity=1, unit='pcs', note=note)
    defaults.update(kw)
    return PlannedPurchase.objects.create(**defaults)


def _build_edit(finding, params):
    po = PlannedPurchase.objects.get(pk=finding.data['po'])
    return Plan(title='Change the note', changes=[{
        'model': 'analyst.PlannedPurchase', 'pk': po.pk, 'field': 'note', 'before': po.note, 'after': 'became',
        'text': 'Note: became'}], notes=['Test action.'])


def _build_create(finding, params):
    return Plan(title='Create a record', changes=[{
        'op': 'create', 'model': 'analyst.PlannedPurchase', 'pk': None,
        'values': {'key': 'var:2', 'label': 'New', 'quantity': '3', 'unit': 'pcs', 'note': 'created'},
        'guard': {'field': 'status', 'value': 'open'}, 'text': 'Create a purchase'}])


def _plan_apply(finding, key, params=None):
    run = actions.make_plan(finding, key, params or {}, OWNER)
    actions.apply_run(run)
    run.refresh_from_db()
    return run


class _WithTestFixers:
    def setUp(self):
        super().setUp()
        self.enterContext(mock.patch.dict(actions.FIXERS, {
            'test_edit': Fixer('test_edit', 'Change', (CHECK,), _build_edit),
            'test_create': Fixer('test_create', 'Create', ('create_check',), _build_create),
            'test_param': Fixer('test_param', 'With a parameter', ('param_check',), _build_edit,
                                params=(Param('n', 'How many', 5, 1, 9),)),
        }))


class MachineryTests(_WithTestFixers, TestCase):
    def setUp(self):
        super().setUp()
        self.po = _purchase()
        self.finding = _finding(data={'po': self.po.pk})

    def test_plan_writes_nothing(self):
        run = actions.make_plan(self.finding, 'test_edit', {}, OWNER)
        self.assertEqual(run.status, ActionRun.PLANNED)
        self.po.refresh_from_db()
        self.assertEqual(self.po.note, 'was')

    def test_apply_writes_marks_done_and_revert_restores(self):
        run = _plan_apply(self.finding, 'test_edit')
        self.po.refresh_from_db()
        self.finding.refresh_from_db()
        self.assertEqual((self.po.note, run.status, self.finding.status), ('became', ActionRun.APPLIED, Finding.DONE))
        actions.revert_run(run)
        self.po.refresh_from_db()
        self.finding.refresh_from_db()
        self.assertEqual((self.po.note, self.finding.status), ('was', Finding.NEW))

    def test_changed_between_plan_and_apply_aborts_and_writes_nothing(self):
        run = actions.make_plan(self.finding, 'test_edit', {}, OWNER)
        PlannedPurchase.objects.filter(pk=self.po.pk).update(note='someone changed it')
        with self.assertRaises(actions.ApplyError):
            actions.apply_run(run)
        run.refresh_from_db()
        self.po.refresh_from_db()
        self.assertEqual((run.status, self.po.note), (ActionRun.FAILED, 'someone changed it'))

    def test_plan_cannot_be_applied_twice(self):
        run = _plan_apply(self.finding, 'test_edit')
        with self.assertRaises(actions.ApplyError):
            actions.apply_run(run)

    def test_expired_plan_is_refused(self):
        run = actions.make_plan(self.finding, 'test_edit', {}, OWNER)
        ActionRun.objects.filter(pk=run.pk).update(created_at=timezone.now() - actions.PLAN_TTL - timedelta(minutes=1))
        run.refresh_from_db()
        with self.assertRaises(actions.ApplyError):
            actions.apply_run(run)
        self.po.refresh_from_db()
        self.assertEqual(self.po.note, 'was')

    def test_revert_refuses_when_data_changed_afterwards(self):
        run = _plan_apply(self.finding, 'test_edit')
        PlannedPurchase.objects.filter(pk=self.po.pk).update(note='changed again')
        with self.assertRaises(actions.ApplyError):
            actions.revert_run(run)
        run.refresh_from_db()
        self.assertEqual(run.status, ActionRun.APPLIED)

    def test_wrong_fixer_for_check_and_unknown_fixer_are_rejected(self):
        with self.assertRaises(actions.PlanError):
            actions.make_plan(_finding('other_check'), 'test_edit', {}, OWNER)
        with self.assertRaises(actions.PlanError):
            actions.make_plan(self.finding, 'no_such_action', {}, OWNER)

    def test_resolved_finding_cannot_be_fixed(self):
        Finding.objects.filter(pk=self.finding.pk).update(resolved_at=timezone.now())
        self.finding.refresh_from_db()
        with self.assertRaises(actions.PlanError):
            actions.make_plan(self.finding, 'test_edit', {}, OWNER)

    def test_parameters_are_validated_and_defaulted(self):
        finding = _finding('param_check', {'po': self.po.pk})
        self.assertEqual(actions.make_plan(finding, 'test_param', {}, OWNER).params, {'n': 5})
        for bad in ({'n': 0}, {'n': 10}, {'n': 'abc'}):
            with self.subTest(bad=bad), self.assertRaises(actions.PlanError):
                actions.make_plan(finding, 'test_param', bad, OWNER)


class CreateOperationTests(_WithTestFixers, TestCase):
    def setUp(self):
        super().setUp()
        self.finding = _finding('create_check')

    def test_create_and_revert_deletes_the_record(self):
        run = _plan_apply(self.finding, 'test_create')
        self.assertEqual(PlannedPurchase.objects.get().note, 'created')
        actions.revert_run(run)
        self.assertEqual(PlannedPurchase.objects.count(), 0)

    def test_revert_refuses_when_the_record_moved_on(self):
        run = _plan_apply(self.finding, 'test_create')
        PlannedPurchase.objects.update(status=PlannedPurchase.RECEIVED)
        with self.assertRaises(actions.ApplyError):
            actions.revert_run(run)
        self.assertEqual(PlannedPurchase.objects.count(), 1)


@override_settings(ANALYST_OWNER_EMAIL=OWNER)
class FixEndpointsTests(_WithTestFixers, TestCase):
    def setUp(self):
        super().setUp()
        User = get_user_model()
        self.owner = User.objects.create_user(email=OWNER, password='x', is_staff=True, is_superuser=True)
        self.other = User.objects.create_user(email='m@example.com', password='x', is_staff=True, is_superuser=True)
        self.po = _purchase()
        self.finding = _finding(data={'po': self.po.pk}, title='Finding with a fix')
        self.client.force_login(self.owner)

    def _post(self, url, data):
        return self.client.post(url, data=json.dumps(data), content_type='application/json')

    def test_full_flow_through_http(self):
        plan = self._post('/admin/analyst/fix/plan/', {'finding_id': self.finding.pk, 'action': 'test_edit'})
        self.assertEqual(plan.status_code, 200, plan.content)
        body = plan.json()
        self.po.refresh_from_db()
        self.assertEqual((self.po.note, body['total']), ('was', 1))         # the plan writes nothing
        applied = self._post('/admin/analyst/fix/apply/', {'run_ids': [body['run_id']]})
        self.assertEqual(applied.json()['results'][0]['ok'], True)
        self.po.refresh_from_db()
        self.assertEqual(self.po.note, 'became')
        self.assertEqual(self._post('/admin/analyst/fix/revert/', {'run_id': body['run_id']}).status_code, 200)
        self.po.refresh_from_db()
        self.assertEqual(self.po.note, 'was')

    def test_plan_error_is_422_with_text(self):
        resp = self._post('/admin/analyst/fix/plan/', {'finding_id': self.finding.pk, 'action': 'no_such_action'})
        self.assertEqual(resp.status_code, 422)
        self.assertIn('There is no such action', resp.json()['error'])

    def test_other_staff_cannot_plan_apply_or_revert(self):
        run = actions.make_plan(self.finding, 'test_edit', {}, OWNER)
        self.client.force_login(self.other)
        for url, data in (('/admin/analyst/fix/plan/', {'finding_id': self.finding.pk, 'action': 'test_edit'}),
                          ('/admin/analyst/fix/plan-all/', {}),
                          ('/admin/analyst/fix/apply/', {'run_ids': [run.pk]}),
                          ('/admin/analyst/fix/revert/', {'run_id': run.pk})):
            with self.subTest(url=url):
                self.assertEqual(self._post(url, data).status_code, 403)
        self.po.refresh_from_db()
        self.assertEqual(self.po.note, 'was')

    def test_get_is_not_allowed_on_write_endpoints(self):
        for url in ('plan', 'plan-all', 'apply', 'revert'):
            self.assertEqual(self.client.get(f'/admin/analyst/fix/{url}/').status_code, 405)

    def test_plan_all_collects_fixable_and_skips_param_actions(self):
        _finding('param_check', {'po': self.po.pk})               # needs parameters → not included
        _finding('disk_usage', {})                                # nothing to fix without a channel to the server
        data = self._post('/admin/analyst/fix/plan-all/', {}).json()
        self.assertEqual(len(data['runs']), 1)
        results = self._post('/admin/analyst/fix/apply/', {'run_ids': [data['runs'][0]['run_id']]}).json()['results']
        self.assertTrue(all(r['ok'] for r in results), results)

    def test_apply_with_garbage_ids(self):
        self.assertEqual(self._post('/admin/analyst/fix/apply/', {'run_ids': ['x', None]}).status_code, 400)
        self.assertEqual(self._post('/admin/analyst/fix/revert/', {'run_id': 'x'}).status_code, 404)

    def test_page_shows_fix_buttons_and_the_all_button_but_no_journal(self):
        page = self.client.get('/admin/analyst/').content.decode()
        self.assertIn('data-fix="test_edit"', page)
        self.assertIn('Fix everything possible (1)', page)
        self.assertNotIn('ar-journal-btn', page)
        self.assertNotIn('Action journal', page)
        self.assertNotIn('ar-lastrun', page)

    def test_unapplied_old_plans_are_pruned(self):
        from analyst.attribution import prune_old_data
        run = actions.make_plan(self.finding, 'test_edit', {}, OWNER)
        ActionRun.objects.filter(pk=run.pk).update(created_at=timezone.now() - timedelta(days=2))
        po2 = _purchase()
        applied = _plan_apply(_finding(data={'po': po2.pk}), 'test_edit')
        prune_old_data(timezone.localdate())
        self.assertFalse(ActionRun.objects.filter(pk=run.pk).exists())
        self.assertTrue(ActionRun.objects.filter(pk=applied.pk).exists(), 'the journal of applied runs is not pruned')
