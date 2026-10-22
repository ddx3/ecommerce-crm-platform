"""Bulk regeneration of product content from raw_text via LLM.

For each product with a non-empty raw_text:
  • description        — OVERWRITE (general description; the LLM sees the packaging names,
                         so as not to tie the description to a single packaging);
  • application_norms  — OVERWRITE;
  • composition        — OVERWRITE (old rows are deleted, fresh ones are written);
  • characteristics    — ADDITIVE: append ONLY new ones that are neither among
                         the product's attributes nor among the existing characteristics.
                         Dedup is double: the LLM by meaning (it sees the existing ones) + code by
                         normalized name.

Attributes are NOT touched — they are only passed to the LLM as a "do not repeat" context.

Examples:
    python manage.py regenerate_content --limit 10
    python manage.py regenerate_content --limit 10 --dry-run
    python manage.py regenerate_content --ids 6452,6311,6300
    python manage.py regenerate_content --all
"""
import re

from django.core.management.base import BaseCommand
from django.db import transaction

from catalog.models import Product, ProductCharacteristic, ProductComposition
from catalog.views import _load_prompt
from core import llm


def _norm(s):
    """Name normalization for dedup: lower case, no spaces/colons at the edges."""
    return re.sub(r'\s+', ' ', (s or '').strip().lower()).rstrip(':').strip()


class Command(BaseCommand):
    help = 'Regenerates product description/norms/composition/characteristics from raw_text via LLM'

    def add_arguments(self, parser):
        parser.add_argument('--ids', type=str, default='', help='Comma-separated list of ids')
        parser.add_argument('--limit', type=int, default=0, help='Process the first N products with raw_text')
        parser.add_argument('--all', action='store_true', help='All products with raw_text')
        parser.add_argument('--dry-run', action='store_true', help='Do not write to the DB, only show the result')
        parser.add_argument('--force', action='store_true', help='Ignore the checkpoint (process even those already done)')

    def handle(self, *args, **opts):
        from django.conf import settings
        qs = Product.objects.exclude(raw_text='').exclude(raw_text__isnull=True)
        if opts['ids']:
            ids = [int(x) for x in opts['ids'].split(',') if x.strip().isdigit()]
            qs = Product.objects.filter(id__in=ids)
        elif opts['limit']:
            qs = qs[:opts['limit']]
        elif not opts['all']:
            self.stderr.write('Specify --ids, --limit or --all')
            return

        # --- checkpoint: ids of already processed products (for recovery after a crash) ---
        ckpt = settings.BASE_DIR / 'regenerate_content_done.log'
        done = set()
        if not opts['force'] and ckpt.exists():
            for ln in ckpt.read_text(encoding='utf-8').splitlines():
                if ln.strip().isdigit():
                    done.add(int(ln.strip()))
            if done:
                self.stdout.write(f'Checkpoint: {len(done)} already done, skipping them.')

        prompt_base = _load_prompt('extract_blocks.txt')
        dry = opts['dry_run']
        total = ok = failed = skipped_done = 0

        for p in qs:
            total += 1
            if p.id in done:
                skipped_done += 1
                continue
            try:
                processed = self._process_one(p, prompt_base, dry)
            except Exception as e:
                failed += 1
                self.stderr.write(f'  ✗ [{p.id}] {p.name[:40]}: {type(e).__name__}: {str(e)[:120]}')
                continue
            if processed:
                ok += 1
                if not dry:
                    with open(ckpt, 'a', encoding='utf-8') as f:
                        f.write(f'{p.id}\n')

        self.stdout.write(self.style.SUCCESS(
            f'\nDone: reviewed {total}, processed {ok}, errors {failed}, '
            f'skipped (checkpoint) {skipped_done}'
            + (' (DRY-RUN, nothing written)' if dry else '')))

    def _process_one(self, p, prompt_base, dry):
        """Processes one product. Returns True if anything was done, False if nothing."""
        raw = (p.raw_text or '').strip()
        if not raw:
            return False

        # --- context for the LLM ---
        attr_names = set()
        variant_names = []
        for v in p.variants.all():
            if v.name:
                variant_names.append(v.name)
            for av in v.attribute_values.select_related('attribute'):
                attr_names.add(av.attribute.name)
        existing_chars = list(
            p.characteristics.filter(variant__isnull=True).values_list('name', 'value')
        )

        ctx = ['\n\n═══════════════════════════════════════════════════',
               'CONTEXT OF THIS PRODUCT (take it into account when generating):',
               '═══════════════════════════════════════════════════']
        if p.name:
            ctx.append(f'Product name: {p.name}. Start the description (description) with exactly this name '
                       '(the first sentence is the introduction), and mention the name only once.')
        if attr_names:
            ctx.append('Product attributes (do NOT duplicate them in characteristics): '
                       + ', '.join(sorted(attr_names)))
        if existing_chars:
            ctx.append('Characteristics that already exist (do NOT repeat them, pick out only the new ones): '
                       + '; '.join(f'{n}: {val}' for n, val in existing_chars))
        ctx.append('In characteristics return ONLY new items that are neither in the '
                   'attributes nor in the existing characteristics. Check that they are '
                   'logical and do not duplicate each other.')
        if variant_names:
            ctx.append('Product packagings: ' + ', '.join(variant_names)
                       + '. The description must be GENERAL for the whole product — '
                       'do not tie it to one specific packaging.')

        full = prompt_base + '\n'.join(ctx) + '\n\n---\n\n' + raw

        data = llm.text_to_json(full)   # the error is caught by handle() → failed++

        desc = (data.get('description') or '').strip()
        norms = (data.get('application_norms') or '').strip()
        comp = data.get('composition') or []
        chars = data.get('characteristics') or []

        # --- dedup of new characteristics by normalized name ---
        blocked = {_norm(n) for n in attr_names} | {_norm(n) for n, _ in existing_chars}
        new_chars, skipped = [], 0
        for c in chars:
            if not isinstance(c, dict):
                continue
            cn = (c.get('name') or '').strip()
            cv = (c.get('value') or '').strip()
            if not cn or not cv:
                continue
            key = _norm(cn)
            if key in blocked:
                skipped += 1
                continue
            blocked.add(key)
            new_chars.append((cn[:100], cv[:300]))   # model field limits

        comp_clean = [(c.get('name', '').strip()[:200], c.get('value', '').strip()[:300])
                      for c in comp if isinstance(c, dict) and (c.get('name') or '').strip()]

        line = (f'  ✓ [{p.id}] {p.name[:40]:<40} '
                f'desc={len(desc)} norms={len(norms)} '
                f'+chars={len(new_chars)}(skip {skipped}) comp={len(comp_clean)}')
        self.stdout.write(line)

        if dry:
            return True

        with transaction.atomic():
            # overwrite
            p.description = desc
            p.application_norms = norms
            p.save(update_fields=['description', 'application_norms'])

            # composition — full overwrite
            p.compositions.all().delete()
            for i, (n, val) in enumerate(comp_clean):
                ProductComposition.objects.create(product=p, name=n, value=val, sort_order=i)

            # characteristics — additive (only new shared ones)
            base = p.characteristics.filter(variant__isnull=True).count()
            for i, (n, val) in enumerate(new_chars):
                ProductCharacteristic.objects.create(
                    product=p, variant=None, name=n, value=val, sort_order=base + i)
        return True
