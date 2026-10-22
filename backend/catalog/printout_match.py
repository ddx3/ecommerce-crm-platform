"""Matching printout files (sticker sheets) to packagings.

Main rule: **the file name lies, the truth is in the content**. In the printouts asset folder (named
"распечатки" in the original tree) all the old Osmocote files are named without a duration, although
the sticker itself says "8-9 months" — name-based matching silently attached them to 5-6 month packagings. So we take the
text from the PDF and use the name only as a fallback.

Some files (.docx and 4 raster PDFs) have no text — the sticker is an image there. For them the
decoded content lives in the sidecar file `_labels.json` next to the folders; whatever is in neither
it nor the PDF is marked "check manually" rather than guessed.

Extracted into a separate module (rather than inside the management command) for the same reason as
flora_feed.py: the same logic is needed by the import command and — someday — by a candidate hint in
the form.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher

SIDECAR = '_labels.json'

# ---------------------------------------------------------------- normalization

# LANGUAGE DATA: fold() makes Ukrainian/Russian spelling variants of a word comparable (і/ї/й -> и,
# є -> е, ґ -> г) and drops the soft sign (ь), so that the stickers (Ukrainian) match the catalog
# names however they were typed.
_FOLD = {'і': 'и', 'ї': 'и', 'й': 'и', 'є': 'е', 'ґ': 'г'}
_DROP = set('ь')

# Words that appear in every other name and distinguish nothing.
# LANGUAGE DATA: a Ukrainian/Russian stop-word list (fertilizer, for, all, plants, and, in, on, from,
# watering, spraying, water, solution, soil, substrate, months, units ...).
_STOP = {'добриво', 'для', 'всіх', 'всих', 'рослин', 'та', 'и', 'і', 'в', 'на',
         'від', 'полив', 'обприскування', 'води', 'розчину', 'ґрунту', 'грунту',
         'ґрунта', 'грунта', 'субстракта', 'субстрату', 'мис', 'міс', 'місяців',
         'те', 'icl', 'л', 'г', 'кг', 'мл'}

# LANGUAGE DATA: the size/duration regexes below match the Ukrainian unit abbreviations (кг = kg, гр/г = g,
# мл = ml, л = l) and the month word stems (мис/міс = months) as printed on the stickers.
_SIZE_RE = re.compile(r'(\d+(?:[.,]\d+)?)\s*(кг|kg|гр|г|g|мл|ml|л|l)\b', re.I)
_SIZE_ONLY_RE = re.compile(r'^\s*(\d+(?:[.,]\d+)?)\s*(кг|kg|гр|г|g|мл|ml|л|l)\s*$', re.I)
_NPK_RE = re.compile(r'\b(\d{1,2})\s*[-:+]\s*(\d{1,2})\s*[-:+]\s*(\d{1,2})\b')
_DUR_RE = re.compile(r'\b(\d{1,2})\s*[-–]\s*(\d{1,2})\s*(?:мис|міс|m)\w*', re.I)
_MULT = {'кг': 1000, 'kg': 1000, 'гр': 1, 'г': 1, 'g': 1, 'мл': 1, 'ml': 1, 'л': 1000, 'l': 1000}


def fold(text: str) -> str:
    out = []
    for ch in (text or '').lower().replace('_', '-'):
        if ch in _DROP:
            continue
        out.append(_FOLD.get(ch, ch))
    return ''.join(out)


def parse_size(text: str):
    """Grams from the LAST size in the string (in names the packaging size is always at the tail)."""
    matches = list(_SIZE_RE.finditer(fold(text)))
    if not matches:
        return None
    num, unit = matches[-1].groups()
    return round(float(num.replace(',', '.')) * _MULT[unit.lower()])


def size_from_lines(lines):
    """On the sticker the size is on a separate line ("300 g"). Lines with an application rate
    ("10 g / 10 l of water") are deliberately not counted."""
    for line in lines:
        m = _SIZE_ONLY_RE.match(fold(line))
        if m:
            return round(float(m.group(1).replace(',', '.')) * _MULT[m.group(2).lower()])
    return None


def npk(text: str) -> set:
    return {'-'.join(m.groups()) for m in _NPK_RE.finditer(fold(text))}


def duration(text: str) -> set:
    return {f'{a}-{b}' for a, b in _DUR_RE.findall(fold(text))}


def tokens(text: str) -> list:
    t = _SIZE_RE.sub(' ', fold(text))
    t = re.sub(r'[^\w\d-]+', ' ', t)
    out = []
    for p in t.split():
        if len(p) < 3 or p in _STOP:
            continue
        # Pure numbers are discarded: size, NPK and duration are checked separately, and otherwise
        # "20" from the application rate attached itself to "Pruning shears ... 200 mm".
        if re.fullmatch(r'[\d.,-]+', p):
            continue
        out.append(p)
    return out


# ------------------------------------------------------------------- reading

def read_pdf_label(path: str):
    """Lines of ONE sticker from a text PDF (a sheet is the same sticker ×28).

    We read by the coordinates of each span rather than as continuous text: in these sheets all 4
    columns lie on shared baselines, and any «by line» grouping would glue neighboring stickers into
    one.

    Deliberately on PyMuPDF, not pypdf: pypdf is not in requirements, and its absence on prod
    silently dropped matching back to file names — exactly the error this module was meant to
    protect against.
    """
    try:
        import pymupdf
    except ImportError:
        return None
    items = []
    try:
        with pymupdf.open(path) as doc:
            if not len(doc):
                return None
            data = doc[0].get_text('dict')
    except Exception:
        return None
    for block in data.get('blocks', []):
        if block.get('type') != 0:
            continue
        for line in block.get('lines', []):
            for span in line.get('spans', []):
                text = (span.get('text') or '').strip()
                if not text:
                    continue
                x, y = span.get('origin', (span['bbox'][0], span['bbox'][3]))
                items.append((round(x, 1), round(y, 1), text))
    if not items:
        return None

    ys = sorted({y for _, y, _ in items})
    first = [ys[0]]
    for y in ys[1:]:
        # within a sticker the line pitch is ~9-17 pt, between stickers — 60 or 120
        if y - first[-1] > 25:
            break
        first.append(y)

    lines_out = []
    for y in first:
        row = sorted([i for i in items if i[1] == y], key=lambda i: i[0])
        if not row:
            continue
        # take only the leftmost column: all spans up to the first large horizontal gap (the start
        # of the next sticker)
        chunk = [row[0]]
        for prev, cur in zip(row, row[1:]):
            if cur[0] - prev[0] > 60:
                break
            chunk.append(cur)
        lines_out.append(' '.join(c[2] for c in chunk).strip())
    return lines_out or None


def title_lines(lines, limit=3):
    """Sticker header — the lines up to the first one where a digit appears.

    On all stickers the name comes first ("Magnesium sulfate" / "Brand X KRISTA
    MgS"), and only then the formula, duration and application rate — and they all contain digits.
    So "the first line with a digit" reliably cuts the name off from the rest without requiring
    markup.
    """
    out = []
    for line in lines[:limit]:
        if any(ch.isdigit() for ch in line):
            break
        out.append(line)
    return out or lines[:1]


def load_sidecar(directory: str) -> dict:
    path = os.path.join(directory, SIDECAR)
    if not os.path.exists(path):
        return {}
    with open(path, encoding='utf-8') as fh:
        return json.load(fh)


@dataclass
class LabelFile:
    name: str
    path: str
    lines: list = field(default_factory=list)
    source: str = 'pdf'          # pdf | sidecar | filename
    grams: int | None = None
    npk: set = field(default_factory=set)
    duration: set = field(default_factory=set)
    tokens: list = field(default_factory=list)
    title: list = field(default_factory=list)

    @property
    def text(self) -> str:
        return ' '.join(self.lines)

    @property
    def needs_review(self) -> bool:
        return self.source == 'filename'


def scan(directory: str) -> list:
    """Reads the printouts folder. Source order: PDF text -> sidecar -> name."""
    sidecar = load_sidecar(directory)
    out = []
    for name in sorted(os.listdir(directory)):
        path = os.path.join(directory, name)
        if not os.path.isfile(path) or name.startswith('_'):
            continue
        base, ext = os.path.splitext(name)
        if ext.lower() not in ('.pdf', '.docx'):
            continue
        lines, source = None, 'filename'
        if ext.lower() == '.pdf':
            lines = read_pdf_label(path)
            source = 'pdf' if lines else 'filename'
        if not lines and name in sidecar:
            lines, source = list(sidecar[name]), 'sidecar'
        if not lines:
            lines = [base]
        body = ' '.join(lines)
        # The product name is always in the sticker header; what follows are application rates,
        # which resemble no name and only dilute the match.
        title = tokens(' '.join(title_lines(lines)))
        if not title:
            # The file has text but no name in it — e.g. a sheet where the whole design is an image
            # and the text layer holds only the weight. Then matching has to go by the FILE NAME,
            # and we don't trust the name: we mark it as needing a human eye instead of silently
            # linking.
            title = tokens(base)
            source = 'filename'
        out.append(LabelFile(
            name=name, path=path, lines=lines, source=source,
            grams=size_from_lines(lines) or parse_size(body) or parse_size(base),
            npk=npk(body), duration=duration(body), tokens=tokens(body),
            title=title,
        ))
    return out


# ------------------------------------------------------------------- matching

@dataclass
class VariantKey:
    pk: int
    sku: str
    name: str
    product: str
    product_pk: int
    grams: set
    npk: set
    duration: set
    tokens: list


def variant_key(variant) -> VariantKey:
    full = f'{variant.product.name} {variant.name or ""}'
    sizes = {s for s in (variant.unit_size, parse_size(variant.name or '')) if s}
    return VariantKey(
        pk=variant.pk, sku=variant.sku or '', name=variant.name or '',
        product=variant.product.name, product_pk=variant.product_id, grams=sizes,
        npk=npk(full), duration=duration(full), tokens=tokens(full),
    )


def _match(token, have) -> float:
    best = 0.0
    for other in have:
        if token == other:
            return 1.0
        if min(len(token), len(other)) >= 4 and (token in other or other in token):
            best = max(best, 0.9)
        else:
            ratio = SequenceMatcher(None, token, other).ratio()
            if ratio > 0.85:
                best = max(best, ratio)
    return best


def build_idf(keys) -> dict:
    """Word weight = how rare the word is in the catalog.

    "fertilizer" is in hundreds of names and proves nothing; "trichosapromix" is in one.
    Without this, a two-word header where one word matched gives exactly 0.5 regardless of which
    word matched.
    """
    import math
    total = max(1, len(keys))
    seen = {}
    for key in keys:
        for token in set(key.tokens):
            seen[token] = seen.get(token, 0) + 1
    return {t: math.log(total / c) for t, c in seen.items()}


UNSEEN_IDF = 7.5      # a word that is not in the catalog at all
RARE_IDF = 5.8        # roughly «occurs in no more than 3 packagings»


def _idf(idf, token):
    return idf.get(token, UNSEEN_IDF)


def unmatched_anchors(label: LabelFile, key: VariantKey, idf: dict) -> list:
    """Rare words from the sticker header that are NOT in the packaging name.

    This is exactly what distinguishes "Ferrilene Trium" from "Ferrilene", "Osmocote Protect" from
    "Osmocote" and "Mycorrhiza Bionorma" from "Mycorrhiza granules". The presence of such a word is a reason to show it to a human, not to link silently.
    """
    out = []
    for token in label.title:
        if len(token) < 5 or _idf(idf, token) < RARE_IDF:
            continue
        if _match(token, key.tokens) < 0.85:
            out.append(token)
    return out


def score(label: LabelFile, key: VariantKey, idf: dict):
    """Returns (score, rejection_reason). Hard conditions cut off silent errors."""
    if label.grams and key.grams and label.grams not in key.grams:
        return None, 'size'
    # NPK and duration are exactly what is absent from file names and what tells apart Osmocote
    # 5-6/8-9 months and Exact Standard 15-9-11/15-9-12.
    if label.npk and key.npk and not (label.npk & key.npk):
        return None, 'NPK'
    if label.duration and key.duration and not (label.duration & key.duration):
        return None, 'duration'
    if not label.title:
        return None, 'no header'
    got = weight = 0.0
    strong = False
    for token in label.title:
        w = _idf(idf, token)
        weight += w
        hit = _match(token, key.tokens)
        got += w * hit
        if hit >= 0.9 and len(token) >= 4 and w >= 1.0:
            strong = True
    if not strong:
        # without any rare shared word this match is about nothing
        return None, 'no anchor word'
    cover_label = got / weight if weight else 0.0
    # The reverse side: how much of the packaging name is covered by the sticker header. Needed
    # because a sticker legitimately carries what the name lacks (sub-brand, release form), and the
    # one-sided score sank obvious matches ("Monopotassium phosphate
    # / Nova Peak").
    got_v = weight_v = 0.0
    for token in set(key.tokens):
        w = _idf(idf, token)
        weight_v += w
        got_v += w * _match(token, label.title)
    cover_variant = got_v / weight_v if weight_v else 0.0
    base = max(cover_label, 0.9 * cover_variant)
    bonus = 0.0
    if label.npk & key.npk:
        bonus += 0.15
    if label.duration & key.duration:
        bonus += 0.15
    return round(base + bonus, 3), None


def rank(label: LabelFile, keys, idf: dict) -> list:
    scored = []
    for key in keys:
        value, _ = score(label, key, idf)
        if value is not None:
            scored.append((value, key))
    scored.sort(key=lambda pair: -pair[0])
    return scored


AUTO_MIN = 0.55
AUTO_GAP = 0.05
REVIEW_MIN = 0.35     # below the auto-link limit, but clearly not chance — show to a human


def decide(label: LabelFile, keys, idf: dict):
    """-> (state, candidates). State: auto | ambiguous | none | review.

    We auto-link ONLY a confidently unique top. The «take the active one» rule is deliberately not
    applied: it yields silently wrong bindings between DIFFERENT products (5-6 vs 8-9 months), not
    between clones.
    """
    ranked = rank(label, keys, idf)
    if not ranked:
        return 'none', []
    top = ranked[0][0]
    if top < REVIEW_MIN:
        return 'none', ranked[:5]
    if top < AUTO_MIN:
        # weak for auto-link, but too similar to throw away silently
        return 'review', ranked[:5]
    near = [pair for pair in ranked if pair[0] >= top - AUTO_GAP]
    # We compare product_pk, not the name: the catalog has 7 pairs of DIFFERENT products with the
    # same name (Booster, Master, Fungicide C, Monopotassium phosphate). By name they merged into one key, the guard did not fire, and the
    # file silently attached to whichever row Postgres returned first — often a dead duplicate.
    if len(near) > 1 and len({p[1].product_pk for p in near}) > 1:
        return 'ambiguous', near
    # (several rows of the same product are clone duplicates, not ambiguity)
    if label.needs_review or unmatched_anchors(label, near[0][1], idf):
        return 'review', near if len(near) > 1 else ranked[:5]
    return 'auto', ranked[:5]
