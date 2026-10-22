from django.test import SimpleTestCase

from core import canary


class ShortTitleNoneTests(SimpleTestCase):
    """If the title is missing (None), truncation must not raise TypeError."""

    def test_none_text_returns_placeholder(self):
        # Before the fix: len(None) → TypeError. After the fix: returns a placeholder.
        result = canary.short_title(None, 20)
        self.assertEqual(result, '—')

    def test_regular_text_still_trimmed(self):
        self.assertEqual(
            canary.short_title('A very long name of a garden shovel', 12),
            'A very long…',
        )

    def test_short_text_unchanged(self):
        self.assertEqual(canary.short_title('Shovel', 20), 'Shovel')
