"""Pulls the statement from the Mono Personal API for all active BankAccount(source='mono').

Usage:
  python manage.py poll_monobank                  # delta from last_synced_at or 24h
  python manage.py poll_monobank --hours 168      # last 7 days (backfill)
  python manage.py poll_monobank --account <id>   # only one account
  python manage.py poll_monobank --dry-run        # show, save nothing

Rate limit: Mono allows 1 request/60s per account. With several accounts the command
waits between them itself (via the local guard in `monobank_personal._rate_guard`).

The command is idempotent - dedup by (source, external_id).
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone as dt_timezone

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from orders import monobank_personal
from orders.models import BankAccount, BankTransaction

log = logging.getLogger(__name__)

# Mono allows a range of up to 31 days. It is better to backfill large ranges with a
# single request than to split them - so as not to eat the rate limit.
DEFAULT_LOOKBACK_HOURS = 24


class Command(BaseCommand):
    help = 'Poll Mono Personal API for transactions and store them as BankTransaction.'

    def add_arguments(self, parser):
        parser.add_argument('--hours', type=int, default=None,
                            help='How many hours back to pull (default: from last_synced_at or 24h)')
        parser.add_argument('--account', type=str, default=None,
                            help='Limit to one external_id (Mono accountId)')
        parser.add_argument('--dry-run', action='store_true',
                            help='Do not write to the DB, only show')

    def handle(self, *args, hours, account, dry_run, **opts):
        qs = BankAccount.objects.filter(source='mono', is_active=True)
        if account:
            qs = qs.filter(external_id=account)

        accounts = list(qs)
        if not accounts:
            self.stdout.write(self.style.WARNING(
                'No active Mono BankAccount. Register one first: '
                '`python manage.py mono_register_account <id> --name "..."`'
            ))
            return

        self.stdout.write(f'Polling {len(accounts)} Mono account(s)…')
        total_new = 0
        total_seen = 0

        for acc in accounts:
            self.stdout.write('')
            self.stdout.write(f'• {acc.name} ({acc.external_id})')

            now = timezone.now()
            if hours is not None:
                frm = now - timedelta(hours=hours)
            elif acc.last_synced_at:
                # Take with a small overlap so as not to miss a transaction at the boundary.
                frm = acc.last_synced_at - timedelta(minutes=10)
            else:
                frm = now - timedelta(hours=DEFAULT_LOOKBACK_HOURS)

            # Mono cap - 31 days.
            max_frm = now - timedelta(days=31)
            if frm < max_frm:
                self.stdout.write(self.style.WARNING(
                    f'  ⚠ frm={frm:%Y-%m-%d} is more than 31 days ago — clamping to 31 days'
                ))
                frm = max_frm

            try:
                items, complete = monobank_personal.get_statement_paged(
                    acc.external_id, frm=frm, to=now,
                )
            except monobank_personal.MonoPersonalRateLimited as e:
                # We do not advance the cursor - the next run will simply take a wider window.
                log.warning('poll_monobank: rate-limited on %s: %s', acc.name, e)
                self.stderr.write(self.style.ERROR(f'  rate-limited: {e}'))
                continue
            except monobank_personal.MonoPersonalError as e:
                log.error('poll_monobank: API error on %s: %s', acc.name, e)
                self.stderr.write(self.style.ERROR(f'  API error: {e}'))
                continue

            if not complete:
                # Could not page all the way back to frm - a gap remains between frm and the oldest
                # received operation. The cursor below will move to `now`, so without a manual
                # backfill
                # these transactions will never arrive.
                oldest = min((int(it.get('time', 0)) for it in items), default=0)
                oldest_dt = datetime.fromtimestamp(oldest, tz=dt_timezone.utc) if oldest else None
                log.error(
                    'poll_monobank: statement %s is INCOMPLETE — hit the Mono limit (500/page). '
                    'Gap in the window %s … %s. A manual backfill of this range is needed.',
                    acc.name, frm, oldest_dt,
                )
                self.stderr.write(self.style.ERROR(
                    f'  ⚠ INCOMPLETE statement: gap {frm:%d.%m %H:%M} … '
                    f'{oldest_dt:%d.%m %H:%M}' if oldest_dt else '  ⚠ INCOMPLETE statement'
                ))

            self.stdout.write(f'  ↳ {len(items)} item(s) in the response (frm={frm:%d.%m %H:%M})')
            total_seen += len(items)

            new_count = 0
            for parsed in monobank_personal.iter_statement_items(items, acc.external_id):
                if dry_run:
                    sign = '+' if parsed['amount'] > 0 else ''
                    self.stdout.write(
                        f'    [dry] {parsed["transacted_at"]:%d.%m %H:%M} '
                        f'{sign}{parsed["amount"]:>10}  {parsed["description"][:40]:<40} '
                        f'| {parsed["comment"][:40]}'
                    )
                    continue
                obj, created = BankTransaction.objects.update_or_create(
                    source='mono', external_id=parsed['external_id'],
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
                log.info('poll_monobank: %s — %d new transactions', acc.name, new_count)

            if not dry_run and items:
                # The newest transaction is items[0] (Mono returns newest first, and the first page
                # of
                # the paged traversal is also the newest).
                with transaction.atomic():
                    BankAccount.objects.filter(pk=acc.pk).update(
                        last_synced_at=now,
                        last_transaction_external_id=monobank_personal.statement_item_external_id(
                            items[0], acc.external_id,
                        ),
                    )

        self.stdout.write('')
        self.stdout.write(self.style.SUCCESS(
            f'Done. seen={total_seen} new={total_new} dry_run={dry_run}'
        ))
