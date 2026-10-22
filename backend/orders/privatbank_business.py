"""Privat24 for business client — account statements for sole proprietors (FOP) / legal entities.

Documentation: https://api.privatbank.ua/  (section "API юр. осіб" — API for legal entities)

Privat API specifics (unlike Mono Personal):
  - NO webhooks. Polling only.
  - Base URL: https://acp.privatbank.ua/api/
  - Authorization: HTTP header `token: <token>` (no prefix).
    Some API versions also accept `id: <client_id>`.
  - Rate limit: ~1 request/60s per endpoint.
  - Date format in parameters: DD-MM-YYYY.
  - Transaction amount — a string with a dot separator ("760.00"), in UAH (NOT kopiykas).
  - Dedup: by the transaction's `ID` field (a string token).

Key endpoints:
  GET /statements/balance/interim?acc=<IBAN>          — current balance
  GET /statements/balance/final?acc=<IBAN>            — balance at the end of the previous day
  GET /statements/transactions/interim?acc=<IBAN>     — statement for the current day
  GET /statements/transactions?acc=<IBAN>&startDate=<dd-mm-yyyy>&endDate=<dd-mm-yyyy>
                                                       — statement for a period
                                                       (period limited to ~31 days)
  GET /statements/transactions?...&followId=<id>      — pagination (if exist_next_page)

Transaction structure (key fields):
  ID                          unique transaction identifier
  TRANTYPE                    "C" = credit (incoming), "D" = debit (outgoing)
  SUM                         amount, a string with 2 decimal places ("760.00")
  CCY                         "UAH" / "USD" / "EUR"
  DATE_TIME_DAT_OD_TIM_P      date+time "06.05.2026 14:30:21"
  AUT_CNTR_NAM                full name or name of the counterparty
  AUT_CNTR_ACC                IBAN/account of the counterparty
  AUT_CNTR_CRF                tax ID / EDRPOU of the counterparty
  OSND                        payment purpose (analogue of Mono `comment`)
  AUT_MY_ACC                  our IBAN

TODO(privat-live): confirm the field names and date format on a real API response.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable

import requests
from django.conf import settings
from django.core.cache import cache

log = logging.getLogger(__name__)

API_BASE = 'https://acp.privatbank.ua/api'
DEFAULT_TIMEOUT = 25
RATE_GUARD_KEY = 'privat_business:last_call:{ep}:{acc}'
RATE_GUARD_TTL = 70

# Privat limits the date range of a single query (usually 31 days).
MAX_STATEMENT_RANGE_DAYS = 31
# Credit transaction type (this is the one we care about).
TRANTYPE_CREDIT = 'C'
TRANTYPE_DEBIT = 'D'


class PrivatBusinessError(Exception):
    """General error of the Privat Business API integration."""


class PrivatBusinessNotConfigured(PrivatBusinessError):
    """PRIVAT_BUSINESS_TOKEN is not configured."""


class PrivatBusinessRateLimited(PrivatBusinessError):
    """429 from Privat — we must wait."""


def _token() -> str:
    token = getattr(settings, 'PRIVAT_BUSINESS_TOKEN', '') or ''
    if not token:
        raise PrivatBusinessNotConfigured('PRIVAT_BUSINESS_TOKEN is not configured in .env')
    return token


def _client_id() -> str:
    """Optional clientId (sometimes needed as a second header). None if not set."""
    return getattr(settings, 'PRIVAT_BUSINESS_CLIENT_ID', '') or ''


def _headers() -> dict:
    h = {'token': _token(), 'Content-Type': 'application/json;charset=utf8'}
    cid = _client_id()
    if cid:
        h['id'] = cid
    return h


def _rate_guard(endpoint: str, account: str = '') -> None:
    key = RATE_GUARD_KEY.format(ep=endpoint, acc=account or '_')
    last = cache.get(key)
    if last:
        elapsed = time.time() - last
        if elapsed < 60:
            wait = 60 - elapsed + 1
            log.info('privat: local rate-guard, sleeping %.1fs (ep=%s)', wait, endpoint)
            time.sleep(min(wait, 70))
    cache.set(key, time.time(), RATE_GUARD_TTL)


def _request(path: str, *, params: dict | None = None,
             rate_guard_endpoint: str | None = None,
             rate_guard_account: str = '') -> Any:
    if rate_guard_endpoint:
        _rate_guard(rate_guard_endpoint, rate_guard_account)
    url = f'{API_BASE}{path}'
    try:
        resp = requests.get(url, headers=_headers(), params=params, timeout=DEFAULT_TIMEOUT)
    except requests.RequestException as e:
        raise PrivatBusinessError(f'request failed: {e}') from e

    if resp.status_code == 429:
        raise PrivatBusinessRateLimited('429 Too Many Requests from Privat')
    if resp.status_code >= 400:
        snippet = resp.text[:300] if resp.text else ''
        raise PrivatBusinessError(f'GET {path} → {resp.status_code}: {snippet}')

    try:
        data = resp.json()
    except ValueError as e:
        raise PrivatBusinessError(f'non-JSON response from {path}: {resp.text[:200]}') from e

    # Privat sometimes returns {"status":"SUCCESS",...} or {"status":"ERROR","message":"..."}
    if isinstance(data, dict) and data.get('status') and str(data['status']).upper() not in ('SUCCESS', 'OK'):
        raise PrivatBusinessError(f'{path} → status={data.get("status")}: {data.get("message") or data}')
    return data


# ── Public API ──────────────────────────────────────────────────────────────

def get_balance_interim(iban: str) -> list[dict]:
    """Current account balance."""
    if not iban:
        raise PrivatBusinessError('iban is required')
    data = _request('/statements/balance/interim', params={'acc': iban},
                    rate_guard_endpoint='balance', rate_guard_account=iban)
    return data.get('balances') or data.get('balance') or []


def get_statement_interim(iban: str) -> list[dict]:
    """Statement for the current day (updates quickly)."""
    if not iban:
        raise PrivatBusinessError('iban is required')
    return _statements_paged('/statements/transactions/interim', {'acc': iban}, iban)


def get_statement(iban: str, *, frm: datetime, to: datetime) -> list[dict]:
    """Statement for a date range. Period ≤ 31 days."""
    if not iban:
        raise PrivatBusinessError('iban is required')
    if (to - frm).days > MAX_STATEMENT_RANGE_DAYS:
        raise PrivatBusinessError(f'range > {MAX_STATEMENT_RANGE_DAYS} days')
    params = {
        'acc': iban,
        'startDate': frm.strftime('%d-%m-%Y'),
        'endDate': to.strftime('%d-%m-%Y'),
    }
    return _statements_paged('/statements/transactions', params, iban)


def _statements_paged(path: str, params: dict, iban: str) -> list[dict]:
    """Privat paginates the statement via `exist_next_page` + `next_page_id`."""
    out: list[dict] = []
    page_params = dict(params)
    while True:
        data = _request(path, params=page_params,
                        rate_guard_endpoint='statement', rate_guard_account=iban)
        items = data.get('transactions') or []
        out.extend(items)
        if not data.get('exist_next_page') and not data.get('existNextPage'):
            break
        next_id = data.get('next_page_id') or data.get('nextPageId')
        if not next_id:
            break
        page_params['followId'] = next_id
        if len(out) > 5000:
            log.warning('privat: paged statement very large (%d items), aborting', len(out))
            break
    return out


# ── Transaction parser ───────────────────────────────────────────────────────

def _parse_amount(value: Any, trantype: str) -> Decimal:
    """SUM arrives as a string "760.00". TRANTYPE='C' → +, 'D' → -."""
    try:
        amount = Decimal(str(value).replace(' ', '').replace(',', '.'))
    except (InvalidOperation, ValueError):
        return Decimal('0')
    if trantype == TRANTYPE_DEBIT:
        amount = -amount
    return amount


def _parse_datetime(date_str: str) -> datetime | None:
    """Expected format: '06.05.2026 14:30:21'. We bind it to the Kyiv tz."""
    if not date_str:
        return None
    from django.utils import timezone as dj_tz
    for fmt in ('%d.%m.%Y %H:%M:%S', '%d.%m.%Y %H:%M', '%d-%m-%Y %H:%M:%S'):
        try:
            naive = datetime.strptime(date_str, fmt)
            return dj_tz.make_aware(naive, dj_tz.get_current_timezone())
        except ValueError:
            continue
    log.warning('privat: cannot parse date "%s"', date_str)
    return None


def _ccy_to_iso(ccy: str | None) -> int:
    """Privat returns a three-letter code, our DB has the ISO number."""
    return {'UAH': 980, 'USD': 840, 'EUR': 978, 'PLN': 985}.get((ccy or '').upper(), 980)


def transaction_external_id(item: dict, iban: str = '') -> str:
    """Unique id for dedup. Privat: `ID` (a string token).

    We prefix it with the IBAN, because the same transaction can appear on two accounts.
    """
    item_id = item.get('ID') or item.get('id') or ''
    if not item_id:
        # Fallback — a hybrid of time + amount + ref.
        item_id = f'{item.get("DATE_TIME_DAT_OD_TIM_P")}_{item.get("SUM")}_{item.get("REF")}'
    return f'{iban}:{item_id}' if iban else str(item_id)


def iter_statement_items(items: Iterable[dict], iban: str) -> Iterable[dict]:
    """Normalizes a Privat StatementItem into a dict for BankTransaction.

    Goes field by field — it will need to be checked separately on real data (TODO(privat-live)).
    The field structure has been historically stable.
    """
    for it in items:
        ts = _parse_datetime(it.get('DATE_TIME_DAT_OD_TIM_P') or it.get('DATE') or '')
        if ts is None:
            continue
        trantype = (it.get('TRANTYPE') or '').upper()
        amount = _parse_amount(it.get('SUM'), trantype)

        counterparty = (it.get('AUT_CNTR_NAM') or '').strip()
        # In Privat the sender's name is right in AUT_CNTR_NAM, without a "From:" prefix.
        # We build a human-readable description for the admin.
        description_parts = []
        if counterparty:
            description_parts.append(f'From: {counterparty}' if trantype == TRANTYPE_CREDIT
                                     else f'To: {counterparty}')
        cntr_acc = (it.get('AUT_CNTR_ACC') or '').strip()
        if cntr_acc:
            tail = cntr_acc[-6:]
            description_parts.append(f'(IBAN …{tail})')
        description = ' '.join(description_parts)

        yield {
            'external_id': transaction_external_id(it, iban),
            'transacted_at': ts,
            'amount': amount,
            'currency_code': _ccy_to_iso(it.get('CCY')),
            'description': description[:500],
            'comment': (it.get('OSND') or '').strip()[:500],
            'counterparty_name': counterparty[:200],
            'raw_data': it,
        }
