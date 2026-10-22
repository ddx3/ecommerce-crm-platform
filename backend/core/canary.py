"""Training functions for the auto-fix drill (analyst/codefix): NOT called in production code.

Each has a deliberate bug on a 'bad' input. The drill (`manage.py run_analyst --codefix-drill`)
takes the first not-yet-fixed
function, reproduces its failure with a real exception and runs the whole Analyst path: from the
agent's draft to rollout
and rollback. A fixed function is not used further; the next drill takes the next one.
"""


def format_price_label(price):
    """Price label for tables: 1250 → "1,250 UAH"."""
    if price is None:
        return '— UAH'
    return f'{int(price):,}'.replace(',', ' ') + ' UAH'


def short_title(text, limit):
    """Truncates a title to limit characters, appending «…»."""
    if text is None:
        return '—'
    if len(text) <= limit:
        return text
    return text[:limit - 1].rstrip() + '…'


def discount_percent(old_price, new_price):
    """By how many percent the price was reduced."""
    return round((old_price - new_price) / old_price * 100)


def average_rating(ratings):
    """Average review rating with one decimal place."""
    return round(sum(ratings) / len(ratings), 1)


def first_word(text):
    """First word of the title (for short labels)."""
    return text.split()[0]


# (function, 'bad' argument(s)): the drill calls them until one fails.
DRILL_CASES = (
    (format_price_label, (None,)),
    (short_title, (None, 20)),
    (discount_percent, (0, 0)),
    (average_rating, ([],)),
    (first_word, ('',)),
)
