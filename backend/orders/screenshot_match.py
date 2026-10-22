"""Anti-fraud and auto-confirmation via a payment receipt screenshot.

Logic:
  1. The customer uploads payment_screenshot (a receipt from a banking app).
  2. Gemini Vision does OCR: extracts the amount, date/time, sender name,
     last digits of the recipient's card/IBAN.
  3. We look for a BankTransaction in the statement with the same amount within a ±2 hour
     window around the time on the screenshot.
  4. If exactly one transaction is found → we create a PaymentMatch
     (reason='screenshot_match', confidence=100) and auto-confirm the
     order as `paid`.
  5. If several or none are found — manual review:
     - 0 transactions + the amount on the screenshot exists → possible fraud (photoshop) or
       the transaction hasn't arrived yet; we set a warning on the order.
     - several transactions → the manager chooses manually.

Usage:
  from orders.screenshot_match import match_screenshot
  result = match_screenshot(order)

  # or batch mode (cron):
  python manage.py process_payment_screenshots
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Optional

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from .models import BankTransaction, Order, PaymentMatch

log = logging.getLogger(__name__)


SCREENSHOT_MATCH_WINDOW = timedelta(hours=2)
SCREENSHOT_AUTO_CONFIDENCE = 100

# Prompt for Gemini Vision. We ask for JSON with a fixed schema —
# google-genai supports structured output via response_mime_type='application/json'.
OCR_PROMPT = """Carefully analyze the screenshot of a bank receipt / transfer and extract the key data.

Return ONLY JSON, on ONE line (no code blocks, no explanations), with this schema:
{
  "amount": number or null,           // transfer amount in hryvnias (without kopecks or with .XX)
  "currency": "UAH" | "USD" | "EUR" | null,
  "date": "YYYY-MM-DD" or null,      // operation date on the receipt
  "time": "HH:MM" or null,           // operation time (if present)
  "sender_name": "sender full name" or null,  // if present on the receipt
  "recipient_name": "recipient full name" or null,
  "recipient_card_last4": "1234" or null,    // last 4 digits of the recipient card/account
  "recipient_iban_tail": "1234" or null,     // last 4-6 characters of the IBAN, if it is a transfer by bank details
  "comment": "payment purpose" or null,
  "is_payment_receipt": true | false,         // is this really a payment receipt? (if not, everything else may be null)
  "confidence": "high" | "medium" | "low"     // your confidence in the reading
}

If something is not visible or is ambiguous, return null for that field. Do not make anything up.
"""


class ScreenshotOCRError(Exception):
    """OCR could not extract any useful data."""


@dataclass
class OCRResult:
    is_payment_receipt: bool
    amount: Optional[Decimal]
    currency: Optional[str]
    date: Optional[datetime]  # combined date+time, local Kyiv tz
    sender_name: Optional[str]
    recipient_name: Optional[str]
    recipient_card_last4: Optional[str]
    recipient_iban_tail: Optional[str]
    comment: Optional[str]
    confidence: str
    raw: dict


def _parse_amount(v) -> Optional[Decimal]:
    if v is None:
        return None
    try:
        # May arrive as a number, or a string with spaces/commas
        s = str(v).replace(' ', '').replace(',', '.').replace('грн', '').strip()  # 'грн' = UAH as printed on Ukrainian receipts (language data)
        return Decimal(s)
    except (InvalidOperation, ValueError):
        return None


def _parse_datetime(date_str: Optional[str], time_str: Optional[str]) -> Optional[datetime]:
    if not date_str:
        return None
    try:
        d = datetime.strptime(date_str, '%Y-%m-%d').date()
    except ValueError:
        return None
    if time_str:
        try:
            t = datetime.strptime(time_str, '%H:%M').time()
        except ValueError:
            t = datetime.min.time()
    else:
        t = datetime.min.time()
    naive = datetime.combine(d, t)
    # The Mono and Privat APIs store UTC; we convert to aware in the Kyiv tz,
    # so that searching by transacted_at works correctly.
    return timezone.make_aware(naive, timezone.get_current_timezone())


def ocr_screenshot(image_path: str) -> OCRResult:
    """Vision OCR of a payment screenshot via the Claude subscription (core.llm). → OCRResult."""
    with open(image_path, 'rb') as f:
        image_bytes = f.read()
    # We determine the mime type by extension (Mono, Privat — ordinary JPEG/PNG).
    # Claude vision accepts jpeg/png/webp/gif (heic — no; rare in screenshots).
    ext = image_path.rsplit('.', 1)[-1].lower()
    mime = {'jpg': 'image/jpeg', 'jpeg': 'image/jpeg', 'png': 'image/png',
            'webp': 'image/webp', 'gif': 'image/gif'}.get(ext, 'image/jpeg')

    try:
        from core import llm
        data = llm.image_to_json(image_bytes, prompt=OCR_PROMPT, media_type=mime)
    except Exception as e:
        raise ScreenshotOCRError(f'LLM OCR failed: {e}') from e

    if not isinstance(data, dict):
        raise ScreenshotOCRError('LLM returned non-dict')

    return OCRResult(
        is_payment_receipt=bool(data.get('is_payment_receipt')),
        amount=_parse_amount(data.get('amount')),
        currency=data.get('currency'),
        date=_parse_datetime(data.get('date'), data.get('time')),
        sender_name=data.get('sender_name'),
        recipient_name=data.get('recipient_name'),
        recipient_card_last4=data.get('recipient_card_last4'),
        recipient_iban_tail=data.get('recipient_iban_tail'),
        comment=data.get('comment'),
        confidence=data.get('confidence') or 'low',
        raw=data,
    )


# ── Searching for a transaction by the OCR result ─────────────────────────────

def find_transactions_for_ocr(ocr: OCRResult) -> list[BankTransaction]:
    """Looks for candidates in BankTransaction by amount + time from the screenshot.

    Returns all candidates (may be 0, 1 or several). The calling code decides
    what to do with them (auto vs manual).
    """
    if not ocr.is_payment_receipt or ocr.amount is None:
        return []

    qs = BankTransaction.objects.filter(amount=ocr.amount, status__in=['unmatched', 'manual_review'])

    # If there is a date — we narrow the window. If only a date without time, the window is wide
    # (the whole day),
    # but limited by SCREENSHOT_MATCH_WINDOW.
    if ocr.date:
        qs = qs.filter(
            transacted_at__gte=ocr.date - SCREENSHOT_MATCH_WINDOW,
            transacted_at__lte=ocr.date + SCREENSHOT_MATCH_WINDOW,
        )
    else:
        # No date — we limit it to the last 24 hours, otherwise it's noise.
        cutoff = timezone.now() - timedelta(hours=24)
        qs = qs.filter(transacted_at__gte=cutoff)

    return list(qs.order_by('-transacted_at'))


# ── High-level function for a single order ──────────────────────────────────

@dataclass
class ScreenshotMatchResult:
    order_number: str
    ocr: Optional[OCRResult]
    candidates_count: int
    auto_confirmed: bool = False
    matched_transaction_id: Optional[str] = None
    reason: str = ''


@transaction.atomic
def match_screenshot(order: Order) -> ScreenshotMatchResult:
    """Runs OCR on the order's screenshot and tries to find a transaction for it.

    Returns a report. Does NOT modify order if the auto-confirm conditions are not met.
    The call is idempotent — if the order is already paid, we do nothing.
    """
    res = ScreenshotMatchResult(order_number=order.order_number, ocr=None,
                                candidates_count=0)

    if order.payment_status == 'paid':
        res.reason = 'order already paid'
        return res

    if not order.payment_screenshot:
        res.reason = 'no screenshot'
        return res

    try:
        ocr = ocr_screenshot(order.payment_screenshot.path)
    except ScreenshotOCRError as e:
        log.warning('screenshot_match: OCR failed for order=%s: %s', order.order_number, e)
        res.reason = f'ocr_error: {e}'
        return res
    res.ocr = ocr

    if not ocr.is_payment_receipt:
        res.reason = 'not a payment receipt according to OCR'
        return res
    if ocr.amount is None:
        res.reason = 'OCR could not extract amount'
        return res

    # Optional check: the amount on the screenshot = the order's total_price.
    # If it differs, that is also a signal (maybe someone else's receipt).
    if order.total_price is not None and Decimal(str(order.total_price)) != ocr.amount:
        res.reason = (f'amount on screenshot ({ocr.amount}) ≠ order total ({order.total_price}); '
                      f'manual review required')
        # We could create a manual candidate, but without a transaction in the DB it's of little
        # use.
        return res

    candidates = find_transactions_for_ocr(ocr)
    res.candidates_count = len(candidates)

    if not candidates:
        res.reason = ('NO matching transaction found in bank statement — '
                      'possible fraud or transaction not yet polled')
        return res

    if len(candidates) > 1:
        # Several transactions with the same amount in the window — we create manual candidates.
        for tx in candidates[:5]:
            PaymentMatch.objects.update_or_create(
                order=order, transaction=tx,
                defaults={
                    'confidence': 70,
                    'reasons': [{
                        'type': 'screenshot_amount_match_ambiguous',
                        'weight': 70,
                        'detail': f'OCR amount {ocr.amount} matches, but there are {len(candidates)} candidates',
                    }],
                    'is_active': False,
                    'auto_confirmed': False,
                },
            )
        res.reason = f'multiple candidates ({len(candidates)}) — manual review'
        return res

    # Exactly one transaction — auto-confirm.
    tx = candidates[0]
    match, _ = PaymentMatch.objects.update_or_create(
        order=order, transaction=tx,
        defaults={
            'confidence': SCREENSHOT_AUTO_CONFIDENCE,
            'reasons': [{
                'type': 'screenshot_match',
                'weight': SCREENSHOT_AUTO_CONFIDENCE,
                'detail': (f'OCR(amount={ocr.amount}, date={ocr.date}, '
                           f'sender={ocr.sender_name}) ↔ tx {tx.external_id}'),
            }],
            'is_active': True,
            'auto_confirmed': True,
            'confirmed_at': timezone.now(),
        },
    )

    if order.payment_status != 'paid':
        order.payment_status = 'paid'
        order.payment_notification = True
        order.save(update_fields=['payment_status', 'payment_notification', 'updated_at'])

    if tx.status != 'matched':
        tx.status = 'matched'
        tx.save(update_fields=['status', 'updated_at'])

    res.auto_confirmed = True
    res.matched_transaction_id = tx.external_id
    res.reason = 'auto-confirmed via screenshot match'
    log.info('screenshot_match: AUTO order=%s tx=%s amount=%s',
             order.order_number, tx.external_id, ocr.amount)
    return res
