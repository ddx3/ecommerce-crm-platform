from django.test import SimpleTestCase

from core import canary


class FormatPriceLabelNoneTests(SimpleTestCase):
    """If the price is missing (None), the label must not raise TypeError."""

    def test_none_price_returns_placeholder(self):
        # Before the fix: int(None) → TypeError. After the fix: returns a placeholder.
        result = canary.format_price_label(None)
        self.assertIsInstance(result, str)
        self.assertIn('UAH', result)

    def test_regular_price_still_formatted(self):
        self.assertEqual(canary.format_price_label(1250), '1 250 UAH')
