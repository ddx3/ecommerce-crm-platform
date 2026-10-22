# -*- coding: utf-8 -*-
"""Bundle instruction markup agent: the same result as `instruction_llm.normalize`,
but not as a single "prompt → JSON" shot, rather STEP BY STEP, with tools.

How it differs from the script:
  script — the model answers once; if it gets one number wrong, the WHOLE result is discarded,
           and a human runs it again blindly;
  agent  — the text is split into numbered blocks; the agent works through them in sections, and
  EVERY
           step is checked by code immediately: an invented dose → the block write is rejected on
           the spot,
           the agent fixes it and writes again; only a block whose doses all appear elsewhere can be
           removed;
           "finish" will not pass until every block is processed and the full check has passed.

What the agent does NOT decide: doses. The final word is the same deterministic `validate` as in
the script, and we run it once more, now OUTSIDE the agent (we do not take the agent's word for it).

The agent is allowed only our tools (mcp__doc__*): `tools=[]` disables all built-in ones
(Bash/Read/Write…), `permission_mode='dontAsk'` denies everything not explicitly allowed.
"""
from __future__ import annotations

import asyncio
import html as html_lib
import json
import logging
import os
import re

from asgiref.sync import sync_to_async
from bs4 import BeautifulSoup, NavigableString
from django.conf import settings

from . import instruction_llm as il
from . import instruction_match as im

logger = logging.getLogger(__name__)

MAX_TURNS = 18          # read + sections (write/mark) + finish + fixes
TIMEOUT = 240           # s: nginx holds 300
AUTO_TIMEOUT = 200      # in auto mode the script has already run before the agent (~20–30 s)
ALLOWED_TAGS = {'h3', 'p', 'ul', 'ol', 'li', 'strong', 'u'}
REMOVE_REASONS = {
    'sheet_title': 'sheet title (printed by the system)',
    'composition_list': 'the "Bundle contents" list (printed by the system)',
    'emoji_only': 'a line made only of emoji',
    'self_reference': 'a self-reference to this sheet',
    'image': 'an image (the printed-instruction standard has no <img>)',
    'empty': 'an empty block',
}
_DATA_SRC = re.compile(r'(src=")data:[^"]{200,}(")', re.I)
TOOL_NAMES = ['read_document', 'write_blocks', 'keep_blocks', 'remove_blocks', 'mark_mentions', 'finish']

SYSTEM_PROMPT = (
    'You are an agent-editor of printed bundle instructions for an online shop of garden goods. '
    'You work ONLY through tools (read_document, write_blocks, keep_blocks, remove_blocks, '
    'mark_mentions, finish), step by step, block by block. The editing and markup rules are '
    'in the first user message and are mandatory. Doses and numbers are untouchable: the tools '
    'themselves reject an invented dose. Answer briefly, without explanations: the result of the '
    'tools matters, not words.'
)

PROCEDURE = """═══════════════════════════════════════════════════════════════
HOW TO WORK — you are an agent, acting step by step
═══════════════════════════════════════════════════════════════
In the rules above, "`html`" means the blocks you write (write_blocks), "`changes`" is the `change`
field of the same calls, "`warnings`" is `finish.warnings`, "`mentions`" are the mark_mentions calls,
"the bundle composition above/below" is the "composition" field in read_document. There will be no
JSON response: the result is the state of the blocks.

The sheet text is split into numbered blocks (i = 0, 1, 2…). Everything is done with tools:

1. `read_document` — ONCE at the start: the bundle composition, all blocks, the doses of each block.
2. Work through the blocks in SECTIONS (one treatment / one chapter = 3–8 blocks per step):
   — `write_blocks` — a block you edit or bring to the standard (`html`; if needed, briefly
     `change` — what exactly changed, for the edit log);
   — `keep_blocks` — the block already meets the standard and stays as is;
   — `remove_blocks` — ONLY what the rules above tell you to remove (with a reason).
   EVERY block must be processed in one of these three ways — otherwise `finish` will not pass.
3. `mark_mentions` — mark up the preparations as you move through the sections. `surface` is EXACTLY
   as the preparation is written in the block already written/kept.
4. The tools check the doses themselves. A block was rejected (invented dose, forbidden tag) — fix
   it and write it again; do not bypass the check. The warning "doses disappeared" means: make sure
   it is a duplicate or the composition list, otherwise put the dose back.
5. `finish` — when all blocks are processed. If it rejected (doses) — fix the indicated blocks and
   call `finish` again. If it returned warnings you can eliminate (e.g. a component not found in the
   text — mark it up or explain in `warnings`), eliminate them and call `finish` once more. Stop when
   `finish` returned ok and only remarks that cannot be fixed are left.
   In `finish.warnings` write only what you are unsure about.

Several tool calls in one step are allowed — use this to work faster.
You make all edits ONLY through the tools; do not repeat anything in your reply, say "Done" at the end.
"""

# Sections of the existing script prompt that we carry over to the agent verbatim: the editing,
# dose and markup rules — a single source of truth for both paths (script and agent).
# The titles must match the section headings of prompts/bundle_instruction.txt (a heading is the text
# between two ═══ banners).
_RULE_SECTIONS = ('RULE ZERO', 'WHAT MUST BE REMOVED', 'DOSES AND NUMBERS', 'FORMATTING STANDARD', 'BLOCK mentions')
_BANNER = re.compile(r'^═{10,}[ \t]*\n', re.M)


def _rules() -> str:
    """Rules from the script prompt file (without the JSON response format and the composition
substitutions)."""
    with open(il._prompt_path(), encoding='utf-8') as f:
        parts = _BANNER.split(f.read())
    out = []
    for k in range(1, len(parts) - 1, 2):
        title, body = parts[k].strip(), parts[k + 1].strip()
        if title.startswith(_RULE_SECTIONS):
            out.append(f'═══ {title} ═══\n{body}')
    return '\n\n'.join(out).replace('`{{СКЛАД}}`', 'the "composition" field in read_document')


def split_blocks(html: str) -> list[str]:
    """Top-level HTML → blocks (each <p>/<h3>/<ul>/<ol>… is a separate one). Empty ones are dropped."""
    soup = BeautifulSoup(html or '', 'html.parser')
    blocks = []
    for node in soup.contents:
        if isinstance(node, NavigableString):
            text = str(node).strip()
            if text:
                blocks.append(f'<p>{html_lib.escape(text)}</p>')
        elif il._plain(str(node)):
            blocks.append(str(node))
    return blocks


def _fmt_doses(doses) -> list[str]:
    return sorted(d.replace(':', ' ') for d in doses)


class Session:
    """State of a single run. All methods are synchronous and return dicts — so they are tested
    without a model; `build_tools` connects them to the agent."""

    def __init__(self, name: str, src_html: str, comps):
        self.name = name or ''
        self.src_html = src_html
        self.comps = list(comps or [])
        self.blocks = split_blocks(src_html)
        self.out: dict[int, str] = {}
        self.removed: dict[int, str] = {}
        self.mentions: dict[str, dict] = {}
        self.changes: list[str] = []
        self.result: dict | None = None
        self.last_rejection = ''
        self._fix = il._unit_fix_pairs(self.comps)
        self._comp_doses = il.dose_set(il.composition_region(src_html, self.comps))
        self._comp_pids = {c.variant.product_id for c in self.comps if getattr(c, 'variant', None)}

    # ── helpers ──────────────────────────────────────────────────────────
    def _valid(self, i) -> bool:
        return isinstance(i, int) and not isinstance(i, bool) and 0 <= i < len(self.blocks)

    def _draft(self) -> str:
        """Current text: what has been written, plus the blocks not yet processed — as in the original."""
        return ''.join(
            '' if i in self.removed else self.out.get(i, b) for i, b in enumerate(self.blocks))

    def _pending(self) -> list[int]:
        return [i for i in range(len(self.blocks)) if i not in self.out and i not in self.removed]

    def _touch(self):
        """Any edit after a successful finish cancels it: the result is stale."""
        self.result = None

    def progress(self) -> dict:
        marked = {m['product'] for m in self.mentions.values() if m.get('kind') == 'component' and m.get('product')}
        return {
            'processed': len(self.out) + len(self.removed),
            'of': len(self.blocks),
            'remaining_blocks': self._pending(),
            'marked_preparations': len(self.mentions),
            'components_without_mention': [c.label for c in self.comps
                                      if getattr(c, 'variant', None) and c.variant.product_id not in marked],
        }

    # ── tools ────────────────────────────────────────────────────────────
    def overview(self) -> dict:
        return {
            'bundle': self.name,
            'composition': [{'product_id': c.variant.product_id, 'catalog_name': c.variant.product.name,
                             'short_name': c.label, 'packaging': c.pack, 'pcs_in_bundle': c.quantity}
                            for c in self.comps if getattr(c, 'variant', None)],
            # base64 images are abbreviated in the display: hundreds of KB of text are of no use to
            # the agent
            'blocks': [{'i': i, 'html': _DATA_SRC.sub(r'\1data:(image)\2', b), 'doses': _fmt_doses(il.dose_set(b))}
                       for i, b in enumerate(self.blocks)],
            'box_contents_list_doses': _fmt_doses(self._comp_doses),
        }

    @staticmethod
    def _markup_problems(html: str) -> list[str]:
        soup = BeautifulSoup(html, 'html.parser')
        out = []
        bad = sorted({t.name for t in soup.find_all(True)} - ALLOWED_TAGS)
        if bad:
            out.append('forbidden tags: ' + ', '.join(bad) + ' (allowed: h3 p ul ol li strong u)')
        if any(t.attrs for t in soup.find_all(True)):
            out.append('tags must have no attributes (style/class are forbidden)')
        return out

    def _check_block(self, i: int, html: str) -> tuple[list[str], list[str]]:
        hard, soft = self._markup_problems(html), []
        if not il._plain(html):
            hard.append('empty result — to remove a block, use remove_blocks')
        src, out = il.dose_set(self.blocks[i]), il.dose_set(html)
        # A unit-of-measure fix allowed by the script (the number matches the pack size).
        for wrong, correct in self._fix.items():
            if wrong in src and correct in out and wrong not in out:
                src = (src - {wrong}) | {correct}
        invented = out - src
        merged = {d for d in invented if il._is_merged_range(d, src)}
        invented -= merged
        if invented:
            hard.append('invented doses that are not in the original block: '
                        + ', '.join(_fmt_doses(invented)) + ' — the same numbers must stay')
        lost = {d for d in src - out if not il._is_range_endpoint(d, merged)}
        if lost:
            soft.append('doses disappeared from the block: ' + ', '.join(_fmt_doses(lost))
                        + ' — this is acceptable only for a duplicate or the composition list; otherwise put them back')
        return hard, soft

    def write(self, items) -> dict:
        results = []
        for it in items or []:
            i = it.get('i') if isinstance(it, dict) else None
            if not self._valid(i):
                results.append({'i': i, 'ok': False, 'problem': 'no such block'})
                continue
            html = str(it.get('html') or '').strip()
            hard, soft = self._check_block(i, html)
            if hard:
                results.append({'i': i, 'ok': False, 'problem': '; '.join(hard)})
                continue
            self.out[i] = html
            self.removed.pop(i, None)
            self._touch()
            change = str(it.get('change') or '').strip()
            if change:
                self.changes.append(change)
            results.append({'i': i, 'ok': True, **({'note': '; '.join(soft)} if soft else {})})
        return {'results': results, **self.progress()}

    def keep(self, indexes) -> dict:
        results = []
        for i in indexes or []:
            if not self._valid(i):
                results.append({'i': i, 'ok': False, 'problem': 'no such block'})
                continue
            problems = self._markup_problems(self.blocks[i])
            if problems:     # "leave as is" — only what already conforms to the standard
                results.append({'i': i, 'ok': False, 'problem': 'the block does not meet the standard: '
                                + '; '.join(problems) + ' — format it via write_blocks'
                                + (' or remove it (remove_blocks, reason=image)' if 'img' in self.blocks[i] else '')})
                continue
            self.out[i] = self.blocks[i]
            self.removed.pop(i, None)
            self._touch()
            results.append({'i': i, 'ok': True})
        return {'results': results, **self.progress()}

    def remove(self, items) -> dict:
        results = []
        for it in items or []:
            i = it.get('i') if isinstance(it, dict) else None
            reason = it.get('reason') if isinstance(it, dict) else None
            if not self._valid(i):
                results.append({'i': i, 'ok': False, 'problem': 'no such block'})
                continue
            if reason not in REMOVE_REASONS:
                results.append({'i': i, 'ok': False,
                                'problem': 'the reason must be one of: ' + ', '.join(REMOVE_REASONS)})
                continue
            rest = ' '.join(b for j, b in enumerate(self.blocks) if j != i)
            unique = il.dose_set(self.blocks[i]) - il.dose_set(rest) - self._comp_doses
            if unique:
                results.append({'i': i, 'ok': False,
                                'problem': 'the block has doses that are nowhere else (' + ', '.join(_fmt_doses(unique))
                                           + ') — this cannot be removed; process the block via write_blocks'})
                continue
            self.removed[i] = reason
            self.out.pop(i, None)
            self._touch()
            self.changes.append(f'removed block #{i + 1}: {REMOVE_REASONS[reason]}')
            results.append({'i': i, 'ok': True})
        return {'results': results, **self.progress()}

    def mark(self, items) -> dict:
        from .models import Product
        folded_draft = im.fold(il._plain(self._draft()))
        results = []
        for it in items or []:
            it = it if isinstance(it, dict) else {}
            surface = str(it.get('surface') or '').strip()
            kind = it.get('kind')
            product = it.get('product')
            doses = [str(d).strip() for d in (it.get('doses') or []) if str(d).strip()]
            problems = []
            if not surface:
                problems.append('empty surface')
            if kind not in ('component', 'external'):
                problems.append('kind: component or external')
            if product is not None and not (isinstance(product, int) and not isinstance(product, bool)):
                problems.append('product is a number (product id) or null')
            elif kind == 'component' and product is None:
                problems.append('component requires product (an id from the composition)')
            elif kind == 'component' and product not in self._comp_pids:
                problems.append(f'product #{product} is not from the bundle composition — set external or product=null')
            elif product is not None and not Product.objects.filter(pk=product).exists():
                problems.append(f'product #{product} does not exist in the catalog')
            if surface and im.fold(surface) not in folded_draft:
                problems.append('there is no such string in the current text — first write the block with this name '
                                'and take the surface verbatim')
            if problems:
                results.append({'surface': surface, 'ok': False, 'problem': '; '.join(problems)})
                continue
            self.mentions[im.fold(surface)] = {
                'surface': surface, 'product': product, 'kind': kind, 'doses': doses}
            self._touch()
            results.append({'surface': surface, 'ok': True})
        return {'results': results, **self.progress()}

    def finish(self, warnings=None) -> dict:
        pending = self._pending()
        if pending:
            return {'ok': False, 'problem': f'blocks not processed: {pending}',
                    'hint': 'process them (write_blocks / keep_blocks / remove_blocks) and call finish again'}
        out_html = self._draft()
        mentions = list(self.mentions.values())
        try:
            warns = il.validate(self.src_html, out_html, mentions, self.comps)
        except il.Rejected as e:
            self.last_rejection = str(e)
            self.result = None
            return {'ok': False, 'rejected': str(e),
                    'hint': 'fix the relevant blocks via write_blocks (restore the lost dose or remove the '
                            'invented one) and call finish again'}
        agent_warns = [str(w).strip() for w in (warnings or []) if str(w).strip()]
        self.result = {'html': out_html, 'mentions': mentions, 'changes': list(self.changes),
                       'warnings': warns + agent_warns}
        return {'ok': True, 'remarks': warns,
                'hint': 'If the remarks include something that can be fixed (e.g. a component not found in the text), '
                        'fix it and call finish once more; otherwise finish.'}


# ── tools for the SDK ─────────────────────────────────────────────────────

def _content(obj) -> dict:
    return {'content': [{'type': 'text', 'text': json.dumps(obj, ensure_ascii=False)}]}


def _err(msg: str) -> dict:
    return {'content': [{'type': 'text', 'text': msg}], 'is_error': True}


def build_tools(session: Session) -> list:
    from claude_agent_sdk import tool

    def in_thread(fn, *a):
        # The tool runs in a separate asgiref thread: unless its DB connection is closed, it stays
        # open forever (in tests this prevented dropping the test database).
        from django.db import connections
        try:
            return fn(*a)
        finally:
            connections.close_all()

    async def run(fn, *a):
        try:
            return _content(await sync_to_async(in_thread)(fn, *a))
        except Exception as e:      # a tool failure must not bring down the whole run — the agent will see the reason
            logger.exception('instruction agent tool failed')
            return _err(f'Tool error: {e}')

    arr = lambda items: {'type': 'array', 'items': items}  # noqa: E731
    INT, STR = {'type': 'integer'}, {'type': 'string'}

    @tool('read_document', 'Read the sheet: the bundle composition, all blocks (i, html, doses), the doses of the contents list. '
          'Call once at the start.', {'type': 'object', 'properties': {}})
    async def read_document(args):
        return await run(session.overview)

    @tool('write_blocks', 'Write the cleaned/formatted HTML of blocks. Allowed tags: h3 p ul ol li strong u, '
          'without attributes. An invented dose → the block is rejected. change — what changed (for the log).',
          {'type': 'object', 'properties': {'blocks': arr({'type': 'object', 'properties': {
              'i': INT, 'html': STR, 'change': STR}, 'required': ['i', 'html']})}, 'required': ['blocks']})
    async def write_blocks(args):
        return await run(session.write, args.get('blocks'))

    @tool('keep_blocks', 'Keep blocks as in the original (they already meet the standard).',
          {'type': 'object', 'properties': {'blocks': arr(INT)}, 'required': ['blocks']})
    async def keep_blocks(args):
        return await run(session.keep, args.get('blocks'))

    @tool('remove_blocks', 'Remove blocks that the rules tell you to remove. reason: '
          + ', '.join(REMOVE_REASONS) + '. A block with unique doses cannot be removed.',
          {'type': 'object', 'properties': {'blocks': arr({'type': 'object', 'properties': {
              'i': INT, 'reason': {'type': 'string', 'enum': list(REMOVE_REASONS)}}, 'required': ['i', 'reason']})},
           'required': ['blocks']})
    async def remove_blocks(args):
        return await run(session.remove, args.get('blocks'))

    @tool('mark_mentions', 'Mark up the preparations: surface (the string exactly as in the text), product (id from the composition or null), '
          'kind (component|external), doses (the doses of this preparation verbatim).',
          {'type': 'object', 'properties': {'mentions': arr({'type': 'object', 'properties': {
              'surface': STR, 'product': {'type': ['integer', 'null']},
              'kind': {'type': 'string', 'enum': ['component', 'external']}, 'doses': arr(STR)},
              'required': ['surface', 'kind']})}, 'required': ['mentions']})
    async def mark_mentions(args):
        return await run(session.mark, args.get('mentions'))

    @tool('finish', 'Finish: the full check of doses and markup. If rejected — fix and call again. '
          'warnings — only what you are unsure about.',
          {'type': 'object', 'properties': {'warnings': arr(STR)}})
    async def finish(args):
        return await run(session.finish, args.get('warnings'))

    return [read_document, write_blocks, keep_blocks, remove_blocks, mark_mentions, finish]


# ── run ────────────────────────────────────────────────────────────────

def _options(session: Session, *, api_key, model):
    from claude_agent_sdk import ClaudeAgentOptions, create_sdk_mcp_server
    from core.llm import _env_for
    opts = dict(
        system_prompt=SYSTEM_PROMPT, model=model,
        tools=[],                                       # no built-in tools at all
        mcp_servers={'doc': create_sdk_mcp_server(name='doc', version='1.0.0', tools=build_tools(session))},
        allowed_tools=[f'mcp__doc__{n}' for n in TOOL_NAMES],
        permission_mode='dontAsk', strict_mcp_config=True, setting_sources=None,
        thinking={'type': 'disabled'}, max_turns=MAX_TURNS, env=_env_for(api_key),
    )
    explicit = getattr(settings, 'CLAUDE_BIN', '')
    if explicit and os.path.exists(explicit):
        opts['cli_path'] = explicit
    return ClaudeAgentOptions(**opts)


def _prompt(session: Session) -> str:
    return (f'{_rules()}\n\n{PROCEDURE}\n'
            f'Bundle: {session.name or "(no name)"}. Blocks in the text: {len(session.blocks)}. '
            f'Start with read_document.')


async def _drive(session: Session, options, prompt: str, timeout: int) -> dict:
    from claude_agent_sdk import AssistantMessage, ResultMessage, ToolUseBlock, query
    from core.llm import _LimitWatch, _tokens_of
    stats = {'turns': 0, 'tool_calls': 0, 'tokens': 0, 'subtype': ''}
    watch = _LimitWatch()

    async def consume():
        async for m in query(prompt=prompt, options=options):
            watch.feed(m)
            if isinstance(m, AssistantMessage):
                stats['tool_calls'] += sum(isinstance(b, ToolUseBlock) for b in m.content)
            elif isinstance(m, ResultMessage):
                stats.update(turns=getattr(m, 'num_turns', 0), tokens=_tokens_of(m),
                             subtype=getattr(m, 'subtype', '') or '')

    try:
        await asyncio.wait_for(consume(), timeout=timeout)
    except asyncio.TimeoutError:
        raise RuntimeError(f'Timeout {timeout}s — the agent did not manage to process the instruction') from None
    return stats


def normalize_by_mode(bv, components=None, mode: str = 'agent', *, auto_timeout: int = AUTO_TIMEOUT) -> dict:
    """The single entry point "run the instruction" for the button, the scheduler and one-off commands.

    agent  — always the agent;
    auto   — the cheap script first, and ONLY when its result is rejected by the dose check
             does the agent finish the job (other failures — timeout, subscription limit — are not
             passed
             to the agent: it would only lengthen the wait);
    script — a single script call (also for any unknown value)."""
    if mode == 'agent':
        return normalize_agent(bv, components)
    if mode == 'auto':
        original = bv.instruction_html          # normalize is fixed "in place" (un-gluing of "+"/"=")
        try:
            return il.normalize(bv, components)
        except il.Rejected as first:
            logger.info('instruction auto: the script was rejected ("%s": %s) — handing over to the agent', bv.name, first)
            bv.instruction_html = original
            return normalize_agent(bv, components, timeout=auto_timeout)
    return il.normalize(bv, components)


def normalize_agent(bv, components=None, timeout: int = TIMEOUT) -> dict:
    """The same as `instruction_llm.normalize`: {'html', 'mentions', 'changes', 'warnings'} (+ 'stats')."""
    import time
    from core.limits import enforce_llm, record_llm_tokens
    from core.llm import _is_rate_limit, _run_sync
    from . import instruction_resolve as ir

    if components is None:
        components = ir.components_of_template(bv)
    if not (bv.instruction_html or '').strip():
        raise il.Rejected('the instruction is empty — there is nothing to normalize')
    bv.instruction_html = il._space_out_operators(bv.instruction_html)   # as in the script: mechanically, before the model
    src = bv.instruction_html
    if not split_blocks(src):
        raise il.Rejected('the instruction is empty — there is nothing to normalize')

    enforce_llm()
    model = getattr(settings, 'CLAUDE_MODEL', 'sonnet')
    reserve_key = settings.ANTHROPIC_API_KEY or None

    def attempt(api_key):
        session = Session(bv.name, src, components)
        t0 = time.time()
        stats = {}
        try:
            stats = _run_sync(_drive(session, _options(session, api_key=api_key, model=model),
                                     _prompt(session), timeout))
        finally:
            if stats.get('tokens'):
                record_llm_tokens(stats['tokens'])
        stats['seconds'] = round(time.time() - t0, 1)
        return session, stats

    if getattr(settings, 'LLM_PREFER_API', False):
        if not reserve_key:
            raise RuntimeError('LLM_PREFER_API=True, but ANTHROPIC_API_KEY is not set')
        session, stats = attempt(reserve_key)
    else:
        try:
            session, stats = attempt(None)
        except Exception as e:
            if reserve_key and _is_rate_limit(e):
                logger.warning('The subscription is rate-limited, the instruction agent switches to the paid API: %s', e)
                session, stats = attempt(reserve_key)
            else:
                raise

    if session.result is None:
        why = session.last_rejection or (
            'the agent ran out of steps' if stats.get('subtype') == 'error_max_turns'
            else 'the agent did not finish the analysis (no successful finish)')
        raise il.Rejected(why)

    # We do not take the agent's word for it: the full check once more, now outside the agent.
    res = session.result
    warns = il.validate(src, res['html'], res['mentions'], components)
    agent_only = [w for w in res['warnings'] if w not in warns]
    logger.info('instruction agent "%s": %s', bv.name, stats)
    return {'html': res['html'], 'mentions': res['mentions'], 'changes': res['changes'],
            'warnings': warns + agent_only, 'stats': stats}
