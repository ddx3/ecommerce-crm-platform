"""Registers a Mono account on our side as a BankAccount so the poller pulls its statement.

Usage:
  python manage.py mono_register_account <accountId> --name "Mono sole proprietor UAH"
  python manage.py mono_register_account <accountId> --name "Mono black 0000" --inactive

Before this you can call `mono_list_accounts` to get the right id.
"""
from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from orders import monobank_personal
from orders.models import BankAccount


class Command(BaseCommand):
    help = 'Register a Mono Personal API account as BankAccount for monitoring.'

    def add_arguments(self, parser):
        parser.add_argument('account_id', help='accountId from client-info')
        parser.add_argument('--name', required=True, help='Internal name (for the admin)')
        parser.add_argument('--inactive', action='store_true', help='Create as inactive')

    def handle(self, *args, account_id, name, inactive, **opts):
        try:
            info = monobank_personal.get_client_info()
        except monobank_personal.MonoPersonalError as e:
            raise CommandError(f'Mono API error: {e}')

        target = next((a for a in info.get('accounts', []) if a['id'] == account_id), None)
        if not target:
            raise CommandError(f'Account {account_id} not found in client-info. '
                               f'Check it with `mono_list_accounts`.')

        defaults = {
            'name': name,
            'iban': target.get('iban', ''),
            'masked_pan': ', '.join(target.get('maskedPan') or []),
            'currency_code': int(target.get('currencyCode') or 980),
            'is_active': not inactive,
            'metadata': {
                'type': target.get('type'),
                'sendId': target.get('sendId'),
                'cashbackType': target.get('cashbackType'),
                'creditLimit': target.get('creditLimit'),
                'client_name': info.get('name'),
                'client_id': info.get('clientId'),
            },
        }

        obj, created = BankAccount.objects.update_or_create(
            source='mono', external_id=account_id, defaults=defaults,
        )
        verb = 'Created' if created else 'Updated'
        self.stdout.write(self.style.SUCCESS(
            f'{verb} BankAccount(pk={obj.pk}): {obj}'
        ))
        self.stdout.write(f'  IBAN: {obj.iban}')
        self.stdout.write(f'  PAN:  {obj.masked_pan or "(no card)"}')
        self.stdout.write(f'  Active: {obj.is_active}')
