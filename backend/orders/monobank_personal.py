"""Monobank Personal API client — statements for personal accounts and sole-proprietor (FOP) accounts.

Documentation: https://api.monobank.ua/docs/

This is a **different** API than Acquiring (`monobank.py`). Personal API:
  - GET /personal/client-info             — list of accounts
  - GET /personal/statement/{acc}/{from}[/{to}]  — statement (rate-limit 1 req/60s)
  - POST /personal/webhook                — set the URL for pushes

Webhook payload (StatementItem):
  {
    "type": "StatementItem",
    "data": {
      "account": "<accountId>",
      "statementItem": { id, time, description, comment, amount, ... }
    }
  }

Rate limits:
  - /client-info: ~1 req / 60s
  - /statement:   1 req / 60s (per account+range)
  - /webhook:     ~1 req / 60s
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone as dt_timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Iterable

import requests
from django.conf import settings
from django.core.cache import cache

log = logging.getLogger(__name__)

API_BASE = 'https://api.monobank.ua'
DEFAULT_TIMEOUT = 20
# Mono limits /statement: 1 request/60s per account. We keep a local guard
# to avoid a 429 and a blocked token.
RATE_GUARD_KEY = 'mono_personal:last_call:{ep}:{acc}'
RATE_GUARD_TTL = 70  # sec, with a margin
# Maximum statement range per request is 31 days and 500 transactions.
MAX_STATEMENT_RANGE_DAYS = 31
MAX_STATEMENT_ITEMS = 500


class MonoPersonalError(Exception):
    """General Mono Personal API integration error."""


class MonoPersonalNotConfigured(MonoPersonalError):
    """MONOBANK_PERSONAL_TOKEN is not configured."""


class MonoPersonalRateLimited(MonoPersonalError):
    """429 from Mono — must wait."""


def _token() -> str:
    token = getattr(settings, 'MONOBANK_PERSONAL_TOKEN', '') or ''
    if not token:
        raise MonoPersonalNotConfigured('MONOBANK_PERSONAL_TOKEN is not configured in .env')
    return token


def _headers() -> dict:
    return {'X-Token': _token()}


def _rate_guard(endpoint: str, account: str = '') -> None:
    """Local rate limiter. If we called recently — wait up to 60s."""
    key = RATE_GUARD_KEY.format(ep=endpoint, acc=account or '_')
    last = cache.get(key)
    if last:
        elapsed = time.time() - last
        if elapsed < 60:
            wait = 60 - elapsed + 1
            log.info('mono_personal: local rate-guard, sleeping %.1fs (ep=%s acc=%s)', wait, endpoint, account[:8])
            time.sleep(min(wait, 70))
    cache.set(key, time.time(), RATE_GUARD_TTL)


def _request(method: str, path: str, *, params: dict | None = None, json_body: dict | None = None,
             rate_guard_endpoint: str | None = None, rate_guard_account: str = '') -> Any:
    if rate_guard_endpoint:
        _rate_guard(rate_guard_endpoint, rate_guard_account)
    url = f'{API_BASE}{path}'
    try:
        resp = requests.request(method, url, headers=_headers(), params=params,
                                json=json_body, timeout=DEFAULT_TIMEOUT)
    except requests.RequestException as e:
        raise MonoPersonalError(f'request failed: {e}') from e

    if resp.status_code == 429:
        raise MonoPersonalRateLimited('429 Too Many Requests from Mono — wait 60s')
    if resp.status_code >= 400:
        try:
            err = resp.json()
        except ValueError:
            err = {'errorDescription': resp.text[:200]}
        raise MonoPersonalError(f'{method} {path} → {resp.status_code}: {err}')
    try:
        return resp.json()
    except ValueError:
        return resp.text


# ── Public API ──────────────────────────────────────────────────────────────

def get_client_info() -> dict:
    """Returns client info and the list of the client's accounts.

    Response structure:
      {
        "clientId": "...",
        "name": "...",
        "webHookUrl": "...",
        "permissions": "psfjm",
        "accounts": [ { id, currencyCode, balance, maskedPan, type, iban, ... }, ... ],
        "jars": [ ... ]
      }
    """
    return _request('GET', '/personal/client-info', rate_guard_endpoint='client-info')


def set_webhook(webhook_url: str) -> None:
    """Set the URL that Mono will send StatementItem to.
    Mono first POSTs an empty body to this URL as a check — it must return 200 OK.
    """
    _request('POST', '/personal/webhook', json_body={'webHookUrl': webhook_url},
             rate_guard_endpoint='webhook')


def get_statement(account_id: str, *, frm: datetime, to: datetime | None = None) -> list[dict]:
    """Statement for a date range.

    `account_id`: 0 = the black card (default UAH), or an id from client-info.
    `frm`/`to`: aware datetime; the difference must be ≤ 31 days.
    Returns a list of StatementItem, newest first.
    """
    if not account_id:
        raise MonoPersonalError('account_id is required')
    if frm.tzinfo is None:
        raise MonoPersonalError('frm must be tz-aware')
    if to and to.tzinfo is None:
        raise MonoPersonalError('to must be tz-aware')

    frm_ts = int(frm.astimezone(dt_timezone.utc).timestamp())
    if to:
        path = f'/personal/statement/{account_id}/{frm_ts}/{int(to.astimezone(dt_timezone.utc).timestamp())}'
    else:
        path = f'/personal/statement/{account_id}/{frm_ts}'

    data = _request('GET', path, rate_guard_endpoint='statement', rate_guard_account=account_id)
    if not isinstance(data, list):
        raise MonoPersonalError(f'unexpected statement response: {type(data).__name__}')
    return data


def get_statement_paged(account_id: str, *, frm: datetime, to: datetime,
                        max_pages: int = 4) -> tuple[list[dict], bool]:
    """Statement with backward paging. Returns (items, complete).

    Mono returns at most 500 operations per range, NEWEST first, and silently drops the oldest
    "tail". Simply calling get_statement() on a wide window (e.g. after a long poller outage) means
    silently losing the oldest transactions — and the cursor then moves to `now`, so they would
    never come back. Therefore: if a page comes back exactly at the limit, we repeat the request
    with `to` = the time of the oldest operation received, narrowing the window backwards.

    complete=False means that even after max_pages we did not page back to `frm` — a gap [frm,
    oldest received) remains, and the caller must complain loudly. Each page costs ~60s of
    rate-guard, so we keep max_pages small: 4 pages = 2000 operations, with margin for any real
    outage.
    """
    collected: list[dict] = []
    seen_ids: set[str] = set()
    page_to = to
    for _ in range(max_pages):
        items = get_statement(account_id, frm=frm, to=page_to)
        for it in items:
            key = statement_item_external_id(it, account_id)
            if key not in seen_ids:
                seen_ids.add(key)
                collected.append(it)
        if len(items) < MAX_STATEMENT_ITEMS:
            return collected, True
        # Hit the limit — page backwards from the oldest operation received.
        oldest_ts = min(int(it.get('time', 0)) for it in items)
        next_to = datetime.fromtimestamp(oldest_ts - 1, tz=dt_timezone.utc)
        if next_to <= frm:
            return collected, True
        page_to = next_to
    return collected, False


# ── Helpers / parsing ───────────────────────────────────────────────────────

def kopecks_to_decimal(kopecks: int | None) -> Decimal:
    """Mono keeps amounts in kopiykas (int). 4200 → Decimal('42.00')."""
    if kopecks is None:
        return Decimal('0.00')
    return (Decimal(int(kopecks)) / Decimal(100)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)


def parse_counterparty_from_description(description: str) -> str:
    """Extract the counterparty's full name from the description.

    Mono format examples:
      "Від: Іваненко Іван Іванович"            → "Іваненко Іван Іванович"   ("Від:" = "From:")
      "Іваненко І.І."                          → "Іваненко І.І."
      "Зарахування з картки 5375********1234"  → ""  (no full name, only the card)
      "Купівля у …"                            → ""  (outgoing — we don't care)
    """
    if not description:
        return ''
    s = description.strip()
    # The most common format for card-to-card / IBAN credits:
    for prefix in ('Від:', 'Від ', 'From:', 'From '):
        if s.startswith(prefix):
            return s[len(prefix):].strip()
    # If "Зарахування" (Credit) — there is no full name in the description.
    lowered = s.lower()
    # Language data: Mono's Ukrainian wording — "зарахування" = credit, "переказ" = transfer, "картки" = card.
    if 'зарахування' in lowered or 'переказ' in lowered and 'картки' in lowered:
        return ''
    # If it is just a full name with no prefix and no keywords — return it as is.
    # Heuristic: ≤4 words, all starting with a capital letter.
    parts = s.split()
    if 1 <= len(parts) <= 4 and all(p[:1].isupper() for p in parts if p):
        return s
    return ''


def statement_item_external_id(item: dict, account_id: str = '') -> str:
    """Unique transaction id for dedup. Mono: the `id` field (base64).

    We prefix it with the account, because the same transaction can appear on two accounts (internal
    transfers), and we want to process each credit separately.
    """
    item_id = item.get('id') or ''
    if not item_id:
        # Fallback — a time+amount+balance hybrid, unlikely to be needed.
        item_id = f'{item.get("time")}_{item.get("amount")}_{item.get("balance")}'
    return f'{account_id}:{item_id}' if account_id else item_id


def iter_statement_items(items: Iterable[dict], account_id: str) -> Iterable[dict]:
    """Normalizes a StatementItem into a dict with our fields.

    Returns dicts with fields ready for BankTransaction:
      external_id, transacted_at, amount, currency_code, description, comment,
      counterparty_name, raw_data.
    """
    for it in items:
        ts = it.get('time')
        if ts is None:
            continue
        transacted_at = datetime.fromtimestamp(int(ts), tz=dt_timezone.utc)
        description = (it.get('description') or '').strip()
        yield {
            'external_id': statement_item_external_id(it, account_id),
            'transacted_at': transacted_at,
            'amount': kopecks_to_decimal(it.get('amount')),
            'currency_code': int(it.get('currencyCode') or 980),
            'description': description[:500],
            'comment': (it.get('comment') or '').strip()[:500],
            'counterparty_name': parse_counterparty_from_description(description)[:200],
            'raw_data': it,
        }
