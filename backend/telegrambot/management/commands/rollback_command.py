"""Manual rollback of a bot write operation outside Telegram (safety net).

  python manage.py rollback_command <rollback_id>
  python manage.py rollback_command --last
  python manage.py rollback_command --command <command_log_id>   # all changes of the command
"""
from django.core.management.base import BaseCommand, CommandError

from telegrambot.models import RollbackEntry
from telegrambot import rollback


class Command(BaseCommand):
    help = 'Roll back Telegram bot change(s) by rollback_id / the last one / by command.'

    def add_arguments(self, parser):
        parser.add_argument('rollback_id', type=int, nargs='?', default=None)
        parser.add_argument('--last', action='store_true', help='Roll back the last change')
        parser.add_argument('--command', type=int, default=None,
                            help='Roll back all changes of a command (command_log_id)')

    def handle(self, *args, rollback_id, last, command, **opts):
        if command is not None:
            entries = list(RollbackEntry.objects.filter(
                command_id=command, is_reverted=False).order_by('-created_at'))
            if not entries:
                raise CommandError(f'Nothing to roll back for command #{command}')
        elif last:
            entry = RollbackEntry.objects.filter(is_reverted=False).order_by('-created_at').first()
            if entry is None:
                raise CommandError('Nothing to roll back')
            entries = [entry]
        elif rollback_id is not None:
            entry = RollbackEntry.objects.filter(pk=rollback_id).first()
            if entry is None:
                raise CommandError(f'Rollback #{rollback_id} not found')
            entries = [entry]
        else:
            raise CommandError('Specify rollback_id, --last or --command <id>')

        for entry in entries:
            try:
                msg = rollback.apply_revert(entry)
                self.stdout.write(self.style.SUCCESS(f'✓ {msg}'))
            except Exception as e:
                self.stderr.write(self.style.ERROR(f'✗ #{entry.pk}: {e}'))
