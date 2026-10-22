# -*- coding: utf-8 -*-
"""Make bundle instructions from the "Raw text" drafts.

There are dozens of bundles without an instruction, yet each one already has a draft written — it
sits in the bundle's "Raw text" field. This command moves it into the instruction field and
immediately runs it through the standardization prompt: the same thing the magic wand does in the
CRM, just in batch.

The model's result goes through the same deterministic dose check as in the CRM. When the check
rejects it, the draft STAYS in the instruction field (so the text doesn't have to be moved by hand a
second time), but the bundle goes into the report: it needs to be run through the wand and the text
fixed by eye.

Nothing is overwritten: bundles that already have an instruction are skipped.
"""
import re
import time

from django.core.management.base import BaseCommand

from catalog import instruction_agent as ia
from catalog import instruction_llm as il
from catalog import instruction_resolve as ir
from catalog.models import Bundle

_TAG_RE = re.compile(r'<[^>]+>')


def _plain_len(html):
    return len(re.sub(r'\s+', ' ', _TAG_RE.sub(' ', html or '')).strip())


class Command(BaseCommand):
    help = 'Move "Raw text" into the instruction and run it through standardization'

    def _normalize_retry(self, bundle, comps, tries, pace):
        """Run with retries: a failure here is almost always throttling, not the text.

        `Rejected` is NOT retried — it is the dose check's verdict, and a second run
        will change nothing. Only infrastructure failures are retried.
        """
        from django.conf import settings
        last = None
        for attempt in range(1, tries + 1):
            try:
                # Batch run without a human: auto (script, with the agent only when the dose check
                # fails).
                return ia.normalize_by_mode(bundle, comps, settings.INSTRUCTION_BACKGROUND_MODE)
            except il.Rejected:
                raise
            except Exception as e:                       # noqa: BLE001
                last = e
                if attempt < tries:
                    wait = pace * 3 * attempt
                    self.stdout.write(f'(attempt {attempt}: {e}; waiting {wait:.0f} s) ',
                                      ending='')
                    self.stdout.flush()
                    time.sleep(wait)
        raise last

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true',
                            help='only show what would be processed')
        parser.add_argument('--limit', type=int, default=0,
                            help='process at most N bundles')
        parser.add_argument('--only', type=str, default='',
                            help='comma-separated list of packaging pks')
        parser.add_argument('--min-chars', type=int, default=80,
                            help='a raw text shorter than this is not considered a draft')
        parser.add_argument('--pace', type=float, default=0,
                            help='pause between bundles, s (0 = derive from the limit)')
        parser.add_argument('--tries', type=int, default=3,
                            help='how many times to try a bundle on failure')

    def handle(self, *args, **opts):
        only = {int(x) for x in opts['only'].split(',') if x.strip().isdigit()}
        todo = []
        for b in Bundle.objects.order_by('pk'):
            if (b.instruction_html or '').strip():
                continue
            if only and b.pk not in only:
                continue
            src = b.raw_text if _plain_len(b.raw_text) >= opts['min_chars'] else ''
            if not src:
                continue
            todo.append((b, src))

        if opts['limit']:
            todo = todo[:opts['limit']]

        self.stdout.write(f'to process: {len(todo)} bundles')
        if opts['dry_run']:
            for b, src in todo:
                self.stdout.write(f'  bundle{b.pk:<5} {b.name[:44]:<46} '
                                  f'draft: {_plain_len(src)} chars')
            return

        # Pacing. The subscription allows 20 requests per 5 min, and a batch easily blows through
        # that —
        # especially when some calls fail INSTANTLY (throttling on the CLI side): the loop
        # speeds up and burns the limit for the rest. Keep the pause slightly above the
        # window/limit ratio so the queue moves evenly.
        from django.conf import settings
        pace = opts['pace'] or (settings.LLM_RATE_WINDOW / max(1, settings.LLM_RATE_LIMIT) + 1)
        self.stdout.write(f'pace: {pace:.0f} s between bundles, tries per bundle: {opts["tries"]}')

        ok, rejected, failed = [], [], []
        for n, (b, src) in enumerate(todo, 1):
            name = b.name[:44]
            self.stdout.write(f'[{n}/{len(todo)}] bundle{b.pk} {name} … ', ending='')
            self.stdout.flush()
            b.instruction_html = src
            comps = ir.components_of_template(b)
            t0 = time.time()
            if n > 1:
                time.sleep(pace)
            try:
                res = self._normalize_retry(b, comps, opts['tries'], pace)
            except il.Rejected as e:
                # Leave the draft in the field: moving it by hand a second time is wasted
                # work, and "instruction present but not cleaned up" is visible both in the CRM and
                # in
                # this report.
                b.save(update_fields=['instruction_html', 'instruction_stamp',
                                      'instruction_updated_at'])
                rejected.append((b.pk, name, str(e)))
                self.stdout.write(self.style.WARNING(f'rejected ({time.time() - t0:.0f} s)'))
                continue
            except Exception as e:                       # noqa: BLE001
                failed.append((b.pk, name, f'{type(e).__name__}: {e}'))
                self.stdout.write(self.style.ERROR(f'error: {e}'))
                continue

            b.instruction_html = res['html']
            b.instruction_map = il.build_map(res['html'], res['mentions'], comps)
            b.save(update_fields=['instruction_html', 'instruction_map',
                                  'instruction_stamp', 'instruction_updated_at'])
            ok.append((b.pk, name, res['warnings']))
            self.stdout.write(self.style.SUCCESS(f'done ({time.time() - t0:.0f} s)'))

        self.stdout.write('')
        self.stdout.write(self.style.SUCCESS(f'standardized: {len(ok)}'))
        warned = [x for x in ok if x[2]]
        if warned:
            self.stdout.write(f'  of them with warnings: {len(warned)}')
            for pk, name, ws in warned:
                self.stdout.write(f'    bundle{pk} {name}')
                for w in ws:
                    self.stdout.write(f'       • {w}')
        if rejected:
            self.stdout.write(self.style.WARNING(
                f'\nrejected by the check (draft left in the field, run it through the wand): '
                f'{len(rejected)}'))
            for pk, name, why in rejected:
                self.stdout.write(f'  bundle{pk} {name}\n     {why}')
        if failed:
            self.stdout.write(self.style.ERROR(f'\nerrors: {len(failed)}'))
            for pk, name, why in failed:
                self.stdout.write(f'  bundle{pk} {name}\n     {why}')
