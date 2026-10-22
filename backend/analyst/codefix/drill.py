"""Auto-fix drill: run the real pipeline on a training 'canary' (core/canary.py) without waiting for a
real error.

Takes the first canary function that is not fixed yet, calls it with 'bad' input, catches the REAL
exception through the same
sanitizer as production errors (analyst/errorlog.py), records it as an ErrorEvent and drives the
CodeFix to the end: agent, sandbox,
reviewer and (in auto mode) a deployment with a health check. With `simulate_failure` the health
check deliberately 'fails',
to prove that the automatic rollback on the server works.
"""
from __future__ import annotations

import sys
import time

from django.utils import timezone

from analyst import errorlog
from analyst.codefix import pipeline
from analyst.models import CodeFix, ErrorEvent

POLL_SECONDS = 15
TOTAL_TIMEOUT = 3 * 3600
STOP_STATES = (CodeFix.READY, CodeFix.DEPLOYED, *CodeFix.DONE)


def first_broken_case():
    """(function, exception in exc_info form) for the first canary function that is still broken, or None
if all are fixed."""
    from core import canary
    for fn, args in canary.DRILL_CASES:
        fn = getattr(canary, fn.__name__)               # fresh reference: the function may have been fixed on the fly
        try:
            fn(*args)
        except Exception:
            return fn.__name__, sys.exc_info()
    return None


def create(now=None, *, simulate_failure: bool = False) -> CodeFix:
    now = now or timezone.now()
    found = first_broken_case()
    if found is None:
        raise RuntimeError('All canary functions are already fixed — the drill has nothing to work on.')
    name, (exc_type, exc, tb) = found
    info = errorlog.describe(exc_type, exc, tb)
    if info is None or info['kind'] != 'code':
        raise RuntimeError('Could not describe the canary error.')
    info['service'] = 'drill'
    errorlog.persist(info, hits=5, now=now)
    event = ErrorEvent.objects.get(fingerprint=info['fingerprint'])
    ErrorEvent.objects.filter(pk=event.pk).update(status=ErrorEvent.OPEN)
    event.refresh_from_db()
    fix = pipeline.start(event, now, drill=True, simulate_failure=simulate_failure)
    return fix


def run(write, *, simulate_failure: bool = False, poll: int = POLL_SECONDS, sleep=time.sleep) -> CodeFix:
    """Drives the drill to a stop (READY / DEPLOYED / finished). `write` is the output function."""
    fix = create(simulate_failure=simulate_failure)
    write(f'Drill: function {fix.event.where_func}, mode {pipeline.mode()}'
          + (', the health check will be failed on purpose' if simulate_failure else ''))
    deadline = time.monotonic() + TOTAL_TIMEOUT
    pipeline.draft(fix, timezone.now())
    last = None
    while True:
        fix.refresh_from_db()
        if fix.status != last:
            write(f'  state: {fix.get_status_display()}' + (f' — {fix.reason}' if fix.reason and fix.status in CodeFix.DONE else ''))
            if fix.status == CodeFix.DEPLOYING:
                write('  (the deployment recreates the analyst container, so this session will be cut off; the service carries on from here — '
                      'see `docker compose logs analyst` and the fix card)')
            last = fix.status
        if fix.status in STOP_STATES or time.monotonic() > deadline:
            break
        sleep(poll)
        with pipeline.exclusive(fix.pk) as got:
            if got:
                fix.refresh_from_db()
                pipeline._advance(fix, timezone.now())
    if fix.summary:
        write(f'What was changed: {fix.summary}')
    return fix
