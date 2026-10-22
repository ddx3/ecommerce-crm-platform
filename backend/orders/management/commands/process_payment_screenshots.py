"""Pass over pending bank_transfer orders that have receipt screenshots.

Does OCR via Gemini Vision and tries to find the matching transaction in
BankTransaction. On a match - auto-confirms.

Usage:
  python manage.py process_payment_screenshots
  python manage.py process_payment_screenshots --order 10056      # one order
  python manage.py process_payment_screenshots --hours 48         # for the last 48 hours
"""
from __future__ import annotations

from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from orders.models import Order
from orders.screenshot_match import match_screenshot


class Command(BaseCommand):
    help = 'OCR payment screenshots and auto-confirm matching bank transactions.'

    def add_arguments(self, parser):
        parser.add_argument('--order', type=str, default=None,
                            help='Process only the given order (order_number)')
        parser.add_argument('--hours', type=int, default=72,
                            help='Look for orders created in the last N hours (default: 72)')

    def handle(self, *args, order, hours, **opts):
        if order:
            qs = Order.objects.filter(order_number=order)
        else:
            cutoff = timezone.now() - timedelta(hours=hours)
            qs = (Order.objects
                  .filter(payment_method='bank_transfer',
                          payment_status='pending',
                          created_at__gte=cutoff)
                  .exclude(status='cancelled')
                  .exclude(payment_screenshot=''))

        orders = list(qs)
        if not orders:
            self.stdout.write('No orders with screenshots to process.')
            return

        self.stdout.write(f'Processing {len(orders)} orders…')
        auto = 0
        manual = 0
        skipped = 0

        for o in orders:
            self.stdout.write(f'• #{o.order_number} ({o.customer_name}, {o.total_price} ₴)')
            res = match_screenshot(o)
            if res.auto_confirmed:
                auto += 1
                self.stdout.write(self.style.SUCCESS(
                    f'  ✓ AUTO matched tx={res.matched_transaction_id}'
                ))
            elif res.candidates_count > 1:
                manual += 1
                self.stdout.write(self.style.WARNING(
                    f'  ? MANUAL — {res.candidates_count} candidates'
                ))
            else:
                skipped += 1
                self.stdout.write(f'  – {res.reason}')

        self.stdout.write('')
        self.stdout.write(self.style.SUCCESS(
            f'Done. auto={auto} manual={manual} skipped={skipped}'
        ))
