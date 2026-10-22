"""Fixes for Analyst findings: plan → owner confirmation → apply → rollback.

Rules that keep this safe:
  * The plan is built from LIVE data (not from the finding, which may be stale) and writes nothing.
  * Only the plan stored on the server is applied: the client cannot substitute edits.
  * Every edit goes through `save(update_fields=…)`, not `.update()`: this way the stock, label and
    derived Product.is_active signals fire.
  * Before writing, the value is checked against what was in the plan; if it changed — stop, write
    nothing, the plan is rebuilt.
  * Rollback restores 'before' only if the current value is still 'after' (we do not overwrite
    other people's edits).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal, InvalidOperation
from typing import Callable

from django.apps import apps
from django.conf import settings
from django.db import transaction
from django.utils import timezone

from . import hostops
from .models import ActionRun, Finding

PLAN_TTL = timedelta(minutes=30)
MAX_CHANGES = 500


class PlanError(Exception):
    """Nothing to fix, or fixing is unsafe. The text is shown to the owner."""


class ApplyError(Exception):
    """The plan could not be applied / rolled back. The text is shown to the owner."""


class _StaleError(ApplyError):
    pass


@dataclass
class Plan:
    title: str
    changes: list = field(default_factory=list)
    notes: list = field(default_factory=list)
    extra: dict = field(default_factory=dict)    # ends up in ActionRun.params (e.g. the helper's plan id)


@dataclass(frozen=True)
class Param:
    name: str
    label: str
    default: int
    minimum: int
    maximum: int


@dataclass(frozen=True)
class Fixer:
    key: str
    label: str
    checks: tuple
    build: Callable[[Finding, dict], Plan]
    params: tuple = ()
    reversible: bool = True
    needs_ops: bool = False      # the action is performed by the helper on the server; without that channel there is no button
    auto: bool = False           # may run without an owner click (ANALYST_AUTOFIX=auto)
    auto_min_severity: int = Finding.WARN   # automation only picks up findings of at least this importance
    always: bool = False         # the button is present in auto/off mode too (the owner's decision, not maintenance)
    single: bool = False         # only on its own, not part of 'Fix everything possible'


def _int_param(params, name, default, lo, hi):
    try:
        value = int(Decimal(str((params or {}).get(name, default))))
    except (InvalidOperation, ValueError, TypeError):
        raise PlanError(f'Parameter "{name}" must be a number.')
    if not lo <= value <= hi:
        raise PlanError(f'Parameter "{name}" must be between {lo} and {hi}.')
    return value


# ── Fixers ────────────────────────────────────────────────────────────────

def _host_build(action: str, title: str, fixed_params: dict | None = None):
    """Server-side action: the helper builds the plan (changing nothing), we only show its lines."""
    def build(finding, params):
        merged = {**(fixed_params or {}), **params}
        try:
            result = hostops.plan(action, merged)
        except hostops.HostError as exc:
            raise PlanError(str(exc))
        return Plan(title=title, changes=[{'host': True, 'text': line} for line in result['lines']],
                    notes=['This is an action on the server itself: there is no rollback in the log.'],
                    extra={'host_plan_id': result['id'], 'host_action': action})
    return build


def _codefix_build(kind: str):
    """Buttons on the auto-fix card (propose mode, or a fix waiting for the night window).
    Changes nothing: shows what exactly will be deployed; the decision comes after 'Apply'
    in the plan window. A fix the reviewer did not
    approve is never deployed by this button at all: this button goes through web, and web has no
    right to override the reviewer's rejection."""
    def build(finding, params):
        from .models import CodeFix
        raw = (finding.data or {}).get('codefix_id')
        fix = CodeFix.objects.filter(pk=raw).first() if str(raw).isdigit() else None
        if fix is None or fix.status != CodeFix.READY:
            raise PlanError('This fix is no longer waiting for a decision.')
        if kind == 'reject':
            return Plan(title=f'Reject the fix: {fix.summary[:80]}',
                        changes=[{'host': True, 'text': 'The fix will be rejected, nothing on the site will change.'}],
                        extra={'codefix_id': fix.pk, 'codefix_action': 'reject'})
        if not fix.approved:
            raise PlanError('The reviewer did NOT approve this fix (or there is no verdict), so it cannot be deployed with the button. If you have read '
                            'the changes and are sure they must be deployed, ask Claude to apply them manually: the diff is in the card.')
        rows = [fix.summary, 'Sandbox check passed: the new test failed without the fix and passes with it.',
                'The reviewer approved.',
                'A thin layer will be built on top of the running image and the site restarted (up to a minute without the site), then '
                'up to 3 minutes of health checks.',
                'If anything goes wrong — automatic rollback to the previous state.', 'Changes (code first, then the test):']
        rows += fix.patch.splitlines()
        return Plan(title=f'Deploy the fix: {fix.summary[:80]}',
                    changes=[{'host': True, 'text': line} for line in rows],
                    notes=['After deployment the fix exists only on the server: move it into git before a manual deploy.'],
                    extra={'codefix_id': fix.pk, 'codefix_action': 'apply'})
    return build


# Server-side actions only: they are performed by the 'helper' on the VPS from an allowlist. `auto`
# — the
# Analyst may run them itself (ANALYST_AUTOFIX=auto mode), because they are narrow and safe by
# default:
# rollback images are deleted only with an explicit parameter that automation does not pass.
FIXERS: dict[str, Fixer] = {f.key: f for f in (
    Fixer('host_disk_cleanup', 'Free up space', ('disk_usage',),
          _host_build('disk_cleanup', 'Free up space on the server'),
          params=(Param('rollback_images', 'Delete rollback images of old releases (0 — no, 1 — yes)', 0, 0, 1),
                  Param('keep_rollback', 'How many of the newest rollback releases to keep', 2, 1, 5)),
          reversible=False, needs_ops=True, auto=True, auto_min_severity=Finding.CRITICAL),   # disk ≥90%
    Fixer('host_backup_now', 'Back up now', ('backup_freshness',),
          _host_build('backup_now', 'Back up the database now'), reversible=False, needs_ops=True, auto=True),
    Fixer('host_restart_scheduler', 'Restart the scheduler', ('scheduler_heartbeat',),
          _host_build('restart_service', 'Restart the scheduler', {'service': 'scheduler'}),
          reversible=False, needs_ops=True, auto=True),
    Fixer('codefix_apply', 'Apply the fix', ('codefix',), _codefix_build('apply'),
          reversible=False, needs_ops=True, always=True, single=True),
    Fixer('codefix_reject', 'Reject', ('codefix',), _codefix_build('reject'),
          reversible=False, always=True, single=True),
)}


def fixers_for(check_name: str) -> list[Fixer]:
    """Buttons on the card. In auto mode there are none (the Analyst performs the actions itself,
`auto_fixers_for`), and in off mode there are none either."""
    ops = hostops.configured()
    if settings.ANALYST_AUTOFIX in ('auto', 'off'):
        return [f for f in FIXERS.values() if f.always and check_name in f.checks and (ops or not f.needs_ops)]
    return [f for f in FIXERS.values() if check_name in f.checks and (ops or not f.needs_ops)]


def auto_fixers_for(check_name: str) -> list[Fixer]:
    if not hostops.configured():
        return []
    return [f for f in FIXERS.values() if f.auto and check_name in f.checks]


# ── Plan / apply / rollback ───────────────────────────────────────────

def make_plan(finding: Finding, fixer_key: str, params: dict | None, user_email: str) -> ActionRun:
    fixer = FIXERS.get(fixer_key)
    if fixer is None or finding.check_name not in fixer.checks:
        raise PlanError('There is no such action for this finding.')
    if fixer.needs_ops and not hostops.configured():
        raise PlanError('The channel to the server is not configured (ANALYST_OPS_DIR).')
    if finding.resolved_at is not None:
        raise PlanError('The finding is no longer relevant.')
    clean = {p.name: _int_param(params, p.name, p.default, p.minimum, p.maximum) for p in fixer.params}
    plan = fixer.build(finding, clean)
    if len(plan.changes) > MAX_CHANGES:
        raise PlanError(f'Too many changes at once ({len(plan.changes)}). Narrow the selection.')
    return ActionRun.objects.create(
        finding=finding, action=fixer.key, title=plan.title[:200], params={**clean, **plan.extra},
        reversible=fixer.reversible,
        changes=plan.changes, notes=plan.notes, created_by=user_email[:254],
    )


def _same(model_field, a, b) -> bool:
    return model_field.to_python(a) == model_field.to_python(b)


def _write(change: dict, expect, new) -> None:
    model = apps.get_model(change['model'])
    obj = model.objects.select_for_update().filter(pk=change['pk']).first()
    if obj is None:
        raise _StaleError(f'{change["text"]}: the record is already gone — build the plan again.')
    model_field = model._meta.get_field(change['field'])
    if not _same(model_field, getattr(obj, change['field']), expect):
        raise _StaleError(f'{change["text"]}: the data has already changed — build the plan again.')
    setattr(obj, change['field'], model_field.to_python(new))
    update_fields = [change['field']]
    if any(f.name == 'updated_at' for f in model._meta.get_fields()):
        update_fields.append('updated_at')
    obj.save(update_fields=update_fields)
    # The model could have recomputed the value itself (the stock level of bulk-bag packaging is
    # derived):
    # then 'success' would be fake, so we check that exactly what was planned got written.
    obj.refresh_from_db(fields=[change['field']])
    if not _same(model_field, getattr(obj, change['field']), new):
        raise ApplyError(f'{change["text"]}: the system recalculated the value itself — the edit was not accepted.')


def _create(change: dict) -> None:
    """A 'create a record' operation: the id of the new record stays in change['pk'] — rollback uses it."""
    model = apps.get_model(change['model'])
    obj = model.objects.create(**change['values'])
    change['pk'] = obj.pk


def _undo_create(change: dict) -> None:
    model = apps.get_model(change['model'])
    obj = model.objects.select_for_update().filter(pk=change.get('pk')).first()
    if obj is None:
        raise _StaleError(f'{change["text"]}: the record is already deleted — nothing to roll back.')
    guard = change.get('guard')            # {'field': 'status', 'value': 'open'} — do not delete if the record has already 'moved on'
    if guard and getattr(obj, guard['field']) != guard['value']:
        raise _StaleError(f'{change["text"]}: the record has already changed (status "{getattr(obj, guard["field"])}") — '
                          f'delete it manually if needed.')
    obj.delete()


def _run_changes(run: ActionRun, *, reverse: bool) -> None:
    items = list(reversed(run.changes)) if reverse else run.changes
    try:
        with transaction.atomic():
            for change in items:
                if change.get('op') == 'create':
                    (_undo_create if reverse else _create)(change)
                elif reverse:
                    _write(change, change['after'], change['before'])
                else:
                    _write(change, change['before'], change['after'])
    except ApplyError:
        raise
    except Exception as exc:   # ValidationError from the order state machine, etc.
        messages = getattr(exc, 'messages', None)
        raise ApplyError('; '.join(messages) if messages else f'{type(exc).__name__}: {exc}')


def _run_host(run: ActionRun) -> None:
    """Apply the helper's plan on the server; its response is appended to the log."""
    try:
        result = hostops.apply(run.params['host_plan_id'])
    except hostops.HostError as exc:
        raise ApplyError(str(exc))
    run.changes = list(run.changes) + [{'host': True, 'text': f'Result: {line}'} for line in result['lines']]


def _run_codefix(run: ActionRun) -> None:
    from .codefix import pipeline
    from .models import CodeFix
    fix = CodeFix.objects.filter(pk=run.params['codefix_id']).first()
    if fix is None or fix.status != CodeFix.READY:
        raise ApplyError('This fix is no longer waiting for a decision.')
    try:
        if run.params.get('codefix_action') == 'apply':
            pipeline.approve(fix)
            text = ('Accepted. The Analyst service will sign and send the deploy task within a minute; the result will appear '
                    'on the fix card in a few minutes.')
        else:
            pipeline.reject(fix)
            text = 'The fix was rejected.'
    except ValueError as exc:
        raise ApplyError(str(exc))
    run.changes = list(run.changes) + [{'host': True, 'text': f'Result: {text}'}]


def apply_run(run: ActionRun) -> ActionRun:
    if run.status != ActionRun.PLANNED:
        raise ApplyError('This plan has already been applied or cancelled.')
    if timezone.now() - run.created_at > PLAN_TTL:
        raise ApplyError('The plan is out of date — build it again.')
    try:
        if run.params.get('host_plan_id'):
            _run_host(run)
        elif run.params.get('codefix_id'):
            _run_codefix(run)
        else:
            _run_changes(run, reverse=False)
    except ApplyError as exc:
        run.status, run.error = ActionRun.FAILED, str(exc)
        run.save(update_fields=['status', 'error'])
        raise
    run.status, run.applied_at, run.error = ActionRun.APPLIED, timezone.now(), ''
    run.save(update_fields=['status', 'applied_at', 'error', 'changes'])
    if run.finding_id and run.finding.resolved_at is None:
        Finding.objects.filter(pk=run.finding_id).update(status=Finding.DONE, snoozed_until=None)
    return run


def revert_run(run: ActionRun) -> ActionRun:
    if run.status != ActionRun.APPLIED:
        raise ApplyError('Only an applied fix can be rolled back.')
    if not run.reversible:
        raise ApplyError('This fix cannot be rolled back.')
    _run_changes(run, reverse=True)
    run.status, run.reverted_at = ActionRun.REVERTED, timezone.now()
    run.save(update_fields=['status', 'reverted_at'])
    if run.finding_id and run.finding.resolved_at is None:
        Finding.objects.filter(pk=run.finding_id).update(status=Finding.NEW)
    return run
