"""Matching BankTransaction ↔ Order for auto-confirming a 'bank transfer' payment.

Logic:
  1. Take an unmatched transaction (incoming only, not our own acquiring revenue).
  2. Candidates: Order.payment_method='bank_transfer' AND payment_status='pending',
     created_at within ±48 h of the transaction.
  3. Build a confidence score from factors; store a PaymentMatch for each
     candidate with score ≥ MANUAL_THRESHOLD.
  4. Best candidate:
        score ≥ AUTO_THRESHOLD → auto-confirm (Order.payment_status='paid')
        AUTO > score ≥ MANUAL → leave in the manual confirmation queue
        score <  MANUAL_THRESHOLD → the transaction stays unmatched.

The payment screenshot (anti-fraud) is detected in a separate module (TODO),
but the model interface is ready — we set a high confidence, reasons contain
the type `screenshot_match`.
"""
from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal
from typing import Iterable

from django.db import transaction
from django.utils import timezone

from .models import BankTransaction, Order, PaymentMatch

log = logging.getLogger(__name__)

# Auto-confirm threshold. Conservative: only if there is an exact order-number match (100) OR exact
# amount + name (>=90).
AUTO_THRESHOLD = 90
# Below this we don't even put it into the manual review queue — too noisy.
MANUAL_THRESHOLD = 50

# Window for searching orders around the transaction time.
# An order is created before payment; the payment may arrive with a delay.
ORDER_LOOKBACK = timedelta(hours=48)
ORDER_LOOKAHEAD = timedelta(hours=2)  # rare case: payment before creation

# 'Noise': transfers that aren't customer payments and must be ignored.
# Mono acquiring (counterparty "JSC Universal Bank"): our own payouts from our own site,
# the order is already paid via the `monobank.py` webhook.
# Language data: the bank name below is matched, lower-cased, against Ukrainian/Russian statement text.
NOISE_COUNTERPARTIES = {
    'ат "універсал банк"',
    'ат універсал банк',
    'универсал банк',
}
# Substrings that are definitely not customers (PUMB, Oschadbank transfers, etc. — without a full
# name):
NOISE_DESCRIPTION_SUBSTRINGS = (
    'fuib moneytransfer',
    'oschad moneytransfer',
)


@dataclass
class MatchReason:
    type: str
    weight: int
    detail: str = ''

    def to_dict(self) -> dict:
        return {'type': self.type, 'weight': self.weight, 'detail': self.detail}


@dataclass
class Candidate:
    order: Order
    score: int = 0
    reasons: list[MatchReason] = field(default_factory=list)


# ── Text utilities ─────────────────────────────────────────────────────────

def _norm(s: str) -> str:
    """Normalization for string comparison: lower + diacritics removal + whitespace."""
    if not s:
        return ''
    s = unicodedata.normalize('NFKD', s)
    s = ''.join(c for c in s if not unicodedata.combining(c))
    s = s.lower().strip()
    s = re.sub(r'\s+', ' ', s)
    return s


def _name_tokens(s: str) -> set[str]:
    """Splits a full name into tokens, dropping short ones (initials like 'i.')."""
    if not s:
        return set()
    return {t for t in re.split(r'[\s.,]+', _norm(s)) if len(t) >= 3}


# Transliteration of Cyrillic to Latin (CMU-2010, simplified — without positional rules, since
# _skeleton below swallows the difference anyway).
_CYR_TO_LAT = {
    'а': 'a', 'б': 'b', 'в': 'v', 'г': 'h', 'ґ': 'g', 'д': 'd', 'е': 'e', 'є': 'ie',
    'ж': 'zh', 'з': 'z', 'и': 'y', 'і': 'i', 'ї': 'i', 'й': 'i', 'к': 'k', 'л': 'l',
    'м': 'm', 'н': 'n', 'о': 'o', 'п': 'p', 'р': 'r', 'с': 's', 'т': 't', 'у': 'u',
    'ф': 'f', 'х': 'kh', 'ц': 'ts', 'ч': 'ch', 'ш': 'sh', 'щ': 'shch', 'ь': '',
    'ю': 'iu', 'я': 'ia',
}

# Pairs that different romanization systems spell differently. We reduce them to a single symbol so
# that Latin "Ivanova" (as Mono writes it) and the Cyrillic surname transliterated to Latin match.
_SKELETON_DIGRAPHS = (
    ('shch', 's'), ('sch', 's'), ('sh', 's'), ('ch', 'c'), ('kh', 'h'), ('ts', 'c'),
    ('zh', 'z'), ('ye', 'e'), ('ie', 'e'), ('yi', 'i'), ('iu', 'u'), ('yu', 'u'),
    ('ia', 'a'), ('ya', 'a'),
)
_SKELETON_SINGLES = str.maketrans({'y': 'i', 'g': 'h', 'k': 'c', 'q': 'c', 'x': 'h', 'w': 'v'})


def _translit(s: str) -> str:
    return ''.join(_CYR_TO_LAT.get(ch, ch) for ch in s)


def _skeleton(token: str) -> str:
    """Romanization-insensitive 'skeleton' of a token.

    Mono returns the full name in Latin in its own romanization, while in the order it is in
    Cyrillic, so direct token comparison never matches — which is why the "full name + amount"
    factor fired only 3 times out of 63. We reduce both sides to a common form:
    transliterate Cyrillic, collapse ambiguous digraphs (kh/h, ts/c, sh/s…) and doubled letters
    («Anna» → «ana»).
    """
    s = _translit(token.lower())
    for a, b in _SKELETON_DIGRAPHS:
        s = s.replace(a, b)
    s = s.translate(_SKELETON_SINGLES)
    return re.sub(r'(.)\1+', r'\1', s)


def _skeleton_tokens(s: str) -> set[str]:
    """Full-name tokens in skeleton form. Empty/short ones are dropped."""
    return {sk for sk in (_skeleton(t) for t in _name_tokens(s)) if len(sk) >= 3}


def _digits_only(s: str) -> str:
    return re.sub(r'\D', '', s or '')


def _is_noise(tx: BankTransaction) -> bool:
    """Acquiring settlements and other transfers that aren't customer payments."""
    cp_norm = _norm(tx.counterparty_name)
    desc_norm = _norm(tx.description)
    if any(noise in cp_norm or noise in desc_norm for noise in NOISE_COUNTERPARTIES):
        return True
    if any(p in desc_norm or p in cp_norm for p in NOISE_DESCRIPTION_SUBSTRINGS):
        return True
    return False


# ── Matching rules (3 factors) ─────────────────────────────────────────────
# Only 3 scenarios give AUTO-confirm:
#   1) Order number in the comment + amount matches exactly
#   2) Counterparty full name ≈ customer_name + amount matches exactly
#   3) The customer's receipt screenshot matches a real transaction in the statement
#      (anti-fraud; implemented separately in the screenshot_match.py module)

def _amount_matches(order: Order, tx: BankTransaction) -> bool:
    return (
        order.total_price is not None
        and Decimal(str(order.total_price)) == tx.amount
    )


def _factor_order_number(order: Order, tx: BankTransaction) -> MatchReason | None:
    """Order number in comment/description + amount check.

      Number found + amount matches EXACTLY → 100 (AUTO)
      Number found + amount does NOT match   → 50  (manual review:
                                                   the customer may have mistyped the amount)
      No number                             → None
    """
    if not order.order_number:
        return None
    haystack = f'{tx.comment} {tx.description}'
    if not re.search(rf'(?<!\d){re.escape(order.order_number)}(?!\d)', haystack):
        return None
    if _amount_matches(order, tx):
        return MatchReason(
            'order_number_with_correct_amount', 100,
            f'Order number {order.order_number} found, amount {tx.amount} = total_price'
        )
    expected = order.total_price if order.total_price is not None else 'not set'
    return MatchReason(
        'order_number_amount_mismatch', 50,
        f'Order number {order.order_number} found, but the amount does NOT match '
        f'(paid {tx.amount}, expected {expected})'
    )


def _factor_name_and_amount(order: Order, tx: BankTransaction) -> MatchReason | None:
    """Payer full name + amount.

      Name matches (≥2 shared tokens, ≥50% of the customer's name) + amount EXACT → 100 (AUTO)
      Otherwise → None (so the matcher won't let this through MANUAL_THRESHOLD)

    We look for the name in two places, because banks put it differently:
      - Mono: in `counterparty_name` («Surname Name»), in Latin;
      - Privat: `counterparty_name` holds a TRANSIT ACCOUNT
        (the bank's Ukrainian wording "Транз.рахунок платежi_ DN, DG," = "Transit account of
        payments_ DN, DG,"), and the real full name is inside the
        payment purpose (the bank's Ukrainian wording "Сплата за ....товар 12762, <customer full name>"
        = "Payment for ....product 12762, ...").
    So the payment purpose goes into the haystack too. False positives are held back not by the
    narrowness of the haystack but by the other conditions: EXACT amount, ≥2 matching tokens and the
    48 h window.
    """
    cust_tokens = _skeleton_tokens(order.customer_name)
    if len(cust_tokens) < 2:
        # A single token (just a first name or just a surname) is not enough for auto-confirm.
        return None
    if not _amount_matches(order, tx):
        return None

    for source, raw in (('counterparty full name', tx.counterparty_name),
                        ('payment purpose', f'{tx.comment} {tx.description}')):
        hay_tokens = _skeleton_tokens(raw)
        if not hay_tokens:
            continue
        common = cust_tokens & hay_tokens
        if len(common) < 2:
            continue
        if len(common) / len(cust_tokens) < 0.5:
            continue
        return MatchReason(
            'name_and_amount_match', 100,
            f'Full name "{order.customer_name}" found in the field "{source}" '
            f'({len(common)} of {len(cust_tokens)} tokens) + amount {tx.amount} = total_price'
        )
    return None


FACTORS = [
    _factor_order_number,
    _factor_name_and_amount,
    # The screenshot is checked separately: on payment_screenshot upload we run OCR and look for a
    # BankTransaction by amount+time → create a PaymentMatch with reason='screenshot_match'. Logic
    # in orders/screenshot_match.py.
]


# ── Main matching function ────────────────────────────────────────────────

def _candidate_orders(tx: BankTransaction):
    """Take pending bank_transfer orders in the window around the transaction time."""
    return (
        Order.objects
        .filter(payment_method='bank_transfer', payment_status='pending')
        .exclude(status='cancelled')
        .exclude(is_imported=True)   # don't match OLD (historical, from the old site)
        .filter(created_at__gte=tx.transacted_at - ORDER_LOOKBACK,
                created_at__lte=tx.transacted_at + ORDER_LOOKAHEAD)
    )


def score_candidate(order: Order, tx: BankTransaction) -> Candidate:
    cand = Candidate(order=order)
    for factor in FACTORS:
        reason = factor(order, tx)
        if reason:
            cand.reasons.append(reason)
            cand.score += reason.weight
    cand.score = min(cand.score, 100)
    return cand


@transaction.atomic
def match_transaction(tx: BankTransaction, *, dry_run: bool = False) -> dict:
    """Finds candidates and applies the best one. Returns a dict with a report."""
    result: dict = {
        'tx_id': tx.pk,
        'tx_external_id': tx.external_id,
        'amount': str(tx.amount),
        'counterparty': tx.counterparty_name,
        'skipped': False,
        'reason': None,
        'candidates': [],
        'auto_confirmed_order': None,
        'manual_queue_orders': [],
    }

    # 1. Ignore 'own' and non-incoming transactions.
    if not tx.is_credit:
        result['skipped'] = True
        result['reason'] = 'not a credit'
        return result
    if _is_noise(tx):
        result['skipped'] = True
        result['reason'] = 'noise (acquiring / inter-bank transfer)'
        if not dry_run:
            tx.status = 'ignored'
            tx.save(update_fields=['status', 'updated_at'])
        return result
    if tx.status not in ('unmatched', 'manual_review'):
        result['skipped'] = True
        result['reason'] = f'tx already in status={tx.status}'
        return result

    # 2. Candidate scoring.
    candidates: list[Candidate] = []
    for order in _candidate_orders(tx):
        cand = score_candidate(order, tx)
        if cand.score >= MANUAL_THRESHOLD:
            candidates.append(cand)
    candidates.sort(key=lambda c: c.score, reverse=True)
    result['candidates'] = [
        {'order': c.order.order_number, 'score': c.score,
         'reasons': [r.to_dict() for r in c.reasons]}
        for c in candidates
    ]

    if not candidates:
        result['reason'] = 'no candidates ≥ MANUAL_THRESHOLD'
        return result

    # 3. Action.
    best = candidates[0]
    if dry_run:
        result['reason'] = 'dry-run'
        return result

    # A tie at the top — we must NOT auto-confirm: both orders are equally similar (same amount in
    # the 48 h window, similar full name), and picking 'the first in the list' would mark someone
    # else's order as paid. Such cases go to a human.
    tied = [c for c in candidates if c.score == best.score]
    if best.score >= AUTO_THRESHOLD and len(tied) > 1:
        log.warning('bank_matcher: tie of %d points between %s for tx=%s — sent to the manual queue',
                    best.score, [c.order.order_number for c in tied], tx.external_id)
        result['reason'] = f'ambiguous: {len(tied)} candidates with the same score={best.score}'
        for cand in tied[:5]:
            PaymentMatch.objects.update_or_create(
                order=cand.order, transaction=tx,
                defaults={
                    'confidence': cand.score,
                    'reasons': [r.to_dict() for r in cand.reasons],
                    'is_active': False,
                    'auto_confirmed': False,
                },
            )
            if not cand.order.payment_notification:
                cand.order.payment_notification = True
                cand.order.save(update_fields=['payment_notification', 'updated_at'])
            result['manual_queue_orders'].append(cand.order.order_number)
        tx.status = 'manual_review'
        tx.save(update_fields=['status', 'updated_at'])
        return result

    if best.score >= AUTO_THRESHOLD:
        # Auto-confirm the best candidate. Every factor with weight 100 already checks the exact
        # amount inside, so no extra safeguard is needed.
        match, _created = PaymentMatch.objects.update_or_create(
            order=best.order, transaction=tx,
            defaults={
                'confidence': best.score,
                'reasons': [r.to_dict() for r in best.reasons],
                'is_active': True,
                'auto_confirmed': True,
                'confirmed_at': timezone.now(),
            },
        )
        # Update the order.
        order = best.order
        order.refresh_from_db()
        if order.payment_status != 'paid':
            order.payment_status = 'paid'
            order.payment_notification = True
            order.save(update_fields=['payment_status', 'payment_notification', 'updated_at'])
        tx.status = 'matched'
        tx.save(update_fields=['status', 'updated_at'])
        result['auto_confirmed_order'] = best.order.order_number
        log.info('bank_matcher: auto-confirmed order=%s tx=%s score=%d',
                 best.order.order_number, tx.external_id, best.score)
        return result

    # 4. Otherwise — put into the manual review queue.
    for cand in candidates[:5]:  # max 5 candidates per transaction
        PaymentMatch.objects.update_or_create(
            order=cand.order, transaction=tx,
            defaults={
                'confidence': cand.score,
                'reasons': [r.to_dict() for r in cand.reasons],
                'is_active': False,  # becomes active after manual confirmation
                'auto_confirmed': False,
            },
        )
        # Signal the manager via the "Notifications → Payments" page.
        if not cand.order.payment_notification:
            cand.order.payment_notification = True
            cand.order.save(update_fields=['payment_notification', 'updated_at'])
        result['manual_queue_orders'].append(cand.order.order_number)
    tx.status = 'manual_review'
    tx.save(update_fields=['status', 'updated_at'])
    return result


def match_unmatched(*, limit: int = 200, dry_run: bool = False) -> list[dict]:
    """Pass over all unmatched transactions. Called after poll_monobank."""
    txs = (
        BankTransaction.objects
        .filter(status='unmatched', amount__gt=0)
        .order_by('-transacted_at')[:limit]
    )
    return [match_transaction(tx, dry_run=dry_run) for tx in txs]


def confirm_match_manually(match: PaymentMatch, user) -> None:
    """The manager manually confirmed the match in the admin."""
    with transaction.atomic():
        match.is_active = True
        match.confirmed_by = user
        match.confirmed_at = timezone.now()
        match.save(update_fields=['is_active', 'confirmed_by', 'confirmed_at'])
        # Deactivate the other candidates of the same transaction.
        PaymentMatch.objects.filter(transaction=match.transaction).exclude(pk=match.pk).update(
            is_active=False,
        )
        # Mark the transaction as matched.
        tx = match.transaction
        if tx.status != 'matched':
            tx.status = 'matched'
            tx.save(update_fields=['status', 'updated_at'])
        # Update the order.
        order = match.order
        order.refresh_from_db()
        if order.payment_status != 'paid':
            order.payment_status = 'paid'
            order.payment_notification = True
            order.save(update_fields=['payment_status', 'payment_notification', 'updated_at'])
