"""Regenerate bundle DESCRIPTIONS from raw_text via an LLM.

Writes ONLY Bundle.description. Does NOT touch the bundle composition (BundleItem),
characteristics, prices, etc.

    python manage.py regenerate_bundles --limit 3 --dry-run
    python manage.py regenerate_bundles --ids 72,73,74
    python manage.py regenerate_bundles --all
"""
from django.core.management.base import BaseCommand
from django.db import transaction

from catalog.models import Bundle
from catalog.views import _load_prompt
from core import llm


class Command(BaseCommand):
    help = 'Regenerates bundle descriptions from raw_text (description only)'

    def add_arguments(self, parser):
        parser.add_argument('--ids', type=str, default='', help='Comma-separated list of bundle ids')
        parser.add_argument('--limit', type=int, default=0, help='First N bundles that have raw_text')
        parser.add_argument('--all', action='store_true', help='All bundles that have raw_text')
        parser.add_argument('--dry-run', action='store_true', help='Do not write to the DB')
        parser.add_argument('--force', action='store_true', help='Ignore the checkpoint')

    def handle(self, *args, **opts):
        from django.conf import settings
        qs = Bundle.objects.exclude(raw_text='').exclude(raw_text__isnull=True)
        if opts['ids']:
            ids = [int(x) for x in opts['ids'].split(',') if x.strip().isdigit()]
            qs = Bundle.objects.filter(id__in=ids)
        elif opts['limit']:
            qs = qs[:opts['limit']]
        elif not opts['all']:
            self.stderr.write('Specify --ids, --limit or --all')
            return

        ckpt = settings.BASE_DIR / 'regenerate_bundles_done.log'
        done = set()
        if not opts['force'] and ckpt.exists():
            for ln in ckpt.read_text(encoding='utf-8').splitlines():
                if ln.strip().isdigit():
                    done.add(int(ln.strip()))
            if done:
                self.stdout.write(f'Checkpoint: {len(done)} already done, skipping them.')

        prompt_base = _load_prompt('bundle_description.txt')
        dry = opts['dry_run']
        total = ok = failed = skipped = 0

        for b in qs:
            total += 1
            if b.id in done:
                skipped += 1
                continue
            try:
                processed = self._process_one(b, prompt_base, dry)
            except Exception as e:
                failed += 1
                self.stderr.write(f'  ✗ [{b.id}] {b.name[:40]}: '
                                  f'{type(e).__name__}: {str(e)[:120]}')
                continue
            if processed:
                ok += 1
                if not dry:
                    with open(ckpt, 'a', encoding='utf-8') as f:
                        f.write(f'{b.id}\n')

        self.stdout.write(self.style.SUCCESS(
            f'\nDone: reviewed {total}, processed {ok}, errors {failed}, '
            f'skipped (checkpoint) {skipped}'
            + (' (DRY-RUN, nothing written)' if dry else '')))

    def _process_one(self, b, prompt_base, dry):
        raw = (b.raw_text or '').strip()
        if not raw:
            return False

        title = b.name or ''
        ctx = ''
        if title:
            ctx = (f'\n\nBundle name: {title}. Start the description with exactly this name '
                   '(the first sentence is the introduction), and mention the name only once.')
        full = prompt_base + ctx + '\n\n---\n\n' + raw

        data = llm.text_to_json(full)
        desc = (data.get('description') or '').strip()
        norms = (data.get('application_norms') or '').strip()

        self.stdout.write(f'  ✓ [{b.id}] {b.name[:42]:<42} '
                          f'desc={len(desc)} norms={len(norms)}')

        if dry:
            return True

        with transaction.atomic():
            b.description = desc
            b.application_norms = norms
            b.save(update_fields=['description', 'application_norms'])
        return True
