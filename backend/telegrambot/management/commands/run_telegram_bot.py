"""Run the Telegram bot (long-polling).

  python manage.py run_telegram_bot

Requires TELEGRAM_BOT_TOKEN and a non-empty TELEGRAM_ALLOWED_USER_IDS in .env.
Single instance (Telegram serves getUpdates to only one poller).
"""
import asyncio
import sys

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = 'Run the Telegram bot (long-polling).'

    def handle(self, *args, **opts):
        token = settings.TELEGRAM_BOT_TOKEN
        if not token:
            raise CommandError('TELEGRAM_BOT_TOKEN is not set in .env')
        if not settings.TELEGRAM_ALLOWED_USER_IDS:
            self.stdout.write(self.style.WARNING(
                'TELEGRAM_ALLOWED_USER_IDS is empty — the bot will not answer anyone. '
                'Add your Telegram user_id to .env.'
            ))

        # On Windows the SDK launches `claude` as a subprocess, so ProactorEventLoop is needed
        # (SelectorEventLoop cannot do subprocesses: NotImplementedError). On Linux
        # (prod) the default loop already supports subprocesses, so we leave the policy alone.
        if sys.platform == 'win32':
            asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

        from telegrambot.bot import run_polling
        self.stdout.write(self.style.SUCCESS('Starting the Telegram bot…'))
        try:
            asyncio.run(run_polling(token))
        except (KeyboardInterrupt, SystemExit):
            self.stdout.write('Stopped.')
