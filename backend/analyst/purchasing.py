"""The Analyst's purchasing calculation: the NUMBERS are computed by the code, the model only
interprets and advises.

Purchase unit: the **bulk bag** (BulkSource) for packagings tracked per bag — the demand of all its
packagings is summed in g/ml/pcs; the rest — the packaging itself, in pieces. Dropshipping, hybrids
and raw
import are not included (the supplier procures those).

Where demand comes from:
  * live orders (`stock_applied=True`, not cancelled) — real stock write-off since June 2026;
  * Horoshop import (February 2024 – June 2026) — only what is linked to a product (FK); it provides
    SEASONALITY: what sold in the same windows of past years;
  * 'waiting' — 'Notify me when available' subscriptions and likes on
    out-of-stock items:
    demand that sales will not show, because the product is not there;
"""
from __future__ import annotations

import math
import statistics
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta

from django.conf import settings
from django.utils import timezone

RECENT_DAYS = 60
SEASON_MIN_UNITS = 3         # last year's window must have at least this many pieces for the season to be trusted
TREND_MIN, TREND_MAX = 0.2, 2.0
SHOP_INDEX_MIN, SHOP_INDEX_MAX = 0.3, 3.0
DEAD_DAYS = 90
LOOKBACK_YEARS = 2
UNIT_LABEL = {'g': 'g', 'ml': 'ml', 'pcs': 'pcs'}


@dataclass
class PurchaseItem:
    key: str
    label: str
    unit: str
    product_id: int
    variant_ids: list
    stock: float
    out_since: str | None
    recent_60: float               # actually sold in the last 60 days (in unit)
    rate_per_day: float            # pace accounting for the fact that the product may have been out of stock
    seasonal_past: float | None    # how much sold in the same 60 days of past years (average)
    seasonal_next: float | None    # how much sold in the same following H days of past years
    history_years: int
    trend: float | None            # this year relative to past years over the same 60 days
    trend_basis: str | None        # item (the product itself) | shop (whole-shop level)
    forecast: float                # demand forecast over an H-day horizon
    forecast_basis: str            # item_seasonal | recent_shop_season | recent | none
    cover_days: float | None
    waiting_subs: int
    waiting_likes: int
    open_po: float
    need: float                    # raw shortfall = forecast - stock - open_po (≥0)
    recommended: float             # rounded quantity to order
    urgency: str                   # out | urgent | soon | ok | dead
    last_sale: str | None
    notes: list = field(default_factory=list)
    siblings: list = field(default_factory=list)   # other packagings of the same product (see _siblings)
    covered_by: str | None = None                   # why no purchase is needed: the same item is already in stock
    # Whether the demand is real and the purchase is worth the effort (see _skip_reason):
    orders_60: int = 0                              # how many DIFFERENT orders there were in 60 days
    top_order_share: float | None = None            # share of the largest order in sales (1.0 = a single one-off bulk order)
    sales_months_12: int = 0                        # in how many months of the last year there were sales
    unit_price: float | None = None                 # selling price per item unit (g/ml/pcs), UAH
    est_revenue: float | None = None                # ≈ revenue over the horizon according to the forecast, UAH
    lot_size: float | None = None                   # typical purchase batch from supplier invoice receipts (in item units)
    receipts: list = field(default_factory=list)    # latest receipts [(date, quantity)]
    skip_reason: str | None = None                  # why this is not a purchase candidate (trivial amount, one-off sales, oversized batch)

    def as_dict(self) -> dict:
        d = asdict(self)
        d['unit_label'] = UNIT_LABEL.get(self.unit, self.unit)
        return d


@dataclass
class ShopContext:
    today: str
    lead_days: int
    cover_days: int
    horizon_days: int
    shop_index: float | None       # how the next horizon compares with the last 60 days in past years
    shop_trend: float | None       # how much the shop sells now relative to the same 60 days of past years
    months_hint: list              # [(YYYY-MM, units)] for the whole shop — seasonality shape
    live_since: str | None
    history_note: str


def _shift_years(d, years: int):
    try:
        return d.replace(year=d.year - years)
    except ValueError:
        return d.replace(year=d.year - years, day=28)


def _round_up(value: float, unit: str) -> float:
    if value <= 0:
        return 0.0
    if unit == 'pcs':
        return float(math.ceil(value))
    step = 500.0 if value < 10_000 else 1000.0
    return float(math.ceil(value / step) * step)


def _horizon() -> int:
    return int(settings.ANALYST_LEAD_DAYS) + int(settings.ANALYST_COVER_DAYS)


def human_qty(value: float, unit: str) -> str:
    if unit in ('g', 'ml'):
        if abs(value) >= 1000:
            return f'{round(value / 1000.0, 1):g} {"kg" if unit == "g" else "l"}'     # 8259 g → 8.3 kg, not 8.259
        return f'{round(value):g} {UNIT_LABEL[unit]}'
    return f'{round(value, 1):g} pcs'


# ── Collecting input data ─────────────────────────────────────────────────────

def _norm(text) -> str:
    return ' '.join(str(text or '').lower().split())


def _is_own(v) -> bool:
    return not (v.is_dropshipping or v.is_hybrid or v.is_supplier_fallback)


def _is_bag(v) -> bool:
    return bool(v.bulk_source_id and v.unit_size and not v.stock_manual)


def _available(v) -> bool:
    """A customer can buy this now: active and in stock, or the supplier (dropshipping, hybrid) will
deliver."""
    return bool(v.is_active and (v.is_dropshipping or v.is_hybrid or (v.stock or 0) > 0))


def _kind(v) -> str:
    if v.is_hybrid:
        return 'hybrid (stock + supplier)'
    if v.is_dropshipping:
        return 'dropship (supplier)'
    if _is_bag(v):
        return 'own packaging from a bag'
    return 'factory-made / own stock'


def _all_variants():
    """All catalog variants (except service 'fallback' ones): we calculate our own, the rest are shown only
as context."""
    from catalog.models import ProductVariant
    return list(ProductVariant.objects.filter(is_supplier_fallback=False).select_related('product', 'bulk_source'))


def _sibling_sales(variant_ids, since):
    """{variant_id: units sold since `since`} — direct sales of live orders, for reference on neighboring packagings."""
    from django.db.models import Sum
    from orders.models import OrderItem
    rows = (OrderItem.objects
            .filter(product_variant_id__in=variant_ids, order__is_imported=False, order__created_at__gte=since)
            .exclude(order__status='cancelled')
            .values('product_variant_id').annotate(q=Sum('quantity')))
    return {r['product_variant_id']: float(r['q'] or 0) for r in rows}


def _order_units(variant_ids, since):
    """{variant_id: {order_id: units}} — live write-offs (direct items and bundle components). No personal data."""
    from orders.models import OrderItem, OrderItemBundleComponent
    acc = defaultdict(lambda: defaultdict(float))
    for vid, oid, qty in (OrderItem.objects
                          .filter(product_variant_id__in=variant_ids, stock_applied=True,
                                  order__is_imported=False, order__created_at__gte=since)
                          .exclude(order__status='cancelled')
                          .values_list('product_variant_id', 'order_id', 'quantity')):
        acc[vid][oid] += qty
    for vid, oid, comp_qty, item_qty in (OrderItemBundleComponent.objects
                                         .filter(product_variant_id__in=variant_ids, order_item__stock_applied=True,
                                                 order_item__order__is_imported=False,
                                                 order_item__order__created_at__gte=since)
                                         .exclude(order_item__order__status='cancelled')
                                         .values_list('product_variant_id', 'order_item__order_id', 'quantity',
                                                      'order_item__quantity')):
        acc[vid][oid] += comp_qty * item_qty
    return acc


def _receipts():
    """Receipts from confirmed supplier invoices: {'bag:ID' | 'var:ID': [(date, quantity in item
    units)]}.
    Invoices exist since June 2026 and do not cover all products: empty means 'unknown', not 'never
    bought'."""
    from orders.models import InvoiceItem
    out = defaultdict(list)
    qs = (InvoiceItem.objects.filter(invoice__status='confirmed').select_related('invoice')
          .order_by('invoice__invoice_date', 'invoice__created_at', 'pk'))
    for it in qs:
        when = it.invoice.invoice_date or it.invoice.created_at.date()
        if it.applied_bag_id and it.applied_bag_amount:
            out[f'bag:{it.applied_bag_id}'].append((when, float(it.applied_bag_amount)))
        elif it.matched_variant_id and (it.applied_delta or 0) > 0:
            out[f'var:{it.matched_variant_id}'].append((when, float(it.applied_delta)))
    return out


def _unit_price(members, eff_unit):
    """Median selling price per stock unit (g/ml/pcs) across the item's active packagings, UAH."""
    prices = []
    for m in members:
        if not m.is_active:
            continue
        price, eff = float(m.effective_price or m.price or 0), eff_unit(m)
        if price > 0 and eff > 0:
            prices.append(price / eff)
    return statistics.median(prices) if prices else None


def _skip_reason(i, promised: bool):
    """Why an item is not a candidate even though stock is zero. The code filters out noise BEFORE the
    agent: the owner does not need purchases
    worth 'a few hundred hryvnias', purchases based on one-off sales (people often buy leftovers),
    or on a batch that would last for years.
    Not filtered out if ≥2 people are waiting or stock is negative (delivery already promised to
    customers)."""
    if promised or (i.waiting_subs + i.waiting_likes) >= 2:
        return None
    min_revenue = float(getattr(settings, 'ANALYST_MIN_REVENUE', 0) or 0)
    if min_revenue and i.est_revenue is not None and i.est_revenue < min_revenue:
        return f'small revenue: ≈{i.est_revenue:.0f} UAH over the horizon — a separate order will not pay off'
    min_months = int(getattr(settings, 'ANALYST_MIN_SALES_MONTHS', 0) or 0)
    if min_months and i.sales_months_12 < min_months:
        if i.sales_months_12 == 0:
            return 'no sales in the last year — this is not current demand'
        return (f'one-off sales: only in {i.sales_months_12} mo. of the year — this is not steady demand '
                f'(looks like a clearance of leftovers)')
    if i.lot_size and i.forecast > 0 and i.lot_size > i.forecast * 3:
        days = i.lot_size / (i.forecast / max(1, int(settings.ANALYST_LEAD_DAYS) + int(settings.ANALYST_COVER_DAYS)))
        return f'the supply lot {human_qty(i.lot_size, i.unit)} is too large: it would cover ≈{days:.0f} days of sales'
    return None


def _purchase_groups(variants):
    """Purchasing groups and 'twins'.

    The purchasing unit is a bulk bag (all its packagings together) or a single packaging. In
    addition, THE SAME items entered
    twice in the catalog (same product name and same packaging name — typically an old card from
    Horoshop and a new
    own packaging from a bag) are merged into one group: demand and stock must be counted together,
    otherwise the agent
    advises restocking the old card with zero stock while the twin is in stock and selling.
    Returns (groups, eff_override): `eff_override[variant_id]` is how many bag units a twin without
    a bag takes
    (taken from the bag's reference packaging with the same name)."""
    clusters = defaultdict(list)
    for v in variants:
        clusters[(_norm(v.product.name), _norm(v.name or v.product.name))].append(v)
    groups: dict[str, list] = defaultdict(list)
    eff_override: dict[int, float] = {}
    for members in clusters.values():
        bags = {m.bulk_source_id for m in members if _is_bag(m)}
        if len(members) > 1 and len(bags) <= 1:
            anchor = next((m for m in members if _is_bag(m)), min(members, key=lambda m: m.pk))
            key = f'bag:{anchor.bulk_source_id}' if _is_bag(anchor) else f'var:{anchor.pk}'
            if _is_bag(anchor):
                unit = float(anchor.bulk_source.effective_unit(anchor.unit_size))
                for m in members:
                    if not _is_bag(m):
                        eff_override[m.pk] = unit
            groups[key].extend(members)
        else:
            for m in members:
                groups[f'bag:{m.bulk_source_id}' if _is_bag(m) else f'var:{m.pk}'].append(m)
    for members in groups.values():
        members.sort(key=lambda m: (not _is_bag(m), m.pk))      # the packaging from the bag goes first: the name and product come from it
    return groups, eff_override


def _siblings(members, family, sold):
    """Other packagings of the same product (by product name) that are not part of this purchasing
    item.
    Returns (siblings, covered_by): `covered_by` is filled if a twin (same packaging name) is in
    stock."""
    member_ids = {m.pk for m in members}
    twin_names = {_norm(m.name or m.product.name) for m in members}
    seen, out = set(), []
    for fam in {_norm(m.product.name) for m in members}:
        for v in family.get(fam, []):
            if v.pk in member_ids or v.pk in seen:
                continue
            seen.add(v.pk)
            avail, sold_60 = _available(v), sold.get(v.pk, 0.0)
            if not (avail or sold_60 or v.is_active):
                continue            # hidden, no stock and no sales — just noise
            out.append({
                'label': (v.name or v.product.name)[:90], 'kind': _kind(v),
                'stock': None if (v.is_dropshipping and not v.is_hybrid) else int(v.stock or 0),
                'active': bool(v.is_active), 'available': avail, 'sold_60': sold_60,
                'twin': _norm(v.name or v.product.name) in twin_names,
            })
    out.sort(key=lambda s: (not s['twin'], not s['available'], -s['sold_60']))
    twin = next((s for s in out if s['twin'] and s['available']), None)
    covered = None
    if twin:
        covered = (f'the same item "{twin["label"]}" ({twin["kind"]}) is already in stock'
                   + (f': {twin["stock"]} pcs' if twin['stock'] is not None else '')
                   + (f', sold in 60 days {twin["sold_60"]:g}' if twin['sold_60'] else ''))
    return out[:6], covered


def sibling_line(s: dict, *, kind: bool = True) -> str:
    """One line about a neighboring packaging. kind=True (for the agent) — with the type ('own
    packaging from a bag', etc.);
    kind=False (for the owner's cards) — in plain language without service words."""
    twin = ' [same item]' if s['twin'] else ''
    if kind:
        state = 'in stock' if s['available'] else ('hidden' if not s['active'] else 'none')
        stock = f', stock {s["stock"]}' if s['stock'] is not None and s['available'] else ''
        sold = f', sold in 60 days {s["sold_60"]:g}' if s['sold_60'] else ''
        return f'"{s["label"]}" ({s["kind"]}): {state}{stock}{sold}{twin}'
    if s['available']:
        state = f'{s["stock"]} pcs in stock' if s['stock'] is not None else 'available (on order)'
    else:
        state = 'hidden' if not s['active'] else 'none'
    sold = f', sold in 60 days: {s["sold_60"]:g}' if s['sold_60'] else ''
    return f'"{s["label"]}" — {state}{sold}' + (' (same item)' if s['twin'] else '')


def _live_rows(variant_ids, since):
    """[(variant_id, created_at, units)] — live stock write-off: direct items + bundle components."""
    from orders.models import OrderItem, OrderItemBundleComponent
    rows = list(
        OrderItem.objects
        .filter(product_variant_id__in=variant_ids, stock_applied=True,
                order__is_imported=False, order__created_at__gte=since)
        .exclude(order__status='cancelled')
        .values_list('product_variant_id', 'order__created_at', 'quantity'))
    for vid, created, comp_qty, item_qty in (
            OrderItemBundleComponent.objects
            .filter(product_variant_id__in=variant_ids, order_item__stock_applied=True,
                    order_item__order__is_imported=False, order_item__order__created_at__gte=since)
            .exclude(order_item__order__status='cancelled')
            .values_list('product_variant_id', 'order_item__order__created_at', 'quantity', 'order_item__quantity')):
        rows.append((vid, created, comp_qty * item_qty))
    return rows


def _history_rows(variant_ids, windows):
    """Horoshop import, only the part linked to a product: [(variant_id, created_at, qty)] in the given
windows."""
    from django.db.models import Q
    from orders.models import OrderItem
    if not windows:
        return []
    cond = Q()
    for start, end in windows:
        cond |= Q(order__created_at__gte=start, order__created_at__lt=end)
    return list(
        OrderItem.objects
        .filter(cond, product_variant_id__in=variant_ids, order__is_imported=True)
        .exclude(order__status='cancelled')
        .values_list('product_variant_id', 'order__created_at', 'quantity'))


def _history_bounds():
    from django.db.models import Max, Min
    from orders.models import Order
    agg = Order.objects.filter(is_imported=True).aggregate(a=Min('created_at'), b=Max('created_at'))
    return agg['a'], agg['b']


def _waiting(variant_ids, own_by_id):
    """{variant_id: (subscriptions, likes)}. A bundle that is not available 'waits' for its components that are out of stock."""
    from django.db.models import Count
    from catalog.models import BundleItem, StockNotification
    from catalog.notifications import unavailable_likes

    subs, likes = defaultdict(int), defaultdict(int)
    bundle_subs, bundle_likes = defaultdict(int), defaultdict(int)
    for row in (StockNotification.objects.filter(is_notified=False)
                .values('variant_id', 'bundle_id').annotate(n=Count('id'))):
        if row['variant_id']:
            subs[row['variant_id']] += row['n']
        elif row['bundle_id']:
            bundle_subs[row['bundle_id']] += row['n']
    for key, rows in unavailable_likes().items():
        if key.startswith('v'):
            likes[int(key[1:])] += len(rows)
        elif key.startswith('b'):
            bundle_likes[int(key[1:])] += len(rows)
    bundle_ids = set(bundle_subs) | set(bundle_likes)
    if bundle_ids:
        for bi in BundleItem.objects.filter(bundle_id__in=bundle_ids).select_related('product_variant'):
            v = bi.product_variant
            if v.pk in own_by_id and (v.stock or 0) <= 0:   # the bundle's 'bottleneck'
                subs[v.pk] += bundle_subs.get(bi.bundle_id, 0)
                likes[v.pk] += bundle_likes.get(bi.bundle_id, 0)
    return {vid: (subs.get(vid, 0), likes.get(vid, 0)) for vid in variant_ids}


PO_MAX_AGE_DAYS = 90


def open_orders(today=None):
    """Ordered and not yet arrived: open records no older than 90 days. Older ones are considered
    forgotten: the delivery
    should have arrived long ago, and subtracting them from the demand forever would be a mistake."""
    from .models import PlannedPurchase
    cutoff = (today or timezone.localdate()) - timedelta(days=PO_MAX_AGE_DAYS)
    return PlannedPurchase.objects.filter(status=PlannedPurchase.OPEN, ordered_at__gte=cutoff)


def _open_pos(today=None):
    """How much has already been ordered and is still on its way (per item)."""
    totals = defaultdict(float)
    for key, qty in open_orders(today).values_list('key', 'quantity'):
        totals[key] += float(qty)
    return totals


# ── Main calculation ───────────────────────────────────────────────────────

def build(now: datetime | None = None):
    """Returns (ShopContext, [PurchaseItem]) — all own items with their calculation."""
    now = now or timezone.now()
    today = timezone.localtime(now).date()
    lead, cover = int(settings.ANALYST_LEAD_DAYS), int(settings.ANALYST_COVER_DAYS)
    horizon = lead + cover

    all_variants = _all_variants()
    variants = [v for v in all_variants if _is_own(v)]
    by_id = {v.pk: v for v in variants}
    variant_ids = list(by_id)
    family = defaultdict(list)
    for v in all_variants:
        family[_norm(v.product.name)].append(v)

    # Purchasing groups: a bag or the packaging itself; identical twin items are merged into one.
    groups, eff_override = _purchase_groups(variants)

    def eff_unit(v) -> float:
        """How many stock units one sold packaging consumes (for a bag — with a margin of error)."""
        if v.pk in eff_override:
            return eff_override[v.pk]
        if _is_bag(v):
            return float(v.bulk_source.effective_unit(v.unit_size))
        return 1.0

    # Sales: live (last 120 days) and historical windows of past years.
    live_since = now - timedelta(days=120)
    live = _live_rows(variant_ids, live_since)

    hist_min, hist_max = _history_bounds()
    past_len = RECENT_DAYS
    windows, window_meta = [], []        # (start, end), (year_offset, kind)
    for k in range(1, LOOKBACK_YEARS + 1):
        p_start, p_end = _shift_years(now - timedelta(days=past_len), k), _shift_years(now, k)
        n_start, n_end = _shift_years(now, k), _shift_years(now + timedelta(days=horizon), k)
        valid = bool(hist_min and hist_max and p_start >= hist_min and n_end <= hist_max)
        if valid:
            windows += [(p_start, p_end), (n_start, n_end)]
            window_meta += [(k, 'past', p_start, p_end), (k, 'next', n_start, n_end)]
    hist = _history_rows(variant_ids, windows)
    years_used = sorted({m[0] for m in window_meta})

    # Sum over the windows for each variant.
    def window_sum(rows, start, end):
        acc = defaultdict(float)
        for vid, ts, qty in rows:
            if start <= ts < end:
                acc[vid] += qty
        return acc

    recent_start = now - timedelta(days=RECENT_DAYS)
    live_recent = window_sum(live, recent_start, now + timedelta(seconds=1))
    last_sale_dt = {}
    for vid, ts, _q in live:
        if vid not in last_sale_dt or ts > last_sale_dt[vid]:
            last_sale_dt[vid] = ts
    past_by_year = {k: window_sum(hist, s, e) for (k, kind, s, e) in window_meta if kind == 'past'}
    next_by_year = {k: window_sum(hist, s, e) for (k, kind, s, e) in window_meta if kind == 'next'}

    # Shop season index: next horizon / last 60 days in past years (all products together).
    shop_index = None
    ratios = []
    for k in years_used:
        past_total, next_total = sum(past_by_year[k].values()), sum(next_by_year[k].values())
        if past_total >= 30 and next_total > 0:
            ratios.append((next_total / horizon) / (past_total / past_len))
    if ratios:
        shop_index = min(max(sum(ratios) / len(ratios), SHOP_INDEX_MIN), SHOP_INDEX_MAX)

    # Shop level this year relative to past years: the platform changed, so absolute figures from
    # past years
    # cannot be taken as is — we scale them by the actual pace.
    shop_trend = None
    live_total = sum(live_recent.values())
    past_totals = [sum(past_by_year[k].values()) for k in years_used if k in past_by_year]
    if past_totals and sum(past_totals) / len(past_totals) >= 30:
        shop_trend = min(max(live_total / (sum(past_totals) / len(past_totals)), TREND_MIN), TREND_MAX)

    sibling_sold = _sibling_sales([v.pk for v in all_variants], recent_start)
    order_units = _order_units(variant_ids, recent_start)
    months_by_variant = defaultdict(set)
    for vid, ts, _q in _history_rows(variant_ids, [(now - timedelta(days=365), now)]):
        months_by_variant[vid].add(ts.strftime('%Y-%m'))
    for vid, ts, _q in live:
        months_by_variant[vid].add(ts.strftime('%Y-%m'))
    receipts_all = _receipts()
    waiting = _waiting(variant_ids, by_id)
    pos = _open_pos(today)

    items: list[PurchaseItem] = []
    for key, members in groups.items():
        first = members[0]
        is_bag = key.startswith('bag:')
        bag = first.bulk_source if is_bag else None
        unit = (bag.unit if is_bag else 'pcs')
        if is_bag:
            # Bag + stock of twins without a bag (in pieces → in bag units).
            stock = float(bag.quantity) + sum((m.stock or 0) * eff_unit(m) for m in members if not _is_bag(m))
        else:
            stock = float(sum(m.stock or 0 for m in members))
        mids = [m.pk for m in members]

        def in_units(per_variant: dict) -> float:
            return sum(per_variant.get(m.pk, 0.0) * eff_unit(m) for m in members)

        recent = in_units(live_recent)
        past_vals = [in_units(past_by_year[k]) for k in years_used if k in past_by_year]
        next_vals = [in_units(next_by_year[k]) for k in years_used if k in next_by_year]
        usable = [(p, n) for p, n in zip(past_vals, next_vals) if (p + n) >= SEASON_MIN_UNITS]
        history_years = len(usable)
        seasonal_past = sum(p for p, _ in usable) / history_years if history_years else None
        seasonal_next = sum(n for _, n in usable) / history_years if history_years else None

        out_since = min((m.out_of_stock_at for m in members if m.out_of_stock_at), default=None) if stock <= 0 else None
        # The product may have been unavailable for part of the window — then the 'last 60 days' are
        # understated: we detect this via out_since.
        rate = recent / RECENT_DAYS
        notes = []
        if out_since and (now - out_since).days >= 7 and recent == 0:
            notes.append(f'out of stock since {timezone.localtime(out_since):%d.%m.%Y} — sales over the last days are zero because of the absence, not because of demand')

        trend, trend_basis = None, None
        if seasonal_next is not None:
            if seasonal_past is not None and seasonal_past >= SEASON_MIN_UNITS and not out_since:
                trend, trend_basis = min(max(recent / seasonal_past, TREND_MIN), TREND_MAX), 'item'
            elif shop_trend is not None:
                # The product was unavailable (sales understated by its absence) or there is little
                # history — use the shop level.
                trend, trend_basis = shop_trend, 'shop'
        if seasonal_next is not None:
            forecast = seasonal_next * (trend if trend is not None else 1.0)
            basis = 'item_seasonal'
        elif recent > 0 and shop_index is not None:
            forecast = recent / RECENT_DAYS * horizon * shop_index
            basis = 'recent_shop_season'
        elif recent > 0:
            forecast = recent / RECENT_DAYS * horizon
            basis = 'recent'
        else:
            forecast, basis = 0.0, 'none'

        subs = sum(waiting[m.pk][0] for m in members)
        likes = sum(waiting[m.pk][1] for m in members)
        open_po = pos.get(key, 0.0)
        need = max(forecast - stock - open_po, 0.0)
        daily = forecast / horizon if forecast > 0 else 0.0
        cover_days = (stock / daily) if daily > 0 and stock > 0 else (0.0 if stock <= 0 and daily > 0 else None)

        last = max((last_sale_dt[m.pk] for m in members if m.pk in last_sale_dt), default=None)
        active_any = any(m.is_active for m in members)
        if stock <= 0:
            urgency = 'out' if (forecast > 0 or subs or likes) else 'dead'
        elif daily > 0 and cover_days < lead:
            urgency = 'urgent'
        elif daily > 0 and cover_days < lead + cover / 2:
            urgency = 'soon'
        elif stock > 0 and not recent and forecast == 0 and (not last or (now - last).days >= DEAD_DAYS):
            urgency = 'dead'
        else:
            urgency = 'ok'
        if not active_any and recent == 0 and not (subs or likes):
            continue   # a hidden item with no sales and no waiting customers is not shown

        siblings, covered_by = _siblings(members, family, sibling_sold)
        if covered_by and urgency != 'dead':
            # Twin in stock: demand goes through it (its own item is calculated separately).
            need, urgency = 0.0, 'ok'
            notes.append(f'Nothing to buy: {covered_by}.')
        else:
            others = [s for s in siblings if s['available'] and s['sold_60']][:3]
            if others and urgency in ('out', 'urgent', 'soon'):
                notes.append('Other pack sizes of this product are in stock and selling: '
                             + '; '.join(sibling_line(s) for s in others) + '. Demand is partly going through them.')

        # Whether demand is real: distinct orders, months with sales, one-off bulk orders.
        per_order = defaultdict(float)
        for m in members:
            for oid, units in order_units.get(m.pk, {}).items():
                per_order[oid] += units * eff_unit(m)
        total_units = sum(per_order.values())
        orders_60 = len(per_order)
        top_share = (max(per_order.values()) / total_units) if total_units > 0 else None
        months_12 = len(set().union(*(months_by_variant.get(m.pk, set()) for m in members)))

        unit_price = _unit_price(members, eff_unit)
        est_revenue = forecast * unit_price if (unit_price and forecast > 0) else None

        # Receipts from invoices: into the bag (in its units) and piece-based ones (twins without a
        # bag — in bag units).
        recs = list(receipts_all.get(key, [])) if is_bag else []
        for m in members:
            if not (is_bag and _is_bag(m)):
                recs += [(d, a * eff_unit(m)) for d, a in receipts_all.get(f'var:{m.pk}', [])]
        recs.sort()
        lot_size = statistics.median([a for _d, a in recs[-3:]]) if recs else None

        label = (f'{first.product.name} (bulk bag "{bag.name}")' if is_bag else (first.name or first.product.name))
        item = PurchaseItem(
            key=key, label=label[:200], unit=unit, product_id=first.product_id, variant_ids=mids,
            stock=round(stock, 3), out_since=out_since.date().isoformat() if out_since else None,
            recent_60=round(recent, 2), rate_per_day=round(rate, 4),
            seasonal_past=None if seasonal_past is None else round(seasonal_past, 2),
            seasonal_next=None if seasonal_next is None else round(seasonal_next, 2),
            history_years=history_years, trend=None if trend is None else round(trend, 2),
            trend_basis=trend_basis,
            forecast=round(forecast, 2), forecast_basis=basis,
            cover_days=None if cover_days is None else round(cover_days, 1),
            waiting_subs=subs, waiting_likes=likes, open_po=round(open_po, 3),
            need=round(need, 2), recommended=_round_up(need, unit), urgency=urgency,
            last_sale=last.date().isoformat() if last else None, notes=notes,
            siblings=siblings, covered_by=covered_by,
            orders_60=orders_60, top_order_share=None if top_share is None else round(top_share, 2),
            sales_months_12=months_12,
            unit_price=None if unit_price is None else round(unit_price, 4),
            est_revenue=None if est_revenue is None else round(est_revenue),
            lot_size=None if lot_size is None else round(lot_size, 2),
            receipts=[(d.isoformat(), round(a, 2)) for d, a in recs[-5:]],
        )
        item.skip_reason = _skip_reason(item, promised=stock < 0)
        items.append(item)

    # Context: seasonality shape by month (all years of history + live) for the explanations.
    monthly = defaultdict(float)
    for vid, ts, qty in _history_rows(variant_ids, [(hist_min, hist_max + timedelta(days=1))]) if hist_min else []:
        monthly[ts.strftime('%Y-%m')] += qty
    for vid, ts, qty in live:
        monthly[ts.strftime('%Y-%m')] += qty
    ctx = ShopContext(
        today=today.isoformat(), lead_days=lead, cover_days=cover, horizon_days=horizon,
        shop_index=None if shop_index is None else round(shop_index, 2),
        shop_trend=None if shop_trend is None else round(shop_trend, 2),
        months_hint=sorted((m, round(q)) for m, q in monthly.items()),
        live_since=live_since.date().isoformat(),
        history_note=(f'Horoshop history: {hist_min:%d.%m.%Y}–{hist_max:%d.%m.%Y}; live orders — since June 2026.'
                      if hist_min else 'There is no sales history from past years.'),
    )
    return ctx, items


URGENCY_ORDER = {'out': 0, 'urgent': 1, 'soon': 2, 'ok': 3, 'dead': 4}


def candidates(items: list[PurchaseItem], limit: int = 30) -> list[PurchaseItem]:
    """What to consider before ordering: items with a shortfall or with people waiting."""
    pool = [i for i in _eligible(items) if not i.skip_reason]
    pool.sort(key=lambda i: (URGENCY_ORDER[i.urgency], -(i.waiting_subs + i.waiting_likes), -i.need))
    return pool[:limit]


def _eligible(items):
    return [i for i in items
            if i.urgency in ('out', 'urgent', 'soon') and (i.need > 0 or i.waiting_subs or i.waiting_likes)]


def filtered_out(items: list[PurchaseItem]) -> list[PurchaseItem]:
    """Items that would be candidates by stock level, but which the code filtered out as noise (trivial
amount, one-off sales, oversized batch)."""
    return [i for i in _eligible(items) if i.skip_reason]


def dead_stock(items: list[PurchaseItem], limit: int = 15) -> list[PurchaseItem]:
    """What NOT to buy: has been sitting with no sales for 90+ days."""
    pool = [i for i in items if i.urgency == 'dead' and i.stock > 0]
    pool.sort(key=lambda i: -i.stock)
    return pool[:limit]


def product_context(i) -> dict:
    """What the product is: name, brand, categories, description (a few hundred characters), visibility,
when it was added."""
    from django.utils.html import strip_tags
    from catalog.models import Product
    p = Product.objects.filter(pk=i.product_id).select_related('brand').prefetch_related('categories').first()
    if p is None:
        return {}
    return {
        'name': p.name, 'brand': p.brand.name if p.brand_id else None,
        'categories': [c.name for c in p.categories.all()][:4],
        'description': ' '.join(strip_tags(p.description or '').split())[:600],
        'active': bool(p.is_active), 'created': timezone.localtime(p.created_at).date().isoformat(),
    }


def recent_sales(i, limit: int = 12) -> list:
    """Recent sales of the item [(date, quantity)] — to see whether the demand is regular or a clearance of
leftovers."""
    from orders.models import OrderItem
    rows = (OrderItem.objects
            .filter(product_variant_id__in=i.variant_ids, stock_applied=True, order__is_imported=False)
            .exclude(order__status='cancelled').order_by('-order__created_at')
            .values_list('order__created_at', 'quantity')[:limit])
    return [(timezone.localtime(ts).date().isoformat(), qty) for ts, qty in rows]


def alternatives(i, limit: int = 5) -> list:
    """Similar products from this product's narrowest category that the shop currently sells and has in
stock (by sales)."""
    from django.db.models import Count
    from catalog.models import Product, ProductVariant
    product = Product.objects.filter(pk=i.product_id).first()
    if product is None:
        return []
    cats = list(product.categories.annotate(n=Count('products')).order_by('n').values_list('pk', 'n'))
    cat_ids = ([pk for pk, n in cats if n >= 2] or [pk for pk, _n in cats])[:1]
    if not cat_ids:
        return []
    qs = (ProductVariant.objects
          .filter(product__categories__in=cat_ids, is_active=True, is_supplier_fallback=False)
          .exclude(product_id=i.product_id).exclude(product__name__iexact=product.name)
          .select_related('product').distinct())
    variants = [v for v in qs if _available(v)]
    sold = _sibling_sales([v.pk for v in variants], timezone.now() - timedelta(days=RECENT_DAYS))
    ranked = sorted((v for v in variants if sold.get(v.pk)), key=lambda v: -sold[v.pk])[:limit]
    return [{'label': (v.name or v.product.name)[:90], 'kind': _kind(v),
             'stock': None if (v.is_dropshipping and not v.is_hybrid) else int(v.stock or 0),
             'sold_60': sold[v.pk]} for v in ranked]


def monthly_sales(variant_ids: list[int], now: datetime | None = None) -> list[tuple[str, float]]:
    """Monthly sales of the item (import + live) for the last ~26 months — to explain seasonality."""
    now = now or timezone.now()
    hist_min, hist_max = _history_bounds()
    totals = defaultdict(float)
    if hist_min:
        for _vid, ts, qty in _history_rows(variant_ids, [(hist_min, hist_max + timedelta(days=1))]):
            totals[ts.strftime('%Y-%m')] += qty
    for _vid, ts, qty in _live_rows(variant_ids, now - timedelta(days=400)):
        totals[ts.strftime('%Y-%m')] += qty
    return sorted((m, round(q, 2)) for m, q in totals.items())
