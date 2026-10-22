# -*- coding: utf-8 -*-
"""Creates missing bundle contents snapshots (OrderItemBundleComponent) for the
bundle items of orders that don't have one.

WHY: new orders from the site get a bundle contents snapshot automatically
(OrderItem post_save signal). Imported (Horoshop) orders were created via
bulk_create — the signal didn't fire, so the snapshot is missing, and in the export their
contents were shown from the LIVE template (Bundle.items) with no way to edit them.
This command materializes the snapshot from the current template → the contents become editable
for this specific order (as with new ones), and VISUALLY the contents don't change (it is the same
template that was shown before).

Safe: OrderItemBundleComponent has no signals, stock is not written off (it is read only by
_apply_for_order_item on write-off/return, which is not called here).
Idempotent: items that have a snapshot are skipped.

Usage:
  python manage.py backfill_bundle_snapshots                 # dry-run (count only)
  python manage.py backfill_bundle_snapshots --order 12385   # one order, dry-run
  python manage.py backfill_bundle_snapshots --order 12385 --apply
  python manage.py backfill_bundle_snapshots --apply         # all
"""
from django.core.management.base import BaseCommand
from django.db import transaction
from django.db.models import Count

from orders.models import OrderItem, _ensure_bundle_components_snapshot


class Command(BaseCommand):
    help = 'Materializes missing bundle composition snapshots (so imported orders become editable, like new ones).'

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true',
                            help='Execute. Without the flag, only show the count (dry-run).')
        parser.add_argument('--order', type=str, default=None,
                            help='Limit to one order (order_number).')

    def handle(self, *args, **opts):
        qs = (OrderItem.objects
              .filter(bundle__isnull=False)
              .annotate(_nc=Count('bundle_components'))
              .filter(_nc=0)
              .select_related('bundle', 'order'))
        if opts['order']:
            qs = qs.filter(order__order_number=opts['order'])
        items = list(qs)
        scope = (' (order %s)' % opts['order']) if opts['order'] else ''
        self.stdout.write('Bundle items without a snapshot%s: %d' % (scope, len(items)))

        if not items:
            self.stdout.write(self.style.SUCCESS('Nothing to do — all of them have a snapshot.'))
            return
        if not opts['apply']:
            self.stdout.write(self.style.WARNING('DRY-RUN. Run with --apply to create them.'))
            return

        done = 0
        with transaction.atomic():
            for it in items:
                _ensure_bundle_components_snapshot(it)
                if it.bundle_components.exists():
                    done += 1
        self.stdout.write(self.style.SUCCESS('Created a composition snapshot for %d items.' % done))
