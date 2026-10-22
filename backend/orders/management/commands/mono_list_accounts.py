"""Prints the list of accounts from the Mono Personal API and marks which ones are
already registered in the DB.

Usage:
  python manage.py mono_list_accounts

Useful before `mono_register_account` - to see what is there and pick the right id.
"""
from __future__ import annotations

from django.core.management.base import BaseCommand

from orders import monobank_personal
from orders.models import BankAccount


CCY_NAMES = {980: 'UAH', 840: 'USD', 978: 'EUR', 985: 'PLN'}

TYPE_LABELS = {
    'black': '🟦 black',
    'white': '⬜ white',
    'platinum': '⬛ platinum',
    'iron': '⬛ iron',
    'fop': '🏢 sole proprietor',
    'yellow': '🟨 yellow',
    'eAid': 'eAid',
    'madeInUkraine': 'madeInUkraine',
}


class Command(BaseCommand):
    help = 'List Mono Personal API accounts; highlights ones already registered as BankAccount.'

    def handle(self, *args, **opts):
        try:
            info = monobank_personal.get_client_info()
        except monobank_personal.MonoPersonalNotConfigured:
            self.stderr.write(self.style.ERROR(
                'MONOBANK_PERSONAL_TOKEN is not configured in backend/.env'
            ))
            return
        except monobank_personal.MonoPersonalError as e:
            self.stderr.write(self.style.ERROR(f'Mono API error: {e}'))
            return

        registered_ids = set(
            BankAccount.objects.filter(source='mono').values_list('external_id', flat=True)
        )

        self.stdout.write(self.style.SUCCESS(f'Client: {info.get("name", "?")}'))
        self.stdout.write(f'Permissions: {info.get("permissions", "")}')
        self.stdout.write(f'WebHook: {info.get("webHookUrl") or "(not set)"}')
        self.stdout.write('')
        self.stdout.write('Accounts:')
        self.stdout.write('─' * 100)

        for acc in info.get('accounts', []):
            ccy = CCY_NAMES.get(acc.get('currencyCode'), str(acc.get('currencyCode')))
            type_label = TYPE_LABELS.get(acc.get('type'), acc.get('type', '?'))
            balance_kop = acc.get('balance', 0)
            balance = balance_kop / 100
            iban = acc.get('iban', '')
            iban_short = f'{iban[:8]}…{iban[-6:]}' if len(iban) >= 14 else iban
            pans = acc.get('maskedPan') or []
            pans_str = ', '.join(pans) if pans else '(no card)'
            mark = self.style.SUCCESS('[REG]') if acc['id'] in registered_ids else '     '

            self.stdout.write(
                f'  {mark} id={acc["id"]:<25} {type_label:<14} {ccy} '
                f'balance={balance:>12,.2f}  pan={pans_str:<32} iban={iban_short}'
            )

        self.stdout.write('─' * 100)
        self.stdout.write('')
        self.stdout.write('Register the account in the DB:')
        self.stdout.write(self.style.WARNING(
            '  python manage.py mono_register_account <accountId> --name "Sole proprietor Jane / IBAN"'
        ))
