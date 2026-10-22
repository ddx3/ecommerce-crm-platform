"""
Snapshot of the whole DB before risky operations (product migration, etc.).

Postgres → pg_dump into a compressed custom format (.dump), restorable with pg_restore.
SQLite  → a plain copy of the DB file.

The file is placed in backend/backups/ with a timestamp in its name.

Usage:
    python manage.py backup_db
    python manage.py backup_db --label before-import

Restore (Postgres):
    pg_restore --clean --if-exists -d <DB_NAME> backups/<file>.dump
Restore (SQLite):
    copy backups/<file>.sqlite3 back to db.sqlite3
"""
import glob
import os
import shutil
import subprocess
from datetime import datetime

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError


def _find_tool(name):
    """Looks for pg_dump/pg_restore in PATH, then in typical PostgreSQL installations."""
    found = shutil.which(name)
    if found:
        return found
    patterns = [
        rf'D:\PostgreSQL\bin\{name}.exe',
        rf'C:\Program Files\PostgreSQL\*\bin\{name}.exe',
        rf'D:\PostgreSQL\*\bin\{name}.exe',
    ]
    for pat in patterns:
        hits = glob.glob(pat)
        if hits:
            return sorted(hits)[-1]  # newest version
    return None


class Command(BaseCommand):
    help = 'Make a full DB backup (pg_dump for Postgres, file copy for SQLite).'

    def add_arguments(self, parser):
        parser.add_argument('--label', default='', help='Suffix in the backup file name')

    def handle(self, *args, **opts):
        db = settings.DATABASES['default']
        engine = db['ENGINE']
        backups_dir = os.path.join(settings.BASE_DIR, 'backups')
        os.makedirs(backups_dir, exist_ok=True)

        stamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        label = f"_{opts['label']}" if opts['label'] else ''

        if 'sqlite' in engine:
            src = str(db['NAME'])
            if not os.path.exists(src):
                raise CommandError(f'SQLite file not found: {src}')
            dst = os.path.join(backups_dir, f'db{label}_{stamp}.sqlite3')
            shutil.copy2(src, dst)
            self._done(dst)
            return

        if 'postgresql' in engine:
            dst = os.path.join(backups_dir, f"{db['NAME']}{label}_{stamp}.dump")
            env = os.environ.copy()
            if db.get('PASSWORD'):
                env['PGPASSWORD'] = db['PASSWORD']
            pg_dump = _find_tool('pg_dump')
            if not pg_dump:
                raise CommandError('pg_dump was not found in PATH or in the usual PostgreSQL folders.')
            cmd = [
                pg_dump,
                '-h', db.get('HOST') or 'localhost',
                '-p', str(db.get('PORT') or 5432),
                '-U', db['USER'],
                '-F', 'c',          # custom format → pg_restore
                '-f', dst,
                db['NAME'],
            ]
            self.stdout.write('Running: ' + ' '.join(cmd))
            try:
                subprocess.run(cmd, env=env, check=True)
            except FileNotFoundError:
                raise CommandError('pg_dump was not found in PATH. Install the PostgreSQL client tools.')
            except subprocess.CalledProcessError as e:
                raise CommandError(f'pg_dump failed: {e}')
            self._done(dst)
            self.stdout.write(self.style.WARNING(
                f"Restore: pg_restore --clean --if-exists -h {db.get('HOST') or 'localhost'} "
                f"-U {db['USER']} -d {db['NAME']} \"{dst}\""))
            return

        raise CommandError(f'Unsupported engine for backup: {engine}')

    def _done(self, path):
        size = os.path.getsize(path) / 1024 / 1024
        self.stdout.write(self.style.SUCCESS(f'Backup ready: {path} ({size:.1f} MB)'))
