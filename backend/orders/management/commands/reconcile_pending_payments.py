"""Reconcile unpaid Monobank orders with the real invoice status.

Run by cron every N minutes. What it does:
- takes all Orders with monobank_invoice_id and payment_status='pending', older than
  --threshold-minutes
  (and younger than POLL_MAX_AGE)
- polls the status via GET /api/merchant/invoice/status
- success → marks order as paid (same as the webhook does)
- expired/failure/reversed → cancels the order ONLY if it is abandoned: Monobank, status
  "New" (the manager has not touched it) and unpaid for longer than CANCEL_AFTER. The
  invoice is
  closed in the process (monobank_invoice_id=''), so the order drops out of the selection
- created/processing/hold → skipped (the user can still pay)

Anything the manager has touched (any status other than "New"), or with a different payment
method, is left alone: that is the manager's decision, the system does not override it.

The command is idempotent: re-running on already paid/cancelled orders changes nothing.
"""
from __future__ import annotations

import logging
from datetime import timedelta
from decimal import Decimal, ROUND_HALF_UP

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from orders.models import Order
from orders import monobank

log = logging.getLogger(__name__)

# A Monobank invoice lives 24 hours, but cancelling right after it expires is too early: of 15
# orders
# not paid within the first hour, 12 were paid later by another method (bank transfer), four of them
# after
# 1–7 days. An unpaid Monobank order does not hold stock, so a longer period costs nothing.
CANCEL_AFTER = timedelta(days=7)
# Older orders are not polled at all: that is already the manager's manual work, not abandoned
# carts,
# and without a limit they would be polled forever.
POLL_MAX_AGE = timedelta(days=14)


def _money(value) -> Decimal:
    return Decimal(str(value or 0)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)


def _auto_cancellable(order, now) -> bool:
    return (order.payment_method == 'monobank'
            and order.status == 'new'
            and order.payment_status == 'pending'
            and now - order.created_at >= CANCEL_AFTER)


class Command(BaseCommand):
    help = 'Reconcile pending Monobank orders against invoice status (cron-friendly).'

    def add_arguments(self, parser):
        parser.add_argument(
            '--threshold-minutes', type=int, default=30,
            help='How many minutes to wait before reconciling (default: 30)',
        )
        parser.add_argument(
            '--limit', type=int, default=200,
            help='Max orders per run (default: 200)',
        )
        parser.add_argument(
            '--dry-run', action='store_true',
            help='Only show what would be done, no DB changes',
        )

    def handle(self, *args, threshold_minutes, limit, dry_run, **opts):
        now = timezone.now()
        cutoff = now - timedelta(minutes=threshold_minutes)
        qs = (
            Order.objects
            .exclude(monobank_invoice_id='')
            .filter(payment_status='pending')
            .filter(created_at__lte=cutoff, created_at__gte=now - POLL_MAX_AGE)
            .exclude(status='cancelled')
            .order_by('created_at')[:limit]
        )

        total = qs.count()
        if not total:
            self.stdout.write('Nothing to reconcile.')
            return

        self.stdout.write(f'Reconciling {total} order(s) older than {threshold_minutes} min…')

        updated_paid = 0
        updated_cancelled = 0
        skipped = 0
        errors = 0

        for order in qs:
            try:
                info = monobank.get_invoice_status(order.monobank_invoice_id)
            except monobank.MonobankNotConfigured:
                self.stderr.write('MONOBANK_TOKEN is not configured — aborting.')
                return
            except monobank.MonobankError as e:
                log.warning('reconcile: status fetch failed for %s: %s', order.order_number, e)
                errors += 1
                continue

            status_value = (info.get('status') or '').lower()
            self.stdout.write(f'  order={order.order_number} invoice={order.monobank_invoice_id} status={status_value}')

            if status_value in monobank.SUCCESS_STATUSES:
                expected = _money(order.total_price)
                got = monobank.kopecks_to_money(int(info.get('amount') or 0))
                if got != expected:
                    log.error(
                        'reconcile: amount mismatch order=%s expected=%s got=%s',
                        order.order_number, expected, got,
                    )
                    errors += 1
                    continue
                if dry_run:
                    self.stdout.write(self.style.SUCCESS('    [dry-run] would mark paid'))
                else:
                    with transaction.atomic():
                        fresh = Order.objects.select_for_update().get(pk=order.pk)
                        if fresh.payment_status != 'paid':
                            fresh.payment_status = 'paid'
                            fresh.payment_notification = True
                            fresh.save(update_fields=['payment_status', 'payment_notification'])
                            self.stdout.write(self.style.SUCCESS('    → marked paid'))
                updated_paid += 1

            elif status_value in monobank.FAILURE_STATUSES:
                if not _auto_cancellable(order, now):
                    skipped += 1
                    continue
                if dry_run:
                    self.stdout.write(self.style.WARNING('    [dry-run] would cancel'))
                    updated_cancelled += 1
                else:
                    with transaction.atomic():
                        fresh = Order.objects.select_for_update().get(pk=order.pk)
                        # While we were talking to Monobank, the manager may have taken the order
                        # into work.
                        if _auto_cancellable(fresh, now):
                            fresh.status = 'cancelled'
                            fresh.monobank_invoice_id = ''
                            fresh.save(update_fields=['status', 'monobank_invoice_id'])
                            updated_cancelled += 1
                            self.stdout.write(self.style.WARNING('    → cancelled (unpaid, invoice closed)'))

            else:
                # created/processing/hold — the user can still pay
                skipped += 1

        self.stdout.write('')
        self.stdout.write(self.style.SUCCESS(
            f'Done. paid={updated_paid} cancelled={updated_cancelled} skipped={skipped} errors={errors}'
        ))
