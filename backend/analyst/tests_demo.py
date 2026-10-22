"""Demo page of the Analyst (/admin/analyst/demo/): all row types on stubs, no DB writes."""
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings

from analyst import actions, demo
from analyst.models import ActionRun, AnalystRun, Finding, PlannedPurchase


@override_settings(ANALYST_OWNER_EMAIL='boss@example.com')
class DemoPageTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.owner = User.objects.create_user(email='boss@example.com', password='x', is_staff=True, is_superuser=True)
        self.other = User.objects.create_user(email='m@example.com', password='x', is_staff=True, is_superuser=True)
        self.client.force_login(self.owner)

    def test_only_owner_sees_it(self):
        self.assertEqual(self.client.get('/admin/analyst/demo/').status_code, 200)
        self.client.force_login(self.other)
        self.assertEqual(self.client.get('/admin/analyst/demo/').status_code, 403)

    def test_shows_every_kind_of_row(self):
        response = self.client.get('/admin/analyst/demo/')
        rows = response.context['rows']
        self.assertEqual({r['is_new'] for r in rows}, {True, False})
        self.assertTrue(any(not r['fixers'] and r['is_new'] for r in rows), 'there are rows without a fix button')
        self.assertTrue(any(len(r['title']) > 90 for r in rows), 'there is a long title that gets truncated')
        self.assertEqual({r['section'] for r in rows}, {'System health', 'Purchasing'})
        page = response.content.decode()
        self.assertIn('Demo · placeholders', page)
        self.assertNotIn('ar-primary', page)
        self.assertNotIn('Importance', page)
        self.assertNotIn('ar-journal-btn', page)
        self.assertNotIn('ar-lastrun', page)
        self.assertEqual(page.count('ar-row ntf-step-1'), sum(not r['is_new'] for r in rows))

    def test_done_button_toggles_the_row_at_once_without_dimming_or_reshuffling(self):
        """✓ / ↩ — as in the rest of the inbox tables: the row changes immediately, with no dimming or table
rebuild."""
        page = self.client.get('/admin/analyst/demo/').content.decode()
        self.assertNotIn('is-busy', page)
        self.assertIn("document.dispatchEvent(new Event('ar:state-changed'))", page)
        self.assertNotIn('ar:rows-changed', page)
        self.assertIn("return action === 'done' ? isNew(tr) : !isNew(tr);", page)   # rows already in place are left alone

    def test_every_fix_action_has_a_demo_row_and_a_demo_plan(self):
        """A new fix was added — the demo must show it, otherwise it will silently go stale."""
        used = {f['key'] for r in demo.rows() for f in r['fixers']}
        self.assertEqual(used, set(actions.FIXERS))
        self.assertEqual(set(demo.plans()), set(actions.FIXERS))

    def test_processed_rows_carry_no_fix_buttons_like_the_real_page(self):
        for r in demo.rows():
            if not r['is_new']:
                self.assertEqual(r['fixers'], [])

    def test_many_flag_triggers_the_pager(self):
        rows = self.client.get('/admin/analyst/demo/?many=1').context['rows']
        self.assertGreater(len(rows), 25)
        self.assertEqual(len({r['id'] for r in rows}), len(rows))

    def test_demo_writes_nothing(self):
        before = (Finding.objects.count(), ActionRun.objects.count(), AnalystRun.objects.count(),
                  PlannedPurchase.objects.count())
        self.client.get('/admin/analyst/demo/')
        self.client.get('/admin/analyst/demo/?many=1')
        after = (Finding.objects.count(), ActionRun.objects.count(), AnalystRun.objects.count(),
                 PlannedPurchase.objects.count())
        self.assertEqual(before, after)

    def test_table_sits_in_a_panel_so_the_pager_padding_can_apply(self):
        """#content has padding:0 !important, so the offset for the fixed pager is set on the table
        wrapper.
        Without it the last row hides under the pager."""
        self.assertContains(self.client.get('/admin/analyst/demo/?many=1'), '<div id="ar-panel">')
        self.assertContains(self.client.get('/admin/analyst/'), '<div id="ar-panel">')

    def test_auto_mode_preview_has_only_the_owner_decision_buttons(self):
        """In auto mode the Analyst maintains the server itself, so there are no maintenance buttons;
        only the owner's decisions remain
        about a ready code auto-fix (apply / reject), and they are not part of "Fix everything
        possible"."""
        response = self.client.get('/admin/analyst/demo/?mode=auto')
        keys = {f['key'] for r in response.context['rows'] for f in r['fixers']}
        self.assertEqual(keys, {'codefix_apply', 'codefix_reject'})
        page = response.content.decode()
        self.assertNotIn('data-fix="host_', page)
        self.assertNotIn('id="ar-fix-all"', page)
        self.assertEqual(response.context['fix_all_count'], 0)

    def test_header_and_date_follow_the_crm_standard(self):
        import re
        response = self.client.get('/admin/analyst/demo/')
        page = response.content.decode()
        self.assertIn('<th class="col-date">Date</th>', page)
        self.assertIn('<th class="col-skip">Type</th>', page)
        self.assertNotIn('First seen', page)
        for r in response.context['rows']:
            self.assertRegex(r['first_seen'], r'^\d{2}\.\d{2}\.\d{4} \d{2}:\d{2}$')

    def test_default_order_is_by_time_newest_first(self):
        keys = [(r['first_seen'][6:10], r['first_seen'][3:5], r['first_seen'][:2], r['first_seen'][11:])
                for r in demo.rows()]
        self.assertEqual(keys, sorted(keys, reverse=True))

    def test_purchase_rows_are_reports_without_buttons(self):
        buy = [r for r in demo.rows() if r['section'] == 'Purchasing']
        self.assertTrue(buy)
        self.assertTrue(all(not r['fixers'] for r in buy))

    def test_demo_has_no_info_only_rows(self):
        titles = [r['title'] for r in demo.rows()]
        self.assertFalse(any(t.startswith('Do not order yet') or t.startswith('Main thing today') for t in titles))

    def test_demo_shows_what_the_analyst_did_on_its_own(self):
        titles = [r['title'] for r in demo.rows()]
        self.assertTrue(any(t.startswith('The Analyst did it itself') for t in titles))
        self.assertTrue(any(t.startswith('The Analyst failed to cope') for t in titles))
        self.assertTrue(any(t.startswith('The Analyst could not run') for t in titles))
