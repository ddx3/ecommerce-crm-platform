# -*- coding: utf-8 -*-
"""Normalization and markup of a bundle's instructions via an LLM.

What the model does: fixes typos, brings repeated blocks to a uniform form and MARKS UP
mentions of products: by what name each one is called in this text and which catalog
product it is. The markup is needed for auto-replacement: when a component has been
swapped, ALL of its mentions must be found, not guessed by a heuristic.

What the model does NOT do: it does not invent or recalculate doses. These are pesticides,
and a mixed-up concentration means a burnt bush for the customer. So the model's text goes
through deterministic checks (`validate`), and if even one dose appears, disappears or
changes, the result is rejected ENTIRELY, with no 'partial application'.

The model here is an editor's assistant, not a source of truth: everything it returns is
laid next to the original for a human to review, not put straight into the field.
"""
from __future__ import annotations

import json
import os
import re

from django.conf import settings

from . import instruction_match as im

PROMPT_FILE = 'bundle_instruction.txt'
PROMPT_VERSION = 1

_TAG_RE = re.compile(r'<[^>]+>')
# A dose: number + unit. Ranges ("8–10 l") are caught as two numbers with one
# unit, so they are normalized separately below.
# LANGUAGE DATA: the units are the Ukrainian abbreviations used in the instruction texts
# (мл = ml, мг = mg, кг = kg, гр/г = g, л = l, шт = pcs).
_DOSE_RE = re.compile(
    r'(\d+(?:[.,]\d+)?)\s*(?:[–—-]\s*(\d+(?:[.,]\d+)?)\s*)?(мл|мг|кг|гр|г|л|шт|%)(?![^\W\d_])',
    re.IGNORECASE)


def _plain(html: str) -> str:
    """Text without tags and without HTML entities.

    Entities must be decoded: Quill generously emits `&nbsp;`, and the line
    "Magnesium sulfate 30 g&nbsp;" was not recognized as a list line:
    it looked as if some description followed the dose. Because of this, perfectly correct
    runs were being rejected.
    """
    import html as _html
    return re.sub(r'\s+', ' ', _html.unescape(_TAG_RE.sub(' ', html or ''))).strip()


# How authors label the list of the box contents. A single "Bundle contents" is not enough:
# real instructions also contain "In the bundle:" and "Included in the set". Because of this
# bv180 was rejected for no reason: the model removed the block as instructed, while the check
# considered its doses lost.
# LANGUAGE DATA: the patterns below match the Ukrainian labels typed by the shop staff
# (склад комплекту = bundle contents, у комплекті = in the bundle, до складу входить = the
# composition includes, комплект містить = the bundle contains, що входить = what is included).
_COMP_HEAD_RE = re.compile(
    r'(?:склад\s+комплекту|у\s+комплект[іi]\s*:|до\s+складу\s+вход|'
    r'комплект\s+м[іi]стить|що\s+входить)', re.IGNORECASE)


# A list label as a SEPARATE block: "CONTENTS:", "CONTENTS", "PACKAGE CONTENTS". This is the
# most common way to label it, and with a single word, so it can't go into the general
# `_COMP_HEAD_RE`: the bare word "contents" also occurs in a sentence ("the tank mix includes…"),
# and confusing a label with a sentence would cut off live text.
# LANGUAGE DATA: Ukrainian labels (склад = composition, вміст = contents, комплектація = package
# contents, набір = set, у комплекті = in the bundle, у складі = in the composition,
# що у коробці = what is in the box).
_COMP_HEAD_SOLO_RE = re.compile(
    r'^(?:склад|вм[іi]ст|комплектац[іi]я|наб[іi]р|у\s+комплект[іi]|у\s+склад[іi]|'
    r'що\s+[ув]\s+коробц[іi])\s*:?\s*$', re.IGNORECASE)

# Start of a list line: name, quantity and (optionally) how many packs.
# "Product A — 3 ml", "Product B, 1.4 g", "Product A 3 ml - 2 pieces", "Potassium sulfate 100 g 2 pcs.".
# LANGUAGE DATA: the units and the "pieces" words are Ukrainian (штуки/штук/шт = pieces,
# одиниці/од = units).
_COMP_ROW_HEAD_RE = re.compile(
    r'^[«"]?[\w\'ʼ\-().:/+]+(?:\s+[\w\'ʼ\-().:/+]+){0,4}[»"]?'
    r'\s*[—–\-,:×xх]?\s*'
    # A boundary after the unit is mandatory: without it the short "pcs" word was caught inside the
    # longer "pieces" word, and the line "Product A 3 ml - 2 pieces" was parsed as name "Product A 3 ml -"
    # with dose "2 pcs" and a leftover tail.
    r'\d+(?:[.,]\d+)?\s*(?:мл|мг|кг|гр|г|л|шт)(?![^\W\d_])\.?'
    # "- 2 pieces", "× 2", "x2", "(2 pcs)", "2 units": the number of packs in the box.
    # Longer forms FIRST: otherwise "pieces"/"units" is caught on its abbreviation, and the tail
    # after it breaks the line parsing.
    r'(?:\s*[—–\-]?\s*\(?\s*(?:[×xх]\s*)?\d+\s*(?:штуки|штук|шт|одиниц[ьі]|од)?'
    r'(?![^\W\d_])\.?\s*\)?)?',
    re.IGNORECASE)

# A quantity with a unit WITHOUT a percent: "83%" at the end of a line is the active
# substance content in the product, not a treatment dose.
_QTY_RE = re.compile(r'\d+(?:[.,]\d+)?\s*(?:мл|мг|кг|гр|г|л|шт)(?![^\W\d_])',
                     re.IGNORECASE)


def _is_comp_row(line: str) -> bool:
    """Whether this is a line of the box contents list.

    Authors write them not only as "Product A — 3 ml": often a description follows the quantity:
    "Product B 500 ml - Copper oxychloride", "Product C 30 D 900 ml - 3 pieces - vegetable oil 83%".
    Such a description is part of the list, and requiring the line to end at the dose would
    fail to recognize half of the live texts.

    We tell it apart from a treatment PROGRAM line by three signs:
      — a mix has '+' and a total '=', a list never has them;
      — the description is set off by a punctuation mark, rather than simply continuing a sentence;
      — there is no SECOND dose in the description: "Product A 3 ml per 10 l" is already a treatment.
    """
    if '+' in line or '=' in line:
        return False
    m = _COMP_ROW_HEAD_RE.match(line)
    if not m:
        return False
    tail = line[m.end():].strip().lstrip(')').strip()
    if not tail:
        return True
    if not re.match(r'^[-–—,;:(]', tail):
        return False
    return not _QTY_RE.search(tail)


def _blocks(html: str) -> list[str]:
    """Text block by block: each <p>/<li>/<h*> is a separate line.

    Block by block is exactly what's needed: `_plain` glues everything into one line, and the
    box contents list becomes indistinguishable from the rest of the text.
    """
    parts = re.split(r'(?i)</(?:p|li|h[1-6]|div|tr|td)>|<br\s*/?>', html or '')
    return [_plain(p) for p in parts]


def _composition_start(html: str, components=None) -> int | None:
    """Index of the block where the box contents list starts. None means there is none.

    First we look for a label. But relying on it alone is not possible: there are more labels
    in live texts than can be listed ("What's in the box", "Package contents", "Contents"),
    and the cost of a miss is high: package weights
    are counted as treatment doses, and when the model removes the block (as it is told to),
    the check cries 'lost doses' on a perfectly correct result.

    So there is also parsing by STRUCTURE: a text tail made of 'name — quantity' lines and
    nothing else. So that a treatment program doesn't accidentally fall under this rule, we
    require at least two such lines in a row at the VERY END, and when the composition is
    known, also that the names in them match the box's products.
    """
    blocks = _blocks(html)
    for i, b in enumerate(blocks):
        if not b:
            continue
        if _COMP_HEAD_RE.search(b):
            return i
        # A single-word label is accepted only when a list really follows it: "CONTENTS:"
        # in the middle of the text must not cut off the rest of the instructions.
        if _COMP_HEAD_SOLO_RE.match(b):
            after = [x for x in blocks[i + 1:] if x]
            rows = sum(1 for x in after if _is_comp_row(x))
            if len(after) >= 2 and rows * 2 >= len(after):
                return i
            # A list also occurs as a solid paragraph, without line breaks:
            # "Product A 250 g Product B 100 g - 3 pieces Magnesium sulfate 30 g …".
            # The signs are the same as for a line: several quantities, no mix
            # ("+", "=") and names from the box. Without a known composition we don't guess.
            joined = ' '.join(after)
            if (components and len(_QTY_RE.findall(joined)) >= 2
                    and '+' not in joined and '=' not in joined
                    and _names_hit(joined, components) >= 2):
                return i

    idx = [i for i, b in enumerate(blocks) if b]
    if not idx:
        return None
    rows = []
    for i in reversed(idx):
        if not _is_comp_row(blocks[i]):
            break
        rows.append(i)
    if len(rows) < 2:
        return None

    if components:
        labels = [fold_label(getattr(c, 'label', '') or getattr(c, 'name', ''))
                  for c in components]
        labels = [x for x in labels if x]
        hit = sum(1 for i in rows
                  if any(lb and lb in im.fold(blocks[i]) for lb in labels))
        if hit * 2 < len(rows):
            return None

    start = min(rows)
    # The label above the list ("What's in the box:") is taken along with it:
    # it is short and contains no doses itself.
    prev = [i for i in idx if i < start]
    if prev:
        p = blocks[prev[-1]]
        if len(p) <= 40 and not _DOSE_RE.search(p):
            start = prev[-1]
    return start


def _names_hit(text: str, components) -> int:
    """How many of the box's products are named in this piece of text.

    By ANCHOR WORDS, not by the whole label: in the catalog the name often comes with a
    storefront prefix ("Fertilizer Deciduous plants. Osmocote Exact Standard
    15-9-12+2MgO+TE"), while in the instruction text it is
    shortened ("Osmocote Standard"). Requiring a match of the WHOLE label would fail to
    recognize any such product; that is exactly how bv105 failed to see its box contents list.
    """
    folded = im.fold(text or '')
    seen = set()
    for c in components or []:
        name = getattr(c, 'name', '') or getattr(c, 'label', '')
        for w in im.anchor_words_of_name(name):
            if w in folded:
                seen.add(name)
                break
    return len(seen)


def fold_label(name: str) -> str:
    """Product name in a form suitable for searching in the text of a list line."""
    base = re.split(r'\s*[,(]', (name or '').strip())[0]
    return im.fold(base.strip())


def composition_region(html: str, components=None) -> str:
    """The piece of text that is the box contents list (from the label to the end).

    This is exactly what the model removes by task, and doses legitimately disappear from it:
    these are package weights, not treatment doses.
    """
    i = _composition_start(html, components)
    return ' '.join(b for b in _blocks(html)[i:] if b) if i is not None else ''


def cut_composition(html: str, components=None) -> str:
    """Text WITHOUT the box contents list."""
    blocks = _blocks(html)
    i = _composition_start(html, components)
    return ' '.join(b for b in (blocks if i is None else blocks[:i]) if b)


# NPK formula in a fertilizer name: "Booster 0:25:50", "Booster 5-15-45",
# "Master 18-18-18". It is PART OF THE NAME, not a dose, and must be removed before
# searching for doses: in "Booster 0:25:50 — 25 g" the pair "50 — 25" was read as a range,
# and the check rejected a perfectly correct model result.
_NPK_RE = re.compile(r'\b\d{1,2}\s*[:\-–—]\s*\d{1,2}\s*[:\-–—]\s*\d{1,2}\b')


def dose_set(html: str) -> set[str]:
    """The set of doses of a text in canonical form: "8–10 л" -> '8-10:л', "50г" -> '50:г'
    (the unit stays Ukrainian, see _DOSE_RE).

    We compare exactly the SET, not the sequence: normalization may rearrange blocks and remove
    a duplicate mention of the same dose, but may neither lose a dose nor invent a new one.
    """
    out = set()
    text = _NPK_RE.sub(' ', _plain(html))
    for m in _DOSE_RE.finditer(text):
        a, b, unit = m.group(1), m.group(2), m.group(3)
        a = _num(a)
        b = _num(b) if b else None
        num = f'{a}-{b}' if b else a
        out.add(f'{num}:{unit.lower()}')
    return out


def _num(s: str) -> str:
    """'5,0' -> '5', '30' -> '30'.

    Trailing zeros are stripped ONLY from fractional numbers. Previously `rstrip('0')` cut
    integers too: 30 became 3, 100 became 1, 250 became 25. Because of this the check
    considered "30 g" and "3 g" the same dose and would silently have let a substitution
    through, which is exactly what it exists to stop.
    """
    s = s.replace(',', '.')
    if '.' in s:
        s = s.rstrip('0').rstrip('.')
    return s or '0'


def _is_merged_range(dose: str, source: set) -> bool:
    """Whether '25-30:г' is a merge of two adjacent doses of the original "25 g" and "30 g".

    Authors repeatedly write a range with an extra space: "Potassium sulfate 25 g -30 g".
    The model reduces this to "25–30 g": essentially correct, but formally it's a new dose,
    and the strict guard rejected a perfectly valid edit.

    We accept ONLY when both ends of the range are really in the original and with the same
    unit. That is, the model invented nothing, it merely put together what was already there.
    The case still goes into a warning: two NON-adjacent doses may be merged too, and a human
    must see that.
    """
    num, _, unit = dose.partition(':')
    if '-' not in num:
        return False
    lo, _, hi = num.partition('-')
    return f'{lo}:{unit}' in source and f'{hi}:{unit}' in source


def _is_range_endpoint(dose: str, merged: set) -> bool:
    """Whether '25:г' is the end of one of the accepted ranges ('25-30:г').

    The flip side of merging: when the model reduces "25 g -30 g" to "25–30 g", both original
    doses disappear from the text. This is not a loss: they are in place, just written as a
    range we have already recognized as legitimate.
    """
    num, _, unit = dose.partition(':')
    for m in merged:
        mnum, _, munit = m.partition(':')
        if munit == unit and num in mnum.split('-'):
            return True
    return False


def _unit_fix_pairs(components) -> dict[str, str]:
    """(number:wrong_unit) -> (number:packaging) for each component.

    Authors sometimes confuse the unit of measure of a product with the one actually on the
    package: "Product A 40 g" in the text while the packaging is "40 ml". The NUMBER does not
    change: it's a confusion in the notation of the release form, not a different dose. The
    owner allowed the model to fix exactly this case (2026-09-05): 'change g to ml if the
    product is in ml, or vice versa, is allowed'. A general unit conversion (ml -> g by
    density, etc.) is NOT allowed by this, only a match of the number with the packaging.
    """
    _UNITS = ('мл', 'мг', 'кг', 'гр', 'г', 'л', 'шт')   # Ukrainian unit abbreviations (see _DOSE_RE)
    out = {}
    for c in components or []:
        pack = getattr(c, 'pack', '') or ''
        m = re.match(r'(\d+(?:[.,]\d+)?)\s*(мл|мг|кг|гр|г|л|шт)$', pack, re.IGNORECASE)
        if not m:
            continue
        num, correct = _num(m.group(1)), m.group(2).lower()
        for wrong in _UNITS:
            if wrong != correct:
                out[f'{num}:{wrong}'] = f'{num}:{correct}'
    return out


def _dose_context(text: str, dose: str, width: int = 34) -> str:
    """A piece of the original around the dose, so that a rejection can be read."""
    num, _, unit = dose.partition(':')
    num = num.split('-')[0].replace('.', '[.,]')
    m = re.search(num + r'\s*' + re.escape(unit), text or '', re.IGNORECASE)
    if not m:
        return ''
    a = max(0, m.start() - width)
    b = min(len(text), m.end() + width)
    return f' (in the text: "…{text[a:b].strip()}…")'


class Rejected(Exception):
    """The model's result failed validation and cannot be applied."""


def validate(src_html: str, out_html: str, mentions: list, components) -> list[str]:
    """Deterministic checks of the model's output. Returns a list of warnings (not errors).

    Raises `Rejected` if something that must never be violated is violated.
    """
    warns = []

    # 1. DOSES. The most important check of the feature.
    #
    # We compare with the original WITHOUT the box-contents list: the model removes it by
    # task, and pack grammages legitimately disappear. But "disappeared along with the list" and
    # "disappeared from the treatment program" are different things, so we count them separately:
    # doses that were ONLY in the list are not part of the comparison; a dose from the treatment
    # program that disappeared is a rejection. An invented dose is always a rejection, no
    # exceptions.
    body_src = cut_composition(src_html, components)
    comp_doses = dose_set(composition_region(src_html, components))
    src, out = dose_set(body_src), dose_set(out_html)

    # Unit-of-measure correction: the owner allowed (2026-09-05) swapping g<->ml<->kg…
    # when the dose NUMBER matches the component's packaging. Then it's not a different dose
    # but a fixed author error in the notation of the release form. We apply this only to pairs
    # where the old form really disappeared and the new one really appeared, so as not to cover
    # up a coincidental match of numbers.
    unit_fixed = []
    for wrong, correct in _unit_fix_pairs(components).items():
        if wrong in src and correct in out and wrong not in out:
            src = (src - {wrong}) | {correct}
            unit_fixed.append((wrong, correct))
    for wrong, correct in unit_fixed:
        warns.append(f'unit of measure corrected from "{wrong.replace(":", " ")}" to '
                     f'"{correct.replace(":", " ")}" — same number, the release form '
                     f'matches the packaging, please check')

    invented = out - src - comp_doses
    merged = {d for d in invented if _is_merged_range(d, src | comp_doses)}
    invented -= merged
    if invented:
        raise Rejected('the model invented doses that were not in the original: '
                       + ', '.join(sorted(invented)))
    for d in sorted(merged):
        warns.append(f'the model merged the dose "{d.replace(":", " ")}" into a range of two '
                     f'adjacent doses of the original — check that it really is a range')

    lost = src - out
    # A dose is not 'lost' if it became the end of a range accepted above:
    # "25 g" and "30 g" disappeared precisely because they merged into "25–30 g".
    absorbed = {d for d in lost if _is_range_endpoint(d, merged)}
    # `src` is already the text WITHOUT the contents list, so everything that vanished from here
    # vanished from
    # the treatment program. Previously the list doses were subtracted as well (`- comp_doses`), and
    # a dose
    # that matches the pack size ("Product A 3 ml" in the program and "Product A — 3 ml" in the
    # list)
    # could vanish from the program, leaving only a warning — a hole in the "a dose will not
    # vanish" guarantee. Doses that were ONLY in the list never get here at all.
    lost_hard = lost - absorbed
    if lost_hard:
        # A rejection can't be diagnosed from a bare '3:мл': it is visible neither what dose this
        # is nor where it disappeared from. We show a piece of the original around it.
        raise Rejected('the model lost doses from the treatment program: '
                       + '; '.join(f'{d.replace(":", " ")}{_dose_context(body_src, d)}'
                                   for d in sorted(lost_hard)))

    # 2. The text must not collapse: normalization rearranges blocks, it does not cut content.
    ls, lo = len(_plain(body_src)), len(_plain(out_html))
    if ls and lo < ls * 0.75:
        raise Rejected(f'the text shrank from {ls} to {lo} characters — this is not normalization')

    # 3. What the model was supposed to remove is really removed.
    if _COMP_HEAD_RE.search(out_html):
        warns.append('the "Bundle contents" block is still in the text — the system prints it, '
                     'so the sheet will have two lists')

    # 4. The markup must point to real products.
    from .models import Product
    ids = {m.get('product') for m in mentions if m.get('product')}
    known = set(Product.objects.filter(pk__in=ids).values_list('pk', flat=True))
    for bad in sorted(ids - known):
        warns.append(f'the markup points to a non-existent product #{bad}')

    # 5. Every marked-up name must really be in the text.
    folded_out = im.fold(_plain(out_html))
    for m in mentions:
        surface = (m.get('surface') or '').strip()
        if surface and im.fold(surface) not in folded_out:
            warns.append(f'"{surface}" is marked up, but there is no such string in the text')

    # 6. Composition components that the model did not find.
    marked = {m.get('product') for m in mentions
              if m.get('kind') == 'component' and m.get('product')}
    for c in components:
        pv = getattr(c, 'variant', None)
        if pv and pv.product_id not in marked:
            warns.append(f'component "{c.label}" was not found in the text')

    # 7. Emoji were supposed to disappear. Printing also strips them
    #    (instruction_docx.strip_emoji), but if the model leaves them, it is visible here,
    #    not after printing.
    from orders.instruction_docx import strip_emoji
    if strip_emoji(_plain(out_html)) != _plain(out_html):
        warns.append('emoji are left in the text — they will not be printed, '
                     'but they will be visible in the customer account')

    # 8. External mentions are not an error, but a human must see them.
    for m in mentions:
        if m.get('kind') == 'external':
            warns.append(f'"{m.get("surface")}" is marked as a preparation OUTSIDE the composition '
                         f'(advice/upsell) — it is not subject to replacement')
    return warns


# The tag separately from the text: we touch only the text BETWEEN tags, never inside them.
# Otherwise we could damage an attribute like `bis_size="{...}"`, which stays in the HTML
# after pasting from some editors/extensions.
_TAG_SPLIT_RE = re.compile(r'(<[^>]+>)')
def _space_out_operators(html: str) -> str:
    """Unstick '+'/'=' glued to adjacent text: "25 g+ Product A 20 ml= 10 ml"
    -> "25 g + Product A 20 ml = 10 ml".

    Gluing happens on only ONE side ("ml+ Product B": a space on the right, none on the left),
    so a space is added on each side separately, rather than requiring gluing on both sides
    at once.

    This is cosmetic, not a change of content: no digit is touched, only the operator and the
    adjacent word are unstuck. We do this MECHANICALLY, before the model sees the text: on
    bv174 the LLM itself didn't think of putting spaces into such a mess of symbols and at the
    same time lost the dose "10 ml" next to it.
    """
    parts = _TAG_SPLIT_RE.split(html or '')
    for i in range(0, len(parts), 2):   # odd indices are the tags themselves, we don't touch them
        t = parts[i]
        # A "+" before a digit is most often the SIGN of the number ("+15…+25 °C", "(+12°C)"), not
        # an operator:
        # it must not be un-glued, otherwise the temperature becomes "+ 15… + 25 °C". It remains an
        # operator
        # when it stands between a unit/letter and a digit: "3 ml+5 ml".
        t = re.sub(r'(?<=\S)=', ' =', t)                       # "=" glued on the left
        t = re.sub(r'(?<=\S)\+(?!\d)', ' +', t)                # "+" glued on the left (not a number sign)
        t = re.sub(r'(?<=[^\W\d_])\+(?=\d)', ' + ', t)         # letter/unit "+" digit
        t = re.sub(r'=(?=\S)', '= ', t)                        # "=" glued on the right
        t = re.sub(r'\+(?=[^\W\d_])', '+ ', t)                 # "+" glued on the right to a letter
        parts[i] = t
    return ''.join(parts)


def _prompt_path() -> str:
    return os.path.join(os.path.dirname(__file__), 'prompts', PROMPT_FILE)


def build_prompt(bv, components) -> str:
    """Prompt: raw text + the exact bundle composition with SKUs."""
    with open(_prompt_path(), encoding='utf-8') as f:
        tpl = f.read()

    rows = []
    for c in components:
        pv = getattr(c, 'variant', None)
        if not pv:
            continue
        rows.append(f'- id={pv.product_id} | {pv.product.name} | packaging: {pv.name} '
                    f'| in the bundle: {c.quantity} pcs')
    # NOTE: the Cyrillic placeholder names are template tags of prompts/bundle_instruction.txt and must
    # stay identical there: {{СКЛАД}} = composition, {{НАЗВА}} = name, {{ТЕКСТ}} = text.
    return (tpl
            .replace('{{СКЛАД}}', '\n'.join(rows) or '(the composition is empty)')
            .replace('{{НАЗВА}}', bv.name or '')
            .replace('{{ТЕКСТ}}', bv.instruction_html or ''))


def normalize(bv, components=None) -> dict:
    """Run the instructions through the model. Saves nothing, returns the result for review.

    {'html': ..., 'mentions': [...], 'changes': [...], 'warnings': [...]}
    """
    from core.llm import text_to_json
    from . import instruction_resolve as ir

    if components is None:
        components = ir.components_of_template(bv)
    if not (bv.instruction_html or '').strip():
        raise Rejected('the instruction is empty — there is nothing to normalize')

    # We unstick the operators BEFORE the model sees the text: we edit only
    # bv.instruction_html in memory for this call and write nothing to the database. It is the
    # source for the prompt and for the dose check below, not the final result.
    bv.instruction_html = _space_out_operators(bv.instruction_html)

    data = text_to_json(build_prompt(bv, components))
    out_html = (data.get('html') or '').strip()
    if not out_html:
        raise Rejected('the model returned no text')

    mentions = data.get('mentions') or []
    warns = validate(bv.instruction_html, out_html, mentions, components)
    return {
        'html': out_html,
        'mentions': mentions,
        'changes': data.get('changes') or [],
        'warnings': warns + (data.get('warnings') or []),
    }


def build_map(html: str, mentions: list, components=None) -> dict:
    """Build instruction_map to save on the packaging.

    `components` is the bundle composition AT THE TIME of the markup. Without it we can't tell
    'the product was there and isn't mentioned' (respirator) from 'the product was just added
    in place of another' (Product A instead of Product B), and a wholesale replacement in the bundle
    reads as a plain removal.
    """
    import hashlib
    clean = []
    for m in mentions:
        surface = (m.get('surface') or '').strip()
        pid = m.get('product')
        if not surface or not pid:
            continue
        clean.append({
            'surface': surface,
            'product': int(pid),
            'kind': 'external' if m.get('kind') == 'external' else 'component',
            'doses': [d for d in (m.get('doses') or []) if d],
        })
    comp_ids, packs = [], {}
    for c in (components or []):
        pv = getattr(c, 'variant', None)
        if pv and pv.product_id not in comp_ids:
            comp_ids.append(pv.product_id)
            # We record the packaging together with the composition: after a product in the bundle
            # is
            # replaced WHOLESALE, the old variant can no longer be retrieved, but 'which packaging
            # was
            # here' is needed to understand whether the dose in the text equalled the pack.
            if getattr(c, 'pack', ''):
                packs[str(pv.product_id)] = c.pack

    return {
        'version': PROMPT_VERSION,
        'source_stamp': hashlib.sha1((html or '').strip().encode('utf-8')).hexdigest(),
        'mentions': clean,
        'components': comp_ids,      # composition at the time of the markup
        'packs': packs,              # and the packaging of each one: product_id -> '10 мл' (Ukrainian unit)
    }
