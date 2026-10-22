"""Run the matcher over all unmatched transactions.

Usage:
  python manage.py match_bank_transactions             # real run
  python manage.py match_bank_transactions --dry-run   # no changes, report only
  python manage.py match_bank_transactions --limit 50  # limit to N transactions

Run after `poll_monobank` (or simultaneously via a cron pipeline).
"""
from __future__ import annotations

from django.core.management.base import BaseCommand

from orders import bank_matcher


class Command(BaseCommand):
    help = 'Match unmatched BankTransaction records to pending bank_transfer Orders.'

    def add_arguments(self, parser):
        parser.add_argument('--limit', type=int, default=200)
        parser.add_argument('--dry-run', action='store_true')

    def handle(self, *args, limit, dry_run, **opts):
        results = bank_matcher.match_unmatched(limit=limit, dry_run=dry_run)
        if not results:
            self.stdout.write('No unmatched transactions.')
            return

        skipped = 0
        auto = 0
        manual = 0
        none = 0

        for r in results:
            if r['skipped']:
                skipped += 1
                continue
            if r.get('auto_confirmed_order'):
                auto += 1
                self.stdout.write(self.style.SUCCESS(
                    f'  ✓ AUTO  tx={r["tx_external_id"][:24]:<24} '
                    f'+{r["amount"]:>10}  {r["counterparty"][:25]:<25} '
                    f'→ order #{r["auto_confirmed_order"]} '
                    f'(score={r["candidates"][0]["score"]})'
                ))
            elif r['manual_queue_orders']:
                manual += 1
                top = r['candidates'][0]
                self.stdout.write(self.style.WARNING(
                    f'  ? MANUAL tx={r["tx_external_id"][:24]:<24} '
                    f'+{r["amount"]:>10}  {r["counterparty"][:25]:<25} '
                    f'→ candidates: {", ".join(r["manual_queue_orders"][:3])} '
                    f'(top score={top["score"]})'
                ))
            else:
                none += 1
                self.stdout.write(
                    f'  – none   tx={r["tx_external_id"][:24]:<24} '
                    f'+{r["amount"]:>10}  {r["counterparty"][:25]:<25}  '
                    f'({r["reason"]})'
                )

        self.stdout.write('')
        self.stdout.write(self.style.SUCCESS(
            f'Done. auto={auto} manual_queue={manual} no_match={none} '
            f'skipped={skipped} dry_run={dry_run}'
        ))
