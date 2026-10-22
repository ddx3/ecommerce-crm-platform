# -*- coding: utf-8 -*-
"""Which instruction to print for THIS composition.

The bundle composition changes in two ways, and both must reach the instruction:

    bundle template    Bundle.items                  — the composition was changed "in general"
    order composition  OrderItemBundleComponent     — it was changed in a single order

There is one resolver, only the input differs. Because of this, "swapped a product in the bundle"
and
"swapped a product in a single order" do not need two different mechanisms.

THE MAIN RULE OF THE FEATURE, from which the rest follows:

    the sheet is printed for THE composition that will go in the box,
    or is not printed at all — and then that is said out loud.

A swap carries the NAME along with the PACK SIZE: "Product A 50 g" -> "Product B 10 ml".
The basis is how the bundles are assembled: the box holds exactly one pack per treatment, so the
pack size
of the new preparation is its dose. Since these are pesticides, every such substitution declares
itself in
the export panel, and the case where the dose in the text did not equal the pack size is flagged
separately (Product C: 10 g from a 100 g pack).

An unambiguous swap (one disappeared — one appeared) is printed right away. An ambiguous one the
machine does not guess: the bundle goes with the PLACEHOLDER "insert the paper
one", the sheet's
position in the stack is preserved, and the export is never blocked.
"""
from __future__ import annotations

import hashlib
import re
from collections import Counter

from . import instruction_match as im

# --- consequence of the replacement -------------------------------------------------------
EQ_SAME = 'same'          # the same variant
EQ_PACK = 'pack'          # the same substance in a different packaging (incl. the own<->factory pair)
EQ_UNREF = 'unref'        # the product is not mentioned in the instructions at all (respirator, bag)
EQ_QTY = 'qty'            # only the quantity changed
EQ_MATERIAL = 'material'  # a different substance: ONLY this goes into the queue

# --- resolve outcome ------------------------------------------------------
KIND_OK = 'ok'                    # we print
KIND_PLACEHOLDER = 'placeholder'  # we print a placeholder
KIND_SKIP = 'skip'                # this is not a bundle


class Resolution:
    __slots__ = ('kind', 'html', 'source', 'reason', 'signature', 'override',
                 'bundle', 'components', 'summary')

    def __init__(self, kind, *, html='', source='', reason='', signature='',
                 override=None, bundle=None, components=None, summary=''):
        self.kind = kind
        self.html = html
        self.source = source          # 'template' | 'override'
        self.reason = reason
        self.signature = signature
        self.override = override
        self.bundle = bundle
        self.components = components or []
        self.summary = summary

    @property
    def ok(self):
        return self.kind == KIND_OK

    def __repr__(self):
        return f'<Resolution {self.kind} {self.source or self.reason!r}>'


# ---------------------------------------------------------------- substance

def canonical_product_id(pv):
    """One substance in different packagings -> one id.

    The dose in the instructions is PER TREATMENT, not per pack, so "Product C 100 g ×3" and
    "Product C 300 g ×1" do not change a single character in the text.

    We deliberately do NOT normalize ml<->g: the catalog has products where these are different
    release forms (Product D 25 g / 100 ml, Product E 10 g / 15 ml), and 'equivalence by mass'
    would silently approve replacing a powder with a liquid.
    """
    if pv is None:
        return None
    ids = {pv.product_id}
    p = pv.product
    if p and p.paired_product_id:
        ids.add(p.paired_product_id)
    if pv.paired_variant_id:
        pair = pv.paired_variant
        if pair:
            ids.add(pair.product_id)
            if pair.product and pair.product.paired_product_id:
                ids.add(pair.product.paired_product_id)
    return min(ids)


def _no_bee(name: str) -> str:
    """Strip service marks from the tail of the name FOR PARSING.

    🐝 in the catalog means 'bee-safe' and is not removed from the product name anywhere else.
    But in the name it stands after the packaging ("Product F, 10 ml 🐝"), because of which the
    packaging was not read, and "Product F 🐝" was substituted into the instruction text.
    """
    import re
    return re.sub(r'[\s🐝‍️]+$', '', name or '')


class Comp:
    """A composition component, identical for the template and for the order snapshot."""

    __slots__ = ('variant', 'quantity', 'name', 'sku', 'canonical')

    def __init__(self, variant, quantity, name='', sku=''):
        self.variant = variant
        self.quantity = quantity
        self.name = name or (variant.name if variant else '')
        self.sku = sku or (variant.sku if variant else '')
        self.canonical = canonical_product_id(variant)

    @property
    def key(self):
        """Key for comparing compositions. A deleted variant (product_variant=NULL) is not collapsed
        to None, otherwise two different deleted components would look the same; we take the
        frozen name."""
        return self.canonical if self.canonical else 'n:' + im.fold(self.name)[:40]

    @property
    def label(self):
        """Short name for the text: "Product G TOP (against…)" -> "Product G TOP"."""
        import re
        base = self.variant.product.name if (self.variant and self.variant.product_id) else self.name
        return re.split(r'\s*[,(]', _no_bee(base).strip())[0].strip() or self.name

    @property
    def pack(self):
        """Pack size from the tail of the variant name: "Product B, 10 ml" -> "10 ml".

        LANGUAGE DATA: the unit words are the Ukrainian abbreviations used in the catalog
        (кг = kg, г = g, мл = ml, л = l, шт = pcs)."""
        import re
        m = re.search(r'(\d+(?:[.,]\d+)?)\s*(кг|г|мл|л|шт)\s*$',
                      _no_bee(self.name or '').strip(), re.I)
        return f'{m.group(1)} {m.group(2)}' if m else ''

    def __repr__(self):
        return f'<Comp {self.name!r} x{self.quantity}>'


def components_of_template(bundle) -> list[Comp]:
    return [Comp(it.product_variant, it.quantity)
            for it in bundle.items.select_related('product_variant__product',
                                                   'product_variant__paired_variant__product')
            if it.product_variant]


def components_of_item(item) -> list[Comp] | None:
    """Composition of an order item, taken from the snapshot.

    Returns None if there is no snapshot. We do NOT fall back to the template: an empty snapshot
    means 'composition unknown', not 'composition is the default one', and printing the default
    instruction for an unknown composition is exactly the error this feature guards against.
    (For other purposes the order composition does fall back to the template —
    orders/models.py:713.)
    """
    rows = list(item.bundle_components.select_related(
        'product_variant__product', 'product_variant__paired_variant__product'))
    if not rows:
        return None
    return [Comp(c.product_variant, c.quantity,
                 name=c.product_variant_name, sku=c.product_variant_sku) for c in rows]


# ---------------------------------------------------------------- markup

def surfaces_of(bundle) -> dict[int, str]:
    """product_id -> the string this product is called IN THIS text.

    Taken from `instruction_map` (LLM markup). When there is no markup it is empty and the matcher
    works by a heuristic on the anchor word; when there is, we look for the exact string.

    Markup from EXTERNAL products (kind=external) deliberately does not end up here: "Product H, can be
    bought additionally" and "Product I for the end of the season"
    are not in the box, and must never be swapped when the
    composition changes.

    If the text was edited after the markup was made (source_stamp diverged from instruction_stamp),
    the markup is considered stale and is not applied — a heuristic is better than confidently wrong
    items.
    """
    m = bundle.instruction_map or {}
    if not m:
        return {}
    if m.get('source_stamp') and bundle.instruction_stamp and \
            m['source_stamp'] != bundle.instruction_stamp:
        return {}
    # A string that is NOT in the text cannot be a name from the text. The model sometimes
    # confuses this: the instruction does not name any preparations at all (it describes a scheme),
    # while the
    # markup listed all 10 from the composition. Such a "surface" is worse than its
    # absence: it says the preparation is in the text, and every swap ran into the
    # placeholder. We check against the text itself — it is cheap and self-healing.
    body = im.fold(im._TAG_RE.sub(' ', bundle.instruction_html or ''))
    out = {}
    for row in m.get('mentions') or []:
        if row.get('kind') == 'external':
            continue
        pid, surface = row.get('product'), (row.get('surface') or '').strip()
        if pid and surface and im.fold(surface) in body:
            out[int(pid)] = surface
    return out


# ---------------------------------------------------------------- difference

def diff_components(template: list[Comp], actual: list[Comp], instruction_html: str,
                    surfaces: dict[int, str] | None = None):
    """What changed and whether it affects the text.

    Returns (removed, added, qty_changed, material) — lists of Comp plus a flag 'there is a change
    that requires editing the text'.

    We compare multisets: then 'remove X and add Y' and 'replace X with Y' give the same result, so
    a manager who made the replacement in two clicks does not create a second entry in the queue.
    """
    t = Counter(c.key for c in template)
    a = Counter(c.key for c in actual)
    by_key_t = {c.key: c for c in template}
    by_key_a = {c.key: c for c in actual}

    removed = [by_key_t[k] for k in (t - a)]
    added = [by_key_a[k] for k in (a - t)]
    qty_changed = [by_key_a[k] for k in (set(t) & set(a)) if t[k] != a[k]]

    # Is this product mentioned in the instruction at all? If not (respirator, bag, gloves),
    # replacing the text is irrelevant.
    surfaces = surfaces or {}
    amb = im.ambiguous_products(template)
    referenced = set()
    for c in template:
        if not c.variant:
            continue
        if im.find_mentions(instruction_html, c.variant,
                            ambiguous_npk=c.variant.product_id in amb,
                            surface=surfaces.get(c.variant.product_id)):
            referenced.add(c.key)

    material = [c for c in removed if c.key in referenced]
    return removed, added, qty_changed, material


def substitution_signature(bundle, removed, added) -> str:
    """The cache key is THE REPLACEMENT ITSELF, not the order and not the full composition.

    "Product A → Product B" recurs in dozens of orders: the manager proofreads it
    once, the rest are printed without questions.
    """
    parts = sorted(['-%s' % c.key for c in removed] + ['+%s' % c.key for c in added])
    payload = f'v1|b{bundle.pk}|' + ';'.join(parts)
    return hashlib.sha1(payload.encode('utf-8')).hexdigest()[:32]


def human_summary(removed, added) -> str:
    """"Product A → Product B" / "removed: Respirator" — for the queue and for the log."""
    if len(removed) == 1 and len(added) == 1:
        return f'{removed[0].label} → {added[0].label}'
    bits = []
    if removed:
        bits.append('removed: ' + ', '.join(c.label for c in removed))
    if added:
        bits.append('added: ' + ', '.join(c.label for c in added))
    return '; '.join(bits) or 'composition changed'


def auto_substitute(html: str, removed, added, template,
                    surfaces: dict[int, str] | None = None) -> tuple[str, int, str, bool]:
    """Auto-replace: substitute the new name TOGETHER WITH THE PACK SIZE throughout the text.

    This is what the owner asked for: "replace product A with product B EVERYWHERE in the
    instruction". Works only for the unambiguous case 'one
    disappeared — one appeared'; otherwise the mapping is ambiguous and the text is left as is, with
    a note in the queue.

    The pack size travels with the name: the bundle is assembled so that the box contains exactly
    one package per treatment, so "Product A 50 g" becomes "Product B 10 ml".

    Also returns a line for a human (what happened to the dose) and a flag 'the dose can be
    trusted'. The flag goes off when a dose we did not take is left next to the name: printing a
    sheet with another product's pesticide dose is not allowed even with a warning.
    """
    if len(removed) != 1 or len(added) != 1:
        return html, 0, '', True
    old, new = removed[0], added[0]
    if not old.variant:
        return html, 0, '', True
    amb = im.ambiguous_products(template)
    out, n, swapped, left = im.replace_mentions(
        html, old.variant, new.label,
        ambiguous_npk=old.variant.product_id in amb,
        surface=(surfaces or {}).get(old.variant.product_id),
        new_qty=new.pack)
    if not n:
        return out, 0, '', True
    return out, n, _dose_note(old, new, swapped, left), not left


def _dose_note(old, new, swapped: list[str], left: list[str]) -> str:
    """In one line: what happened to the dose. This is the only thing the machine does not guarantee.

    Three different things that are easy to mistake for silence:
      — the dose was converted (the usual case);
      — a dose next to the name EXISTS but is written in a way we did not take: a FOREIGN dose is
      left under the new name, and this is the most dangerous thing that can happen here;
      — there is no dose next to the name at all — then there is nothing to say.

    We separately flag the case where the dose in the text did NOT equal the pack size of the old
    product: there the 'one package per treatment' convention did not apply (Product C is dosed at 10
    g from a 100 g pack), so the new dose must be checked by eye.
    """
    new_pack = _norm_dose(new.pack)
    if not new_pack:
        return f'the packaging of "{new.label}" is not determined — the old dose was left, please check'
    if left:
        return (f'DOSE NOT REPLACED: "{left[0]}" from '
                f'"{old.label}" is left next to the name — fix it by hand')
    if not swapped:
        return ''
    old_pack = _norm_dose(old.pack)
    was = ', '.join(dict.fromkeys(swapped))
    note = f'dose converted {was} → {new_pack}'
    if old_pack and any(_norm_dose(q) != old_pack for q in swapped):
        note += (f' — but in the text it did not equal the packaging of "{old.label}" '
                 f'({old_pack}), please check')
    return note


def _norm_dose(text: str) -> str:
    """"10 ml", "10ml", "10,0 ml" -> "10 ml". Empty if it is not a dose.

    LANGUAGE DATA: the unit words are the Ukrainian abbreviations (мл = ml, мг = mg, кг = kg, гр/г = g,
    л = l, шт = pcs)."""
    m = re.match(r'\s*(\d+(?:[.,]\d+)?)\s*(мл|мг|кг|гр|г|л|шт)(?![^\W\d_])',
                 text or '', re.I)
    if not m:
        return ''
    num = m.group(1).replace(',', '.')
    if '.' in num:
        num = num.rstrip('0').rstrip('.')
    return f'{num} {m.group(2).lower()}'


# ---------------------------------------------------------------- composition block

def composition_block(components: list[Comp]) -> str:
    """The "Bundle contents" block is GENERATED, not stored as text.

    Because of this the block physically cannot diverge from the box. In bundle 9950 it will fix
    itself from "Product A — 50 g" to "Product B — 10 ml".
    """
    rows = []
    for c in components:
        pack = f' — {c.pack}' if c.pack else ''
        qty = f' × {c.quantity}' if c.quantity > 1 else ''
        rows.append(f'<li>{c.label}{pack}{qty}</li>')
    return '<h3>Bundle contents</h3>\n<ul>\n' + '\n'.join(rows) + '\n</ul>'


# ---------------------------------------------------------------- resolve

def _expected_components(bundle, template: list[Comp], surfaces: dict) -> list[Comp]:
    """The composition the instruction was written FOR, according to the text markup.

    The markup says which products the text names as components. If it contains a product that is no
    longer in the template (Product A after replacement with Product B), it still counts as 'expected': the
    text mentions it, and until the text is fixed the sheet cannot be printed.

    Without markup we return the template — then the old 'template vs order' logic works, which
    catches only per-order replacements.
    """
    if not surfaces:
        return template

    from .models import ProductVariant

    recorded = (bundle.instruction_map or {}).get('components') or []
    packs = (bundle.instruction_map or {}).get('packs') or {}
    by_product = {c.variant.product_id: c for c in template if c.variant}

    # The most precise source is the composition recorded in the markup during normalization. It is
    # what distinguishes 'the product was there and is not mentioned' (respirator) from 'the product
    # was just put in instead of another' (Product B instead of Product A).
    wanted = list(recorded) if recorded else list(surfaces)

    out = []
    for pid in wanted:
        c = by_product.get(pid)
        if c is not None:
            out.append(c)
            continue
        # The product is no longer in the template — it was replaced in the bundle ENTIRELY. We take
        # the variant whose pack size is recorded in the markup: otherwise an arbitrary one is
        # picked ("Product J 280 ml" instead of "10 ml"), and the 'dose = package' check will lie.
        qs = ProductVariant.objects.filter(product_id=pid).order_by('pk')
        want = (packs.get(str(pid)) or '').strip().lower()
        pv = None
        if want:
            pv = next((v for v in qs if Comp(v, 1).pack.lower() == want), None)
        pv = pv or qs.first()
        if pv:
            out.append(Comp(pv, 1))

    if not recorded:
        # Without a recorded composition we have to assume the unmentioned components were always
        # there — otherwise every respirator would be read as new.
        out += [c for c in template if c.variant and c.variant.product_id not in surfaces]
    return out


def resolve_bundle(bundle, components: list[Comp] | None = None, *,
                   create_pending=False, order=None, user=None) -> Resolution:
    """Instruction for a specific composition. Without create_pending it writes nothing to the DB."""
    from .models import BundleInstructionOverride

    if bundle is None:
        return Resolution(KIND_PLACEHOLDER, reason='the bundle is not determined')

    # There is no separate 'draft/ready' state: a non-empty text means the instruction is in effect.
    base = (bundle.instruction_html or '').strip()
    if not base:
        return Resolution(KIND_PLACEHOLDER, reason='there is no instruction', bundle=bundle)

    template = components_of_template(bundle)
    if components is None:
        components = template

    surfaces = surfaces_of(bundle)
    # WHAT to compare against. Not the template, but the composition the text was written FOR — this
    # is visible from the markup. The difference is fundamental: when the composition is changed in
    # the bundle ENTIRELY, the template and the order agree, and the template↔order comparison sees
    # nothing, although the old product remained in the text. This is exactly how a sheet with
    # "Product A 10 g" was printed silently after Product A was replaced with Product B.
    expected = _expected_components(bundle, template, surfaces)

    removed, added, qty_changed, material = diff_components(
        expected, components, base, surfaces)

    # The text does NOT name THIS product — so there is nothing to fix in the text.
    #
    # Most often this is a respirator, bag or gloves: they get swapped, and the instruction says not
    # a word about them. Previously such a replacement produced a stub, and a bundle with a
    # perfectly good instruction went out with the note "insert the paper one".
    # The bundle composition block in the sheet is assembled from the actual composition anyway, so
    # the printed page is correct.
    if not material:
        return Resolution(KIND_OK, html=base, source='template',
                          bundle=bundle, components=components)

    sig = substitution_signature(bundle, removed, added)
    summary = human_summary(removed, added)

    # Auto-replace of the name right at generation. An empty `draft` means the replacement is
    # ambiguous (more than one disappeared/appeared) — there is nothing to substitute.
    draft, n_repl, dose_note, dose_ok = auto_substitute(base, removed, added,
                                                        expected, surfaces)
    auto_ok = bool(n_repl) and dose_ok
    if dose_note:
        summary = f'{summary} — {dose_note}'

    ov = BundleInstructionOverride.objects.filter(bundle=bundle, signature=sig).first()

    if ov is None and create_pending:
        ov = BundleInstructionOverride.objects.create(
            bundle=bundle, signature=sig, summary=summary,
            status=BundleInstructionOverride.PENDING,
            instruction_html=draft, auto_substituted=auto_ok,
            base_stamp=bundle.instruction_stamp,
            composition=[{'sku': c.sku, 'name': c.name, 'qty': c.quantity} for c in components],
        )

    # A human-proofread version always takes priority over machine substitution.
    if ov and ov.status == BundleInstructionOverride.SAME:
        return Resolution(KIND_OK, html=base, source='template', signature=sig,
                          override=ov, bundle=bundle, components=components)

    if (ov and ov.status == BundleInstructionOverride.READY
            and ov.instruction_html.strip()
            and not (ov.base_stamp and ov.base_stamp != bundle.instruction_stamp)):
        return Resolution(KIND_OK, html=ov.instruction_html, source='override',
                          signature=sig, override=ov, bundle=bundle,
                          components=components)

    # The correction is stale: the base instruction was edited after confirmation, so the old
    # version of the correction is no longer about this text. We take the fresh auto-replacement.
    if ov and ov.base_stamp and ov.base_stamp != bundle.instruction_stamp and not ov.is_stale:
        BundleInstructionOverride.objects.filter(pk=ov.pk).update(is_stale=True)

    if auto_ok:
        # We print IMMEDIATELY, without waiting for proofreading: the product name is substituted
        # everywhere, and the composition block is assembled from the actual order composition
        # anyway. The pack size moved along with the name; what exactly happened to the dose is in
        # summary, and it also goes to the export panel. Printing is not blocked.
        return Resolution(KIND_OK, html=draft, source='auto', signature=sig,
                          summary=summary, override=ov, bundle=bundle,
                          components=components)

    # Substitution failed: the replacement is ambiguous (several disappeared and several appeared) —
    # the machine has no right to guess what maps to what.
    return Resolution(KIND_PLACEHOLDER, reason='the composition changed ambiguously — match it by hand',
                      signature=sig, summary=summary, override=ov,
                      bundle=bundle, components=components)


def resolve_order_item(item, *, create_pending=False, user=None) -> Resolution:
    """Instruction for an order item."""
    if not item.bundle_id:
        return Resolution(KIND_SKIP)

    bundle = item.bundle

    comps = components_of_item(item)
    if comps is None:
        return Resolution(KIND_PLACEHOLDER, reason='the composition of the item is unknown',
                          bundle=bundle)

    return resolve_bundle(bundle, comps, create_pending=create_pending,
                          order=item.order, user=user)
