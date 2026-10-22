# -*- coding: utf-8 -*-
"""Resolves the BundleInstructionOverride (PENDING) queue via an LLM — with no human involved.

A row in PENDING means the deterministic substitution (auto_substitute) could not cope on its own:
the replacement is ambiguous (N-to-M), or the dose next to the new name could not be confirmed.
Previously these waited for a human (the "Reconcile with stock" button + manual
review in the admin). Now the command runs the bundle through instruction_agent.normalize_by_mode()
itself (script, with the agent when the check fails) and applies the result.

The same run also finishes the queue of the "Run through AI" button
(InstructionJob): runs that hit the AI limit wait there until the limit recovers
(instruction_jobs.py).

Rejected (the model itself rejected its result — an invented or lost pesticide dose) does NOT stop
the queue and does NOT need a human: the row stays PENDING and is retried the next time the command
runs (from the scheduler). Same for rate limits / network failures. No exceptions: everything that
could not be applied simply waits for the next run.
"""
import logging
import time

from django.core.management.base import BaseCommand
from django.utils import timezone

from catalog import instruction_agent as ia
from catalog import instruction_jobs
from catalog import instruction_resolve as ir
from catalog.models import BundleInstructionOverride, InstructionJob
from core.llm import SubscriptionLimit

log = logging.getLogger(__name__)


class Command(BaseCommand):
    help = 'Resolve the instruction queue (BundleInstructionOverride) via an LLM'

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true',
                            help='only show what would be processed')
        parser.add_argument('--limit', type=int, default=5,
                            help='how many rows to process per run (pacing for the shared LLM limit)')
        parser.add_argument('--pace', type=float, default=0,
                            help='pause between rows, in seconds (0 = derive from the limit)')

    def handle(self, *args, **opts):
        qs = (BundleInstructionOverride.objects
              .filter(status=BundleInstructionOverride.PENDING)
              .select_related('bundle')
              .order_by('created_at'))
        todo = list(qs[:opts['limit']])

        self.stdout.write(f'in the queue: {qs.count()}, taking on: {len(todo)}')
        if opts['dry_run']:
            for ov in todo:
                self.stdout.write(f'  override{ov.pk} bundle{ov.bundle_id} {ov.summary}')
            self.stdout.write(f'instruction runs waiting for the AI limit: '
                              f'{InstructionJob.objects.filter(status=InstructionJob.PENDING).count()}')
            return

        from django.conf import settings
        pace = opts['pace'] or (settings.LLM_RATE_WINDOW / max(1, settings.LLM_RATE_LIMIT) + 1)

        # First the ones a human triggered with the button and that hit the AI limit: they are
        # waiting for the result.
        jobs = instruction_jobs.process(limit=opts['limit'], pace=pace)
        if any(jobs[k] for k in ('done', 'stale', 'failed', 'waiting')) or jobs['stopped']:
            self.stdout.write(
                f"button queue: done {jobs['done']}, not applied {jobs['stale']}, "
                f"failed {jobs['failed']}, waiting for the composition to be saved {jobs['waiting']}"
                + (' — the AI limit has not recovered yet' if jobs['stopped'] else ''))
        if jobs['stopped']:
            return        # the rest will hit the same limit

        resolved, failed = 0, 0
        for n, ov in enumerate(todo, 1):
            bundle = ov.bundle
            if bundle is None:
                continue
            name = bundle.name[:44]
            self.stdout.write(f'[{n}/{len(todo)}] override{ov.pk} bundle{bundle.pk} {name} … ',
                              ending='')
            self.stdout.flush()
            if n > 1:
                time.sleep(pace)
            t0 = time.time()
            try:
                # components=None -> the bundle's current template. No human: auto (script, with the
                # agent
                # only on rejection), so the queue doesn't eat the daily token limit.
                res = ia.normalize_by_mode(bundle, None, settings.INSTRUCTION_BACKGROUND_MODE)
            except SubscriptionLimit as e:
                # The subscription limit is shared across all rows: the rest will hit it too. We
                # stop —
                # the rows stay PENDING and will go in the next scheduler run (every 10 min).
                log.warning('resolve_pending_instructions: subscription limit, stopping the run: %s', e)
                self.stdout.write(self.style.WARNING(f'subscription limit — stopping: {e}'))
                failed += len(todo) - n + 1
                break
            except Exception as e:            # noqa: BLE001 — Rejected comes here too, on purpose
                log.warning('resolve_pending_instructions: override=%s bundle=%s: %s',
                           ov.pk, bundle.pk, e)
                failed += 1
                self.stdout.write(self.style.WARNING(
                    f'leaving it in the queue ({time.time() - t0:.0f} s): {e}'))
                continue

            ov.instruction_html = res['html']
            ov.status = BundleInstructionOverride.READY
            ov.auto_substituted = True
            ov.is_stale = False
            ov.base_stamp = bundle.instruction_stamp
            ov.resolved_by = None
            ov.resolved_at = timezone.now()
            ov.save(update_fields=['instruction_html', 'status', 'auto_substituted',
                                   'is_stale', 'base_stamp', 'resolved_by',
                                   'resolved_at', 'updated_at'])

            # An override whose removed composition matches the bundle's CURRENT template is not a
            # one-off replacement for a single order but a permanent composition change (it got here
            # via _resync_instruction_after_swap): we also sync the bundle's own instruction_html,
            # otherwise the CRM shows the old text even though the new one is already going to
            # print.
            current = [{'sku': c.sku, 'name': c.name, 'qty': c.quantity}
                      for c in ir.components_of_template(bundle)]
            if ov.composition == current:
                bundle.instruction_html = res['html']
                bundle.save(update_fields=['instruction_html', 'instruction_stamp',
                                           'instruction_updated_at'])

            resolved += 1
            self.stdout.write(self.style.SUCCESS(f'done ({time.time() - t0:.0f} s)'))

        self.stdout.write('')
        self.stdout.write(self.style.SUCCESS(f'resolved: {resolved}'))
        if failed:
            self.stdout.write(self.style.WARNING(f'left in the queue (retried next time): {failed}'))
