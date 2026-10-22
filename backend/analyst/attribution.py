"""Collects data for the Analyst that cannot be reconstructed after the fact: where an order came from
and what people search for on the site. Both functions are best-effort: a failure here never breaks
checkout or search."""
from __future__ import annotations

import logging
from datetime import timedelta
from urllib.parse import urlsplit

from django.db.models import F
from django.utils import timezone

log = logging.getLogger('analyst')

_UTM_KEYS = {'utm_source': 100, 'utm_medium': 100, 'utm_campaign': 150, 'utm_content': 150}
SEARCH_MAX_NEW_QUERIES_PER_DAY = 5000   # protects the table from bots sending random queries
SEARCH_KEEP_DAYS = 400


def _clean(value, limit: int) -> str:
    return str(value).strip()[:limit] if isinstance(value, (str, int)) else ''


def _referrer(value) -> str:
    """Scheme + host + path only: the referrer's query string may contain third-party tokens."""
    if not isinstance(value, str) or not value.strip():
        return ''
    try:
        parts = urlsplit(value.strip())
    except ValueError:
        return ''
    if not parts.netloc:
        return ''
    return f'{parts.scheme}://{parts.netloc}{parts.path}'[:300]


def save_order_attribution(order_id: int, raw) -> None:
    """Saves the utm tags and the referrer that the frontend sent along with the order."""
    if not isinstance(raw, dict):
        return
    try:
        from .models import OrderAttribution
        fields = {key: _clean(raw.get(key), limit) for key, limit in _UTM_KEYS.items()}
        referrer = _referrer(raw.get('referrer'))
        landing = _clean(raw.get('landing_path'), 300).split('?')[0].split('#')[0]
        if not any(fields.values()) and not referrer:
            return
        OrderAttribution.objects.update_or_create(
            order_id=order_id,
            defaults={**fields, 'referrer': referrer, 'landing_path': landing},
        )
    except Exception:
        log.warning('analyst: could not save the order source %s', order_id, exc_info=True)


def log_search(query: str, results: int) -> None:
    """Counts a search query for today; `results == 0` adds to zero_count."""
    try:
        from .models import SearchLog
        q = ' '.join(str(query).lower().split())[:120]
        if len(q) < 2:
            return
        today = timezone.localdate()
        zero = 1 if results <= 0 else 0
        updated = SearchLog.objects.filter(date=today, query=q).update(
            count=F('count') + 1, zero_count=F('zero_count') + zero)
        if updated:
            return
        if SearchLog.objects.filter(date=today).count() >= SEARCH_MAX_NEW_QUERIES_PER_DAY:
            return
        SearchLog.objects.create(date=today, query=q, count=1, zero_count=zero)
    except Exception:
        log.warning('analyst: could not record the search query', exc_info=True)


def prune_old_data(today) -> None:
    """Old search logs are not needed (we look at seasonality via orders); fix plans that were
    never applied live for only one day."""
    from .models import ActionRun, SearchLog
    SearchLog.objects.filter(date__lt=today - timedelta(days=SEARCH_KEEP_DAYS)).delete()
    ActionRun.objects.filter(status=ActionRun.PLANNED,
                             created_at__lt=timezone.now() - timedelta(days=1)).delete()
    from . import errorlog
    errorlog.purge()                      # errors without repeats older than 60 days are not needed (and shouldn't lie around forever)
