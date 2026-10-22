"""Checks of the 'System health' module: disk, backups, SSL, scheduler heartbeat.

They work from inside the stack, so they will not see a complete site outage (the owner deliberately
chose the CRM inbox as the only channel). We catch 'sick but alive': a disk that fills up for the
second
time in a year, a silent scheduler, a stale backup.
"""
from __future__ import annotations

import os
import shutil
import socket
import ssl
from datetime import datetime, timedelta, timezone as dt_timezone
from pathlib import Path
from urllib.parse import urlparse

from django.conf import settings

from analyst.models import Finding
from . import Item, check

DISK_WARN_PCT = 90    # show only what is truly critical: below 90% there is nothing to do
DISK_CRIT_PCT = 90
BACKUP_WARN_DAYS = 3
BACKUP_CRIT_DAYS = 7
SSL_WARN_DAYS = 21
SSL_CRIT_DAYS = 7
BACKUP_SCAN_LIMIT = 2000

# How long a task may stay silent before that becomes a problem. Keys are the sub-command names of
# run_scheduler. We evaluate only those that have already written a heartbeat: poll_* depend on
# tokens,
# sync_dropshipping runs once a day from the last successful sync (not from the process start).
FREQUENT = timedelta(minutes=45)
DAILY = timedelta(hours=36)
EXPECTED_JOBS = {
    'reconcile_pending_payments': FREQUENT,
    'match_bank_transactions': FREQUENT,
    'process_payment_screenshots': FREQUENT,
    'send_stock_notifications': FREQUENT,
    'poll_monobank': FREQUENT,
    'poll_privat': FREQUENT,
    'resolve_pending_instructions': timedelta(minutes=60),
    'recompute_khit': DAILY,
    'warmup_np_cache': DAILY,
    'heal_stock_drift': DAILY,
    'seo_apply_content_meta': DAILY,
    'sync_dropshipping': DAILY,
}


def _ago(delta: timedelta) -> str:
    hours = int(delta.total_seconds() // 3600)
    return f'{hours // 24} days' if hours >= 48 else f'{hours} h'


@check('disk_usage', Finding.SECTION_HEALTH)
def disk_usage(now):
    paths = {Path(settings.BASE_DIR), Path(getattr(settings, 'MEDIA_ROOT', settings.BASE_DIR))}
    seen_devices, items = set(), []
    for path in sorted(paths, key=str):
        try:
            device = os.stat(path).st_dev
            total, used, free = shutil.disk_usage(path)
        except OSError:
            continue
        # Two paths (code and the media volume) may be different mounts of the same disk with their
        # own st_dev,
        # but with the same total/free: it is one disk, one finding.
        fingerprint = (total, free // (100 * 2**20))
        if device in seen_devices or fingerprint in seen_devices or not total:
            continue
        seen_devices.update({device, fingerprint})
        # As in df: the root reserve (≈5%) is not counted, otherwise 91% would look like 86%.
        pct = round(used / max(used + free, 1) * 100)
        if pct < DISK_WARN_PCT:
            continue
        items.append(Item(
            key=f'disk:{device}',
            severity=Finding.CRITICAL if pct >= DISK_CRIT_PCT else Finding.WARN,
            title=f'Disk is {pct}% full',
            body=f'{free / 2**30:.1f} GB free out of {total / 2**30:.0f} GB ({path}). '
                 f'When the disk is full, rebuilding containers fails with "no space left on device".',
            data={'path': str(path), 'used_pct': pct, 'free_gb': round(free / 2**30, 1)},
        ))
    return items


@check('backup_freshness', Finding.SECTION_HEALTH)
def backup_freshness(now):
    directory = getattr(settings, 'ANALYST_BACKUP_DIR', '')
    if not directory:
        return []
    root = Path(directory)
    if not root.is_dir():
        if settings.DEBUG:
            return []
        return [Item(
            key='backup:no_dir', severity=Finding.WARN,
            title='The Analyst cannot see the backup directory',
            body=f'The directory {directory} is not mounted into the container. Mount /opt/backups '
                 f'into the analyst service (read-only), otherwise backup freshness cannot be checked.',
            data={'no_auto': True},      # this is about the mount, not the backup: a new dump will not fix anything
        )]
    newest, scanned = None, 0
    for entry in root.rglob('*'):
        scanned += 1
        if scanned > BACKUP_SCAN_LIMIT:
            break
        try:
            if entry.is_file() and not entry.name.startswith('.'):
                mtime = entry.stat().st_mtime
                if newest is None or mtime > newest[0]:
                    newest = (mtime, entry.name)
        except OSError:
            continue
    if newest is None:
        return [Item(
            key='backup:none', severity=Finding.CRITICAL,
            title='There are no database backups',
            body=f'There are no files in {directory}. Check the pg_dump cron job on the server.',
        )]
    age = now - datetime.fromtimestamp(newest[0], tz=dt_timezone.utc)
    if age < timedelta(days=BACKUP_WARN_DAYS):
        return []
    return [Item(
        key='backup:stale',
        severity=Finding.CRITICAL if age >= timedelta(days=BACKUP_CRIT_DAYS) else Finding.WARN,
        title=f'The last database backup was {_ago(age)} ago',
        body=f'Newest file: {newest[1]}. Check the pg_dump cron job on the server.',
        data={'file': newest[1], 'age_hours': int(age.total_seconds() // 3600)},
    )]


@check('ssl_expiry', Finding.SECTION_HEALTH)
def ssl_expiry(now):
    parsed = urlparse(getattr(settings, 'SITE_URL', ''))
    host = parsed.hostname or ''
    if parsed.scheme != 'https' or not host or host in ('localhost', '127.0.0.1'):
        return []
    last_error = ''
    for _attempt in range(2):
        try:
            context = ssl.create_default_context()
            with socket.create_connection((host, parsed.port or 443), timeout=6) as sock:
                with context.wrap_socket(sock, server_hostname=host) as tls:
                    not_after = tls.getpeercert()['notAfter']
            expires = datetime.fromtimestamp(ssl.cert_time_to_seconds(not_after), tz=dt_timezone.utc)
            break
        except (OSError, ssl.SSLError, KeyError) as exc:
            last_error = str(exc)[:200]
    else:
        return [Item(
            key='ssl:unreachable', severity=Finding.WARN,
            title=f'Could not check the SSL certificate of {host}',
            body=last_error,
        )]
    days = (expires - now).days
    if days >= SSL_WARN_DAYS:
        return []
    return [Item(
        key='ssl:expiring',
        severity=Finding.CRITICAL if days < SSL_CRIT_DAYS else Finding.WARN,
        title=f'The SSL certificate of {host} expires in {max(days, 0)} days',
        body='The certificate is reissued manually (acme.sh, DNS validation) — there is no auto-renewal.',
        data={'expires': expires.date().isoformat(), 'days_left': days},
    )]


@check('scheduler_heartbeat', Finding.SECTION_HEALTH)
def scheduler_heartbeat(now):
    from analyst.models import JobHeartbeat
    beats = {b.name: b for b in JobHeartbeat.objects.all()}
    if not beats:
        if settings.DEBUG:
            return []
        return [Item(
            key='scheduler:no_heartbeat', severity=Finding.WARN,
            title='The scheduler is not writing a heartbeat',
            body='The heartbeat table is empty: the scheduler was not updated to the version with heartbeat '
                 'or it is not running. Check the scheduler container.',
            data={'no_auto': True},      # release state, not an incident: restarting the old code will not add a heartbeat
        )]
    # There is a frequent heartbeat and it is old → the scheduler is dead; we do not multiply the
    # remaining cards.
    frequent = [b.last_ok_at for name, b in beats.items()
                if EXPECTED_JOBS.get(name) == FREQUENT and b.last_ok_at]
    if frequent and now - max(frequent) > FREQUENT:
        gap = now - max(frequent)
        return [Item(
            key='scheduler:silent', severity=Finding.CRITICAL,
            title=f'The scheduler has been silent for {_ago(gap)}',
            body='No regular task (payment reconciliation, bank statements, screenshots) has run. '
                 'In August it likewise stood idle for 4 days. Restart the scheduler container.',
            data={'silent_hours': int(gap.total_seconds() // 3600)},
        )]
    items = []
    for name, limit in EXPECTED_JOBS.items():
        beat = beats.get(name)
        if beat is None:
            continue
        failing = beat.last_fail_at and (not beat.last_ok_at or beat.last_fail_at > beat.last_ok_at)
        if failing:
            items.append(Item(
                key=f'job_failing:{name}', severity=Finding.WARN,
                title=f'Task {name} is failing',
                body=(beat.last_error or 'no error text')[:300],
                data={'job': name, 'no_auto': True},   # fails every time (external service, bug): a restart does not cure it
            ))
        elif beat.last_ok_at and now - beat.last_ok_at > limit:
            gap = now - beat.last_ok_at
            items.append(Item(
                key=f'job_stale:{name}', severity=Finding.WARN,
                title=f'Task {name} has not run for {_ago(gap)}',
                body='The rest of the scheduler is working, but this task is silent.',
                data={'job': name, 'silent_hours': int(gap.total_seconds() // 3600)},
            ))
    return items


@check('ops_helper', Finding.SECTION_HEALTH)
def ops_helper(now):
    """We see the shop-helper service on the VPS (the 'free up space' / 'backup' /
    'restart' buttons) by
    its heartbeat; it also makes a backup every day and reports its result."""
    from analyst import hostops
    if not getattr(settings, 'ANALYST_OPS_DIR', ''):
        return []
    st = hostops.status()
    if st is None:
        return [Item(
            key='ops_helper:down', severity=Finding.WARN,
            title='The helper on the server is not responding',
            body='There is no state file in the shared folder. Check the service: systemctl status shop-helper. '
                 'Without it the "Free up space" and "Back up now" buttons and the daily auto-backup do not work.')]
    items = []
    if not hostops.helper_alive(now):
        items.append(Item(
            key='ops_helper:down', severity=Finding.WARN,
            title='The helper on the server is silent',
            body=f'Last heartbeat: {st.get("heartbeat", "unknown")}. Check: systemctl status shop-helper.'))
    last = st.get('last_auto_backup') or {}
    if last and not last.get('ok'):
        items.append(Item(
            key='ops_helper:autobackup_failed', severity=Finding.CRITICAL,
            title='The daily auto-backup failed',
            body=(last.get('error') or 'no explanation')[:300]))
    return items


@check('stuck_instruction_jobs', Finding.SECTION_HEALTH)
def stuck_instruction_jobs(now):
    """The 'Run through AI' queue is not being processed: the scheduler picks it up
once every 10 min."""
    from catalog.models import InstructionJob
    stuck = InstructionJob.objects.filter(status=InstructionJob.PENDING,
                                          created_at__lt=now - timedelta(hours=3))
    count = stuck.count()
    if not count:
        return []
    return [Item(
        key='instruction_jobs:stuck', severity=Finding.WARN,
        title=f'The bundle instruction queue is stuck: {count} tasks for over 3 h',
        body='Probably the Claude subscription limit is exhausted or resolve_pending_instructions is not running.',
        data={'count': count},
    )]


@check('analyst_previous_run', Finding.SECTION_HEALTH)
def analyst_previous_run(now):
    from analyst.models import AnalystRun
    last = AnalystRun.objects.exclude(status=AnalystRun.RUNNING).order_by('-date').first()
    if last is None or last.status != AnalystRun.FAILED:
        return []
    return [Item(
        key='analyst:last_run_failed', severity=Finding.WARN,
        title=f'The analyst run of {last.date:%d.%m} ended with an error',
        body=(last.error or '')[:300],
    )]
