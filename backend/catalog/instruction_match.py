# -*- coding: utf-8 -*-
"""Finding mentions of a bundle's products in the instruction text.

WHAT THIS MODULE DOES: finds where exactly in the instructions a composition component is
named, in order to (a) highlight it for the manager and (b) make a DRAFT of auto-replacement
when a component has been swapped. WHAT IT DOES NOT DO: it prints nothing and approves
nothing; a human always reads the auto-replacement result before the leaflet goes into the
box.

Why a matcher and not tokens in the markup. The Quill editor in the CRM has no custom
blots registered (`catalog/widgets.py`, `buildVariantEditor` in bundle/change_form.html),
so any `<span data-p="...">` is stripped when the value is loaded into the editor.
Storing the link in the HTML itself is impossible, so we compute it every time.

The main difficulty: the catalog name almost never matches the name in the text.
Measured on 67 bundles: there are almost no matches 'as is'.

    catalog                                      instruction
    Magnesium sulfate Brand X KRISTA MgS     ->  "Magnesium Sulfate"
    Booster 5-15-45                          ->  "Booster (5:15:45)"
    Respirator Model 3 with valve FFP3       ->  "Respirator"
    Fungicide TOP (against late blight…)     ->  "Fungicide"
    Oracle iron chelate                      ->  "Iron Chelate"

So we recognize by the ANCHOR WORD (the rarest significant word of the name) plus, when it
is ambiguous, by the NPK formula.
"""
from __future__ import annotations

import re

from .printout_match import fold, tokens

# Words that occur everywhere in product names and cannot be anchors.
# LANGUAGE DATA: a stop-word list for Ukrainian product names (fertilizer, set, preparation, agent,
# solution, concentrate, water-soluble, mix, universal, organic, plants, for, from, kind, about, plus,
# top, ml, kg, pcs, grams, granules, granular, liquid ...), so it stays Ukrainian.
_GENERIC = {
    'добриво', 'комплект', 'набир', 'препарат', 'засиб', 'розчин', 'концентрат',
    'водорозчинне', 'мисто', 'універсальне', 'універсал', 'органичне', 'органічне',
    'рослин', 'рослини', 'для', 'від', 'вид', 'про', 'plus', 'плюс', 'top', 'топ',
    'мл', 'кг', 'шт', 'грам', 'гранули', 'гранульоване', 'ридке', 'рідке',
}

# NPK in any everyday notation: 5-15-45, 5:15:45, 5.15.45, 18 18 18.
_NPK_ANY_RE = re.compile(r'\b(\d{1,2})\s*[-:.\s]\s*(\d{1,2})\s*[-:.\s]\s*(\d{1,2})\b')

_TAG_RE = re.compile(r'<[^>]+>')


def npk_key(text: str) -> str:
    """"Booster (5:15:45)" and "Booster 5-15-45" -> '5-15-45'. Empty if there is none."""
    m = _NPK_ANY_RE.search(text or '')
    return '-'.join(m.groups()) if m else ''


def anchor_words_of_name(name: str) -> list[str]:
    """Anchor words from an arbitrary product name (without a ProductVariant object)."""
    out = []
    for t in tokens(name or ''):
        if t in _GENERIC or len(t) < 4:
            continue
        out.append(t)
    return out


def anchor_words(pv) -> list[str]:
    """Anchor words of a component: significant words of the product name, from rare to common.

    We take the Product name (not the variant's): the variant has a packaging tail
    ("…, 10 ml"), which in the instruction text means a dose, not a product.
    """
    name = pv.product.name if (pv and pv.product_id) else (pv.name if pv else '')
    out = []
    for t in tokens(name):
        if t in _GENERIC or len(t) < 4:
            continue
        out.append(t)
    return out


def aliases(pv, extra: dict | None = None) -> list[str]:
    """All strings by which this component can be named in the text."""
    out = list(anchor_words(pv))
    if extra:
        for a in extra.get(pv.product_id, ()):
            f = fold(a)
            if f not in out:
                out.append(f)
    return out


def _visible_spans(html: str):
    """Spans of visible text (outside tags) as (start, end)."""
    spans, pos = [], 0
    for m in _TAG_RE.finditer(html or ''):
        if m.start() > pos:
            spans.append((pos, m.start()))
        pos = m.end()
    if pos < len(html or ''):
        spans.append((pos, len(html)))
    return spans


def _fold_map(text: str) -> tuple[str, list[int]]:
    """fold() with a position map: (folded string, idx[i] -> position in the original).

    fold drops the soft/hard sign, so the lengths don't match. Without the map the offsets
    would drift and a replacement would cut a word in the middle.
    """
    out, idx = [], []
    for i, ch in enumerate(text):
        f = fold(ch)
        if not f:
            continue
        out.append(f)
        idx.append(i)
    idx.append(len(text))
    return ''.join(out), idx


def find_mentions(html: str, pv, *, extra_aliases: dict | None = None,
                  ambiguous_npk: bool = False,
                  surface: str | None = None) -> list[tuple[int, int, str]]:
    """Where in the HTML this component is named. Returns [(start, end, surface), ...].

    We search only in VISIBLE text, otherwise the anchor word would be caught in a tag
    attribute (href, alt) and a replacement would break the markup.

    `ambiguous_npk=True` means the composition has several products with the same anchor word
    (four Plantafols); then a match counts only if the same NPK formula is next to it.
    """
    # `surface` is a string from the LLM markup: 'by which exact word this product is named in
    # THIS text'. When it is present, we search for exactly it and do not touch anchor words:
    # the markup was made by a model that saw the whole text, and it is more precise than a
    # heuristic.
    if surface:
        words = [fold(surface)]
        want_npk = ''
    else:
        words = aliases(pv, extra_aliases)
        if not words:
            return []
        want_npk = npk_key(pv.product.name) if (ambiguous_npk and pv.product_id) else ''

    hits = []
    for s, e in _visible_spans(html):
        chunk = html[s:e]
        folded, idx = _fold_map(chunk)
        for w in words:
            found = False
            for m in re.finditer(re.escape(w), folded):
                a, b = m.start(), m.end()
                # word boundaries: we don't catch a short name inside another word
                if a > 0 and (folded[a - 1].isalpha() or folded[a - 1].isdigit()):
                    continue
                if b < len(folded) and folded[b].isalpha():
                    # we allow a Ukrainian case ending (up to 3 letters), e.g. the dative form of a name
                    tail = 0
                    while b + tail < len(folded) and folded[b + tail].isalpha() and tail < 3:
                        tail += 1
                    if b + tail < len(folded) and folded[b + tail].isalpha():
                        continue
                    b += tail
                ra, rb = idx[a], idx[b]
                if want_npk and npk_key(chunk[rb:rb + 24]) != want_npk:
                    continue
                hits.append((s + ra, s + rb, chunk[ra:rb]))
                found = True
            if found:
                break  # found by the rarest word, nothing more needed
    return sorted(set(hits))


def ambiguous_products(components) -> set[int]:
    """Products whose anchor word coincides with another one in the same composition.

    A real case: a bundle has four Plantafols with different NPK; it is for them that the
    formula check is turned on.
    """
    seen, dup = {}, set()
    for c in components:
        pv = getattr(c, 'variant', None) or getattr(c, 'product_variant', None)
        if not pv:
            continue
        ws = anchor_words(pv)
        if not ws:
            continue
        key = ws[0]
        if key in seen and seen[key] != pv.product_id:
            dup.add(pv.product_id)
            dup.add(seen[key])
        seen[key] = pv.product_id
    return dup


def highlight(html: str, components, *, extra_aliases: dict | None = None) -> str:
    """Wrap the found mentions in <mark>, ONLY for showing to the manager."""
    amb = ambiguous_products(components)
    spans = []
    for c in components:
        pv = getattr(c, 'variant', None) or getattr(c, 'product_variant', None)
        if not pv:
            continue
        for a, b, _ in find_mentions(html, pv, extra_aliases=extra_aliases,
                                     ambiguous_npk=pv.product_id in amb):
            spans.append((a, b))
    out, prev = [], 0
    for a, b in sorted(set(spans)):
        if a < prev:
            continue
        out.append(html[prev:a])
        out.append(f'<mark>{html[a:b]}</mark>')
        prev = b
    out.append(html[prev:])
    return ''.join(out)


# A dose standing IMMEDIATELY AFTER a product mention: "Product A 10 g", "Product B — 30 ml",
# "Potassium sulfate 25–30 g", "Product C (3 ml)". Measured on 12 real instructions:
# 76 mentions with a dose, all right after the name, none before it.
#
# The separator and highlighting tags are carried over as is; only the number with the unit
# is replaced. We step over tags deliberately ("<strong>Product A</strong> 10 g"), but ONLY
# inline ones: we don't touch <p>/<li>/<br>, otherwise a number from the next paragraph
# could be pulled in.
_SPACE = r'[ \t ]'
# LANGUAGE DATA: Ukrainian unit abbreviations as typed in the instruction texts
# (мл = ml, мг = mg, кг = kg, гр/г = g, л = l, шт = pcs).
_UNIT = r'(?:мл|мг|кг|гр|г|л|шт)'
_QTY_NUM = r'\d+(?:[.,]\d+)?(?:' + _SPACE + r'*[–—-]' + _SPACE + r'*\d+(?:[.,]\d+)?)?'
_QTY_ONE = _QTY_NUM + _SPACE + r'*' + _UNIT + r'\b'

# "30 ml/40 g" is ONE dose, written for two release forms: it is replaced entirely, because
# the new product has a single form.
#
# Whereas "10 g / 10 l of water" is a dose PER VOLUME OF SOLUTION, and
# eating the tail "/ 10 l" would mean printing "Potassium monophosphate 300 g of water", with neither
# dose nor volume. We tell them apart by what differs between these notations in live texts
# (measured: 2 versus 5): the form is written with the slash TIGHT, and the second unit is
# the product mass; the water volume is written with spaces and in liters.
_QTY_FORM_TAIL = r'(?:/\d+(?:[.,]\d+)?' + _SPACE + r'*(?:мл|мг|кг|гр|г|шт)\b)*'

# LANGUAGE DATA: 'по' is the Ukrainian preposition "at / of" that can stand between a name and a dose
# ("Potassium sulfate по 25 g" = "... at 25 g").
_QTY_AFTER_RE = re.compile(
    r'(?P<sep>(?:</?(?:strong|em|b|i|span|u)\b[^>]*>|' + _SPACE + r'|&nbsp;)*'
    r'(?:[,—–-]' + _SPACE + r'*)?(?:по' + _SPACE + r'+)?\(?)'
    r'(?P<qty>' + _QTY_ONE + _QTY_FORM_TAIL + r')',
    re.IGNORECASE)

# The same, but deliberately imprecise, ONLY to tell the human 'there is a number near the
# name, and we did not touch it'. Silence here is the worst of all: the old dose stays under
# the new name, and the export panel says nothing about it. We don't let it through tags and
# a period, otherwise we'd grab the dose of a neighboring product.
# We do let it through tags, since a dose can be a separate list item. But between the name
# and the dose we allow ONLY function words, and by no means an arbitrary word: in bv99
# "…after treating with Product A</p><p>Product B 40 g" an arbitrary word
# allowed grabbing the dose of the NEIGHBORING product and declaring it unreplaced.
# LANGUAGE DATA: the function words are Ukrainian prepositions and adverbs (по = at/of, у/в = in,
# із/з = with/from, на = on, дозою = with a dose of, кількості = in the amount of,
# приблизно/орієнтовно = approximately).
_QTY_LOOSE_RE = re.compile(
    r'(?:</?[a-z][^>]*>|\s|&nbsp;)*'
    r'(?:(?:по|у|в|із|з|на|дозою|кількості|приблизно|орієнтовно)\s+|[,:—–-]\s*|\(\s*)*'
    r'(?P<qty>' + _QTY_ONE + r')',
    re.IGNORECASE)

def replace_mentions(html: str, pv_old, new_label: str, *,
                     extra_aliases: dict | None = None,
                     ambiguous_npk: bool = False,
                     surface: str | None = None,
                     new_qty: str = '') -> tuple[str, int, list[str], list[str]]:
    """Replace all mentions of a product with the new name (and packaging).

    Returns (html, how many names replaced, which doses replaced, which REMAINED).

    This is the 'auto-replacement across the instructions': Product A -> Product B in all places at
    once. The PACKAGING moves along with the name: the bundle contains exactly one pack per
    treatment, so "Product A 50 g" becomes "Product B 10 ml" rather than staying with someone
    else's dose.

    The packaging changes only right next to the mention, which justifies the walk by
    positions rather than a regex on the new name: if the new product was already mentioned
    earlier in the text ("Product C at the end of the season"), its own
    dose stays untouched.

    The fourth result is doses that ARE next to the name but that we did not touch (written in
    a way the regex doesn't catch). Without it, silence would mean both 'there is no dose' and
    'the old dose remained', and these are different things: in the second case someone else's
    dose is printed under the new name, and a human must hear about it.
    """
    spans = find_mentions(html, pv_old, extra_aliases=extra_aliases,
                          ambiguous_npk=ambiguous_npk, surface=surface)
    if not spans:
        return html, 0, [], []
    out, prev, swapped, left = [], 0, [], []
    for a, b, _ in spans:
        if a < prev:
            continue
        out.append(html[prev:a])
        out.append(new_label)
        prev = b
        if not new_qty:
            continue
        m = _QTY_AFTER_RE.match(html, b)
        if m:
            out.append(m.group('sep'))
            out.append(new_qty)
            swapped.append(re.sub(r'\s+', ' ', m.group('qty')).strip())
            prev = m.end()
            continue
        loose = _QTY_LOOSE_RE.match(html, b)
        if loose:
            left.append(re.sub(r'\s+', ' ', loose.group('qty')).strip())
    out.append(html[prev:])
    return ''.join(out), len(spans), swapped, left
