"""Monobank Acquiring API client + ECDSA signature verification of the webhook.

Documentation: https://monobank.ua/api-docs/acquiring
Minimal flow:
  1. POST /api/merchant/invoice/create → {invoiceId, pageUrl}
  2. Redirect the customer to pageUrl
  3. Monobank sends a webhook to webHookUrl with the X-Sign header (ECDSA)
  4. Verify the signature, update the order's payment_status
  5. Fallback: GET /api/merchant/invoice/status?invoiceId=... for reconciliation
"""

from __future__ import annotations

import base64
import logging
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

import requests
from django.conf import settings
from django.core.cache import cache
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec

log = logging.getLogger(__name__)

PUBKEY_CACHE_KEY = 'monobank:pubkey:v1'
PUBKEY_CACHE_TTL = 60 * 60 * 24  # 24 hours

DEFAULT_TIMEOUT = 15  # sec

# Statuses that mark a successful payment (treat as paid)
SUCCESS_STATUSES = {'success'}
# Statuses that mean failure/rollback
FAILURE_STATUSES = {'failure', 'reversed', 'expired'}


class MonobankError(Exception):
    """General Monobank integration error."""


class MonobankNotConfigured(MonobankError):
    """MONOBANK_TOKEN is not configured."""


def _headers() -> dict:
    if not settings.MONOBANK_TOKEN:
        raise MonobankNotConfigured('MONOBANK_TOKEN is not configured')
    return {'X-Token': settings.MONOBANK_TOKEN, 'Content-Type': 'application/json'}


def _money_to_kopecks(amount) -> int:
    """Decimal/number → kopecks. 42.0 UAH → 4200."""
    if amount is None:
        return 0
    d = Decimal(str(amount)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
    return int((d * 100).to_integral_value())


def kopecks_to_money(kopecks: int) -> Decimal:
    return (Decimal(int(kopecks)) / Decimal(100)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)


def create_invoice(order, *, redirect_url: str, webhook_url: str) -> dict:
    """Creates a Monobank invoice for a specific order.
    Returns a dict with invoiceId and pageUrl."""
    url = settings.MONOBANK_API_BASE.rstrip('/') + '/api/merchant/invoice/create'
    payload: dict[str, Any] = {
        'amount': _money_to_kopecks(order.total_price),
        'ccy': 980,  # UAH
        'merchantPaymInfo': {
            'reference': str(order.order_number),
            'destination': f'Order #{order.order_number}',
        },
        'redirectUrl': redirect_url,
        'webHookUrl': webhook_url,
        # validity is 24h by default, not set explicitly
    }
    try:
        resp = requests.post(url, json=payload, headers=_headers(), timeout=DEFAULT_TIMEOUT)
    except requests.RequestException as e:
        raise MonobankError(f'request failed: {e}') from e

    if resp.status_code != 200:
        log.error('monobank create_invoice failed: %s %s', resp.status_code, resp.text[:500])
        try:
            data = resp.json()
        except ValueError:
            data = {'errText': resp.text[:200]}
        raise MonobankError(f"create_invoice {resp.status_code}: {data.get('errText') or data}")

    data = resp.json()
    if 'invoiceId' not in data or 'pageUrl' not in data:
        raise MonobankError(f'unexpected response: {data}')
    return data


def get_invoice_status(invoice_id: str) -> dict:
    """Polls the invoice status (for reconciliation if the webhook didn't arrive)."""
    if not invoice_id:
        raise MonobankError('invoice_id is required')
    url = settings.MONOBANK_API_BASE.rstrip('/') + '/api/merchant/invoice/status'
    try:
        resp = requests.get(url, params={'invoiceId': invoice_id}, headers=_headers(), timeout=DEFAULT_TIMEOUT)
    except requests.RequestException as e:
        raise MonobankError(f'request failed: {e}') from e
    if resp.status_code != 200:
        raise MonobankError(f'status {resp.status_code}: {resp.text[:200]}')
    return resp.json()


def cancel_invoice(invoice_id: str, amount=None) -> dict:
    """Cancels a successful payment (full or partial refund)."""
    url = settings.MONOBANK_API_BASE.rstrip('/') + '/api/merchant/invoice/cancel'
    payload: dict[str, Any] = {'invoiceId': invoice_id}
    if amount is not None:
        payload['amount'] = _money_to_kopecks(amount)
    try:
        resp = requests.post(url, json=payload, headers=_headers(), timeout=DEFAULT_TIMEOUT)
    except requests.RequestException as e:
        raise MonobankError(f'request failed: {e}') from e
    if resp.status_code != 200:
        raise MonobankError(f'cancel {resp.status_code}: {resp.text[:200]}')
    return resp.json()


def remove_invoice(invoice_id: str) -> dict:
    """Invalidates an UNPAID invoice (POST /api/merchant/invoice/remove).
    Unlike cancel (refund of a paid one), it deactivates a not-yet-paid pageUrl,
    so the customer can't pay the old amount after the order contents/amount changed."""
    url = settings.MONOBANK_API_BASE.rstrip('/') + '/api/merchant/invoice/remove'
    try:
        resp = requests.post(url, json={'invoiceId': invoice_id}, headers=_headers(), timeout=DEFAULT_TIMEOUT)
    except requests.RequestException as e:
        raise MonobankError(f'request failed: {e}') from e
    if resp.status_code != 200:
        raise MonobankError(f'remove {resp.status_code}: {resp.text[:200]}')
    return resp.json() if resp.content else {}


# ── Webhook signature verification ──────────────────────────────────────────

def _fetch_pubkey_pem() -> bytes:
    """Fetches the public key PEM from Monobank. Cached in cache."""
    url = settings.MONOBANK_API_BASE.rstrip('/') + '/api/merchant/pubkey'
    resp = requests.get(url, headers=_headers(), timeout=DEFAULT_TIMEOUT)
    if resp.status_code != 200:
        raise MonobankError(f'pubkey fetch failed: {resp.status_code}')
    data = resp.json()
    key_b64 = data.get('key')
    if not key_b64:
        raise MonobankError('pubkey response has no "key"')
    pem = base64.b64decode(key_b64)
    return pem


def get_pubkey_pem(force_refresh: bool = False) -> bytes:
    """Returns the public key PEM bytes. Cached. force_refresh bypasses the cache."""
    if not force_refresh:
        cached = cache.get(PUBKEY_CACHE_KEY)
        if cached:
            return cached
    pem = _fetch_pubkey_pem()
    cache.set(PUBKEY_CACHE_KEY, pem, PUBKEY_CACHE_TTL)
    return pem


def _load_public_key(pem: bytes) -> ec.EllipticCurvePublicKey:
    key = serialization.load_pem_public_key(pem)
    if not isinstance(key, ec.EllipticCurvePublicKey):
        raise MonobankError('pubkey is not ECDSA')
    return key


def verify_signature(body: bytes, x_sign_b64: str) -> bool:
    """Verifies the ECDSA(SHA-256) signature of the webhook body.
    If verification fails with the current key, we retry with a refreshed one (in case of key
    rotation)."""
    if not body or not x_sign_b64:
        return False
    try:
        signature = base64.b64decode(x_sign_b64)
    except Exception:
        return False

    for force_refresh in (False, True):
        try:
            pem = get_pubkey_pem(force_refresh=force_refresh)
            pubkey = _load_public_key(pem)
            pubkey.verify(signature, body, ec.ECDSA(hashes.SHA256()))
            return True
        except InvalidSignature:
            if force_refresh:
                return False
            # retry with a fresh key
            continue
        except MonobankError:
            return False
        except Exception as e:
            log.warning('monobank verify_signature error: %s', e)
            return False
    return False
