"""Purchasing advice from the Analyst → inbox cards.

Two sources of one card (key `purchase:<item>`):
  * RULES — always: the code itself turns the candidates from the calculation (`analyst.purchasing`)
    into cards with a templated explanation. Works without an LLM;
  * AGENT — if enabled and it worked: the model selects what is really worth buying, refines the
    quantity and writes the explanation. Then rule cards for items it did not submit are not shown
    (it deliberately screened them out).
An agent failure (limit, timeout) does not leave a void: the rule cards are shown, and the failure
is reported as a separate finding.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

from django.conf import settings
from django.utils import timezone

from . import purchasing as P
from .checks import Item
from .models import Finding

log = logging.getLogger('analyst')

CHECK_ADVICE = 'purchase_advice'
CHECK_DIGEST = 'purchase_digest'      # the digest is no longer shown; the name remains so that old cards can be closed
CHECK_ORDERED = 'purchase_ordered'    # reference line 'Ordered …': what the owner marked ✓ and is still on its way
PURCHASE_CHECKS = (CHECK_ADVICE, CHECK_DIGEST, CHECK_ORDERED)
MAX_RULE_CARDS = 12
ARRIVAL_SHARE = 0.5                   # stock rose by at least this share of the ordered quantity → we assume it has arrived
SEVERITY = {'high': Finding.CRITICAL, 'normal': Finding.WARN, 'low': Finding.INFO}


@dataclass
class Advice:
    items: list = field(default_factory=list)       # list[Item] for runner.apply
    source: str = 'rules'                            # rules | agent
    error: str = ''
    stats: dict = field(default_factory=dict)


def plural(n: int, forms: tuple) -> str:
    """English plural forms (buyer): (1 buyer, 2 buyers). `forms` = (singular, plural)."""
    n = abs(int(n))
    return forms[0] if n == 1 else forms[1]


def _waits(n: int) -> str:
    return 'is waiting' if abs(int(n)) == 1 else 'are waiting'


def _horizon_text(days: int) -> str:
    months = max(1, round(days / 30))
    return f'over {months} mo.'


def _facts(i: P.PurchaseItem, ctx) -> str:
    """The line of figures under the explanation — in plain words, without internal jargon."""
    h = lambda v: P.human_qty(v, i.unit)  # noqa: E731
    if i.stock > 0:
        parts = [f'in stock: {h(i.stock)}']
    else:
        since = f' since {i.out_since[8:10]}.{i.out_since[5:7]}' if i.out_since else ''
        parts = [f'out of stock{since}']
    if i.forecast > 0:
        parts.append(f'{_horizon_text(ctx.horizon_days)} we expect to sell ≈{h(i.forecast)}')
    if i.cover_days is not None and i.stock > 0:
        parts.append(f'enough for ≈{i.cover_days:.0f} days')
    if i.waiting_subs or i.waiting_likes:
        parts.append(f'waiting: {i.waiting_subs} {plural(i.waiting_subs, ("subscription", "subscriptions"))}, '
                     f'{i.waiting_likes} {plural(i.waiting_likes, ("like", "likes"))}')
    if i.open_po:
        parts.append(f'already ordered: {h(i.open_po)}')
    others = [s for s in i.siblings if s['available']][:3]
    if others:
        parts.append('other pack sizes: ' + '; '.join(P.sibling_line(s, kind=False) for s in others))
    text = ' · '.join(parts)
    return text[:1].upper() + text[1:]      # only the first letter: product names in the line must stay as they are


def _link(i: P.PurchaseItem) -> str:
    return f'/admin/catalog/product/{i.product_id}/change/'


def _rule_reason(i: P.PurchaseItem, ctx) -> str:
    waiting = i.waiting_subs + i.waiting_likes
    if i.stock <= 0 and waiting:
        reason = (f'Out of stock, and {waiting} '
                  f'{plural(waiting, ("buyer", "buyers"))} {_waits(waiting)}.')
    elif i.stock <= 0:
        reason = 'Out of stock, although the season and sales show there is demand.'
    else:
        reason = 'Stock will run out soon at the current sales rate.'
    if any(s['available'] and s['sold_60'] for s in i.siblings):
        reason += ' Other pack sizes of this product are in stock and selling: demand may be partly going through them.'
    return reason


def _card(i: P.PurchaseItem, ctx, *, action: str, qty: float, reason: str, urgency: str, source: str) -> Item:
    title = f'Order {P.human_qty(qty, i.unit)}: {i.label}'
    return Item(
        key=f'purchase:{i.key}', severity=SEVERITY.get(urgency, Finding.WARN), title=title[:200],
        body=f'{reason}\n{_facts(i, ctx)}', link=_link(i), check=CHECK_ADVICE, section=Finding.SECTION_PURCHASE,
        data={'item': i.as_dict(), 'action': action, 'qty': qty if action == 'buy' else 0,
              'unit': i.unit, 'reason': reason, 'urgency': urgency, 'source': source},
    )


def rule_cards(ctx, items) -> list[Item]:
    cards = []
    for i in P.candidates(items, MAX_RULE_CARDS):
        waiting = i.waiting_subs or i.waiting_likes
        if i.recommended <= 0 and (i.open_po > 0 or not waiting):
            continue                                  # no need, or what was ordered is already on its way
        qty = i.recommended or (1000.0 if i.unit in ('g', 'ml') else float(max(i.waiting_subs + i.waiting_likes, 1)))
        # 'High' — only what is truly urgent: a promised delivery (negative stock level)
        # or a stock that is shorter than the delivery lead time.
        urgency = 'high' if (i.stock < 0 or i.urgency == 'urgent') else 'normal'
        cards.append(_card(i, ctx, action='buy', qty=qty, reason=_rule_reason(i, ctx), urgency=urgency, source='rules'))
    return cards


def agent_cards(session, ctx) -> list[Item]:
    """Only what really needs to be ordered right now. We do not show 'Don't order
    yet', 'come back in spring' or the daily summary to the owner:
    this is information nobody keeps in their head, and the table should contain only what is
    important."""
    cards = []
    for key, rec in session.recs.items():
        if rec['action'] != 'buy' or rec['urgency'] == 'low':
            continue
        i = session.items[key]
        reason = rec['reason'] + (f' [{rec["cap_note"]}]' if rec.get('cap_note') else '')
        cards.append(_card(i, ctx, action=rec['action'], qty=rec['qty'], reason=reason,
                           urgency=rec['urgency'], source='agent'))
    return cards


TICK_NOTE = 'Marked ✓ in the Analyst'


def mark_ordered(findings, user_email: str = '') -> int:
    """✓ on an 'Order …' card = the owner has ordered (or taken note). We record the
    purchase in 'Ordered
    purchases': the calculation subtracts it from the need, so the card does not
    repeat while the goods are on the way.
    There is no separate 'Ordered' button — it is the same ✓ as in the rest of the inbox."""
    from .models import PlannedPurchase
    created = 0
    for f in findings:
        data = f.data or {}
        item = data.get('item') or {}
        if f.check_name != CHECK_ADVICE or data.get('action') != 'buy' or not data.get('qty') or not item.get('key'):
            continue
        if PlannedPurchase.objects.filter(finding=f, status=PlannedPurchase.OPEN).exists():
            continue
        stock = item.get('stock')
        PlannedPurchase.objects.create(
            key=item['key'], label=(item.get('label') or 'item')[:300], quantity=str(data['qty']),
            unit=data.get('unit', 'pcs'),
            stock_at_order=stock if isinstance(stock, (int, float)) and not isinstance(stock, bool) else None,
            note=TICK_NOTE, finding=f, created_by=user_email[:254])
        created += 1
    return created


def unmark_ordered(findings) -> int:
    """Restore the card (↩): cancel the record made by its ✓ (manually entered purchases are left alone)."""
    from .models import PlannedPurchase
    return (PlannedPurchase.objects
            .filter(finding__in=list(findings), status=PlannedPurchase.OPEN, note=TICK_NOTE)
            .update(status=PlannedPurchase.CANCELLED))


def unmark_ordered_rows(findings) -> int:
    """↩ on the 'Ordered …' row: cancel these purchases. The need will bring back the regular
'Order …' card."""
    from .models import PlannedPurchase
    ids = [pk for f in findings for pk in ((f.data or {}).get('po_ids') or [])]
    return PlannedPurchase.objects.filter(pk__in=ids, status=PlannedPurchase.OPEN).update(status=PlannedPurchase.CANCELLED)


def settle_received(items, today=None) -> int:
    """Ordered goods that have already arrived stop being subtracted from the need. The arrival signal
    is that the item's stock rose by at least
    ARRIVAL_SHARE of the ordered quantity compared with what it was when the owner clicked ✓. A
    false positive is
    harmless (the Analyst will simply advise again), while without this the ordered amount would
    still be counted for up to 90 days after arrival."""
    by_key = {i.key: i for i in items}
    settled = 0
    for po in P.open_orders(today):
        item = by_key.get(po.key)
        if item is None or po.stock_at_order is None:
            continue
        if item.stock - float(po.stock_at_order) >= ARRIVAL_SHARE * float(po.quantity):
            po.status = po.RECEIVED
            po.note = f'{po.note} · arrived: stock rose'.strip(' ·')
            po.save(update_fields=['status', 'note'])
            settled += 1
    return settled


def ordered_cards(items, cards, today=None) -> list[Item]:
    """We keep as a grey row what the owner marked ✓ and is still on its way: without this the
    'Order …' row would disappear
    together with the need, and there would be nowhere to see what was ordered. An item we advise
    ordering again is shown as a
    regular card (it has the 'already ordered' figures)."""
    advised = {c.key for c in cards}
    by_key = {i.key: i for i in items}
    groups: dict[str, list] = {}
    for po in P.open_orders(today).order_by('ordered_at', 'pk'):
        groups.setdefault(po.key, []).append(po)
    rows = []
    for key, pos in groups.items():
        if f'purchase:{key}' in advised:
            continue
        item = by_key.get(key)
        total, unit = sum(float(p.quantity) for p in pos), pos[0].unit
        since = min(p.ordered_at for p in pos).strftime('%d.%m.%Y')
        rows.append(Item(
            key=f'ordered:{key}', severity=Finding.INFO,
            title=f'Ordered {P.human_qty(total, unit)}: {item.label if item else pos[0].label}'[:200],
            body=f'Marked ✓ {since}. The Analyst does not advise this while the goods are on the way. '
                 f'Once they arrive the row disappears on its own; if you did not order, press ↩.',
            link=_link(item) if item else '', check=CHECK_ORDERED, section=Finding.SECTION_PURCHASE,
            data={'processed': True, 'key': key, 'po_ids': [p.pk for p in pos], 'qty': total, 'unit': unit}))
    return rows


def generate(now=None, *, use_agent: bool | None = None, settle: bool = True) -> Advice:
    """Build the purchasing cards. `use_agent=None` → per the ANALYST_AGENT_ENABLED setting.
    `settle=False` (dry-run): arrived items are not closed — nothing is written to the DB."""
    now = now or timezone.now()
    ctx, items = P.build(now)
    if settle and settle_received(items):
        ctx, items = P.build(now)           # arrived items are no longer subtracted from the need
    wanted_agent = settings.ANALYST_AGENT_ENABLED if use_agent is None else use_agent
    rules = rule_cards(ctx, items)
    if not wanted_agent:
        return Advice(items=rules + ordered_cards(items, rules), source='rules')
    from . import agent
    try:
        session, stats = agent.run(ctx, items)
    except Exception as exc:
        log.warning('analyst: purchasing agent failed: %s', exc, exc_info=True)
        return Advice(items=rules + ordered_cards(items, rules), source='rules', error=f'{type(exc).__name__}: {exc}'[:300])
    cards = agent_cards(session, ctx)
    return Advice(items=cards + ordered_cards(items, cards), source='agent', stats=stats)
