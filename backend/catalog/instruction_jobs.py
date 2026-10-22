# -*- coding: utf-8 -*-
"""Queue of bundle instruction runs for when the AI limit is exhausted.

The "Run through AI" button does not leave the person with nothing: on hitting
the limit it puts
a snapshot of the form (name, text, composition) here, and the scheduler finishes the run once the
limit
recovers and ITSELF writes the result into the bundle. There is no need to press again.

What is deliberately absent here: nothing is overwritten blindly. The result goes into the bundle
only if the bundle is still as the person left it (see `_check`), and the replacement itself goes
through
the same dose check as a manual press.
"""
import logging
import re
import time
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from core.limits import LimitExceeded
from core.llm import SubscriptionLimit

from . import instruction_agent as ia
from . import instruction_llm as il
from .models import Bundle, InstructionJob

log = logging.getLogger(__name__)

LIMIT_ERRORS = (SubscriptionLimit, LimitExceeded)
MAX_ATTEMPTS = 3                       # ordinary failures (network, timeout); they do not eat the attempt limit
WAIT = timedelta(days=3)               # how long to wait for the manager to save the composition / the bundle itself
OK, STALE, WAIT_SAVE = 'ok', 'stale', 'wait'


def _plain(html: str) -> str:
    """Text without tags and extra spaces: Quill sometimes adds and sometimes removes markup, but the words
are the same."""
    return ' '.join(re.sub(r'<[^>]+>', ' ', html or '').split())


def _wanted(items) -> dict:
    out: dict = {}
    for it in items or []:
        try:
            vid, qty = int(it['variant_id']), max(1, int(it.get('qty') or 1))
        except (KeyError, TypeError, ValueError, AttributeError):
            continue
        out[vid] = out.get(vid, 0) + qty
    return out


def _saved(bundle) -> dict:
    out: dict = {}
    for vid, qty in bundle.items.values_list('product_variant_id', 'quantity'):
        out[vid] = out.get(vid, 0) + qty
    return out


def _check(job, bundle):
    """(OK | STALE | WAIT_SAVE, reason). Whether the result may be written to this bundle."""
    changed_after = bundle.instruction_updated_at and bundle.instruction_updated_at > job.created_at
    if changed_after and _plain(bundle.instruction_html) != _plain(job.text):
        return STALE, 'the instruction text was changed after it was queued'
    if _saved(bundle) != _wanted(job.items):
        return WAIT_SAVE, 'the bundle composition in the form has not been saved'
    return OK, ''


def _finish(job, status, note=''):
    job.status = status
    job.note = (note or '')[:500]
    job.finished_at = timezone.now()
    job.seen = False
    job.save(update_fields=['status', 'note', 'finished_at', 'seen', 'attempts'])


def enqueue(*, text, name, items, bundle_id, user) -> InstructionJob:
    """Put a run in the queue. For an already saved bundle there is only one: a repeated press
    just refreshes the snapshot (and the time from which "the text was changed later" is
    counted)."""
    bundle = None
    try:
        bundle = Bundle.objects.filter(pk=int(bundle_id)).first() if bundle_id else None
    except (TypeError, ValueError):
        pass
    fields = dict(name=(name or '')[:255], text=text, items=list(items or []), attempts=0, note='',
                  seen=False, created_at=timezone.now(), created_by=user if getattr(user, 'pk', None) else None)
    if bundle:
        job = InstructionJob.objects.filter(bundle=bundle, status=InstructionJob.PENDING).first()
        if job:
            for k, v in fields.items():
                setattr(job, k, v)
            job.save()
            return job
    return InstructionJob.objects.create(bundle=bundle, **fields)


def queued_message(job) -> str:
    head = 'The AI limit is exhausted — the instruction has been queued. It will be processed automatically once the limit recovers'
    if job.bundle_id:
        return f'{head}; the result will be written to the bundle. No need to press again.'
    return f'{head}. Save the bundle — otherwise the queue has nowhere to write the result.'


def link(job_id, bundle, user) -> None:
    """Attach a queue entry set from the ADD form to the just-saved bundle."""
    try:
        pk = int(job_id)
    except (TypeError, ValueError):
        return
    jobs = InstructionJob.objects.filter(pk=pk, bundle__isnull=True, status=InstructionJob.PENDING)
    if getattr(user, 'pk', None):
        jobs = jobs.filter(created_by=user)
    if jobs.update(bundle=bundle):
        # Earlier, another entry for this same bundle might have been queued — the freshest one
        # stays.
        InstructionJob.objects.filter(bundle=bundle, status=InstructionJob.PENDING).exclude(pk=pk).delete()


def cancel_pending(bundle_id) -> None:
    """The person managed to run it manually (the limit recovered) — the old queue entry is now redundant."""
    try:
        pk = int(bundle_id) if bundle_id else None
    except (TypeError, ValueError):
        return
    if pk:
        InstructionJob.objects.filter(bundle_id=pk, status=InstructionJob.PENDING).update(
            status=InstructionJob.STALE, note='run manually', finished_at=timezone.now(), seen=True)


_UI = {
    InstructionJob.PENDING: ('wait', 'The instruction is in the queue: the AI limit is exhausted. It will be processed automatically once '
                                      'the limit recovers (checked every 10 min).'),
    InstructionJob.DONE: ('ok', 'The instruction was processed by the queue — the result is already in the text.'),
    InstructionJob.STALE: ('warn', 'The queue result was not applied: {note}. Press the wand again.'),
    InstructionJob.FAILED: ('warn', 'The queue could not process it: {note}. Press the wand again.'),
}


def ui_state(bundle_id):
    """What to show on the bundle form: {'tone', 'text'} or None. The outcome (done/failed)
    is shown once; "in the queue" — for as long as it sits there."""
    job = InstructionJob.objects.filter(bundle_id=bundle_id).order_by('-created_at').first()
    if not job:
        return None
    if job.status != InstructionJob.PENDING:
        if job.seen or (job.finished_at and job.finished_at < timezone.now() - timedelta(days=7)):
            return None
        InstructionJob.objects.filter(pk=job.pk).update(seen=True)
    tone, text = _UI[job.status]
    return {'tone': tone, 'text': text.format(note=job.note or 'no reason given')}


def process(limit: int = 3, pace: float = 0, sleep=time.sleep) -> dict:
    """One pass over the queue. Returns counters; the AI limit stops the pass without losses."""
    from .views import _components_from_form
    stats = dict(done=0, stale=0, failed=0, waiting=0, stopped=False)
    now = timezone.now()
    InstructionJob.objects.filter(status=InstructionJob.PENDING, bundle__isnull=True,
                                  created_at__lt=now - WAIT).update(
        status=InstructionJob.STALE, note='the bundle was never saved', finished_at=now)

    runs = 0
    pending = (InstructionJob.objects.filter(status=InstructionJob.PENDING, bundle__isnull=False)
               .select_related('bundle').order_by('created_at')[:50])
    for job in pending:
        verdict, why = _check(job, job.bundle)
        if verdict == STALE:
            _finish(job, InstructionJob.STALE, why)
            stats['stale'] += 1
            continue
        if verdict == WAIT_SAVE:
            if now - job.created_at > WAIT:
                _finish(job, InstructionJob.STALE, f'{why} within {WAIT.days} days')
                stats['stale'] += 1
            else:
                stats['waiting'] += 1
            continue
        if runs >= limit:
            break
        comps = _components_from_form(job.items)
        if not comps:
            _finish(job, InstructionJob.STALE, 'no existing product is left in the composition')
            stats['stale'] += 1
            continue
        if runs:
            sleep(pace)
        runs += 1
        carrier = Bundle(name=job.name or job.bundle.name, instruction_html=job.text)
        try:
            res = ia.normalize_by_mode(carrier, comps, getattr(settings, 'INSTRUCTION_MODE', 'agent'))
        except LIMIT_ERRORS as e:
            log.warning('instruction queue: AI limit, stopping the pass: %s', e)
            stats['stopped'] = True
            break
        except il.Rejected as e:
            _finish(job, InstructionJob.FAILED, str(e))
            stats['failed'] += 1
            continue
        except Exception as e:                      # noqa: BLE001 — network/CLI failure: try again next time
            job.attempts += 1
            log.warning('instruction queue: job=%s attempt %s: %s', job.pk, job.attempts, e)
            if job.attempts >= MAX_ATTEMPTS:
                _finish(job, InstructionJob.FAILED, f'{MAX_ATTEMPTS} failed attempts: {e}')
                stats['failed'] += 1
            else:
                job.save(update_fields=['attempts'])
            continue
        outcome = _apply(job, res, comps)
        stats['done' if outcome == InstructionJob.DONE else 'stale'] += 1
    return stats


def _apply(job, res, comps) -> str:
    """Write the result into the bundle, re-checking it under a lock: while the processing
    was going on (tens of seconds), the person could have saved the bundle."""
    with transaction.atomic():
        bundle = Bundle.objects.select_for_update().get(pk=job.bundle_id)
        verdict, why = _check(job, bundle)
        if verdict != OK:
            _finish(job, InstructionJob.STALE, why)
            return InstructionJob.STALE
        bundle.instruction_html = res['html']
        bundle.instruction_map = il.build_map(res['html'], res['mentions'], comps)
        bundle.save(update_fields=['instruction_html', 'instruction_map',
                                   'instruction_stamp', 'instruction_updated_at'])
    if res.get('warnings'):
        log.info('instruction queue "%s" warnings: %s', bundle.name, res['warnings'])
    _finish(job, InstructionJob.DONE)
    return InstructionJob.DONE
