"""The 'Analyst' section in the CRM inbox: the list of findings, actions on them, and fixes
(plan → 'Apply')."""
from __future__ import annotations

import json

from django.contrib import admin
from django.http import JsonResponse
from django.shortcuts import render
from django.utils import timezone

from . import actions, advice, demo
from .access import owner_only
from .models import ActionRun, Finding

ROW_LIMIT = 300
PREVIEW_LINES = 40


def _fixer_info(finding):
    result = []
    for f in actions.fixers_for(finding.check_name):
        params = [{'name': p.name, 'label': p.label, 'default': p.default, 'min': p.minimum, 'max': p.maximum}
                  for p in f.params]
        result.append({'key': f.key, 'label': f.label, 'params': params, 'params_json': json.dumps(params), 'single': f.single})
    return result


def _plan_payload(run):
    texts = [c['text'] for c in run.changes]
    return {
        'run_id': run.pk, 'title': run.title, 'notes': run.notes,
        'changes': texts[:PREVIEW_LINES], 'total': len(texts),
    }


def _body(request) -> dict:
    try:
        data = json.loads(request.body or b'{}')
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


@owner_only
def inbox_view(request):
    """A single table by time: the newest findings on top. What is new or already handled is visible
    from the red marker and ✓ / ↩,
    not from the row order (this is the default order, not a filter: sorting by the 'Date'
    column overrides it)."""
    findings = list(Finding.objects.filter(resolved_at__isnull=True)
                    .order_by('-first_seen', '-severity', '-pk')[:ROW_LIMIT])

    rows, fix_all_count = [], 0
    for f in findings:
        is_new = f.status == Finding.NEW
        fixers = _fixer_info(f) if is_new else []
        if any(not x['params'] and not x['single'] for x in fixers):
            fix_all_count += 1
        rows.append({
            'id': f.pk,
            'is_new': is_new,
            'section': f.get_section_display(),
            'title': f.title,
            'body': f.body,
            'link': f.link,
            'fixers': fixers,
            'first_seen': timezone.localtime(f.first_seen).strftime('%d.%m.%Y %H:%M'),   # like the rest of the CRM tables
        })

    return render(request, 'admin/analyst/inbox.html', {
        **admin.site.each_context(request),
        'title': 'Analyst',
        'rows': rows,
        'fix_all_count': fix_all_count,
        'has_fixers': any(r['fixers'] for r in rows),
    })


@owner_only
def action_view(request):
    """POST {id | ids, action}: done | reopen. `ids` — a bulk action over several rows."""
    if request.method != 'POST':
        return JsonResponse({'error': 'POST only'}, status=405)
    try:
        data = json.loads(request.body)
        raw_ids = data['ids'] if 'ids' in data else [data['id']]
        ids = [int(x) for x in raw_ids]
        action = data['action']
    except (ValueError, KeyError, TypeError):
        return JsonResponse({'error': 'bad request'}, status=400)
    if action == 'done':
        changes = {'status': Finding.DONE, 'snoozed_until': None}
    elif action == 'reopen':
        changes = {'status': Finding.NEW, 'snoozed_until': None}
    else:
        return JsonResponse({'error': 'unknown action'}, status=400)
    rows = Finding.objects.filter(pk__in=ids, resolved_at__isnull=True)
    purchase_cards = list(rows.filter(check_name=advice.CHECK_ADVICE))
    ordered_rows = list(rows.filter(check_name=advice.CHECK_ORDERED))
    updated = rows.update(**changes)
    if not updated:
        return JsonResponse({'error': 'not found'}, status=404)
    # ✓ on a purchasing card = 'ordered': we record the purchase, ↩ cancels that record.
    if action == 'done':
        advice.mark_ordered(purchase_cards, request.user.email)
    else:
        advice.unmark_ordered(purchase_cards)
        if ordered_rows:       # ↩ on an 'Ordered …' row: it was not ordered — cancel and remove the row right away
            advice.unmark_ordered_rows(ordered_rows)
            Finding.objects.filter(pk__in=[f.pk for f in ordered_rows]).update(resolved_at=timezone.now())
    _broadcast()
    return JsonResponse({'ok': True, 'updated': updated})


def _broadcast():
    from catalog.notifications import broadcast_notification_counts
    broadcast_notification_counts()


@owner_only
def plan_view(request):
    """Step 1: build a fix plan. NOTHING is written to the shop data."""
    if request.method != 'POST':
        return JsonResponse({'error': 'POST only'}, status=405)
    data = _body(request)
    finding = Finding.objects.filter(pk=data.get('finding_id')).first() if str(data.get('finding_id')).isdigit() else None
    if finding is None:
        return JsonResponse({'error': 'The finding was not found.'}, status=404)
    try:
        run = actions.make_plan(finding, str(data.get('action', '')), data.get('params') or {},
                                request.user.email)
    except actions.PlanError as exc:
        return JsonResponse({'error': str(exc)}, status=422)
    return JsonResponse(_plan_payload(run))


@owner_only
def plan_all_view(request):
    """Step 1 for 'Fix everything possible': plans for all new findings whose
action asks for no parameters."""
    if request.method != 'POST':
        return JsonResponse({'error': 'POST only'}, status=405)
    runs, skipped = [], []
    for finding in Finding.objects.filter(resolved_at__isnull=True, status=Finding.NEW).order_by('-severity')[:100]:
        fixer = next((f for f in actions.fixers_for(finding.check_name) if not f.params and not f.single), None)
        if fixer is None:
            continue
        try:
            run = actions.make_plan(finding, fixer.key, {}, request.user.email)
        except actions.PlanError as exc:
            skipped.append({'title': finding.title, 'reason': str(exc)})
            continue
        runs.append(_plan_payload(run))
    return JsonResponse({'runs': runs, 'skipped': skipped})


@owner_only
def apply_view(request):
    """Step 2: apply the saved plans (only after the owner's 'Apply')."""
    if request.method != 'POST':
        return JsonResponse({'error': 'POST only'}, status=405)
    ids = [int(x) for x in (_body(request).get('run_ids') or []) if str(x).isdigit()]
    if not ids:
        return JsonResponse({'error': 'Nothing to apply.'}, status=400)
    results = []
    for run in ActionRun.objects.filter(pk__in=ids).select_related('finding').order_by('pk'):
        try:
            actions.apply_run(run)
            results.append({'run_id': run.pk, 'title': run.title, 'ok': True})
        except actions.ApplyError as exc:
            results.append({'run_id': run.pk, 'title': run.title, 'ok': False, 'error': str(exc)})
    _broadcast()
    return JsonResponse({'results': results})


@owner_only
def revert_view(request):
    if request.method != 'POST':
        return JsonResponse({'error': 'POST only'}, status=405)
    run = ActionRun.objects.filter(pk=_body(request).get('run_id')).select_related('finding').first() \
        if str(_body(request).get('run_id')).isdigit() else None
    if run is None:
        return JsonResponse({'error': 'The fix was not found.'}, status=404)
    try:
        actions.revert_run(run)
    except actions.ApplyError as exc:
        return JsonResponse({'error': str(exc)}, status=422)
    _broadcast()
    return JsonResponse({'ok': True})


@owner_only
def demo_view(request):
    """Demo page: all kinds of table rows on made-up data (`analyst/demo.py`). It reads nothing from
    and writes nothing to
    the shop DB; the button responses are substituted by the script itself. `?many=1` — 60+ rows, to
    see the pager;
    `?mode=auto` — the view without fix buttons (this is how it will look when the Analyst fixes
    things on its own)."""
    rows = demo.rows(many=bool(request.GET.get('many')), buttons=request.GET.get('mode') != 'auto')
    return render(request, 'admin/analyst/inbox.html', {
        **admin.site.each_context(request),
        'title': 'Analyst — demo',
        'demo': True,
        'rows': rows,
        'demo_plans': demo.plans(),
        'fix_all_count': sum(1 for r in rows if any(not f['params'] and not f['single'] for f in r['fixers'])),
        'has_fixers': any(r['fixers'] for r in rows),
    })
