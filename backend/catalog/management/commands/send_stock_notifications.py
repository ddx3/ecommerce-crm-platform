"""Pass over the "notify me when available" subscriptions: sends emails to
those whose target is already in stock.

The main path is the packaging/bundle save signals. This pass is a safety net for availability
changes that bypass post_save (.update()/F(): stock return when an order is cancelled, bulk bag
recalculation), and especially for bundles: their availability is derived from their components and
changes without the bundle itself being saved.
Idempotent — a row is claimed before sending, so a repeat pass duplicates nothing.

  python manage.py send_stock_notifications
"""
from django.core.management.base import BaseCommand

from catalog.models import StockNotification


class Command(BaseCommand):
    help = 'Send back-in-stock emails for pending subscriptions whose target is available.'

    def handle(self, *args, **options):
        sent = StockNotification.send_due()
        if sent:
            self.stdout.write(f'Emails sent: {sent}')
