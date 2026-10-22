from django.test import SimpleTestCase

from core import canary


class CanaryTests(SimpleTestCase):
    """Ordinary inputs work. Behavior on bad inputs is checked by the new tests that the Analyst writes
during the drill."""

    def test_price_label(self):
        self.assertEqual(canary.format_price_label(1250), '1 250 UAH')
        self.assertEqual(canary.format_price_label(0), '0 UAH')

    def test_short_title(self):
        self.assertEqual(canary.short_title('Spade', 20), 'Spade')
        self.assertEqual(canary.short_title('Quite long name of a garden spade', 12), 'Quite long…')

    def test_discount_percent(self):
        self.assertEqual(canary.discount_percent(200, 150), 25)

    def test_average_rating(self):
        self.assertEqual(canary.average_rating([5, 4, 4]), 4.3)

    def test_first_word(self):
        self.assertEqual(canary.first_word('Garden spade'), 'Garden')
