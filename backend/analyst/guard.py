"""Watchdog: frequent system health checks and, if the owner allows it, autonomous server maintenance.

The daily run (`runner.run_daily`) is too infrequent for 'the scheduler is silent' or 'the disk is
almost
full', so the service loop calls `run_guard` every few minutes: it runs only the cheap system health
checks, and then, depending on `ANALYST_AUTOFIX`, either does nothing (ask/off) or itself performs
the
permitted actions on the server (auto).

Automation safeguards:
  * only actions from the helper's allowlist on the VPS (`Fixer.auto`), without parameters that
    destroy
    the rollback;
  * an action is not repeated until the pause has passed (`ANALYST_AUTOFIX_COOLDOWN_HOURS`); if the
    problem
    remains after it, the Analyst does NOT repeat, but writes a 'failed to cope' card — restarting
    in a
    loop is not allowed;
  * no more than `ANALYST_AUTOFIX_MAX_PER_DAY` identical actions per day (a problem that keeps
    coming
    back again and again);
  * a finding the owner closed with ✓ is not touched by automation;
  * at most one action of each kind is executed per cycle (the helper itself keeps a per-hour pause
    and
    limit).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import timedelta

from django.conf import settings
from django.utils import timezone

from . import actions
from .checks import Item
from .models import ActionRun, Finding
from .runner import Outcome, _broadcast_badge, apply, collect

log = logging.getLogger('analyst')

# Cheap health checks: disk, backup, scheduler, helper, queue. SSL and 'previous run' — daily.
GUARD_CHECKS = ('disk_usage', 'backup_freshness', 'scheduler_heartbeat', 'ops_helper', 'stuck_instruction_jobs',
                'code_errors', 'autofix_ahead', 'autofix_status')
AUTO_USER = 'analyst:auto'
CHECK_AUTO = 'auto_action'          # card 'The Analyst did it itself: …'
CHECK_STUCK = 'autofix_stuck'       # card 'The Analyst failed to cope: …'


@dataclass
class GuardResult:
    fresh: int = 0                                  # new findings of this cycle
    applied: list = field(default_factory=list)     # executed ActionRuns (auto)
    stuck: int = 0                                  # findings the Analyst failed to cope with


def autofix_mode() -> str:
    mode = getattr(settings, 'ANALYST_AUTOFIX', 'ask')
    return 'auto' if mode == 'auto' else 'ask'


def _hhmm(dt) -> str:
    return timezone.localtime(dt).strftime('%d.%m %H:%M')


def _stuck_item(finding: Finding, run: ActionRun | None, note: str = '') -> Item:
    if note:
        body = note
    elif run is not None and run.status == ActionRun.FAILED:
        body = f'"{run.title}" ({_hhmm(run.created_at)}) failed: {run.error[:300]}'
    elif run is not None:
        body = f'Ran "{run.title}" ({_hhmm(run.created_at)}), but the problem did not go away.'
    else:
        body = 'The problem remains.'
    return Item(
        key=f'autofix_stuck:{finding.key}', severity=Finding.CRITICAL, check=CHECK_STUCK,
        section=Finding.SECTION_HEALTH, title=f'The Analyst failed to cope: {finding.title}'[:200],
        body=body + ' Your decision is needed; I will not repeat it on my own.',
        data={'finding_key': finding.key})


def _report(run: ActionRun, finding: Finding, now, error: str = '') -> None:
    """A visible card saying what the Analyst did itself: without it the owner would not learn about the
action."""
    if error:
        title, severity = f'The Analyst could not run: {run.title}', Finding.WARN
        body = f'Finding: {finding.title}. {error[:300]}'
    else:
        results = [c['text'] for c in run.changes if str(c.get('text', '')).startswith('Result:')]
        title, severity = f'The Analyst did it itself: {run.title}', Finding.INFO
        body = f'Was: {finding.title}. ' + ' '.join(results[:3])
    Finding.objects.create(
        key=f'auto:{run.pk}', check_name=CHECK_AUTO, section=Finding.SECTION_HEALTH, severity=severity,
        title=title[:200], body=body.strip(), first_seen=now, last_seen=now)


def _autofix(now, mode: str, result: GuardResult) -> list[Item]:
    """The 'fix' step: returns the 'failed to cope' cards (the watchdog cycle itself closes/opens them)."""
    cooldown = timedelta(hours=settings.ANALYST_AUTOFIX_COOLDOWN_HOURS)
    day_ago = now - timedelta(hours=24)
    stuck: list[Item] = []
    used_fixers: set[str] = set()
    candidates = Finding.objects.filter(check_name__in=GUARD_CHECKS, resolved_at__isnull=True).order_by('-severity', 'pk')
    for finding in candidates:
        if (finding.data or {}).get('no_auto'):
            continue                                  # the check itself marked it: automation does not cure this
        fixers = [f for f in actions.auto_fixers_for(finding.check_name) if finding.severity >= f.auto_min_severity]
        if not fixers:
            continue
        fixer = fixers[0]
        # We count the Analyst's attempts per THIS 'life' of the finding (from first_seen), not per
        # day: otherwise after 24 hours
        # the 'failed to cope' card would quietly vanish, while the finding would stay a grey
        # 'processed' one with the problem still alive.
        recent = list(ActionRun.objects.filter(
            finding=finding, created_by=AUTO_USER, created_at__gte=finding.first_seen
        ).order_by('-created_at'))
        if finding.status == Finding.DONE and not recent:
            continue                                  # the owner closed it with ✓ — automation does not interfere
        if recent:
            if now - recent[0].created_at >= cooldown:
                stuck.append(_stuck_item(finding, recent[0]))
            continue
        if fixer.key in used_fixers:
            continue                                  # the same restart was already done in this cycle
        if ActionRun.objects.filter(action=fixer.key, created_by=AUTO_USER, created_at__gte=now - cooldown).exists():
            continue                                  # this action was already performed recently (for another finding): waiting
        done_today = ActionRun.objects.filter(action=fixer.key, created_by=AUTO_USER, created_at__gte=day_ago).count()
        if done_today >= settings.ANALYST_AUTOFIX_MAX_PER_DAY:
            stuck.append(_stuck_item(finding, None, f'"{fixer.label}" has already run {done_today} time(s) in 24 hours, '
                                                    f'and the problem keeps coming back.'))
            continue
        try:
            run = actions.make_plan(finding, fixer.key, {}, AUTO_USER)
        except actions.PlanError as exc:
            log.warning('analyst: watchdog could not build a plan "%s" for "%s": %s', fixer.key, finding.key, exc)
            continue
        used_fixers.add(fixer.key)
        try:
            actions.apply_run(run)
        except actions.ApplyError as exc:
            log.warning('analyst: watchdog did not run "%s" for "%s": %s', fixer.key, finding.key, exc)
            run.refresh_from_db()
            _report(run, finding, now, error=str(exc))
            continue
        result.applied.append(run)
        _report(run, finding, now)
    return stuck


def run_guard(now=None) -> GuardResult:
    now = now or timezone.now()
    result = GuardResult()
    out = collect(now, only=GUARD_CHECKS)
    result.fresh = apply(out, now, only=GUARD_CHECKS)
    mode = autofix_mode()
    stuck: list[Item] = []
    if mode != 'ask':
        stuck = _autofix(now, mode, result)
    # 'Failed to cope' cards live as long as the condition holds; once the problem is gone they are
    # closed.
    apply(Outcome(items=stuck), now, exact_ran=(CHECK_STUCK,))
    result.stuck = len(stuck)
    if result.fresh or result.applied or result.stuck:
        _broadcast_badge()
    return result
