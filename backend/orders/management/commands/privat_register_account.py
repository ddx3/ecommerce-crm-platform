"""Registers a Privat24 for Business account as a BankAccount.

Usage:
  python manage.py privat_register_account <IBAN> \
      --name "Privat sole proprietor <owner>"
  python manage.py privat_register_account UA55... --name "..." --inactive

Unlike Mono, Privat has no API for fetching the list of accounts - the IBAN must be
given explicitly (it is visible in the business cabinet in the account details).
"""
from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from orders import privatbank_business as privat
from orders.models import BankAccount


class Command(BaseCommand):
    help = 'Register a Privat24 for Business IBAN as BankAccount for monitoring.'

    def add_arguments(self, parser):
        parser.add_argument('iban', help='Sole proprietor (FOP) account IBAN (UA...)')
        parser.add_argument('--name', required=True, help='Internal name (for the admin)')
        parser.add_argument('--inactive', action='store_true', help='Create as inactive')
        parser.add_argument('--skip-check', action='store_true',
                            help='Do not verify the token via the API (for offline registration)')

    def handle(self, *args, iban, name, inactive, skip_check, **opts):
        iban = iban.strip().upper().replace(' ', '')
        if not iban.startswith('UA') or len(iban) != 29:
            raise CommandError(f'IBAN looks invalid: {iban} (expected "UA" + 27 characters)')

        # Optionally verify the token - make a balance request, the cheapest one.
        if not skip_check:
            try:
                privat.get_balance_interim(iban)
            except privat.PrivatBusinessNotConfigured:
                raise CommandError('PRIVAT_BUSINESS_TOKEN is not configured in backend/.env')
            except privat.PrivatBusinessError as e:
                self.stderr.write(self.style.WARNING(
                    f'⚠ The API check failed: {e}\n'
                    f'  Registering the account anyway. Verify the IBAN/token and run '
                    f'`poll_privat --account {iban} --dry-run` later.'
                ))

        defaults = {
            'name': name,
            'iban': iban,
            'masked_pan': '',
            'currency_code': 980,  # sole proprietor (FOP) accounts are UAH; change manually if another currency
            'is_active': not inactive,
            'metadata': {},
        }

        # For Privat we use the IBAN as external_id (unique within the bank).
        obj, created = BankAccount.objects.update_or_create(
            source='privat', external_id=iban, defaults=defaults,
        )
        verb = 'Created' if created else 'Updated'
        self.stdout.write(self.style.SUCCESS(f'{verb} BankAccount(pk={obj.pk}): {obj}'))
        self.stdout.write(f'  IBAN: {obj.iban}')
        self.stdout.write(f'  Active: {obj.is_active}')
        self.stdout.write('')
        self.stdout.write('For a backfill:  python manage.py poll_privat --hours 168')
