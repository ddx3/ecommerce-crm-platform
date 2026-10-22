# -*- coding: utf-8 -*-
"""Freezes instruction_snapshot (the instruction text) for bundle items of
orders where it is still empty — the same principle as
backfill_bundle_snapshots for bundle contents.

WHY: before instruction_snapshot was introduced, the instruction text for an item was
resolved live from the current Bundle.instruction_html — i.e. it could
silently change retroactively if the bundle template was edited later.
This command materializes a snapshot from the CURRENT state
(the best available approximation — no historical snapshot from the time of purchase
exists), and from then on this order's instruction no longer changes.

Idempotent: items with an already filled instruction_snapshot are skipped.

Usage:
  python manage.py backfill_instruction_snapshots                 # dry-run (count only)
  python manage.py backfill_instruction_snapshots --order 12385   # one order, dry-run
  python manage.py backfill_instruction_snapshots --order 12385 --apply
  python manage.py backfill_instruction_snapshots --apply         # all
"""
from django.core.management.base import BaseCommand
from django.db import transaction

from orders.models import OrderItem, _ensure_instruction_snapshot


class Command(BaseCommand):
    help = 'Freezes instruction_snapshot for the bundle positions of orders where it is empty.'

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true',
                            help='Execute. Without the flag, only show the count (dry-run).')
        parser.add_argument('--order', type=str, default=None,
                            help='Limit to one order (order_number).')

    def handle(self, *args, **opts):
        qs = (OrderItem.objects
              .filter(bundle__isnull=False, instruction_snapshot='')
              .select_related('bundle', 'order'))
        if opts['order']:
            qs = qs.filter(order__order_number=opts['order'])
        items = list(qs)
        scope = (' (order %s)' % opts['order']) if opts['order'] else ''
        self.stdout.write('Bundle items without an instruction_snapshot%s: %d' % (scope, len(items)))

        if not items:
            self.stdout.write(self.style.SUCCESS('Nothing to do — all of them have a snapshot (or have no instruction).'))
            return
        if not opts['apply']:
            self.stdout.write(self.style.WARNING('DRY-RUN. Run with --apply to freeze them.'))
            return

        done = 0
        with transaction.atomic():
            for it in items:
                _ensure_instruction_snapshot(it)
                if it.instruction_snapshot:
                    done += 1
        self.stdout.write(self.style.SUCCESS(
            'Froze %d items (the rest have no instruction at the moment).' % done))
