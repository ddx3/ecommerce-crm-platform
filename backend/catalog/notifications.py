"""CRM notification counter: counting + realtime broadcast via Channels.

Shared source for:
  - the HTTP endpoint /admin/notifications/count/
  - the WebSocket consumer NotificationConsumer
  - model signals (orders/catalog) that send a broadcast when data changes
"""

import math
from datetime import timedelta

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from django.db.models import Q
from django.db.models.functions import Lower


GROUP_NOTIFICATIONS = 'admin_notifications'

# Event type for group_send → NotificationConsumer.notif_counts method
_EVENT_TYPE = 'notif.counts'


# Cart re-checkout window: a duplicate is placed right away — in the cases found
# the gap was 4–17 minutes. A week is taken with margin; it can't be wider, otherwise
# any subsequent purchase by a regular customer would hide a live request.
SUPERSEDED_WINDOW = timedelta(days=7)


def _client_keys(row: dict) -> list:
    """Keys by which we recognize the same person: account / email / phone.
    A placeholder phone (Horoshop legacy) is NOT a key — it is shared by
    dozens of people. See [[project_horoshop_placeholder_phone]]."""
    from orders.models import PLACEHOLDER_PHONES
    keys = []
    if row['user_id']:
        keys.append(('u', row['user_id']))
    if row['customer_email']:
        keys.append(('e', row['customer_email'].strip().lower()))
    phone = row['phone_normalized']
    if phone and phone not in PLACEHOLDER_PHONES:
        keys.append(('p', phone))
    return keys


def _superseded_by_paid_ids(qs) -> list:
    """IDs of unpaid queue orders where the customer RE-PLACED the cart and paid another
    order close in time (±SUPERSEDED_WINDOW). Such a request is dead: the payment details
    are no longer needed, the money came in via the neighboring order.

    Risk — family phone numbers (one phone shared by two people). Kept small
    thanks to the narrow window and the requirement that the neighboring order be specifically paid.

    Two queries for the whole calculation: candidates + paid ones by their keys. The keys
    go in three `__in` lists, NOT as an OR chain per candidate: get_notification_counts()
    hangs on post_save of every Order, so SQL size matters here more than the data itself —
    with 300 candidates the OR chain (900 conditions) cost 343 ms vs 25 ms for the lists.
    The exact "who matches whom" matching is still done in Python below."""
    from orders.models import Order
    fields = ('id', 'user_id', 'customer_email', 'phone_normalized', 'created_at')
    cands = list(qs.filter(payment_status='pending').values(*fields))
    if not cands:
        return []
    uids, emails, phones = set(), set(), set()
    for c in cands:
        for kind, val in _client_keys(c):
            (uids if kind == 'u' else emails if kind == 'e' else phones).add(val)
    if not (uids or emails or phones):
        return []
    cond = Q()
    if uids:
        cond |= Q(user_id__in=uids)
    if phones:
        cond |= Q(phone_normalized__in=phones)
    if emails:
        # Compare email in lowercase on both sides — at checkout it is
        # typed in any case, and _client_keys also lowercases it.
        cond |= Q(_email_lc__in=emails)
    paid = (Order.objects.annotate(_email_lc=Lower('customer_email'))
            .filter(cond, payment_status='paid')
            .exclude(id__in=[c['id'] for c in cands])
            .values(*fields))
    paid_at = {}
    for p in paid:
        for key in _client_keys(p):
            paid_at.setdefault(key, []).append(p['created_at'])
    dead = []
    for c in cands:
        for key in _client_keys(c):
            if any(abs(t - c['created_at']) <= SUPERSEDED_WINDOW
                   for t in paid_at.get(key, ())):
                dead.append(c['id'])
                break
    return dead


def requisites_orders():
    """"Payment details" queue: the customer asked for payment details to be sent to their
    phone.

    The SINGLE source for both the list and the counter — otherwise the badge would diverge from
    the table. Rows that no longer mean anything are removed entirely:
      • the order is canceled — there is nobody to send details to;
      • the customer re-placed the cart and paid another order.
    Paid ones stay visible, but dimmed (step 1 in the list)."""
    from orders.models import Order
    qs = (Order.objects
          .filter(payment_contact_requested=True)
          .exclude(is_imported=True)
          .exclude(status='cancelled'))
    dead = _superseded_by_paid_ids(qs)
    return qs.exclude(id__in=dead) if dead else qs


def chat_active_count() -> int:
    """Number of ACTIVE conversations (status=open with at least one message) — NOT unread ones.
    Matches the list at /admin/chat/ (Conversation.last_message_at not null + status=open).
    SINGLE source of the chat badge: context_processor (initial) and get_notification_counts (WS
    payload)."""
    try:
        from chat.models import Conversation
        return Conversation.objects.filter(
            status=Conversation.STATUS_OPEN,
            last_message_at__isnull=False,
        ).count()
    except Exception:
        return 0

# How many days a like stays in "Subscriptions". A like doesn't disappear from the DB on
# its own (only on unlike,
# product deletion, or removal by the manager), and items that never come back would accumulate
# it forever. An older one disappears from the CRM and the counter but stays in the DB ("Favorites"
# (Favorites) analytics).
LIKE_TTL_DAYS = 45


def unavailable_likes() -> dict:
    """Likes ("Favorites") on displayed items that are currently out of stock — demand for
    restocking. This is a notification of the "Subscriptions" section, like a
    subscription: "Done" removes
    it from the counter (NotificationState section='stock_like'), "Delete" removes
    the like. A like on something that can be bought is a shopping list; it is not here.

    {'v<id>'|'b<id>': [{'ids': [...], 'who': str|None, 'at': datetime, 'step': int}]}
    — a notification row. A registered user with several devices is collapsed into one row
    (likes are recorded per device), an anonymous one gets a row per device; the row's state lives
    on
    ids[0] (the newest like). SINGLE source for both the page and the counter."""
    from collections import defaultdict
    from django.db.models import Prefetch
    from .models import Favorite, ProductVariant, Bundle, BundleItem, NotificationState

    from django.utils import timezone
    favs = list(
        Favorite.objects.filter(course__isnull=True,
                                created_at__gte=timezone.now() - timedelta(days=LIKE_TTL_DAYS))
        .values('id', 'product_variant_id', 'bundle_id', 'user_id', 'user__email',
                'user__phone', 'user__first_name', 'user__last_name', 'created_at')
        .order_by('-created_at')
    )
    # Shown on the storefront but cannot be bought (the variantInStock condition: 50/50 is always
    # "in stock").
    # A hidden item was deliberately taken off sale — demand for it is just noise.
    gone = {
        f'v{pk}' for pk, active, product_active, hybrid, stock in
        ProductVariant.objects
        .filter(pk__in={f['product_variant_id'] for f in favs if f['product_variant_id']})
        .values_list('pk', 'is_active', 'product__is_active', 'is_hybrid', 'stock')
        if active and product_active and not hybrid and (stock or 0) <= 0
    }
    for b in (Bundle.objects
               .filter(pk__in={f['bundle_id'] for f in favs if f['bundle_id']})
               # get_stock() reads stock from the prefetch cache (_items_cached) — no N+1.
               .prefetch_related(Prefetch('items', queryset=BundleItem.objects.select_related('product_variant__bulk_source')))):
        if b.is_active and b.get_stock() <= 0:
            gone.add(f'b{b.pk}')

    rows, by_user = defaultdict(list), {}
    for f in favs:
        key = f'v{f["product_variant_id"]}' if f['product_variant_id'] else f'b{f["bundle_id"]}'
        if key not in gone:
            continue
        if f['user_id'] is not None and (key, f['user_id']) in by_user:
            by_user[(key, f['user_id'])]['ids'].append(f['id'])
            continue
        who = None
        if f['user_id'] is not None:
            who = (f['user__email'] or f['user__phone']
                   or f'{f["user__first_name"] or ""} {f["user__last_name"] or ""}'.strip()
                   or f'User #{f["user_id"]}')
        row = {'ids': [f['id']], 'who': who, 'at': f['created_at'], 'step': 0}
        if f['user_id'] is not None:
            by_user[(key, f['user_id'])] = row
        rows[key].append(row)

    heads = {r['ids'][0]: r for key_rows in rows.values() for r in key_rows}
    for object_id, step in (NotificationState.objects
                            .filter(section='stock_like', object_id__in=list(heads))
                            .values_list('object_id', 'step')):
        heads[object_id]['step'] = step
    return rows


def get_notification_counts() -> dict:
    """Number of UNPROCESSED notifications per section.

    Takes NotificationState.step into account: records with step>0 (viewed/dismissed)
    are not counted — same as the tabs on the /admin/notifications/ page.
    """
    from orders.models import Order
    from .models import Review, StockNotification, DropSyncEvent, NotificationState

    def dismissed(section):
        return NotificationState.objects.filter(section=section, step__gt=0).values('object_id')

    # A payment that confirmed ITSELF (bank_matcher auto-confirm or screenshot OCR) is
    # not a notification: the manager has nothing to do. The row stays in the "Payments"
    # list, but dimmed (catalog/views.py: state='paid_auto' → step 1).
    # Exception — if the order still has candidates pending manual review:
    # the second OR branch below catches them, and the notification stays.
    auto_paid = Order.objects.filter(
        bank_matches__is_active=True, bank_matches__auto_confirmed=True,
    ).values('id')
    payments = (
        Order.objects
        .filter(payment_method='bank_transfer')   # bank transfer payments only
        .filter(
            (Q(payment_notification=True) & ~Q(id__in=auto_paid))
            | Q(bank_matches__transaction__status='manual_review',
                bank_matches__is_active=False)
        )
        .exclude(is_imported=True)   # OLD (historical) orders are not counted in "Payments"
        .distinct()
        .exclude(id__in=dismissed('payments'))
        .count()
    )
    # "Payment details": the customer asked for payment details to be sent to their
    # phone and hasn't paid yet.
    # Dead rows (canceled / re-placed-and-paid) are filtered out by requisites_orders().
    requisites = (
        requisites_orders()
        .exclude(payment_status='paid')
        .exclude(id__in=dismissed('requisites'))
        .count()
    )
    # Reviews/questions are now in a single Review model; they differ only by the presence of
    # rating.
    # Counter = all root Reviews (as on the /admin/notifications/reviews/ page),
    # minus those marked as read (NotificationState.step>0).
    reviews_base = Review.objects.filter(parent__isnull=True)
    reviews   = reviews_base.filter(rating__isnull=False).exclude(id__in=dismissed('reviews')).count()
    questions = reviews_base.filter(rating__isnull=True).exclude(id__in=dismissed('questions')).count()
    # "Subscriptions": unprocessed subscriptions + unprocessed likes on out-of-stock
    # items.
    stock = (
        StockNotification.objects.filter(is_notified=False)
        .exclude(id__in=dismissed('stock')).count()
    ) + sum(1 for rows in unavailable_likes().values() for r in rows if not r['step'])
    stock_out = out_of_stock_variants().count()
    stock_low = low_stock_unprocessed_count()
    # The "Supplier changes" section is hidden when DROPSYNC_ENABLED=False —
    # then it is
    # also excluded from the inbox counter (otherwise the badge shows more than the visible
    # sections).
    from django.conf import settings
    if getattr(settings, 'DROPSYNC_ENABLED', True):
        dropsync = (
            DropSyncEvent.objects.filter(is_seen=False)
            .exclude(id__in=dismissed('dropsync')).count()
        )
    else:
        dropsync = 0
    # "the Analyst, the daily analyst": its own inbox section, one simple query.
    from analyst.inbox import new_count as analyst_new_count
    return {
        'payments':   payments,
        'requisites': requisites,
        'reviews':    reviews,
        'questions':  questions,
        'stock':      stock,
        'stock_out':  stock_out,
        'stock_low':  stock_low,
        'dropsync':   dropsync,
        'analyst':    analyst_new_count(),
        'chat':       chat_active_count(),
    }


def oos_dismissed_map() -> dict:
    """{variant_id: (step, updated_at)} — dismissal state in the stock_out section.

    step=1 — someone manually clicked "Done": the row stays in the list, only
    dimmed (as in the other sections).
    step=2 — a one-time bulk closure of the old backlog (migration 0118, "start
    from a clean slate"): such a row is hidden ENTIRELY until it runs out again —
    otherwise restoring the fix would at once dump hundreds of long-forgotten
    items into the list instead of only what is relevant "from now on".

    Shared source for the counter (out_of_stock_variants) and the row list
    (catalog/views.py) — otherwise they would diverge."""
    from .models import NotificationState
    return {
        object_id: (step, updated_at)
        for object_id, step, updated_at in (
            NotificationState.objects
            .filter(section='stock_out', step__gt=0)
            .values_list('object_id', 'step', 'updated_at')
        )
    }


def oos_row_step(variant_id, out_of_stock_at, seen: dict):
    """Step for a "Sold out" row, or None if the row must be hidden entirely.

    The date check is the same as in out_of_stock_variants(): if the packaging
    ran out AGAIN after being dismissed, it is a new event, and the step is 0 again."""
    entry = seen.get(variant_id)
    if entry is None:
        return 0
    step, dismissed_at = entry
    if dismissed_at < out_of_stock_at:
        return 0
    return None if step == 2 else 1


def out_of_stock_variants():
    """Packagings that are out of stock and not yet dismissed by the manager (for the counter).

    Derived from stock: a variant disappears ON ITS OWN as soon as its stock level is > 0.
    Dropshipping and hybrid variants never land here (they are always made to order), so they have
    no marks either.

    This is only for the counter of unprocessed items — the row list on the page itself is wider
    (catalog/views.py shows all stock=0, dismissed ones too, like the other notification sections):
    otherwise "Done" on a row would remove the row itself, not just the mark, and a
    packaging that is still out of stock would be invisible.

    Dismissal is checked against the date: a row drops out of the counter only if the mark was set
    AFTER the packaging hit zero. So when the product is restocked and then runs out again, that is
    a new event and it surfaces despite the old dismissal (object_id here is the packaging id, which
    does not change between events).
    """
    from .models import ProductVariant

    qs = (
        ProductVariant.objects
        # lte, not exact: a write-off against a future delivery (allow_negative_stock) can
        # push stock below zero, skipping exactly 0.
        .filter(stock__lte=0, out_of_stock_at__isnull=False)
        .select_related('product')
    )
    seen = oos_dismissed_map()
    if not seen:
        return qs
    stale_ids = [
        v_id for v_id, out_at in qs.values_list('id', 'out_of_stock_at')
        if v_id in seen and seen[v_id][1] >= out_at
    ]
    return qs.exclude(id__in=stale_ids) if stale_ids else qs


# ── "Low stock" ────────────────────────────────────────────
# Dynamic threshold based on the sales rate (Croston's method + SBA correction), not a single number
# set by hand per product. Most packagings here sell rarely (once every week or month), so a plain
# moving average over calendar days is noisy: a batch of 5 pcs 25 days ago and a batch of 5 pcs 31
# days ago give a wildly different estimate purely because of the window edge. Croston estimates the
# sale size (q) and the interval between sales (p) separately and smooths BOTH only at the moment of
# a sale — the zeros between sales do not spoil the estimate. SBA (Syntetos-Boylan, 2005) removes
# the systematic overestimation of classic Croston on this kind of demand.
LOW_STOCK_MIN_EVENTS = 3       # fewer sales in history — we do not trust the estimate, the product does not get into "Low"
LOW_STOCK_LOOKBACK_DAYS = 395  # a bit over a year — upper bound; the REAL new record-keeping only starts in mid-2026
LOW_STOCK_RECENT_EVENTS = 10   # we compute the rate only over the LAST N sales of the packaging. Demand here is not seasonal but
                                # spiky (a product suddenly becomes relevant — say, a pest outbreak
                                # — and that is exactly where emphasis is needed): old slow history
                                # should be forgotten as soon as enough fresh events accumulate,
                                # instead of diluting them forever.
LOW_STOCK_ALPHA = 0.3          # Croston smoothing: slightly above the typical 0.1–0.3 to react faster
                                # to rate changes — consistent with LOW_STOCK_RECENT_EVENTS above.
LOW_STOCK_QTY_CAP_PERCENTILE = 95   # one-off wholesale sales (a reseller took 20 pcs at once) must not on their own
                                     # sharply inflate the rate estimate on a thin sample — we cap
                                     # the sale size at the 95th percentile of the REAL
                                     # distribution, not at a ceiling pulled out of thin air.
LOW_STOCK_QTY_CAP_FLOOR = 3         # the percentile must not lower the ceiling below a typical retail purchase.
LOW_STOCK_SAFETY_UNITS = 1     # a fixed safety margin instead of a statistical one: too few samples for variance
LOW_STOCK_COVER_DAYS = 45      # How many days of sales the threshold should cover (time to react + restock).
                                # Raised from 21 to 45 (2026-10-01) at the business's direct request
                                # — a wider restocking
                                # planning horizon. Previously checked against supplier invoices
                                # (2026-09): the interval
                                # between TWO receipts of the SAME product is 14-42 days (median
                                # 17.5, n=7 products), but of
                                # the 31 items on the then "Low" list only 13 (42%) ever had
                                # a receipt via an invoice
                                # at all — i.e. this is not 'verified by data' but 'doesn't
                                # contradict the little data there
                                # is'. For products where the auto-calculation doesn't fit,
                                # ProductVariant.low_stock_threshold_override gives a manual threshold bypassing the formula.


def _percentile(sorted_vals, pct):
    """Linear interpolation of a percentile over an ALREADY sorted list (no numpy)."""
    if not sorted_vals:
        return 0
    k = (len(sorted_vals) - 1) * (pct / 100.0)
    lo, hi = math.floor(k), math.ceil(k)
    if lo == hi:
        return sorted_vals[int(k)]
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (k - lo)


def _croston_sba_rate(events):
    """Forecast of average demand/day using Croston's method with SBA correction.

    events — [(days_since_prev_sale, quantity), ...] in chronological order (the first pair is the
    anchor: the interval and size of its first sale, not «from zero»).
    None if there is too little data for an estimate.
    """
    if len(events) < LOW_STOCK_MIN_EVENTS - 1:
        return None
    q = p = None
    for interval, qty in events:
        interval = max(float(interval), 1.0)
        if q is None:
            q, p = float(qty), interval
            continue
        q += LOW_STOCK_ALPHA * (float(qty) - q)
        p += LOW_STOCK_ALPHA * (interval - p)
    if not p:
        return None
    return (1 - LOW_STOCK_ALPHA / 2) * (q / p)


def low_stock_variants():
    """Packagings whose stock has fallen below the dynamic threshold but is not yet zero
    (zero is already "Out of stock"). A derived list: we store nothing extra,
    we only read the existing sales history and the live stock.

    Excluded (as in out_of_stock_variants): dropshipping/hybrid/unprocessed supplier items —
    they don't 'run out' in this sense (_tracks_running_out). Products WITHOUT enough sales
    history (< LOW_STOCK_MIN_EVENTS) do NOT get into the list — the estimate is unreliable on
    such a sample, and a wrong threshold is worse than no warning. Exception —
    ProductVariant.low_stock_threshold_override: a manually set threshold ignores both the
    Croston calculation and this history gate (saves new products that have no sales yet).

    Returns a list of (variant, daily_rate, threshold), sorted by low_stock_at — the newest
    "Low" events (just fell below the threshold) on top.
    daily_rate is None for rows with a manual threshold (the rate was not computed).
    """
    from django.utils import timezone
    from .models import ProductVariant, _tracks_running_out
    from orders.models import OrderItem

    # A light query without select_related/prefetch — the predicate reads only flags, and the heavy
    # fields (photo, bulk bag) are fetched later, only for those that pass the filter.
    # is_active is NOT filtered: as in out_of_stock_variants, a packaging hidden from the storefront
    # still has to be restocked (the CRM sees everything, hidden only from customers on the
    # frontend). The frontend itself marks such rows with a "hidden" label.
    light = (
        ProductVariant.objects
        .filter(stock__gt=0)
        .only('id', 'is_dropshipping', 'is_hybrid', 'is_supplier_fallback', 'stock',
              'low_stock_threshold_override')
    )
    tracked = [v for v in light if _tracks_running_out(v)]
    if not tracked:
        return []
    ids = [v.id for v in tracked]
    overrides = {v.id: v.low_stock_threshold_override for v in tracked if v.low_stock_threshold_override}

    # One query for all packagings at once: paid and NOT legacy-import (the old Horoshop bookkeeping
    # system has a different sales rate and spoils the estimate).
    since = timezone.now() - timedelta(days=LOW_STOCK_LOOKBACK_DAYS)
    rows = (
        OrderItem.objects
        .filter(
            product_variant_id__in=ids,
            order__payment_status='paid',
            order__is_imported=False,
            order__created_at__gte=since,
        )
        .exclude(order__status='cancelled')
        .values_list('product_variant_id', 'order__created_at', 'quantity')
        .order_by('product_variant_id', 'order__created_at')
    )
    # Ceiling on the size of a SINGLE sale — computed once from the real distribution (95th
    # percentile), not pulled out of thin air: a thin sample (3-10 sales) must not jump sharply up
    # because of one wholesale/reseller order.
    qty_cap = (
        max(LOW_STOCK_QTY_CAP_FLOOR, _percentile(sorted(qty for _, _, qty in rows), LOW_STOCK_QTY_CAP_PERCENTILE))
        if rows else LOW_STOCK_QTY_CAP_FLOOR
    )

    by_variant = {}
    for vid, created_at, qty in rows:
        by_variant.setdefault(vid, []).append((created_at, min(qty, qty_cap)))
    # Stock — for ALL tracked ids in one later query, not only those that have sales: a packaging
    # with a manual threshold may have no sales history at all.
    stocks = dict(ProductVariant.objects.filter(id__in=ids).values_list('id', 'stock'))

    hits = []   # (variant_id, rate_or_None, threshold, urgency)
    for vid in ids:
        # We re-check stock>0 (not only in light above): stocks reads the stock level in a separate,
        # later query — the product could have sold out to zero right between them. Zero is already
        # "Out of stock", it must not get in here.
        stock = stocks.get(vid, 0)
        if stock <= 0:
            continue
        if vid in overrides:
            threshold = overrides[vid]
            if stock <= threshold:
                hits.append((vid, None, threshold, stock / threshold))
            continue
        history = by_variant.get(vid)
        if not history or len(history) < LOW_STOCK_MIN_EVENTS:
            continue
        # Only the LAST N sales — an old slow (or, conversely, hot) stretch should be forgotten as
        # soon as enough fresh events have accumulated.
        recent = history[-(LOW_STOCK_RECENT_EVENTS + 1):]
        events, prev = [], None
        for created_at, qty in recent:
            if prev is not None:
                events.append(((created_at - prev).total_seconds() / 86400.0, qty))
            prev = created_at
        rate = _croston_sba_rate(events)
        if not rate:
            continue
        threshold = math.ceil(rate * LOW_STOCK_COVER_DAYS) + LOW_STOCK_SAFETY_UNITS
        if stock <= threshold:
            hits.append((vid, rate, threshold, stock / threshold))
    if not hits:
        _sync_low_stock_marks(set())   # the list has emptied — clear all old marks
        return []

    hit_ids = {h[0] for h in hits}
    _sync_low_stock_marks(hit_ids)   # BEFORE fetching variants — so that low_stock_at is fresh

    variants = {
        v.id: v for v in (
            ProductVariant.objects
            .filter(id__in=hit_ids)
            .select_related('product', 'bulk_source')
            .prefetch_related('images', 'product__images')
        )
    }
    result = [
        (variants[vid], rate, threshold)
        for vid, rate, threshold, _ in hits if vid in variants
    ]
    # A "Low" event that just appeared is the most noticeable, so it goes on top. Not
    # «hottest by sales rate» as it was before: the manager expects a feed ordered by time.
    result.sort(key=lambda r: r[0].low_stock_at, reverse=True)
    return result


def _sync_low_stock_marks(hit_ids: set) -> None:
    """Maintains ProductVariant.low_stock_at — since when the variant has been continuously in "Low".

    NOT a signal on every stock change: the threshold is dynamic (sales rate), and
    low_stock_variants() is called often anyway (the notification counter hangs on a pile of signals
    all over the site) — updating opportunistically here is enough. Minute-level accuracy is not
    critical, what matters is only "lowstock_row_step below".
    Writes only when something REALLY changed — 2 light selects + at most 2 bulk updates, not per
    row."""
    from django.utils import timezone
    from .models import ProductVariant

    to_set_ids = list(
        ProductVariant.objects.filter(id__in=hit_ids, low_stock_at__isnull=True)
        .values_list('id', flat=True)
    )
    if to_set_ids:
        ProductVariant.objects.filter(id__in=to_set_ids).update(low_stock_at=timezone.now())

    to_clear_ids = list(
        ProductVariant.objects.filter(low_stock_at__isnull=False)
        .exclude(id__in=hit_ids)
        .values_list('id', flat=True)
    )
    if to_clear_ids:
        ProductVariant.objects.filter(id__in=to_clear_ids).update(low_stock_at=None)


def lowstock_dismissed_map() -> dict:
    """{variant_id: (step, updated_at)} — dismissal state in the stock_low section.

    The same technique as oos_dismissed_map() for "Out of stock"."""
    from .models import NotificationState
    return {
        object_id: (step, updated_at)
        for object_id, step, updated_at in (
            NotificationState.objects
            .filter(section='stock_low', step__gt=0)
            .values_list('object_id', 'step', 'updated_at')
        )
    }


def lowstock_row_step(variant_id, low_stock_at, seen: dict):
    """Step for a "Low" row — 0 (new/again) or the saved step.

    The date check is the same as in oos_row_step(): if the stock level became low AGAIN after the
    dismissal, it is a new event, and the step is 0 again. Unlike "Out of stock",
    there is no "step=2" here — there was no old backlog at feature launch, so
    nothing to close one-off."""
    entry = seen.get(variant_id)
    if entry is None:
        return 0
    step, dismissed_at = entry
    if low_stock_at is not None and dismissed_at < low_stock_at:
        return 0
    return step


def low_stock_unprocessed_count() -> int:
    """Number of "Low" warnings not yet dismissed (for the counter).

    Counts from the already computed low_stock_variants() (no second pass over sales) — like
    out_of_stock_variants(), it is a separate, narrower selection from the same source, so the
    counter and the list cannot diverge."""
    hits = low_stock_variants()
    if not hits:
        return 0
    seen = lowstock_dismissed_map()
    if not seen:
        return len(hits)
    return sum(
        1 for v, _, _ in hits
        if lowstock_row_step(v.id, v.low_stock_at, seen) == 0
    )


def broadcast_notification_counts() -> None:
    """Broadcast fresh counters to all staff tabs in the admin_notifications group."""
    # Invalidate the sidebar placeholder cache: the data changed, so the next reload must take fresh
    # numbers from the DB rather than show a stale badge (flicker).
    from django.core.cache import cache
    from .context_processors import SIDEBAR_COUNTS_CACHE_KEY
    cache.delete(SIDEBAR_COUNTS_CACHE_KEY)

    layer = get_channel_layer()
    if layer is None:
        return
    counts = get_notification_counts()
    async_to_sync(layer.group_send)(GROUP_NOTIFICATIONS, {
        'type': _EVENT_TYPE,
        'payload': counts,
    })
