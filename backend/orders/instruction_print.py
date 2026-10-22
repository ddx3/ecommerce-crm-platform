# -*- coding: utf-8 -*-
"""Batch of instructions for printing: one PDF for duplex printing.

Padding rule - PER INSTRUCTION, not a running total:

    1 page  -> 2   (blank back side)
    2 pages -> 2
    3 pages -> 4   (blank back side)

Then the whole file prints as a single duplex job, and EVERY instruction starts on
the front side of a fresh sheet. Chromium cannot do `break-before: recto` (verified),
so a blank sheet can only be added by post-processing the finished PDF.

Duplex turns on BY ITSELF: if at least one of the sheets is two pages long, the whole
file is printed duplex and odd ones are padded with a blank back side. If all
instructions are single-page, there is nothing to pad, and printing is ordinary
single-sided.

Sheet order = docx page order, because both files go through
`orders.views.build_export_plan`. This is not a comment but a shared function - the
only reason the two files fit together at all.

When an instruction cannot be handed out without a human (it is missing, the
position's composition is unknown, the replacement is not verified), a BLANK sheet
takes its place. The position in the stack is kept, export is never blocked, and
what exactly needs to be inserted by hand is written in the export result panel,
not spent as ink on a sheet.

The number of sheets is counted PER ORDER, not per unit: three identical bundles in
one order = ONE instruction sheet, since it is one box for one buyer. The "Packing"
page counts the same way (orders/views.py `_add_bundle`: there `orders` is
a set of order_id).

Within an order the sheets go in the SAME order as the positions in the order
(2026-09-05, at the owner's request) - so the printout can be sorted simply by sheet
order, without searching the pile for the right bundle.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import tempfile

from django.template.loader import render_to_string

log = logging.getLogger(__name__)

_RENDER_SCRIPT = os.path.join(os.path.dirname(__file__), 'chromium_pdf.py')

# Beyond this it is almost certainly a broken template, not an instruction. We do not
# put such a sheet in the batch, so that one bundle does not eat all the paper; we
# report it.
MAX_PAGES = 6

# The size Chromium produces for `format='A4'` - NOT the ISO 595×842.
CHROMIUM_A4 = (595.92, 842.88)


class Sheet:
    """One sheet in the stack: whose instruction, under which number, and what its status is."""

    __slots__ = ('index', 'order', 'item', 'res', 'pdf_path', 'pages')

    def __init__(self, index, order, item, res):
        self.index = index
        self.order = order
        self.item = item
        self.res = res
        self.pdf_path = None
        self.pages = 0

    @property
    def bundle_name(self):
        it = self.item
        return (it.bundle.name if it.bundle_id else '') or it.product_variant_name

def collect_sheets(plan, excluded_item_ids=(), *, user=None):
    """Export plan -> list of sheets. The "needs review" queue is also filled here.

    ONE sheet per (order, bundle) pair. The number of bundles in a position does not
    multiply sheets: three identical bundles travel in one box to one buyer, and three
    identical instructions are not needed there.

    `create_pending=True` is deliberately here: the manager must see the replacement in
    the queue exactly when it would really go to print.
    """
    from catalog import instruction_resolve as ir

    sheets = []
    excluded = set(excluded_item_ids or ())
    for index, order, items in plan:
        seen = set()
        for item in items:
            if not item.bundle_id or item.pk in excluded:
                continue
            key = item.bundle_id
            if key in seen:          # same bundle in two positions - one sheet
                continue
            seen.add(key)
            res = ir.resolve_order_item(item, create_pending=True, user=user)
            if res.kind == ir.KIND_SKIP:
                continue
            sheets.append(Sheet(index, order, item, res))
    # Order - BY ORDERS: first all bundles of the first order, then of the second.
    # The stack goes alongside the docx, where each order is its own page under the same
    # number, so the packer assembles box after box without sorting through the pile.
    #
    # Within an order - by position pk, i.e. in the order the products appear IN THE ORDER
    # ITSELF: the owner wants to take printout sheets simply in order, not look for the
    # right bundle in the pile. Earlier this was the alphabetical packaging name (to match
    # the list on "Packing") - this was deliberately dropped at request, the
    # two lists may diverge in order, and that is acceptable.
    sheets.sort(key=lambda sh: (sh.index, sh.item.pk))
    return sheets


def _render_sheets(sheets, tmpdir):
    """Render all sheets in ONE browser launch."""
    from catalog import instruction_resolve as ir

    jobs = []
    for i, sh in enumerate(sheets):
        # We do not render a not-ready instruction at all: a blank sheet goes in its place,
        # and what exactly to insert is stated in the export result panel.
        if sh.res.kind != ir.KIND_OK:
            continue
        # The bundle composition is already in the instruction text - the generated block
        # duplicated it. We leave the sheet as the manager wrote it.
        html = render_to_string('print/bundle_instruction.html', {
            'title': sh.bundle_name,
            'body': sh.res.html,
        })
        html_path = os.path.join(tmpdir, f'{i:04d}.html')
        pdf_path = os.path.join(tmpdir, f'{i:04d}.pdf')
        with open(html_path, 'w', encoding='utf-8') as fh:
            fh.write(html)
        sh.pdf_path = pdf_path
        jobs.append({'html': html_path, 'pdf': pdf_path})

    manifest = os.path.join(tmpdir, 'manifest.json')
    with open(manifest, 'w', encoding='utf-8') as fh:
        json.dump({'jobs': jobs}, fh, ensure_ascii=False)

    # One cold browser start for the whole batch; after that each sheet takes hundreds of ms.
    timeout = min(600, 60 + 15 * len(jobs))
    subprocess.run([sys.executable, _RENDER_SCRIPT, '--manifest', manifest],
                   check=True, timeout=timeout, capture_output=True)


def _merge(sheets):
    """Merge the sheets. Duplex and padding turn on by themselves. Returns (bytes, report).

    First count the pages of all sheets, and only then decide: if at least one is two
    pages long, the whole file goes duplex, so every odd one must be padded with a blank
    back side. If there is none, there is nothing to pad, otherwise the manager would
    throw away a stack of blank sheets.
    """
    import pymupdf
    from catalog import instruction_resolve as ir

    counts = {}
    for i, sh in enumerate(sheets):
        if not sh.pdf_path or not os.path.exists(sh.pdf_path):
            continue
        src = pymupdf.open(sh.pdf_path)
        try:
            counts[i] = src.page_count
        finally:
            src.close()
    duplex = any(n > 1 for n in counts.values() if n <= MAX_PAGES)

    out = pymupdf.open()
    printed, oversize, blanks = [], [], 0

    def _blank():
        """A blank sheet of the same size as its neighbors.

        Chromium produces A4 as 595.92×842.88 pt, not the ISO 595×842. The printer would pull
        a sheet of a different size from another tray or scale it, so we take the size from
        the previous page, and use the constant only when there is no previous page yet.
        """
        if out.page_count:
            r = out[-1].rect
            out.new_page(width=r.width, height=r.height)
        else:
            out.new_page(width=CHROMIUM_A4[0], height=CHROMIUM_A4[1])

    try:
        for i, sh in enumerate(sheets):
            if sh.res.kind != ir.KIND_OK:
                # Instead of a placeholder - just a blank sheet: the place in the stack behind the
                # instruction is kept, no ink is spent.
                _blank()
                sh.pages = 1
                if duplex:
                    _blank(); blanks += 1
                printed.append(sh)
                continue
            if i not in counts:
                oversize.append((sh, 'not rendered'))
                continue
            src = pymupdf.open(sh.pdf_path)
            try:
                n = counts[i]               # count FROM THE FILE, not from assumptions
                if n == 0 or n > MAX_PAGES:
                    oversize.append((sh, f'{n} pages'))
                    continue
                out.insert_pdf(src)
                sh.pages = n
                if duplex and n % 2:
                    _blank()
                    blanks += 1
                printed.append(sh)
            finally:
                src.close()
        # garbage=4 deduplicates font subsets - on a batch this makes a difference of several times.
        data = out.tobytes(garbage=4, deflate=True)
    finally:
        out.close()
    return data, {'printed': printed, 'oversize': oversize,
                  'blanks': blanks, 'duplex': duplex}


def render_single(item, *, user=None):
    """PDF of the instruction for ONE position - to give to the buyer in their account
    ("My orders").

    Priority is instruction_snapshot (frozen at purchase time,
    orders.models._ensure_instruction_snapshot): this is what the customer would always
    see, no matter how much the bundle template or the instruction text itself is edited
    later. The live resolve (instruction_resolve, the same one used for printing in the
    CRM) stays only as a fallback for old orders with no snapshot - create_pending=False
    deliberately: the customer merely viewing the order page should not create a task for
    the manager to verify the replacement; it will be created anyway on the first print
    in the CRM.
    Returns (bytes|None, refusal_reason).
    """
    from catalog import instruction_resolve as ir

    if not item.bundle_id:
        return None, 'not a bundle'

    from django.utils.html import escape
    from .instruction_docx import strip_emoji

    display_name = strip_emoji(item.bundle.name)

    if item.instruction_snapshot.strip():
        body = item.instruction_snapshot
        components = ir.components_of_item(item) or []
    else:
        res = ir.resolve_order_item(item, create_pending=False, user=user)
        if res.kind != ir.KIND_OK or not res.html.strip():
            return None, res.reason or 'no instruction'
        # The same title as on the printout that goes into the box (see
        # instruction_docx._title_node): the bundle name. The instruction text itself
        # doesn't contain the title (AI normalization strips it), so we add it as a generated
        # h1 rather than relying on the text.
        body = f'<h1>{escape(display_name)}</h1>{res.html}'
        components = res.components

    # "Bundle composition" is GENERATED from the position's frozen
    # components (not from the text: the instruction text itself does not contain it, AI
    # normalization cuts it out), and CRM printing does the same (instruction_docx.build).
    # It is safe to recompute every time, even for a frozen instruction: the components
    # come from the snapshot fields of OrderItemBundleComponent (name/SKU at purchase
    # time), not from the live product variant - so it never drifts apart.
    comp_html = ''
    try:
        comp_html = ir.composition_block(components)
    except Exception:
        log.exception('composition_block failed for item=%s', item.pk)

    html = render_to_string('print/bundle_instruction.html', {
        'title': display_name,
        'body': body,
        'composition_html': comp_html,
    })
    with tempfile.TemporaryDirectory() as tmp:
        html_path = os.path.join(tmp, 'instruction.html')
        pdf_path = os.path.join(tmp, 'instruction.pdf')
        with open(html_path, 'w', encoding='utf-8') as fh:
            fh.write(html)
        manifest = os.path.join(tmp, 'manifest.json')
        with open(manifest, 'w', encoding='utf-8') as fh:
            json.dump({'jobs': [{'html': html_path, 'pdf': pdf_path}]}, fh, ensure_ascii=False)
        subprocess.run([sys.executable, _RENDER_SCRIPT, '--manifest', manifest],
                       check=True, timeout=90, capture_output=True)
        with open(pdf_path, 'rb') as fh:
            return fh.read(), None


def build(plan, excluded_item_ids=(), *, user=None):
    """Build a PDF with instructions. Returns (bytes|None, report).

    An empty result (None) is not an error: the selected orders simply contain no
    bundles. The caller must return the docx and say so in words.
    """
    from catalog import instruction_resolve as ir

    sheets = collect_sheets(plan, excluded_item_ids, user=user)
    if not sheets:
        return None, {'printed': [], 'oversize': [], 'blanks': 0,
                      'placeholders': [], 'duplex': False}

    with tempfile.TemporaryDirectory() as tmp:
        _render_sheets(sheets, tmp)
        data, report = _merge(sheets)

    report['placeholders'] = [
        {'index': sh.index, 'order_number': sh.order.order_number,
         'bundle': sh.bundle_name, 'reason': sh.res.reason,
         'summary': sh.res.summary,
         'override_id': sh.res.override.pk if sh.res.override else None}
        for sh in sheets if sh.res.kind != ir.KIND_OK
    ]
    report['sheets'] = sum(sh.pages for sh in report['printed'])
    report['kits'] = len(sheets)
    return data, report
