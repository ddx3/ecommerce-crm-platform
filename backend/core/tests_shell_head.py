from unittest import mock

from django.test import TestCase


class ShellHeadRequestTests(TestCase):
    """HEAD on storefront pages: uptime monitors and link checkers used to see 405 (shell_view was
@require_GET)."""

    def test_head_is_allowed_where_get_is(self):
        with mock.patch('core.shell._read_index', return_value=(None, 0)):
            self.assertEqual(self.client.get('/', secure=True).status_code, 200)
            resp = self.client.head('/', secure=True)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.content, b'')

    def test_other_methods_are_still_refused(self):
        with mock.patch('core.shell._read_index', return_value=(None, 0)):
            self.assertEqual(self.client.post('/', secure=True).status_code, 405)
