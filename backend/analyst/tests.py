from datetime import timedelta
from decimal import Decimal
from unittest import mock

from django.test import TestCase, override_settings
from django.utils import timezone

from analyst import runner
from analyst.checks import Item, health
from analyst.models import AnalystRun, Finding, JobHeartbeat, StockSnapshot
from analyst.testutils import NoOrderThrottle


def _item(key='k1', severity=Finding.WARN, title='t', check='disk_usage'):
    return Item(key=key, severity=severity, title=title, check=check, section=Finding.SECTION_HEALTH)


def _order(**kw):
    from orders.models import Order
    # Order.save() derives phone_normalized itself from customer_phone.
    defaults = dict(customer_name='Test', customer_phone='+380000000000',
                    delivery_type='pickup', payment_method='cod', total_price=Decimal('100'))
    defaults.update(kw)
    created_at = defaults.pop('created_at', None)
    order = Order.objects.create(**defaults)
    if created_at is not None:
        Order.objects.filter(pk=order.pk).update(created_at=created_at)
        order.refresh_from_db()
    return order


class ApplyDedupTests(TestCase):
    """Finding lifecycle: new → repeat → close → reopen."""

    def setUp(self):
        self.now = timezone.now()

    def _apply(self, items, now=None, crashed=()):
        out = runner.Outcome(items=items, crashed=list(crashed))
        fresh = runner.apply(out, now or self.now)
        return fresh, out

    def test_new_finding_is_created_once(self):
        fresh, _ = self._apply([_item()])
        self.assertEqual(fresh, 1)
        fresh, _ = self._apply([_item()], self.now + timedelta(days=1))
        self.assertEqual(fresh, 0, 'a repeat of the same problem is not a new finding')
        self.assertEqual(Finding.objects.count(), 1)

    def test_done_status_survives_repeat_but_not_escalation(self):
        self._apply([_item()])
        Finding.objects.update(status=Finding.DONE)
        fresh, _ = self._apply([_item()], self.now + timedelta(days=1))
        self.assertEqual(fresh, 0)
        self.assertEqual(Finding.objects.get().status, Finding.DONE)
        fresh, _ = self._apply([_item(severity=Finding.CRITICAL)], self.now + timedelta(days=2))
        self.assertEqual(fresh, 1, 'an increase in severity raises the card again')
        self.assertEqual(Finding.objects.get().status, Finding.NEW)

    def test_snooze_expires(self):
        self._apply([_item()])
        until = self.now + timedelta(days=7)
        Finding.objects.update(status=Finding.SNOOZED, snoozed_until=until)
        fresh, _ = self._apply([_item()], self.now + timedelta(days=3))
        self.assertEqual(fresh, 0)
        self.assertEqual(Finding.objects.get().status, Finding.SNOOZED)
        fresh, _ = self._apply([_item()], until + timedelta(minutes=1))
        self.assertEqual(fresh, 1)
        f = Finding.objects.get()
        self.assertEqual((f.status, f.snoozed_until), (Finding.NEW, None))

    def test_vanished_finding_is_resolved_and_reopens_as_new(self):
        self._apply([_item()])
        # The check ran, but no longer returned the problem.
        with mock.patch.object(runner, 'all_checks',
                               return_value={'disk_usage': (Finding.SECTION_HEALTH, lambda n: [])}):
            self._apply([], self.now + timedelta(days=1))
        self.assertIsNotNone(Finding.objects.get().resolved_at)
        Finding.objects.update(status=Finding.DONE)
        fresh, _ = self._apply([_item()], self.now + timedelta(days=5))
        self.assertEqual(fresh, 1)
        f = Finding.objects.get()
        self.assertIsNone(f.resolved_at)
        self.assertEqual(f.status, Finding.NEW)

    def test_crashed_check_does_not_resolve_its_findings(self):
        self._apply([_item()])
        with mock.patch.object(runner, 'all_checks',
                               return_value={'disk_usage': (Finding.SECTION_HEALTH, lambda n: [])}):
            self._apply([], self.now + timedelta(days=1), crashed=['disk_usage'])
        self.assertIsNone(Finding.objects.get().resolved_at)

    def test_duplicate_keys_in_one_run_collapse(self):
        fresh, _ = self._apply([_item(title='a'), _item(title='b')])
        self.assertEqual(fresh, 1)
        self.assertEqual(Finding.objects.get().title, 'b')


class CollectTests(TestCase):
    def test_crashing_check_becomes_a_finding_and_others_still_run(self):
        def boom(now):
            raise RuntimeError('x')
        registry = {
            'boom': (Finding.SECTION_HEALTH, boom),
            'ok': (Finding.SECTION_HEALTH, lambda now: [Item(key='a', severity=1, title='ok')]),
        }
        with mock.patch.object(runner, 'all_checks', return_value=registry):
            out = runner.collect(timezone.now())
        self.assertEqual(out.crashed, ['boom'])
        keys = {i.key for i in out.items}
        self.assertEqual(keys, {'check_failed:boom', 'a'})


class RunDailyTests(TestCase):
    def test_one_successful_run_per_day(self):
        with mock.patch.object(runner, 'collect', return_value=runner.Outcome()), \
             mock.patch('analyst.snapshots.take_stock_snapshot', return_value=0):
            first = runner.run_daily()
            second = runner.run_daily()
            forced = runner.run_daily(force=True)
        self.assertEqual(first.status, AnalystRun.OK)
        self.assertIsNone(second)
        self.assertIsNotNone(forced)
        self.assertEqual(AnalystRun.objects.count(), 1)

    def test_failed_run_is_recorded(self):
        with mock.patch.object(runner, 'collect', side_effect=RuntimeError('db down')):
            run = runner.run_daily()
        self.assertEqual(run.status, AnalystRun.FAILED)
        self.assertIn('db down', run.error)


class ReadOnlyTests(TestCase):
    """The analyst must not modify shop data — only the analyst_* tables."""

    def test_real_checks_do_not_touch_shop_tables(self):
        from catalog.models import Product, ProductVariant
        from orders.models import Order
        now = timezone.now()
        _order(status='processing', payment_status='paid', created_at=now - timedelta(days=10))
        before = (Order.objects.count(), list(Order.objects.values_list('pk', 'updated_at', 'status')),
                  Product.objects.count(), ProductVariant.objects.count())
        run = runner.run_daily()
        after = (Order.objects.count(), list(Order.objects.values_list('pk', 'updated_at', 'status')),
                 Product.objects.count(), ProductVariant.objects.count())
        self.assertEqual(run.status, AnalystRun.OK, run.error)
        self.assertEqual(before, after)


class HealthChecksTests(TestCase):
    def setUp(self):
        self.now = timezone.now()

    def test_heartbeat_empty_table_is_ignored_in_debug(self):
        with override_settings(DEBUG=True):
            self.assertEqual(health.scheduler_heartbeat(self.now), [])
        with override_settings(DEBUG=False):
            self.assertEqual([i.key for i in health.scheduler_heartbeat(self.now)], ['scheduler:no_heartbeat'])

    def test_silent_scheduler_is_one_critical_card(self):
        old = self.now - timedelta(hours=5)
        for name in ('reconcile_pending_payments', 'match_bank_transactions', 'recompute_khit'):
            JobHeartbeat.objects.create(name=name, last_ok_at=old)
        items = health.scheduler_heartbeat(self.now)
        self.assertEqual([i.key for i in items], ['scheduler:silent'])
        self.assertEqual(items[0].severity, Finding.CRITICAL)

    def test_stale_daily_job_and_failing_job(self):
        fresh = self.now - timedelta(minutes=3)
        JobHeartbeat.objects.create(name='reconcile_pending_payments', last_ok_at=fresh)
        JobHeartbeat.objects.create(name='heal_stock_drift', last_ok_at=self.now - timedelta(hours=60))
        JobHeartbeat.objects.create(name='match_bank_transactions', last_ok_at=fresh - timedelta(minutes=10),
                                    last_fail_at=fresh, last_error='RuntimeError: x')
        keys = {i.key for i in health.scheduler_heartbeat(self.now)}
        self.assertEqual(keys, {'job_stale:heal_stock_drift', 'job_failing:match_bank_transactions'})

    def test_healthy_scheduler_has_no_cards(self):
        fresh = self.now - timedelta(minutes=3)
        for name in ('reconcile_pending_payments', 'recompute_khit'):
            JobHeartbeat.objects.create(name=name, last_ok_at=fresh)
        self.assertEqual(health.scheduler_heartbeat(self.now), [])

    def test_disk_thresholds(self):
        gib = 2 ** 30
        with mock.patch.object(health.shutil, 'disk_usage', return_value=(100 * gib, 94 * gib, 6 * gib)):
            items = health.disk_usage(self.now)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].severity, Finding.CRITICAL)
        with mock.patch.object(health.shutil, 'disk_usage', return_value=(100 * gib, 50 * gib, 50 * gib)):
            self.assertEqual(health.disk_usage(self.now), [])

    def test_disk_below_90_percent_is_not_worth_a_card(self):
        gib = 2 ** 30
        with mock.patch.object(health.shutil, 'disk_usage', return_value=(100 * gib, 84 * gib, 16 * gib)):
            self.assertEqual(health.disk_usage(self.now), [])

    def test_one_disk_mounted_twice_gives_one_finding(self):
        """The code and the media volume are different mounts (different st_dev) of the same disk: one finding,
not two."""
        gib = 2 ** 30
        devices = iter([101, 202, 303])
        fake = mock.Mock(side_effect=lambda path: mock.Mock(st_dev=next(devices)))
        with override_settings(MEDIA_ROOT='/media-mount'),                 mock.patch.object(health.os, 'stat', fake),                 mock.patch.object(health.shutil, 'disk_usage', return_value=(100 * gib, 94 * gib, 6 * gib)):
            items = health.disk_usage(self.now)
        self.assertEqual(len(items), 1)

    def test_backup_freshness(self):
        import os
        import tempfile
        with tempfile.TemporaryDirectory() as tmp, override_settings(ANALYST_BACKUP_DIR=tmp):
            self.assertEqual([i.key for i in health.backup_freshness(self.now)], ['backup:none'])
            path = os.path.join(tmp, 'db.dump')
            open(path, 'wb').close()
            self.assertEqual(health.backup_freshness(self.now), [])
            old = (self.now - timedelta(days=10)).timestamp()
            os.utime(path, (old, old))
            items = health.backup_freshness(self.now)
            self.assertEqual((items[0].key, items[0].severity), ('backup:stale', Finding.CRITICAL))

    @override_settings(ANALYST_BACKUP_DIR='')
    def test_backup_check_can_be_disabled(self):
        self.assertEqual(health.backup_freshness(self.now), [])

    @override_settings(SITE_URL='http://localhost:5173')
    def test_ssl_skipped_for_plain_http(self):
        self.assertEqual(health.ssl_expiry(self.now), [])


@override_settings(ANALYST_OWNER_EMAIL='boss@example.com')
class InboxViewTests(TestCase):
    def setUp(self):
        from django.contrib.auth import get_user_model
        User = get_user_model()
        # The owner's email in the DB may be in a different case — the comparison is
        # case-insensitive.
        self.staff = User.objects.create_user(
            email='Boss@Example.com', password='x', is_staff=True, is_superuser=True)
        self.other = User.objects.create_user(
            email='manager@example.com', password='x', is_staff=True, is_superuser=True)
        self.client.force_login(self.staff)

    def _finding(self, **kw):
        defaults = dict(key='k', check_name='x', section=Finding.SECTION_HEALTH, severity=Finding.WARN, title='Test finding')
        defaults.update(kw)
        return Finding.objects.create(**defaults)

    def test_page_is_one_table_ordered_by_time_with_state_shown_by_marks(self):
        """No tabs: the default order is by time (newest first), and whether an item is new or handled is
visible from the label."""
        now = timezone.now()
        self._finding(key='done1', status=Finding.DONE, title='Already done', severity=Finding.CRITICAL,
                      first_seen=now - timedelta(days=3))
        self._finding(key='new1', body='details', link='/admin/orders/order/1/change/', title='New finding',
                      first_seen=now - timedelta(days=1))
        self._finding(key='done2', status=Finding.DONE, title='Recently handled', first_seen=now)
        response = self.client.get('/admin/analyst/')
        self.assertEqual(response.status_code, 200)
        titles = [r['title'] for r in response.context['rows']]
        self.assertEqual(titles, ['Recently handled', 'New finding', 'Already done'])   # time only, status is not counted
        page = response.content.decode()
        self.assertEqual(page.count('ar-row ntf-step-0'), 1)             # red label only on a new one
        self.assertEqual(page.count('ar-row ntf-step-1'), 2)
        self.assertNotIn('class="ntf-subtab', page)                      # no tabs
        self.assertNotIn('?tab=', page)
        by_title = {r['title']: r for r in response.context['rows']}
        self.assertTrue(by_title['New finding']['is_new'])
        self.assertFalse(by_title['Already done']['is_new'])
        import re
        self.assertRegex(response.context['rows'][0]['first_seen'], r'^\d{2}\.\d{2}\.\d{4} \d{2}:\d{2}$')   # date with time

    @mock.patch('analyst.hostops.configured', return_value=True)
    def test_processed_rows_have_no_fix_buttons(self, _configured):
        v_finding = dict(check_name='disk_usage')
        self._finding(key='n', **v_finding)
        self._finding(key='d', status=Finding.DONE, **v_finding)
        rows = self.client.get('/admin/analyst/').context['rows']
        self.assertEqual({r['is_new']: bool(r['fixers']) for r in rows}, {True: True, False: False})   # order by time, not by state

    def test_empty_page_is_a_plain_standard_table(self):
        """As in all inbox tables: empty = just the header and zebra stripes, no placeholder text."""
        response = self.client.get('/admin/analyst/')
        self.assertContains(response, 'class="ntf-table ar-nofix"')      # no buttons → the "Action" column is not shown
        self.assertNotContains(response, 'class="ar-row')

    def test_page_requires_staff(self):
        self.client.logout()
        response = self.client.get('/admin/analyst/')
        self.assertEqual(response.status_code, 302)

    def test_actions_change_status_and_badge_count(self):
        from analyst.inbox import new_count
        f = self._finding()
        self.assertEqual(new_count(), 1)
        post = lambda action, **kw: self.client.post(  # noqa: E731
            '/admin/analyst/action/', data={'id': f.pk, 'action': action, **kw}, content_type='application/json')
        self.assertEqual(post('done').status_code, 200)
        f.refresh_from_db()
        self.assertEqual(f.status, Finding.DONE)
        self.assertEqual(new_count(), 0)
        post('reopen')
        f.refresh_from_db()
        self.assertEqual((f.status, f.snoozed_until), (Finding.NEW, None))
        self.assertEqual(post('bogus').status_code, 400)
        self.assertEqual(post('snooze').status_code, 400, '"Snooze" was removed from the interface and from the API')

    def test_bulk_action_changes_several_rows_at_once(self):
        ids = [self._finding(key=f'b{i}').pk for i in range(3)]
        response = self.client.post('/admin/analyst/action/', data={'ids': ids[:2], 'action': 'done'},
                                    content_type='application/json')
        self.assertEqual(response.json()['updated'], 2)
        self.assertEqual(Finding.objects.filter(status=Finding.DONE).count(), 2)

    def test_action_rejects_get_and_unknown_id(self):
        self.assertEqual(self.client.get('/admin/analyst/action/').status_code, 405)
        response = self.client.post('/admin/analyst/action/', data={'id': 999, 'action': 'done'},
                                    content_type='application/json')
        self.assertEqual(response.status_code, 404)

    def test_resolved_findings_are_not_shown_or_counted(self):
        from analyst.inbox import new_count
        self._finding(resolved_at=timezone.now())
        self.assertEqual(new_count(), 0)
        self.assertNotContains(self.client.get('/admin/analyst/'), 'Test finding')

    def test_notification_counts_include_analyst(self):
        from catalog.notifications import get_notification_counts
        self._finding()
        self.assertEqual(get_notification_counts()['analyst'], 1)


@override_settings(ANALYST_OWNER_EMAIL='boss@example.com')
class OwnerOnlyTests(TestCase):
    """The Analyst is visible only to the owner: other staff get 403, no row in the sidebar and no
count."""

    def setUp(self):
        from django.contrib.auth import get_user_model
        User = get_user_model()
        self.owner = User.objects.create_user(
            email='boss@example.com', password='x', is_staff=True, is_superuser=True)
        self.other = User.objects.create_user(
            email='manager@example.com', password='x', is_staff=True, is_superuser=True)
        self.finding = Finding.objects.create(
            key='k', check_name='x', section=Finding.SECTION_HEALTH, severity=Finding.WARN, title='Secret finding')

    def _act(self, action='done'):
        return self.client.post('/admin/analyst/action/', data={'id': self.finding.pk, 'action': action},
                                content_type='application/json')

    def test_page_and_action_are_forbidden_for_other_staff(self):
        self.client.force_login(self.other)
        self.assertEqual(self.client.get('/admin/analyst/').status_code, 403)
        self.assertEqual(self._act().status_code, 403)
        self.finding.refresh_from_db()
        self.assertEqual(self.finding.status, Finding.NEW)

    def test_owner_has_access(self):
        self.client.force_login(self.owner)
        self.assertContains(self.client.get('/admin/analyst/'), 'Secret finding')
        self.assertEqual(self._act().status_code, 200)

    def test_empty_owner_email_matches_nobody(self):
        from types import SimpleNamespace
        from analyst.access import is_owner
        nameless = SimpleNamespace(is_authenticated=True, is_staff=True, email='')
        with override_settings(ANALYST_OWNER_EMAIL=''):
            self.assertFalse(is_owner(nameless))

    def test_non_staff_is_not_owner(self):
        from analyst.access import is_owner
        self.owner.is_staff = False
        self.assertFalse(is_owner(self.owner))

    def test_count_endpoint_hides_number_from_other_staff(self):
        self.client.force_login(self.other)
        self.assertEqual(self.client.get('/admin/notifications/count/').json()['analyst'], 0)
        self.client.force_login(self.owner)
        self.assertEqual(self.client.get('/admin/notifications/count/').json()['analyst'], 1)

    def test_sidebar_row_and_initial_badge_only_for_owner(self):
        from django.core.cache import cache
        cache.clear()
        self.client.force_login(self.other)
        page = self.client.get('/admin/').content.decode()
        self.assertNotIn('sidebar-analyst-row', page)
        self.assertIn('"analyst": 0', page)
        cache.clear()
        self.client.force_login(self.owner)
        page = self.client.get('/admin/').content.decode()
        self.assertIn('sidebar-analyst-row', page)
        self.assertIn('"analyst": 1', page)


class AttributionTests(TestCase):
    def test_saves_utm_and_strips_referrer_query(self):
        from analyst.attribution import save_order_attribution
        from analyst.models import OrderAttribution
        save_order_attribution(7, {
            'utm_source': 'instagram', 'utm_medium': 'paid', 'utm_campaign': 'autumn_2026',
            'referrer': 'https://l.instagram.com/path?u=secret-token#frag',
            'landing_path': '/product/x?utm_source=instagram',
        })
        a = OrderAttribution.objects.get(order_id=7)
        self.assertEqual((a.utm_source, a.utm_medium, a.utm_campaign), ('instagram', 'paid', 'autumn_2026'))
        self.assertEqual(a.referrer, 'https://l.instagram.com/path')
        self.assertEqual(a.landing_path, '/product/x')

    def test_ignores_garbage_and_empty(self):
        from analyst.attribution import save_order_attribution
        from analyst.models import OrderAttribution
        for raw in (None, 'x', [], {}, {'utm_source': ''}, {'referrer': 'not a url'}, {'utm_source': {'a': 1}}):
            save_order_attribution(1, raw)
        self.assertEqual(OrderAttribution.objects.count(), 0)

    def test_long_values_are_truncated(self):
        from analyst.attribution import save_order_attribution
        from analyst.models import OrderAttribution
        save_order_attribution(2, {'utm_campaign': 'x' * 500})
        self.assertEqual(len(OrderAttribution.objects.get(order_id=2).utm_campaign), 150)

    def test_repeat_overwrites_instead_of_failing(self):
        from analyst.attribution import save_order_attribution
        from analyst.models import OrderAttribution
        save_order_attribution(3, {'utm_source': 'a'})
        save_order_attribution(3, {'utm_source': 'b'})
        self.assertEqual(OrderAttribution.objects.get(order_id=3).utm_source, 'b')


@override_settings(ANTIBOT_ENABLED=False)
class OrderCreationAttributionTests(NoOrderThrottle, TestCase):
    """End to end: the frontend sends attribution together with the order — it lands in analyst_*."""

    def _create(self, extra):
        from catalog.models import Category, Product, ProductVariant
        import json
        cat = Category.objects.create(name='Test')
        p = Product.objects.create(name='Test product')
        p.categories.add(cat)
        v = ProductVariant.objects.create(product=p, price=Decimal('300'), stock=10)   # above the minimum order amount
        payload = {
            'customer_name': 'Test', 'customer_phone': '+380000000000', 'delivery_type': 'nova_poshta',
            'delivery_address': 'Kyiv, branch 1', 'payment_method': 'cod',
            'items': [{'variant_id': v.id, 'qty': 1}], **extra,
        }
        return self.client.post('/api/orders/create/', data=json.dumps(payload), content_type='application/json')

    def test_attribution_is_saved_with_the_order(self):
        from analyst.models import OrderAttribution
        from orders.models import Order
        resp = self._create({'attribution': {'utm_source': 'instagram', 'utm_campaign': 'story_1'}})
        self.assertEqual(resp.status_code, 200, resp.content)
        order = Order.objects.get(order_number=resp.json()['order_number'])
        self.assertEqual(OrderAttribution.objects.get(order_id=order.pk).utm_campaign, 'story_1')

    def test_order_succeeds_without_or_with_broken_attribution(self):
        from analyst.models import OrderAttribution
        self.assertEqual(self._create({}).status_code, 200)
        self.assertEqual(self._create({'attribution': 'garbage'}).status_code, 200)
        self.assertEqual(OrderAttribution.objects.count(), 0)

    def test_attribution_failure_never_breaks_the_order(self):
        with mock.patch('analyst.models.OrderAttribution.objects.update_or_create',
                        side_effect=RuntimeError('db')):
            resp = self._create({'attribution': {'utm_source': 'x'}})
        self.assertEqual(resp.status_code, 200, resp.content)


class SearchLogTests(TestCase):
    def test_counts_queries_and_zero_results(self):
        from analyst.attribution import log_search
        from analyst.models import SearchLog
        log_search('  Seeds  ', 5)
        log_search('seeds', 0)
        log_search('x', 3)              # too short
        row = SearchLog.objects.get()
        self.assertEqual((row.query, row.count, row.zero_count), ('seeds', 2, 1))

    def test_new_query_cap_protects_the_table(self):
        from analyst import attribution
        from analyst.attribution import log_search
        from analyst.models import SearchLog
        with mock.patch.object(attribution, 'SEARCH_MAX_NEW_QUERIES_PER_DAY', 2):
            for q in ('aa', 'bb', 'cc'):
                log_search(q, 1)
            log_search('aa', 1)         # an existing one is counted even over the limit
        self.assertEqual(SearchLog.objects.count(), 2)
        self.assertEqual(SearchLog.objects.get(query='aa').count, 2)

    def test_catalog_search_endpoint_logs_first_page_only(self):
        from analyst.models import SearchLog
        self.client.get('/api/products/', {'search': 'lime'})
        self.client.get('/api/products/', {'search': 'lime', 'page': '2'})
        self.client.get('/api/products/')       # no search — nothing
        row = SearchLog.objects.get()
        self.assertEqual((row.query, row.count, row.zero_count), ('lime', 1, 1))


class SnapshotTests(TestCase):
    def test_snapshot_is_idempotent_and_skips_pure_drop(self):
        from catalog.models import Product, ProductVariant
        product = Product.objects.create(name='Test')
        own = ProductVariant.objects.create(product=product, name='own', price=Decimal('10'), stock=5)
        ProductVariant.objects.create(product=product, name='drop', price=Decimal('10'), stock=7,
                                      is_dropshipping=True)
        from analyst.snapshots import take_stock_snapshot
        today = timezone.localdate()
        take_stock_snapshot(today)
        take_stock_snapshot(today)
        rows = list(StockSnapshot.objects.values_list('variant_id', 'stock'))
        self.assertEqual(rows, [(own.pk, 5)])


@override_settings(ANALYST_OWNER_EMAIL='boss@example.com')
class NoStandaloneAdminTests(TestCase):
    """In the CRM the Analyst has only one page (/admin/analyst/). There must be no separate
    Django-admin sections
    (formerly "Ordered purchases", /admin/analyst/plannedpurchase/), even for
    the owner."""

    def test_no_analyst_model_is_registered_in_the_django_admin(self):
        from django.contrib import admin
        self.assertEqual([m.__name__ for m in admin.site._registry if m._meta.app_label == 'analyst'], [])

    def test_old_section_urls_are_gone_for_the_owner(self):
        from django.contrib.auth import get_user_model
        owner = get_user_model().objects.create_user(
            email='boss@example.com', password='x', is_staff=True, is_superuser=True)
        self.client.force_login(owner)
        for url in ('/admin/analyst/plannedpurchase/', '/admin/analyst/plannedpurchase/add/',
                    '/admin/analyst/finding/', '/admin/analyst/codefix/'):
            self.assertEqual(self.client.get(url, secure=True).status_code, 404, url)
        self.assertEqual(self.client.get('/admin/analyst/', secure=True).status_code, 200)       # the Analyst page itself remained

    def test_marking_a_purchase_still_records_it_without_any_admin(self):
        """The ✓ on the "Order …" card goes on to write a purchase, which the calculation
subtracts from the demand."""
        from analyst import advice
        from analyst.models import PlannedPurchase
        f = Finding.objects.create(
            key='purchase:bag:7', check_name=advice.CHECK_ADVICE, section=Finding.SECTION_PURCHASE, severity=Finding.WARN,
            title='Order 1 kg X', data={'action': 'buy', 'qty': 1000, 'unit': 'g', 'item': {'key': 'bag:7', 'label': 'X'}})
        self.assertEqual(advice.mark_ordered([f]), 1)
        self.assertEqual(PlannedPurchase.objects.filter(key='bag:7', status=PlannedPurchase.OPEN).count(), 1)
        self.assertEqual(advice.unmark_ordered([f]), 1)
