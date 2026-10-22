"""Cards about the helper's state regarding auto-fixes: failed recovery after a crash, missing signing
key."""
from datetime import datetime, timezone
from unittest import mock

from django.test import TestCase, override_settings

from analyst import hostops
from analyst.checks import code_errors
from analyst.guard import GUARD_CHECKS
from analyst.models import Finding

NOW = datetime(2026, 10, 21, 3, 0, tzinfo=timezone.utc)


class AutofixStatusTests(TestCase):
    def items(self, status, codefix='propose'):
        with mock.patch.object(hostops, 'status', return_value=status), override_settings(ANALYST_CODEFIX=codefix):
            return code_errors.autofix_status(NOW)

    def test_a_failed_recovery_is_a_critical_card(self):
        items = self.items({'heartbeat': NOW.isoformat(), 'key_ok': True,
                            'recovery': {'ok': False, 'error': 'up: boom', 'at': NOW.isoformat()}})
        self.assertEqual([(i.key, i.severity) for i in items], [('autofix_recovery', Finding.CRITICAL)])
        self.assertIn('up: boom', items[0].body)
        self.assertTrue(items[0].data['no_auto'])

    def test_a_missing_key_on_the_server_is_explained(self):
        items = self.items({'heartbeat': NOW.isoformat(), 'key_ok': False})
        self.assertEqual([i.key for i in items], ['autofix_no_key'])
        self.assertIn('install.sh', items[0].body)

    def test_nothing_to_say_when_all_is_fine_or_the_feature_is_off(self):
        self.assertEqual(self.items({'heartbeat': NOW.isoformat(), 'key_ok': True, 'recovery': {'ok': True}}), [])
        self.assertEqual(self.items({'heartbeat': NOW.isoformat(), 'key_ok': True, 'recovery': {'ok': None, 'skipped': 'x'}}), [])
        self.assertEqual(self.items({'heartbeat': NOW.isoformat(), 'key_ok': False}, codefix='off'), [])
        self.assertEqual(self.items(None), [])

    def test_the_check_is_part_of_the_guard_cycle_and_registered(self):
        self.assertIn('autofix_status', GUARD_CHECKS)
        from analyst.checks import REGISTRY
        self.assertIn('autofix_status', REGISTRY)
