"""Pulls the Privat24 for Business statement for all active BankAccount(source='privat').

Usage:
  python manage.py poll_privat                 # delta from last_synced_at or 24h
  python manage.py poll_privat --hours 168     # backfill for 7 days
  python manage.py poll_privat --account UA... # only one IBAN
  python manage.py poll_privat --dry-run       # no DB writes

  # Current day: fast, but no history
  python manage.py poll_privat --interim

PrivatBank has NO webhooks - polling only. For prod set up a cron every 1-3 minutes.
"""
from __future__ import annotations

import logging
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from orders import privatbank_business as privat
from orders.models import BankAccount, BankTransaction

log = logging.getLogger(__name__)

DEFAULT_LOOKBACK_HOURS = 24


class Command(BaseCommand):
    help = 'Poll Privat24 for Business statements and store as BankTransaction.'

    def add_arguments(self, parser):
        parser.add_argument('--hours', type=int, default=None,
                            help='How many hours back to pull (default: from last_synced_at or 24h)')
        parser.add_argument('--account', type=str, default=None,
                            help='Limit to one IBAN')
        parser.add_argument('--interim', action='store_true',
                            help='Use only /transactions/interim (fast, today only)')
        parser.add_argument('--dry-run', action='store_true', help='Do not write to the DB')

    def handle(self, *args, hours, account, interim, dry_run, **opts):
        qs = BankAccount.objects.filter(source='privat', is_active=True)
        if account:
            qs = qs.filter(iban=account)
        accounts = list(qs)
        if not accounts:
            self.stdout.write(self.style.WARNING(
                'No active Privat BankAccount. Register one via '
                '`privat_register_account <IBAN> --name "..."`'
            ))
            return

        self.stdout.write(f'Polling {len(accounts)} Privat account(s)…')
        total_new = 0
        total_seen = 0

        for acc in accounts:
            self.stdout.write('')
            self.stdout.write(f'• {acc.name} ({acc.iban})')

            try:
                if interim:
                    items = privat.get_statement_interim(acc.iban)
                else:
                    now = timezone.now()
                    if hours is not None:
                        frm = now - timedelta(hours=hours)
                    elif acc.last_synced_at:
                        frm = acc.last_synced_at - timedelta(minutes=15)
                    else:
                        frm = now - timedelta(hours=DEFAULT_LOOKBACK_HOURS)

                    max_frm = now - timedelta(days=privat.MAX_STATEMENT_RANGE_DAYS)
                    if frm < max_frm:
                        self.stdout.write(self.style.WARNING(
                            f'  ⚠ frm > {privat.MAX_STATEMENT_RANGE_DAYS} days — clamping'
                        ))
                        frm = max_frm

                    items = privat.get_statement(acc.iban, frm=frm, to=now)
            except privat.PrivatBusinessRateLimited as e:
                # We do not advance the cursor - the next run will take a wider window.
                log.warning('poll_privat: rate-limited on %s: %s', acc.name, e)
                self.stderr.write(self.style.ERROR(f'  rate-limited: {e}'))
                continue
            except privat.PrivatBusinessError as e:
                log.error('poll_privat: API error on %s: %s', acc.name, e)
                self.stderr.write(self.style.ERROR(f'  API error: {e}'))
                continue

            self.stdout.write(f'  ↳ {len(items)} item(s) in the response')
            total_seen += len(items)

            new_count = 0
            for parsed in privat.iter_statement_items(items, acc.iban):
                if dry_run:
                    sign = '+' if parsed['amount'] > 0 else ''
                    self.stdout.write(
                        f'    [dry] {parsed["transacted_at"]:%d.%m %H:%M} '
                        f'{sign}{parsed["amount"]:>10}  {parsed["counterparty_name"][:30]:<30} '
                        f'| {parsed["comment"][:50]}'
                    )
                    continue
                _, created = BankTransaction.objects.update_or_create(
                    source='privat', external_id=parsed['external_id'],
                    defaults={
                        'account': acc,
                        'transacted_at': parsed['transacted_at'],
                        'amount': parsed['amount'],
                        'currency_code': parsed['currency_code'],
                        'description': parsed['description'],
                        'comment': parsed['comment'],
                        'counterparty_name': parsed['counterparty_name'],
                        'raw_data': parsed['raw_data'],
                    },
                )
                if created:
                    new_count += 1

            total_new += new_count
            self.stdout.write(f'  ↳ new: {new_count}')
            if new_count:
                log.info('poll_privat: %s — %d new transactions', acc.name, new_count)

            if not dry_run and items:
                with transaction.atomic():
                    BankAccount.objects.filter(pk=acc.pk).update(
                        last_synced_at=timezone.now(),
                        last_transaction_external_id=privat.transaction_external_id(
                            items[0], acc.iban,
                        ),
                    )

        self.stdout.write('')
        self.stdout.write(self.style.SUCCESS(
            f'Done. seen={total_seen} new={total_new} dry_run={dry_run}'
        ))
