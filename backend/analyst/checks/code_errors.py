"""Cards about code errors (auto-fix level 1): what breaks, how many times, where exactly.

Takes the `ErrorEvent`s collected from the logs (analyst/errorlog.py). A card lives while the error
keeps recurring within the
last 24 hours; as soon as there are no repeats, it closes itself. Fixing it is done by a separate
pipeline (analyst/codefix),
and here it is display only.
"""
from __future__ import annotations

from datetime import timedelta

from analyst.checks import Item, check
from analyst.models import ErrorEvent, Finding

MIN_REPEATS = 3               # a one-off failure is not a card
EXTERNAL_MIN_REPEATS = 10     # an external service failure is shown only when it really hurts
CRITICAL_REPEATS = 50


def short_where(path: str) -> str:
    return path[len('backend/'):] if path.startswith('backend/') else path


def _manual_note(event) -> str:
    """When the Analyst will NOT fix this error itself, the owner must know right away rather than wait:
the file is outside the list, or the function handles access or writes."""
    from analyst import codefix_policy as policy
    from analyst.codefix import pipeline
    if pipeline.mode() == 'off':
        return ''
    if policy.classify(event.where_path) != 'code':
        return ' Auto-fix does not touch this file — a manual fix is needed.'
    reason = pipeline.frozen_reason(event)
    return f' Auto-fix does not go here ({reason}) — a manual fix is needed.' if reason else ''


@check('code_errors', Finding.SECTION_HEALTH)
def code_errors(now):
    since = now - timedelta(hours=24)
    items = []
    events = ErrorEvent.objects.filter(last_seen__gte=since).exclude(status=ErrorEvent.IGNORED).exclude(service='drill')
    for event in events:
        n = event.count_since(since)
        short = event.exc_type.rsplit('.', 1)[-1]
        if event.kind == ErrorEvent.EXTERNAL:
            if n < EXTERNAL_MIN_REPEATS:
                continue
            items.append(Item(
                key=f'code_error:{event.fingerprint}', severity=Finding.INFO,
                title=f'External service failure: {short} ({n} times in 24 h)',
                body=f'{event.message or "no explanation"} · service {event.service}. Looks like the network or an external service, '
                     f'not a bug in the code: the Analyst changes nothing here.',
                data={'fingerprint': event.fingerprint, 'count_24h': n, 'kind': event.kind, 'no_auto': True}))
            continue
        if n < MIN_REPEATS:
            continue
        where = f'{short_where(event.where_path)} ({event.where_func})' if event.where_path else 'unknown location'
        route = f' · URL {event.request_path}' if event.request_path else ''
        manual = ''
        if event.kind == ErrorEvent.CODE and event.where_path:
            manual = _manual_note(event)
        items.append(Item(
            key=f'code_error:{event.fingerprint}',
            severity=Finding.CRITICAL if n >= CRITICAL_REPEATS else Finding.WARN,
            title=f'Code error: {short} in {where}',
            body=f'{event.message or "no explanation"} · {n} times in 24 h, last at '
                 f'{event.last_seen.astimezone().strftime("%d.%m %H:%M")}{route}.{manual}'
                 + ('' if event.kind == ErrorEvent.CODE else ' This is not a logic error (looks like an environment or data problem): '
                                                          'the Analyst does not fix such errors itself.'),
            data={'fingerprint': event.fingerprint, 'count_24h': n, 'where': event.where_path, 'kind': event.kind,
                  'no_auto': True}))
    return items


@check('autofix_ahead', Finding.SECTION_HEALTH)
def autofix_ahead(now):
    """Fixes the Analyst deployed straight to the server that are not in git yet (the deploy key on the VPS
is read-only)."""
    from analyst import hostops
    n = int((hostops.status() or {}).get('autofix_ahead') or 0)
    if n <= 0:
        return []
    return [Item(
        key='autofix_ahead', severity=Finding.INFO,
        title=f'The server has {n} auto-fixes that are not in git yet',
        body='The Analyst deployed fixes straight to the server. Before the next manual deploy, move them into the repository '
             '(on the VPS: git log origin/<branch>..HEAD), otherwise an update from git will wipe them.',
        data={'no_auto': True, 'count': n})]


@check('autofix_status', Finding.SECTION_HEALTH)
def autofix_status(now):
    """State of the helper regarding auto-fixes: failed recovery after a failure in the middle of a
deployment, missing signing key."""
    from django.conf import settings
    from analyst import hostops
    status = hostops.status() or {}
    items = []
    recovery = status.get('recovery') or {}
    if recovery.get('ok') is False:
        items.append(Item(
            key='autofix_recovery', severity=Finding.CRITICAL,
            title='The helper could not restore the site after a failure in the middle of a deployment',
            body=f'{recovery.get("error") or "no explanation"} · {recovery.get("at", "")}. Check the site and the shop-helper log '
                 f'on the server immediately (journalctl -u shop-helper).',
            data={'no_auto': True}))
    if getattr(settings, 'ANALYST_CODEFIX', 'off') != 'off' and status and status.get('heartbeat') and status.get('key_ok') is False:
        items.append(Item(
            key='autofix_no_key', severity=Finding.WARN,
            title='Auto-fixes do not work: the server has no signing key',
            body='Run `sh ops/install.sh` on the VPS from a full copy of the project — it will create /opt/shop-autofix/codefix.key; '
                 'then restart analyst (`docker compose up -d --no-deps analyst`).',
            data={'no_auto': True}))
    return items
