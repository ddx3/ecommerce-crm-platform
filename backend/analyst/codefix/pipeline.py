"""Auto-fix pipeline: error → agent draft → sandbox → reviewer → deployment → monitoring →
confirmation.

One pipeline step is `tick()`; it is called by the thread of the `run_analyst` service (separate
from the watchdog) and by the drill command. All long work
is done by the helper on the VPS (sandbox verification, deployment); here there is only the dispatch
of signed jobs and the reading of results, so a
restart of the analyst container in the middle of a deployment breaks nothing: the state lives in
the `CodeFix` table, and an unfinished deployment on the
host is rolled back by the helper itself.

`ANALYST_CODEFIX` modes:
  off     — does nothing;
  propose — prepares the fix to the end (sandbox + reviewer) and waits for the owner's 'Apply'
  button;
  auto    — after reviewer approval and a successful check, deploys by itself, only in the night
  window.
The hard safeguards (file policy, sandbox, window, limits, rollback) are on the host; here are
selection, bookkeeping and SEALS: the patch and the verdict of the
reviewer are sealed with a key that web doesn't have (`auth.py`), so tampering with a DB record
won't bypass the agent and the reviewer.
"""
from __future__ import annotations

import logging
import uuid
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path

from django.conf import settings
from django.db import connection
from django.utils import timezone

from analyst import codefix_policy as policy
from analyst import hostops
from analyst.codefix import auth
from analyst.models import CodeFix, ErrorEvent, Finding

log = logging.getLogger('analyst')

CHECK_NAME = 'codefix'
VALIDATE_TIMEOUT = timedelta(minutes=110)
DEPLOY_TIMEOUT = timedelta(minutes=45)
DRAFT_STALE = timedelta(minutes=40)
REVIEW_STALE = timedelta(minutes=20)
WAIT_SHORT = timedelta(minutes=30)
WAIT_WINDOW = timedelta(hours=1)
MAX_REVALIDATIONS = 3
MAX_INTERRUPTIONS = 3
MAX_REVIEW_TRIES = 3
MAX_LLM_WAITS = 6
DIFF_IN_CARD = 6000
TAMPER_TEXT = 'The fix record was changed outside the Analyst (the seal does not match) — deployment cancelled.'


def mode() -> str:
    value = getattr(settings, 'ANALYST_CODEFIX', 'off')
    return value if value in ('propose', 'auto') else 'off'


LOCK_BASE = 7_340_000_000


@contextmanager
def exclusive(key: int):
    """At most one process per pipeline step (the service, the manual command and the drill can run at
    the same time).
    A session-level Postgres advisory lock: it disappears by itself if the process or connection
    dies. Returns True if the lock was acquired."""
    with connection.cursor() as cur:
        cur.execute('SELECT pg_try_advisory_lock(%s)', [LOCK_BASE + key])
        got = bool(cur.fetchone()[0])
    try:
        yield got
    finally:
        if got:
            with connection.cursor() as cur:
                cur.execute('SELECT pg_advisory_unlock(%s)', [LOCK_BASE + key])


def _setting(name: str, default: int) -> int:
    return int(getattr(settings, name, default))


# ── readiness and selection ──────────────────────────────────────────────────

def ops_ready() -> tuple[bool, str]:
    if not hostops.configured():
        return False, 'The channel to the server is not configured.'
    if not hostops.helper_alive():
        return False, 'The helper on the server is not responding.'
    status = hostops.status() or {}
    if auth.key() is None:
        return False, 'No signing key for auto-fixes (ANALYST_CODEFIX_KEY_FILE) — tasks for the helper cannot be signed.'
    if status.get('policy_sha') != policy.policy_sha():
        return False, ('The auto-fix policy on the server does not match the Analyst\'s policy '
                       '(run ops/install.sh on the VPS).')
    if status.get('key_fp') != auth.key_fingerprint():
        return False, 'The signing key on the server does not match the Analyst\'s key (see /opt/shop-autofix/codefix.key and docker-compose).'
    if status.get('paused'):
        return False, 'Auto-fix is paused on the server (PAUSE file).'
    return True, ''


def event_payload(event: ErrorEvent, now) -> dict:
    return {
        'exc_type': event.exc_type, 'message': event.message, 'where_path': event.where_path,
        'where_func': event.where_func, 'where_line': event.where_line, 'request_path': event.request_path,
        'service': event.service, 'traceback': event.traceback,
        'count_24h': event.count_since(now - timedelta(hours=24)),
    }


def review_event(event: ErrorEvent | None) -> dict:
    """What the reviewer knows about the error: only the type and location in the code (the error text and
the request address can be injected from outside)."""
    if event is None:
        return {'exc_type': '?', 'where_path': '?', 'where_func': '?'}
    return {'exc_type': event.exc_type, 'where_path': event.where_path, 'where_func': event.where_func}


def file_ok(repo_path: str) -> bool:
    """Whether the Workspace is able to edit this file (size, line endings…). Otherwise the agent session
would be wasted."""
    rel = repo_path[len('backend/'):] if repo_path.startswith('backend/') else repo_path
    path = Path(settings.BASE_DIR) / rel
    try:
        if path.is_symlink() or not path.is_file():
            return False
        return not policy.file_problem(path.read_bytes().decode('utf-8', errors='replace'))
    except OSError:
        return False


def frozen_reason(event: ErrorEvent) -> str:
    """Why auto-fix won't touch the function where the error occurred (empty — it may): it writes to the
database or does the access check itself."""
    rel = event.where_path[len('backend/'):] if event.where_path.startswith('backend/') else event.where_path
    try:
        text = (Path(settings.BASE_DIR) / rel).read_bytes().decode('utf-8', errors='replace')
    except OSError:
        return ''
    return policy.frozen_reason(text, event.where_line or 0, event.where_path)


def pick_event(now) -> ErrorEvent | None:
    """Which error to take on: it recurs, lies in an allowed and suitable file, and hasn't been tried
    recently.
    The hottest one over the last 24 hours goes first (not by the lifetime counter)."""
    if CodeFix.objects.filter(created_at__gte=now - timedelta(hours=24)).count() >= _setting('ANALYST_CODEFIX_MAX_STARTS_PER_DAY', 3):
        return None
    day, retry = now - timedelta(hours=24), timedelta(hours=_setting('ANALYST_CODEFIX_RETRY_HOURS', 24))
    min_repeats = _setting('ANALYST_CODEFIX_MIN_REPEATS', 3)
    ranked = []
    for event in (ErrorEvent.objects.filter(kind=ErrorEvent.CODE, status=ErrorEvent.OPEN, last_seen__gte=day)
                  .exclude(service='drill')):
        if policy.classify(event.where_path) != 'code':
            continue
        per_day = event.count_since(day)
        if per_day < min_repeats:
            continue
        if event.fixes.filter(created_at__gte=now - retry).exists():
            continue
        if event.fixes.filter(created_at__gte=now - timedelta(days=7)).count() >= 3:
            continue
        if not file_ok(event.where_path) or frozen_reason(event):
            continue
        ranked.append((per_day, event))
    ranked.sort(key=lambda row: -row[0])
    return ranked[0][1] if ranked else None


# ── inbox cards ──────────────────────────────────────────────────────────────

def _where(fix: CodeFix) -> str:
    event = fix.event
    if event is None:
        return 'unknown location'
    short = event.exc_type.rsplit('.', 1)[-1]
    place = event.where_path[len('backend/'):] if event.where_path.startswith('backend/') else event.where_path
    return f'{short} in {place}'


def _short(text: str, limit: int = 90) -> str:
    text = ' '.join((text or '').split())
    return text if len(text) <= limit else text[:limit - 1].rstrip() + '…'


def _tests_line(fix: CodeFix) -> str:
    patched = (fix.phases or {}).get('patched') or {}
    base = (fix.phases or {}).get('baseline') or {}
    if patched.get('ran') and base.get('ran'):
        return (f'Sandbox check passed: {base["ran"]} new tests failed without the fix and passed with it; '
                f'the existing tests of the app are not broken.')
    return 'Sandbox check passed.'


def _card_data(fix: CodeFix) -> tuple[str, int, str, bool]:
    """(title, importance, text, whether to open the card as new)."""
    s = fix.status
    where = _where(fix)
    short = _short(fix.summary or where)
    if s == CodeFix.READY:
        if fix.approved:
            review = 'The reviewer approved.'
        elif (fix.stats or {}).get('review', {}).get('tries', 0) >= MAX_REVIEW_TRIES and not (fix.stats or {}).get('review', {}).get('verdict'):
            review = f'The reviewer is temporarily unavailable ({fix.review or "no explanation"}) — there is no verdict.'
        else:
            review = f'The reviewer did NOT approve: {fix.review or "no explanation"}.'
        diff = fix.patch if len(fix.patch) <= DIFF_IN_CARD else fix.patch[:DIFF_IN_CARD] + '\n… (shortened, the code goes before the tests)'
        if fix.approved:
            if mode() == 'propose':
                auto_note = ''
            elif owner_decides(fix):
                auto_note = '\nThis file decides what is visible and to whom (API, serializers, CRM): I will deploy only after your "Apply".'
            else:
                auto_note = '\nA night deployment is scheduled (00:00–07:00 Kyiv time).'
            todo = 'To deploy to the site, press "Apply the fix" in the card window.'
        else:
            auto_note = '\nI do not deploy automatically: without the independent reviewer\'s approval there is no button.'
            todo = 'If you consider the fix good, ask Claude to apply it manually (changes below).'
        body = f'{fix.summary}\n\nError: {where}.\n{_tests_line(fix)} {review}{auto_note}\n\n{todo}\n\nChanges:\n{diff}'
        return f'The error fix is ready: {short}', Finding.WARN, body, True
    if s == CodeFix.DEPLOYED:
        when = timezone.localtime(fix.deployed_at).strftime('%d.%m %H:%M') if fix.deployed_at else ''
        body = (f'{fix.summary}\n\nError: {where}. Deployed {when} (commit {fix.commit[:8]}). {_tests_line(fix)}\n'
                f'I am watching for a day: if the error returns, the fix will be reverted by itself. '
                f'For now it exists only on the server: move it into git before the next manual deploy.')
        return f'The Analyst fixed the error: {short}', Finding.INFO, body, True
    if s == CodeFix.CONFIRMED:
        return (f'Fix confirmed: {short}', Finding.INFO,
                f'{fix.summary}\n\nError: {where}. It did not recur within a day after the fix.', False)
    if s == CodeFix.ROLLED_BACK:
        if (fix.stats or {}).get('rollback_unhealthy'):
            return (f'WARNING: the site may not work after the fix rollback: {short}', Finding.CRITICAL,
                    f'Error: {where}. The fix was deployed and failed the check; the rollback did not confirm the site\'s health either.\n'
                    f'Reason: {fix.reason or "no explanation"}.\nCheck the site and the shop-helper log on the server immediately.', True)
        return (f'The Analyst rolled back the fix: {short}', Finding.WARN,
                f'Error: {where}. The fix was deployed, but the health check failed, and everything was restored as it was.\n'
                f'Reason: {fix.reason or "no explanation"}.', True)
    if s == CodeFix.REVERTED:
        return (f'The fix did not help, reverted: {short}', Finding.WARN,
                f'Error: {where} returned after the fix, so it was reverted. A manual investigation is needed.\n{fix.reason}', True)
    if s == CodeFix.REJECTED:
        return (f'The Analyst did not undertake the fix: {where}', Finding.WARN, f'{fix.reason}\nA manual investigation is needed.', True)
    return (f'The Analyst could not fix the error: {where}', Finding.WARN, f'{fix.reason}\nA manual investigation is needed.', True)


CARD_STATES = (CodeFix.READY, CodeFix.DEPLOYED, CodeFix.CONFIRMED, CodeFix.ROLLED_BACK, CodeFix.REVERTED, CodeFix.REJECTED, CodeFix.FAILED)


def card(fix: CodeFix, now=None) -> None:
    """Card about the state of a fix. One record per CodeFix, rewritten on every change."""
    if fix.status not in CARD_STATES or fix.drill:
        return                          # a drill is a check of the mechanism: the owner doesn't need its cards (there is the command output)
    now = now or timezone.now()
    title, severity, body, reopen = _card_data(fix)
    finding, created = Finding.objects.get_or_create(
        key=f'codefix:{fix.pk}', defaults=dict(
            check_name=CHECK_NAME, section=Finding.SECTION_HEALTH, severity=severity, title=title[:200], body=body,
            data={'codefix_id': fix.pk, 'no_auto': True}, first_seen=now, last_seen=now))
    if created:
        return
    finding.title, finding.severity, finding.body, finding.last_seen = title[:200], severity, body, now
    finding.data = {**(finding.data or {}), 'codefix_id': fix.pk, 'no_auto': True, 'status': fix.status}
    finding.resolved_at = None
    if reopen:
        finding.status, finding.first_seen = Finding.NEW, now
    finding.save()


def _close_card(fix: CodeFix, now) -> None:
    Finding.objects.filter(key=f'codefix:{fix.pk}', resolved_at__isnull=True).update(resolved_at=now)


def _set_event(fix: CodeFix, status: str) -> None:
    if fix.event_id:
        ErrorEvent.objects.filter(pk=fix.event_id).update(status=status)


# ── transitions ──────────────────────────────────────────────────────────────

def _end(fix: CodeFix, status: str, reason: str = '', now=None, event_status: str = ErrorEvent.OPEN) -> None:
    fix.status, fix.reason = status, reason[:1500] if reason else fix.reason
    fix.job_id = ''
    fix.save()
    _set_event(fix, event_status)
    card(fix, now)
    log.info('analyst codefix %s: %s — %s', fix.pk, status, reason[:200])


def _fail(fix: CodeFix, reason: str, now=None) -> None:
    _end(fix, CodeFix.FAILED, reason, now)


def _submit(fix: CodeFix, action: str, params: dict, now) -> bool:
    """Send a signed job to the helper. False — the helper is temporarily unavailable (we'll try later)."""
    try:
        fix.job_id = hostops.submit_job(action, params)
    except hostops.HostError as exc:
        fix.wait_until = now + WAIT_SHORT
        fix.job_id = ''
        fix.save()
        log.warning('analyst codefix %s: %s', fix.pk, exc)
        return False
    fix.job_started_at, fix.wait_until = now, None
    return True


def owner_decides(fix: CodeFix) -> bool:
    """Whether the deployment of this fix is decided by the owner alone: the patch touches a file from
    the policy.OWNER_ONLY list (API, serializers, CRM views,
    feed, moderation, media…). Then even in auto mode it waits for 'Apply'."""
    return policy.needs_owner(fix.patch)


def auto_deploys(fix: CodeFix) -> bool:
    """Whether the service will deploy this fix by itself (in the night window): auto mode, the reviewers
approved, and the file is not one that a human decides on."""
    return mode() == 'auto' and fix.approved and not owner_decides(fix)


def _patch_intact(fix: CodeFix) -> bool:
    return auth.valid(fix.patch_mac, auth.seal_patch(fix.patch, fix.patch_id, fix.simulate_failure, fix.drill))


def start(event: ErrorEvent, now, *, drill: bool = False, simulate_failure: bool = False) -> CodeFix:
    fix = CodeFix.objects.create(event=event, fingerprint=event.fingerprint, mode=mode(), drill=drill,
                                 simulate_failure=simulate_failure, created_at=now)
    event.status = ErrorEvent.FIXING
    event.save(update_fields=['status'])
    return fix


def _defer_llm(fix: CodeFix, now, exc, status: str) -> None:
    """LLM unavailable (limit, authentication…): this is not an agent failure — we postpone the step and
don't count the attempt."""
    waits = (fix.stats or {}).get('llm_waits', 0) + 1
    fix.stats = {**(fix.stats or {}), 'llm_waits': waits}
    if waits > MAX_LLM_WAITS:
        _fail(fix, f'The LLM has been unavailable for too long: {exc}', now)
        return
    fix.status = status
    fix.wait_until = now + timedelta(seconds=getattr(exc, 'wait', 3600))
    fix.save()
    log.warning('analyst codefix %s: LLM unavailable, paused until %s — %s', fix.pk, fix.wait_until, exc)


def draft(fix: CodeFix, now) -> None:
    """The agent writes the fix (a few minutes) and, if it succeeded, the patch goes to the sandbox for
verification."""
    from analyst.codefix import drafter, llm
    fix.status, fix.attempt = CodeFix.DRAFTING, fix.attempt + 1
    fix.save()
    try:
        result = drafter.run_draft(event_payload(fix.event, now), fix.history)
    except llm.LLMUnavailable as exc:
        fix.attempt -= 1
        _defer_llm(fix, now, exc, CodeFix.QUEUED)
        return
    except Exception as exc:
        log.exception('analyst codefix %s: the agent failed', fix.pk)
        _fail(fix, f'The agent failed: {exc}', now)
        return
    fix.stats = {**(fix.stats or {}), 'draft': result.stats}
    if not result.ok:
        _end(fix, CodeFix.REJECTED, f'The agent refused to fix it: {result.reason}', now)
        return
    fix.patch, fix.summary = result.patch, result.summary
    fix.patch_id = uuid.uuid4().hex[:16]
    fix.patch_mac = auth.seal_patch(fix.patch, fix.patch_id, fix.simulate_failure, fix.drill)
    if not fix.patch_mac:
        _fail(fix, 'No signing key for auto-fixes — the patch cannot be sealed.', now)
        return
    try:
        hostops.put_artifact(fix.patch_id, fix.patch)
    except hostops.HostError as exc:
        fix.status, fix.wait_until = CodeFix.QUEUED, now + WAIT_SHORT
        fix.save()
        log.warning('analyst codefix %s: %s', fix.pk, exc)
        return
    _validate(fix, now)


def _retry_draft(fix: CodeFix, now, failure: str, tails=None) -> None:
    """Verification failed: we return the failure to the agent for another attempt while attempts remain."""
    fix.history = list(fix.history or []) + [{'patch': fix.patch, 'failure': f'{failure} {tails or ""}'[:1500]}]
    if fix.attempt < _setting('ANALYST_CODEFIX_MAX_DRAFTS', 3):
        fix.save()
        draft(fix, now)
        return
    _fail(fix, f'The check failed after {fix.attempt} attempts: {failure}', now)


def _validate(fix: CodeFix, now) -> None:
    if not _patch_intact(fix):
        _fail(fix, TAMPER_TEXT, now)
        return
    fix.status = CodeFix.VALIDATING
    _submit(fix, 'codefix_validate', {'patch_id': fix.patch_id, 'patch_sha': auth.patch_sha(fix.patch)}, now)
    fix.save()


def _result(fix: CodeFix, action: str):
    """Job result, with a check that it is THIS job and THIS patch (the result file is read from the shared
folder)."""
    res = hostops.job_result(fix.job_id)
    if not isinstance(res, dict) or res.get('id') != fix.job_id:
        return None
    if res.get('status') == 'done':
        if res.get('action') != action:
            return None
        if action in ('codefix_validate', 'codefix_deploy') and res.get('patch_sha') != auth.patch_sha(fix.patch):
            return {'status': 'failed', 'code': 'error', 'error': 'The helper\'s result refers to a different patch.'}
    return res


def _timed_out(fix: CodeFix, now, limit: timedelta) -> bool:
    return now - (fix.job_started_at or now) > limit


def _poll_validate(fix: CodeFix, now) -> None:
    if not fix.job_id:                                    # the helper was unavailable — try again
        if fix.wait_until is None or now >= fix.wait_until:
            _validate(fix, now)
        return
    res = _result(fix, 'codefix_validate')
    if res is None or res.get('status') == 'running':
        if _timed_out(fix, now, VALIDATE_TIMEOUT):
            _fail(fix, 'The helper did not finish the sandbox check in time.', now)
        return
    if res.get('status') == 'failed':
        code, error = res.get('code'), res.get('error', 'no explanation')
        if code == 'wait':
            fix.job_id, fix.wait_until = '', now + WAIT_SHORT
            fix.save()
            return
        if code == 'interrupted' and (fix.stats or {}).get('interruptions', 0) < MAX_INTERRUPTIONS:
            fix.stats = {**(fix.stats or {}), 'interruptions': (fix.stats or {}).get('interruptions', 0) + 1}
            fix.job_id, fix.wait_until = '', now + timedelta(minutes=2)
            fix.save()
            return
        if code == 'policy':
            _retry_draft(fix, now, error)                  # the host policy is stricter than the local one — this is a hint for the agent
            return
        _fail(fix, f'The sandbox check did not take place: {error}', now)
        return
    fix.phases = res.get('phases') or {}
    if res.get('verdict') != 'pass':
        reason = res.get('reason') or 'no explanation'
        tails = {k: v.get('tail', [])[-6:] for k, v in (res.get('phases') or {}).items() if isinstance(v, dict)}
        _retry_draft(fix, now, reason, tails)
        return
    fix.base_sha = res.get('head', '')
    review(fix, now)


def review(fix: CodeFix, now) -> None:
    """Independent reviewer. The verdict is STICKY: if one already exists for this same patch (even
    after a re-verification), we don't ask again.
    The reviewer being unavailable is not a rejection: we retry later, and after several attempts
    hand the decision to the owner."""
    from analyst.codefix import reviewer
    sha = auth.patch_sha(fix.patch)
    known = (fix.stats or {}).get('review') or {}
    for sealed in (True, False):                   # a verdict sealed with the key is final: wiping the counters in the DB doesn't give a new roll
        if auth.valid(fix.review_mac, auth.seal_review(fix.patch, sealed)):
            fix.approved = sealed
            _after_review(fix, now)
            return
    if known.get('patch_sha') == sha and known.get('final'):
        _after_review(fix, now)
        return
    fix.status = CodeFix.REVIEWING
    fix.job_id = ''
    fix.save()
    verdict = reviewer.review_consensus(fix.patch, review_event(fix.event), fix.summary)
    tries = known.get('tries', 0) + 1 if known.get('patch_sha') == sha else 1
    if verdict.unavailable:
        if tries < MAX_REVIEW_TRIES:
            fix.stats = {**(fix.stats or {}), 'review': {'patch_sha': sha, 'tries': tries}}
            fix.wait_until = now + timedelta(seconds=max(300, verdict.wait))
            fix.save()
            return
        fix.approved, fix.review = False, verdict.concerns
        fix.stats = {**(fix.stats or {}), 'review': {'patch_sha': sha, 'tries': tries, 'final': True, 'verdict': False}}
    else:
        fix.approved, fix.review = verdict.approved, verdict.concerns
        fix.stats = {**(fix.stats or {}), 'review': {'patch_sha': sha, 'tries': tries, 'final': True, 'verdict': True},
                     'review_llm': verdict.stats}
    fix.review_mac = auth.seal_review(fix.patch, fix.approved)
    fix.wait_until = None
    _after_review(fix, now)


def _after_review(fix: CodeFix, now) -> None:
    fix.job_id = ''
    if auto_deploys(fix):
        deploy(fix, now)
        return
    fix.status = CodeFix.READY
    fix.save()
    card(fix, now)


def deploy(fix: CodeFix, now, *, by_owner: bool = False) -> None:
    """Signed deployment job. Two things are needed: an intact patch (seal) and the reviewer's approval
    (verdict seal). The owner's decision
    only allows deploying earlier than the night window (`by_owner`), but doesn't cancel the
    reviewer's verdict: 'Apply' goes through web,
    and web has no right to override a rejection issued by the reviewer."""
    if not _patch_intact(fix):
        _fail(fix, TAMPER_TEXT, now)
        return
    if not fix.approved:
        fix.status = CodeFix.READY
        fix.save()
        return
    if not auth.valid(fix.review_mac, auth.seal_review(fix.patch, True)):
        _fail(fix, TAMPER_TEXT, now)                # the record says 'approved', but there is no verdict seal
        return
    fix.status = CodeFix.DEPLOYING
    params = {'patch_id': fix.patch_id, 'patch_sha': auth.patch_sha(fix.patch), 'base_sha': fix.base_sha,
              'summary': (fix.summary or 'fix')[:160], 'by_owner': bool(by_owner),
              'simulate_failure': bool(fix.simulate_failure), 'drill': bool(fix.drill)}
    if not _submit(fix, 'codefix_deploy', params, now):
        fix.status = CodeFix.READY
    fix.save()


def _poll_deploy(fix: CodeFix, now) -> None:
    res = _result(fix, 'codefix_deploy')
    if res is None or res.get('status') == 'running':
        if _timed_out(fix, now, DEPLOY_TIMEOUT):
            _fail(fix, 'The helper did not finish the deployment in time — check the state of the site and the shop-helper log.', now)
        return
    if res.get('status') == 'failed':
        code, error = res.get('code'), res.get('error', 'no explanation')
        if code == 'wait':
            fix.status, fix.job_id = CodeFix.READY, ''
            fix.wait_until = now + (WAIT_WINDOW if 'at night' in error else WAIT_SHORT)
            fix.save()
            return
        if code == 'stale' and (fix.stats or {}).get('revalidations', 0) < MAX_REVALIDATIONS:
            fix.stats = {**(fix.stats or {}), 'revalidations': (fix.stats or {}).get('revalidations', 0) + 1}
            fix.save()
            _validate(fix, now)
            return
        if code == 'interrupted':
            _end(fix, CodeFix.FAILED, 'The helper restarted in the middle of the deployment; it restored the previous state by itself. '
                                      'Check the site and repeat if needed.', now)
            return
        _end(fix, CodeFix.FAILED, f'The deployment did not take place: {error}', now)
        return
    outcome = res.get('outcome')
    if outcome == 'deployed':
        fix.status, fix.commit, fix.deployed_at, fix.job_id = CodeFix.DEPLOYED, res.get('commit', ''), now, ''
        fix.save()
        _set_event(fix, ErrorEvent.FIXING)
        card(fix, now)
        return
    if outcome == 'rollback_unhealthy':
        fix.stats = {**(fix.stats or {}), 'rollback_unhealthy': True}
    _end(fix, CodeFix.ROLLED_BACK, res.get('reason') or 'the health check failed', now)


def _watch(fix: CodeFix, now) -> None:
    event, since = fix.event, fix.deployed_at or fix.created_at
    again = event.count_since(since) if event else 0
    if again >= _setting('ANALYST_CODEFIX_MIN_REPEATS', 3):
        revert(fix, now, 'The error recurred after the fix.')
        return
    if now - since >= timedelta(hours=_setting('ANALYST_CODEFIX_WATCH_HOURS', 24)):
        fix.status = CodeFix.CONFIRMED
        fix.save()
        _set_event(fix, ErrorEvent.FIXED)
        card(fix, now)


def revert(fix: CodeFix, now, why: str) -> None:
    fix.reason = why
    fix.status = CodeFix.REVERTING
    if not _submit(fix, 'codefix_revert', {'commit': fix.commit, 'summary': 'did not help: ' + _short(fix.summary, 80)}, now):
        fix.status = CodeFix.DEPLOYED
    fix.save()


def _poll_revert(fix: CodeFix, now) -> None:
    res = _result(fix, 'codefix_revert')
    if res is None or res.get('status') == 'running':
        if _timed_out(fix, now, DEPLOY_TIMEOUT):
            _fail(fix, 'The helper did not finish the revert in time — check the state of the site and the shop-helper log.', now)
        return
    if res.get('status') == 'failed':
        _fail(fix, f'Could not revert the fix: {res.get("error", "no explanation")}. Check the site.', now)
        return
    _end(fix, CodeFix.REVERTED, fix.reason, now)


# ── main loop ────────────────────────────────────────────────────────────────

def approve(fix: CodeFix, now=None) -> None:
    """The owner pressed 'Apply the fix' (propose mode, or a fix without reviewer
    approval).
    This may be called from web, which has no signing key: so we only set a flag, and the deployment
    job is signed and
    sent by the analyst service on its nearest step (≤ a minute)."""
    if fix.status != CodeFix.READY:
        raise ValueError('This fix is no longer waiting for a decision.')
    if not fix.approved:
        raise ValueError('The reviewer did not approve this fix — it cannot be deployed with the button.')
    fix.owner_approved, fix.wait_until = True, None
    fix.save(update_fields=['owner_approved', 'wait_until', 'updated_at'])


def reject(fix: CodeFix, now=None) -> None:
    now = now or timezone.now()
    if fix.status != CodeFix.READY:
        raise ValueError('This fix is no longer waiting for a decision.')
    _end(fix, CodeFix.REJECTED, 'Rejected by the owner.', now)
    _close_card(fix, now)


def _due(fix: CodeFix, now) -> bool:
    return fix.wait_until is None or now >= fix.wait_until


def _wants_host(fix: CodeFix) -> bool:
    if fix.status in (CodeFix.VALIDATING, CodeFix.DEPLOYING, CodeFix.REVERTING):
        return True
    return fix.status == CodeFix.READY and fix.approved and (fix.owner_approved or auto_deploys(fix))


def _advance(fix: CodeFix, now) -> str:
    s = fix.status
    if s == CodeFix.DRAFTING:
        if now - fix.updated_at > DRAFT_STALE:
            _fail(fix, 'The agent\'s work was interrupted by a service restart.', now)
        return 'drafting'
    if s == CodeFix.QUEUED:
        if _due(fix, now):
            draft(fix, now)
        return 'queued'
    if s == CodeFix.REVIEWING:
        if _due(fix, now):
            review(fix, now)
        return s
    if s == CodeFix.VALIDATING:
        _poll_validate(fix, now)
    elif s == CodeFix.DEPLOYING:
        _poll_deploy(fix, now)
    elif s == CodeFix.REVERTING:
        _poll_revert(fix, now)
    elif s == CodeFix.DEPLOYED:
        _watch(fix, now)
    elif s == CodeFix.READY and _due(fix, now) and fix.approved and (fix.owner_approved or auto_deploys(fix)):
        deploy(fix, now, by_owner=fix.owner_approved)
    return s


def tick(now=None) -> list[str]:
    """One pipeline step. Returns short lines for the command log."""
    now = now or timezone.now()
    if mode() == 'off':
        return []
    ready, why = ops_ready()
    lines = []
    for fix in CodeFix.objects.exclude(status__in=CodeFix.DONE).select_related('event').order_by('pk'):
        if not ready and _wants_host(fix):
            lines.append(f'#{fix.pk}: waiting for the helper ({why})')
            continue
        before = fix.status
        with exclusive(fix.pk) as got:
            if not got:
                lines.append(f'#{fix.pk}: currently being handled by another process')
                continue
            fix.refresh_from_db()                     # while waiting for the lock, the state may have changed
            try:
                _advance(fix, now)
            except Exception:
                log.exception('analyst codefix %s: pipeline step failed', fix.pk)
                lines.append(f'#{fix.pk}: step failed, see the log')
                continue
        fix.refresh_from_db()
        lines.append(f'#{fix.pk}: {before} → {fix.status}')
    if CodeFix.objects.filter(status__in=CodeFix.ACTIVE + (CodeFix.QUEUED, CodeFix.DEPLOYED)).exists():
        return lines            # one change at a time: this shows what exactly broke, and there is something to roll back
    if not ready:
        return lines + [why]
    with exclusive(0) as got:                        # we take a new run under the global lock: there won't be two fixes at once
        if not got:
            return lines
        event = pick_event(now)
        if event is None:
            return lines
        fix = start(event, now)
        draft(fix, now)
    fix.refresh_from_db()
    lines.append(f'#{fix.pk}: new error {event.exc_type.rsplit(".", 1)[-1]} in {event.where_path} → {fix.status}')
    return lines
