"""The Analyst's LLM purchasing agent.

Division of roles: the NUMBERS (sales rate, season, stock level, coverage, waiting customers,
advertising) are computed by the code — `analyst.purchasing`. The model receives them ready-made
through
READ-ONLY tools, interprets them (is the season declining or ramping up, is it worth buying for the
waiting customers, is advertising coming) and submits recommendations via `submit_recommendation`.
The server validates each of its answers: the quantity may not exceed the calculated one by more
than
1.5 times, a reason is mandatory, an unknown product is rejected. The agent writes nothing to shop
data
and does not order from the supplier.

Safety: `tools=[]` (no built-in tools), only our in-process MCP server, deny-by-default,
`permission_mode='dontAsk'` — the same pattern proven in production as in the bundle instruction
agent.
The tools expose no customers' personal data: counters only.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re

from asgiref.sync import sync_to_async
from django.conf import settings

from . import purchasing as P

log = logging.getLogger('analyst')

MAX_TURNS = 45
TIMEOUT = 600
MAX_RECOMMENDATIONS = 8
CAP_FACTOR = 1.5
MIN_REASON = 15
MAX_REASON = 400
MAX_DIGEST = 900
ACTIONS = ('buy', 'wait')
URGENCIES = ('high', 'normal', 'low')
# Internal words that do not belong in an explanation to the owner (snake_case fields and English
# jargon).
# The owner never specified a delivery lead time (ANALYST_LEAD_DAYS is an assumption), so the
# explanation must not present it as a fact.
# LANGUAGE DATA (Ukrainian): LEAD_CLAIM matches the Ukrainian wording the model uses in `reason` when it
# claims a delivery/lead time (e.g. 'поставка йде 14 днів', 'доставка триває 2 тижні'). The prompt tells the
# model to write `reason` in Ukrainian, so this pattern (and the English-word JARGON filter below) stays as is.
LEAD_CLAIM = re.compile(r'(?:постав\w*|доставк\w*)\s+(?:йде|іде|триває|займає|за\s+\d|\d|(?:\w+\s+){1,2}\d+\s*(?:дн|тиж))'
                        r'|\d+\s*(?:дн\w*|тиж\w*)\s+(?:на\s+)?(?:постав|доставк)\w*'
                        r'|(?:час\w*|термін\w*)\s+(?:постав|доставк)\w*', re.I)
JARGON = re.compile(r'\b[a-z]{2,}(?:_[a-z0-9]+)+\b|\b(?:lead|shop|index|share)\b', re.I)
TOOL_NAMES = ['shop_context', 'list_candidates', 'list_dead_stock', 'item_detail',
              'submit_recommendation', 'finish']

SYSTEM_PROMPT = """You are a careful purchasing analyst for an online shop selling garden and vegetable-garden supplies (Ukraine). The shop buys \
wholesale bags and repacks them into smaller packages itself (it also sells factory-made packages). The owner receives from you ONLY what \
is really worth ordering RIGHT NOW. Better 2-3 precise recommendations than 12 doubtful ones; zero recommendations is a normal result.

MAIN RULE: by default do NOT buy. A shortage of a product in stock is not a reason by itself. Submit an item \
(action=buy) only when you have CHECKED everything below and all of it passes. What does not need buying, or what will make sense only in a \
future season, simply do NOT submit — the owner does not see it, and that is right.

CHECKS BEFORE EVERY buy (for each item call item_detail FIRST):
1. The demand is real, not a clearance of leftovers. People often buy the last pieces that remain: a few sales right \
before the stock ran out are not demand. Look at orders_60 (how many DIFFERENT orders), sales_months_12 (in how many months \
of the year there were sales), top_order_share (1.0 = a single one-off batch), recent_sales (dates and quantities). You need \
regularity: several different orders and several months, not one or two purchases.
2. Season. shop_context (shop_index < 1 — decline), months_hint, monthly_sales: is this selling now or in the coming \
weeks? The season is over or has not started yet — do not submit.
3. Alternatives. other_packagings — other package sizes of THE SAME product; alternatives — similar products that we \
are selling right now anyway. If the customer will happily take another package size, or a similar product is more suitable now — there is no need to buy. \
An item with covered_by must not be bought.
4. Relevance. product: description, categories, active, created — is it a live product of the assortment, not discontinued, not \
outdated.
5. Weight of the purchase. est_revenue_uah — expected revenue over the horizon; lot_size and receipts — what lot was really \
bought according to the delivery notes. A purchase for the sake of a few hundred hryvnias, or a lot that will cover years of sales, is not worth \
the effort — do not submit. Invent nothing about the lot size: if lot_size is empty, say so ("lot size \
unknown") and submit the need in units of the item, not an invented "lot".

ALSO
6. Take all numbers ONLY from the tools. Do not invent sales, stock, lead times, prices. The forecast and the calculated need are already \
computed by the code; the code has already cut off obvious noise (filtered_out_by_code).
7. People who are WAITING (waiting_subs, waiting_likes) are real demand that sales will not show, because the product is out of stock; \
but a single like is not a reason. A negative stock level = a delivery already promised to customers: priority. open_po — already \
ordered, do not duplicate.
8. qty for buy is no more than the calculated need ×1.5 (the server will cap it). urgency: high — the stock will run out before \
the delivery arrives or the stock level is negative; normal — the rest; do not use low.
9. No more than 8 recommendations. reason — ONE or TWO short sentences in plain words, as to a fellow manager in a chat \
(up to 250 characters): what is running out and why buy exactly now. WRITE THE reason IN UKRAINIAN (the owner reads Ukrainian). No field names \
(orders_60, top_order_share, lead, shop_index, covered_by, etc.), no English words and no service terms ("coverage", "top_order_share 0.06"). \
Bad: "coverage 10.5 days at lead 14, 50 orders, top_order_share 0.06". Good: "10 days left. It sells \
every day, and it is exactly the season now." (the examples are shown in English here; your reason must be in Ukrainian). About other package sizes and similar products — only if they affect the decision, \
and in one phrase. Do the checks of points 1-5 IN YOUR HEAD, and in reason write only the conclusion.
The owner did NOT specify a delivery lead time: lead_days is only our assumption for calculating urgency. Never \
write in reason about the delivery or shipping time ("delivery takes 14 days", etc.) — you do not know that.
10. Order: shop_context → list_candidates → item_detail for every item you consider → submit_recommendation \
only for those that passed all the checks → finish with a one-sentence summary (not shown to the owner)."""


def _basis_text(i: P.PurchaseItem, ctx) -> str:
    if i.forecast_basis == 'item_seasonal':
        extra = ''
        if i.trend is not None:
            extra = (f', adjusted for the level of the item itself ×{i.trend}' if i.trend_basis == 'item'
                     else f', adjusted for the level of the shop ×{i.trend}')
        return f'based on the season of past years ({i.history_years} yr of history){extra}'
    if i.forecast_basis == 'recent_shop_season':
        return f'based on the current pace, adjusted for the shop season ×{ctx.shop_index}'
    if i.forecast_basis == 'recent':
        return 'based on the current pace'
    return 'no sales and no seasonal history'


def brief(i: P.PurchaseItem, ctx) -> dict:
    h = lambda v: P.human_qty(v, i.unit)  # noqa: E731
    return {
        'key': i.key, 'name': i.label, 'unit': P.UNIT_LABEL.get(i.unit, i.unit),
        'stock': h(i.stock), 'out_since': i.out_since,
        'sold_last_60d': h(i.recent_60),
        'forecast_horizon': h(i.forecast), 'forecast_basis': _basis_text(i, ctx),
        'cover_days': i.cover_days,
        'waiting_subs': i.waiting_subs, 'waiting_likes': i.waiting_likes,
        'already_ordered': h(i.open_po),
        'orders_60': i.orders_60, 'sales_months_12': i.sales_months_12, 'top_order_share': i.top_order_share,
        'est_revenue_uah': i.est_revenue, 'lot_size': h(i.lot_size) if i.lot_size else None,
        'last_restock': i.receipts[-1][0] if i.receipts else None,
        'calculated_need': h(i.recommended), 'urgency': i.urgency, 'notes': i.notes,
        'other_packagings': [P.sibling_line(s) for s in i.siblings],
        'covered_by': i.covered_by,
    }


class Session:
    """State of a single run: what the agent sees and what it has submitted. All methods are plain
    synchronous code
    (called by the SDK tools from a separate thread and directly by tests)."""

    def __init__(self, ctx: P.ShopContext, items: list[P.PurchaseItem]):
        self.ctx = ctx
        self.items = {i.key: i for i in items}
        self.candidates = P.candidates(items, 25)
        self.filtered = P.filtered_out(items)
        self.dead = P.dead_stock(items)
        self.recs: dict[str, dict] = {}
        self.digest: str | None = None

    # ── reading ──
    def shop_context(self) -> dict:
        c = self.ctx
        return {
            'today': c.today, 'lead_days': c.lead_days, 'cover_days': c.cover_days,
            'lead_days_note': 'an assumption for urgency; the owner did not specify a delivery lead time — do not write about it in reason',
            'horizon_days': c.horizon_days, 'shop_index': c.shop_index, 'shop_trend': c.shop_trend,
            'months_hint': c.months_hint, 'history_note': c.history_note,
            'how_to_read': ('shop_index: by what factor the next horizon sells differently than the last 60 days, '
                            'according to past years (below 1 — decline). shop_trend: the shop\'s current level relative to last year\'s. '
                            'months_hint: the sum of units sold per month (the shape of the season).'),
        }

    def list_candidates(self, limit: int = 20) -> dict:
        limit = max(1, min(int(limit or 20), 25))
        rows = [brief(i, self.ctx) for i in self.candidates[:limit]]
        return {'candidates': rows, 'shown': len(rows), 'total_candidates': len(self.candidates),
                'filtered_out_by_code': len(self.filtered),
                'note': ('The code has already cut off trifles (small revenue), one-off sales and an oversized lot — they are not here. '
                         'Still check each of these candidates through item_detail.')}

    def list_dead_stock(self) -> dict:
        return {'dead_stock': [{'key': i.key, 'name': i.label, 'stock': P.human_qty(i.stock, i.unit),
                                'last_sale': i.last_sale} for i in self.dead],
                'note': 'These items sit without sales for 90+ days — no need to buy more.'}

    def item_detail(self, key: str) -> dict:
        i = self.items.get(key)
        if i is None:
            return {'ok': False, 'problem': f'item "{key}" does not exist'}
        out = brief(i, self.ctx)
        out.update({
            'monthly_sales': P.monthly_sales(i.variant_ids),
            'history_years': i.history_years, 'trend': i.trend, 'trend_basis': i.trend_basis,
            'seasonal_past_60d': i.seasonal_past, 'seasonal_next_horizon': i.seasonal_next,
            'last_sale': i.last_sale, 'variants': len(i.variant_ids),
            'other_packagings_detail': i.siblings,
            'unit_price_uah': i.unit_price,
            'receipts': [{'date': d, 'amount': P.human_qty(a, i.unit)} for d, a in i.receipts],
            'lot_note': (None if i.lot_size else 'the purchase lot size is unknown: there are no receipt notes for this item'),
            'recent_sales': P.recent_sales(i),
            'product': P.product_context(i),
            'alternatives': P.alternatives(i),
        })
        return out

    # ── submitting ──
    def _cap(self, i: P.PurchaseItem) -> float:
        if i.recommended > 0:
            return i.recommended * CAP_FACTOR
        # The need calculation does not see it, but people are waiting: we give the minimum purchase
        # unit.
        return 1000.0 if i.unit in ('g', 'ml') else float(max(i.waiting_subs + i.waiting_likes, 1))

    def submit(self, key, action, qty, reason, urgency='normal') -> dict:
        i = self.items.get(key)
        problems = []
        if i is None:
            return {'ok': False, 'problem': f'item "{key}" does not exist — take the key from the candidate list'}
        action = str(action or '').strip()
        if action not in ACTIONS:
            problems.append('action: buy or wait')
        urgency = str(urgency or 'normal').strip()
        if urgency not in URGENCIES:
            problems.append('urgency: high, normal or low')
        reason = ' '.join(str(reason or '').split())[:MAX_REASON]
        if len(reason) < MIN_REASON:
            problems.append(f'reason: explain the decision in plain words (at least {MIN_REASON} characters)')
        elif JARGON.search(reason):
            problems.append('reason: write in plain words, without field names and English words '
                            '(orders_60, lead, top_order_share, etc.) — as to a fellow manager')
        elif LEAD_CLAIM.search(reason):
            problems.append('reason: do not write about the delivery or shipping time — the owner did not specify it, '
                            'it is only our assumption; explain with what is known: stock, sales, season, those who are waiting')
        final_qty, note = 0.0, None
        if action == 'buy':
            try:
                qty = float(qty)
            except (TypeError, ValueError):
                qty = 0.0
            if qty <= 0:
                problems.append('qty: buy needs a number greater than zero (in the item\'s units)')
            else:
                cap = self._cap(i)
                if qty > cap:
                    note = f'quantity capped to {P.human_qty(P._round_up(cap, i.unit), i.unit)} (ceiling = need ×{CAP_FACTOR})'
                    qty = cap
                final_qty = P._round_up(qty, i.unit)
            if i.urgency == 'dead':
                problems.append('this item has no sales and nobody waiting — there is no point in buying (wait or nothing)')
            if i.skip_reason:
                problems.append(f'not worth buying: {i.skip_reason}')
            if i.covered_by:
                problems.append(f'buying is forbidden: {i.covered_by} — the shortage is covered by a twin (wait or nothing)')
        if problems:
            return {'ok': False, 'problem': '; '.join(problems)}
        if key not in self.recs and len(self.recs) >= MAX_RECOMMENDATIONS:
            return {'ok': False, 'problem': f'already {MAX_RECOMMENDATIONS} recommendations — keep the most important ones'}
        self.recs[key] = {'key': key, 'action': action, 'qty': final_qty, 'reason': reason,
                          'urgency': urgency, 'cap_note': note}
        return {'ok': True, 'saved': len(self.recs), **({'warning': note} if note else {})}

    def finish(self, summary) -> dict:
        text = ' '.join(str(summary or '').split())[:MAX_DIGEST]
        if len(text) < MIN_REASON:
            return {'ok': False, 'problem': 'summary: 1-4 sentences about the main thing today'}
        self.digest = text
        return {'ok': True, 'recommendations': len(self.recs)}


# ── SDK tools ───────────────────────────────────────────────────────

def _content(obj) -> dict:
    return {'content': [{'type': 'text', 'text': json.dumps(obj, ensure_ascii=False, default=str)}]}


def _err(msg: str) -> dict:
    return {'content': [{'type': 'text', 'text': msg}], 'is_error': True}


def build_tools(session: Session) -> list:
    from claude_agent_sdk import tool

    def in_thread(fn, *args):
        # A tool runs in a separate asgiref thread: unless we close its DB connection, it stays
        # open.
        from django.db import connections
        try:
            return fn(*args)
        finally:
            connections.close_all()

    async def run(fn, *args):
        try:
            return _content(await sync_to_async(in_thread)(fn, *args))
        except Exception as exc:    # a tool failure does not crash the run: the agent will see the reason
            log.exception('analyst agent tool failed')
            return _err(f'Tool error: {exc}')

    STR, NUM = {'type': 'string'}, {'type': 'number'}

    @tool('shop_context', 'Shop context: horizon, assumed delivery lead time (nobody specified it), seasonal indices, the shape of the season by month. '
          'Call it first.', {'type': 'object', 'properties': {}})
    async def shop_context(args):
        return await run(session.shop_context)

    @tool('list_candidates', 'Purchase candidates with the numbers already calculated (stock, forecast, coverage, '
          'those waiting, advertising, need). The most important on top.',
          {'type': 'object', 'properties': {'limit': {'type': 'integer'}}})
    async def list_candidates(args):
        return await run(session.list_candidates, args.get('limit', 20))

    @tool('list_dead_stock', 'What sits without sales for 90+ days (do not buy more).',
          {'type': 'object', 'properties': {}})
    async def list_dead_stock(args):
        return await run(session.list_dead_stock)

    @tool('item_detail', 'Item details: monthly sales for ~2 years, seasonal numbers, trend.',
          {'type': 'object', 'properties': {'key': STR}, 'required': ['key']})
    async def item_detail(args):
        return await run(session.item_detail, args.get('key'))

    @tool('submit_recommendation', 'Submit a decision for an item. action: buy (order qty in the item\'s units — '
          'grams/ml/pieces) or wait (do not order yet). urgency: high|normal|low. reason — concise, based on the numbers.',
          {'type': 'object', 'properties': {
              'key': STR, 'action': {'type': 'string', 'enum': list(ACTIONS)}, 'qty': NUM,
              'reason': STR, 'urgency': {'type': 'string', 'enum': list(URGENCIES)}},
           'required': ['key', 'action', 'reason']})
    async def submit_recommendation(args):
        return await run(session.submit, args.get('key'), args.get('action'), args.get('qty'),
                         args.get('reason'), args.get('urgency', 'normal'))

    @tool('finish', 'Finish: a short summary "The main thing today" (1-4 sentences).',
          {'type': 'object', 'properties': {'summary': STR}, 'required': ['summary']})
    async def finish(args):
        return await run(session.finish, args.get('summary'))

    return [shop_context, list_candidates, list_dead_stock, item_detail, submit_recommendation, finish]


# ── run ────────────────────────────────────────────────────────────────

def _thinking() -> dict:
    """The agent must THINK: check several things for each item rather than answer from a first impression."""
    budget = int(getattr(settings, 'ANALYST_THINKING_TOKENS', 0) or 0)
    return {'type': 'enabled', 'budget_tokens': budget} if budget >= 1024 else {'type': 'disabled'}


def _options(session: Session, *, api_key, model):
    from claude_agent_sdk import ClaudeAgentOptions, create_sdk_mcp_server
    from core.llm import _env_for
    opts = dict(
        system_prompt=SYSTEM_PROMPT, model=model,
        tools=[],                                       # no built-in tools at all
        mcp_servers={'analyst': create_sdk_mcp_server(name='analyst', version='1.0.0', tools=build_tools(session))},
        allowed_tools=[f'mcp__analyst__{n}' for n in TOOL_NAMES],
        permission_mode='dontAsk', strict_mcp_config=True, setting_sources=None,
        thinking=_thinking(), max_turns=MAX_TURNS, env=_env_for(api_key),
    )
    explicit = getattr(settings, 'CLAUDE_BIN', '')
    if explicit and os.path.exists(explicit):
        opts['cli_path'] = explicit
    return ClaudeAgentOptions(**opts)


PROMPT = ('Do the daily purchasing review. Start with shop_context, then list_candidates. '
          'Submit the recommendations and complete with finish.')


async def _drive(session: Session, options, prompt: str, timeout: int, stats: dict | None = None) -> dict:
    """One SDK run. `stats` can be passed in from outside: then even on an exception (timeout,
    subscription limit) the caller sees how many
    tokens have already been spent and records them in the budget. The SDK generator is always
    closed (aclosing), otherwise the `claude` process would be left behind."""
    from contextlib import aclosing
    from claude_agent_sdk import AssistantMessage, ResultMessage, ToolUseBlock, query
    from core.llm import _LimitWatch, _tokens_of
    stats = stats if stats is not None else {}
    stats.update({'turns': 0, 'tool_calls': 0, 'tokens': 0, 'subtype': '', 'is_error': False, 'result_text': ''})
    watch = _LimitWatch()
    per_message: dict = {}

    async def consume():
        async with aclosing(query(prompt=prompt, options=options)) as stream:
            async for m in stream:
                watch.feed(m)
                if isinstance(m, AssistantMessage):
                    stats['tool_calls'] += sum(isinstance(b, ToolUseBlock) for b in m.content)
                    if getattr(m, 'message_id', None) and getattr(m, 'usage', None):
                        per_message[m.message_id] = _tokens_of(m)          # one message_id may arrive in several chunks
                        stats['tokens'] = sum(per_message.values())
                elif isinstance(m, ResultMessage):
                    stats.update(turns=getattr(m, 'num_turns', 0), tokens=_tokens_of(m),
                                 subtype=getattr(m, 'subtype', '') or '', is_error=bool(getattr(m, 'is_error', False)),
                                 result_text=str(getattr(m, 'result', '') or '')[:300],
                                 cost_usd=getattr(m, 'total_cost_usd', None))

    try:
        await asyncio.wait_for(consume(), timeout=timeout)
    except asyncio.TimeoutError:
        raise RuntimeError(f'Timeout {timeout}s — the purchasing agent did not finish in time') from None
    return stats


def run(ctx: P.ShopContext, items: list[P.PurchaseItem], *, timeout: int = TIMEOUT) -> tuple[Session, dict]:
    """Agent run. Returns (session with recommendations, statistics). Raises an exception if the agent
    did not complete
    (subscription limit, timeout, no answer at all) — the caller decides what to replace it with
    (see advice.generate)."""
    import time
    from core.limits import enforce_analyst_llm, record_analyst_llm_tokens
    from core.llm import _is_rate_limit, _run_sync

    enforce_analyst_llm()
    model = getattr(settings, 'ANALYST_MODEL', '') or getattr(settings, 'CLAUDE_MODEL', 'sonnet')
    reserve_key = settings.ANTHROPIC_API_KEY or None

    def attempt(api_key):
        session = Session(ctx, items)
        t0, stats = time.time(), {}
        try:
            _run_sync(_drive(session, _options(session, api_key=api_key, model=model), PROMPT, timeout, stats))
        finally:
            if stats.get('tokens'):
                record_analyst_llm_tokens(stats['tokens'])
        stats['seconds'] = round(time.time() - t0, 1)
        return session, stats

    if getattr(settings, 'LLM_PREFER_API', False):
        if not reserve_key:
            raise RuntimeError('LLM_PREFER_API=True, but ANTHROPIC_API_KEY is not set')
        session, stats = attempt(reserve_key)
    else:
        try:
            session, stats = attempt(None)
        except Exception as exc:
            if reserve_key and _is_rate_limit(exc):
                log.warning('Subscription rate-limited, the purchasing agent uses the paid API: %s', exc)
                session, stats = attempt(reserve_key)
            else:
                raise
    if session.digest is None and not session.recs:
        raise RuntimeError('the purchasing agent submitted no recommendations and did not complete the review'
                           + (f' ({stats["subtype"]})' if stats.get('subtype') else ''))
    log.info('analyst agent: %s recommendations, %s', len(session.recs), stats)
    return session, stats
