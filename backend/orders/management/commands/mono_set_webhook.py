"""Registers the webhook URL in the Mono Personal API.

Mono will POST a StatementItem to this URL on every money movement on any account
the token has access to.

Usage:
  # Suggests a URL built from your BACKEND_URL + the current secret from .env
  python manage.py mono_set_webhook

  # Or give the URL explicitly
  python manage.py mono_set_webhook --url https://example.com/api/webhooks/monobank-personal/<secret>/

  # Check the current URL in Mono without changing it
  python manage.py mono_set_webhook --check

  # Remove the webhook (empty string)
  python manage.py mono_set_webhook --clear

Important:
  - The URL must be publicly reachable over HTTPS (Mono checks access before activation).
  - Locally use ngrok / cloudflared / localtunnel.
  - Mono rate limit: 1 request/60s on /personal/webhook.
"""
from __future__ import annotations

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from orders import monobank_personal


class Command(BaseCommand):
    help = 'Set/check/clear webhook URL in Mono Personal API.'

    def add_arguments(self, parser):
        parser.add_argument('--url', type=str, default=None,
                            help='Specify the webhook URL explicitly')
        parser.add_argument('--check', action='store_true',
                            help='Only check the current URL in Mono')
        parser.add_argument('--clear', action='store_true',
                            help='Remove the webhook (set an empty URL)')

    def handle(self, *args, url, check, clear, **opts):
        if check or (not url and not clear):
            try:
                info = monobank_personal.get_client_info()
            except monobank_personal.MonoPersonalError as e:
                raise CommandError(f'Mono API error: {e}')
            current = info.get('webHookUrl') or '(not set)'
            self.stdout.write(f'Current webhook URL in Mono: {current}')
            if check:
                return

        if clear:
            target_url = ''
        elif url:
            target_url = url
        else:
            # Build from BACKEND_URL + secret.
            backend = (settings.BACKEND_URL or '').rstrip('/')
            secret = settings.MONOBANK_PERSONAL_WEBHOOK_SECRET
            if not backend:
                raise CommandError('BACKEND_URL is not set in .env')
            if not secret:
                raise CommandError('MONOBANK_PERSONAL_WEBHOOK_SECRET is not set in .env')
            target_url = f'{backend}/api/webhooks/monobank-personal/{secret}/'

        if target_url and not target_url.startswith('https://'):
            self.stdout.write(self.style.WARNING(
                f'⚠ URL is not HTTPS: {target_url}\n'
                f'  Mono will most likely refuse it. For local development use '
                f'ngrok/cloudflared.'
            ))

        self.stdout.write(f'Setting the webhook in Mono: {target_url or "(empty)"}')
        try:
            monobank_personal.set_webhook(target_url)
        except monobank_personal.MonoPersonalError as e:
            raise CommandError(f'Could not set it: {e}')

        self.stdout.write(self.style.SUCCESS('✓ Webhook URL updated in Mono.'))
        self.stdout.write('Check the status: `mono_set_webhook --check`')
