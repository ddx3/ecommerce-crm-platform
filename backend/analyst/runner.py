"""Running the checks and recording findings with deduplication.

Finding lifecycle (one `Finding.key` per problem):
  - new problem → created with status 'New';
  - repeats → text and numbers are updated, the human's status ('Done' / 'Postponed')
    is kept; importance rose or the snooze period expired → 'New' again;
  - stopped repeating → `resolved_at` (only if the check ran without
    error — a check that failed does not close other checks' findings);
  - reappeared after closing → opened as new.
"""
from __future__ import annotations

import logging
import traceback
from dataclasses import dataclass, field

from django.db import transaction
from django.utils import timezone

from analyst.attribution import prune_old_data
from analyst.checks import Item, all_checks
from analyst.models import AnalystRun, Finding

log = logging.getLogger('analyst')

RUNNER_CHECK = 'runner'


@dataclass
class Outcome:
    items: list[Item] = field(default_factory=list)
    crashed: list[str] = field(default_factory=list)
    new_keys: list[str] = field(default_factory=list)   # filled in dry-run too


def collect(now, only=None) -> Outcome:
    """Runs the checks; nothing is written to the DB."""
    out = Outcome()
    for name, (section, fn) in all_checks().items():
        if only and name not in only:
            continue
        try:
            found = fn(now) or []
        except Exception as exc:
            log.exception('analyst: check %s crashed', name)
            out.crashed.append(name)
            out.items.append(Item(
                key=f'check_failed:{name}', severity=Finding.WARN,
                title=f'Check "{name}" crashed',
                body=f'{type(exc).__name__}: {exc}'[:300],
                check=RUNNER_CHECK, section=Finding.SECTION_HEALTH,
            ))
            continue
        for item in found:
            item.check, item.section = name, section
        out.items.extend(found)
    return out


def apply(out: Outcome, now, only=None, dry_run=False, extra_ran=(), exact_ran=None) -> int:
    """Records the findings. Returns the number of new ones (including reopened).

    `extra_ran` — names of 'checks' outside the registry (purchasing advice) that ran this time:
    their disappeared cards are closed the same way as for regular checks.
    `exact_ran` — close disappeared cards ONLY of these checks (for the watchdog: it does not know
    about the rest)."""
    unique = {i.key: i for i in out.items}   # duplicate key within one run = one finding
    keys = list(unique)
    existing = {f.key: f for f in Finding.objects.filter(key__in=keys)}
    fresh = 0
    to_create, to_update = [], []
    for item in unique.values():
        f = existing.get(item.key)
        # Reference line (data.processed, e.g. 'Ordered …'): requires no action, so it
        # is immediately 'processed' and not counted as new.
        processed = bool(item.data.get('processed'))
        if f is None:
            if not processed:
                out.new_keys.append(item.key)
                fresh += 1
            to_create.append(Finding(
                key=item.key, check_name=item.check, section=item.section, severity=item.severity,
                status=Finding.DONE if processed else Finding.NEW,
                title=item.title[:200], body=item.body, data=item.data, link=item.link[:200],
                first_seen=now, last_seen=now))
            continue
        reopened = f.resolved_at is not None
        escalated = item.severity > f.severity
        woke = (f.status == Finding.SNOOZED and f.snoozed_until is not None and f.snoozed_until <= now)
        if processed:
            if reopened:
                f.first_seen = now
            f.status, f.snoozed_until = Finding.DONE, None
        elif reopened or escalated or woke:
            if f.status != Finding.NEW or reopened:
                out.new_keys.append(item.key)
                fresh += 1
            f.status, f.snoozed_until = Finding.NEW, None
            if reopened:
                f.first_seen = now
        f.check_name, f.section, f.severity = item.check, item.section, item.severity
        f.title, f.body, f.data, f.link = item.title[:200], item.body, item.data, item.link[:200]
        f.last_seen, f.resolved_at = now, None
        to_update.append(f)
    if dry_run:
        return fresh
    with transaction.atomic():
        Finding.objects.bulk_create(to_create)
        if to_update:
            Finding.objects.bulk_update(
                to_update, ['check_name', 'section', 'severity', 'title', 'body', 'data', 'link',
                            'status', 'snoozed_until', 'first_seen', 'last_seen', 'resolved_at'])
        if exact_ran is not None:
            ran = set(exact_ran)
        else:
            ran = {n for n in all_checks() if (not only or n in only) and n not in out.crashed}
            if not only:
                ran.add(RUNNER_CHECK)
            ran.update(extra_ran)
        (Finding.objects.filter(check_name__in=ran, resolved_at__isnull=True)
         .exclude(key__in=keys).update(resolved_at=now))
    return fresh


def _broadcast_badge() -> None:
    """Refresh the 'Analyst' badge in open CRM tabs. A failure here does not break the run."""
    try:
        from catalog.notifications import broadcast_notification_counts
        broadcast_notification_counts()
    except Exception:
        log.warning('analyst: could not refresh the badge in the CRM', exc_info=True)


def run_daily(*, force=False, now=None, use_agent=None) -> AnalystRun | None:
    """Daily run: checks + stock snapshot. One successful attempt per calendar
    day (Europe/Kyiv); `force` ignores this. Returns the run record, or None if
    it has already been done today."""
    from analyst.snapshots import take_stock_snapshot

    # close_old_connections() is deliberately absent here: the service loop (run_analyst
    # _serve) calls it, and inside a transaction (tests) it would close the connection.
    now = now or timezone.now()
    today = timezone.localtime(now).date()
    run, created = AnalystRun.objects.get_or_create(date=today)
    if not created and run.status == AnalystRun.OK and not force:
        return None
    run.status, run.started_at, run.finished_at, run.error = AnalystRun.RUNNING, now, None, ''
    run.save()
    try:
        out = collect(now)
        try:
            take_stock_snapshot(today)
        except Exception as exc:
            log.exception('analyst: the stock snapshot failed')
            out.items.append(Item(
                key='snapshot_failed', severity=Finding.WARN, check=RUNNER_CHECK,
                section=Finding.SECTION_HEALTH, title='The stock snapshot was not recorded',
                body=f'{type(exc).__name__}: {exc}'[:300]))
        advice_ran = False
        try:
            from analyst import advice
            result = advice.generate(now, use_agent=use_agent)
            out.items.extend(result.items)
            advice_ran = True
            if result.error:
                out.items.append(Item(
                    key='advice:agent_failed', severity=Finding.WARN, check=RUNNER_CHECK,
                    section=Finding.SECTION_HEALTH, title='The purchasing agent failed — the rule-based calculation is shown',
                    body=result.error))
        except Exception as exc:
            log.exception('analyst: the purchasing advice could not be built')
            out.items.append(Item(
                key='advice:failed', severity=Finding.WARN, check=RUNNER_CHECK, section=Finding.SECTION_HEALTH,
                title='The purchasing advice could not be built', body=f'{type(exc).__name__}: {exc}'[:300]))
        from analyst.advice import PURCHASE_CHECKS
        fresh = apply(out, now, extra_ran=PURCHASE_CHECKS if advice_ran else ())
        try:
            prune_old_data(today)
        except Exception:
            log.warning('analyst: could not prune the old search logs', exc_info=True)
        run.status = AnalystRun.OK
        run.new_count = fresh
        run.total_count = Finding.objects.filter(resolved_at__isnull=True).count()
        _broadcast_badge()
    except Exception:
        log.exception('analyst: the run crashed')
        run.status = AnalystRun.FAILED
        run.error = traceback.format_exc()[-1500:]
    run.finished_at = timezone.now()
    run.save()
    return run
