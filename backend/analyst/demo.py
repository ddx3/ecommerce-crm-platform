"""Demo stubs for the Analyst page (`/admin/analyst/demo/`): all kinds of table rows on made-up data.

Reads nothing from the shop DB and writes nothing: the page only shows what each variant looks like
(importance, new and processed, with and without a fix button, purchasing, system health, what the
Analyst did itself, long texts), and lets you press the buttons — server responses are substituted
in
the script itself (see inbox.html, DEMO).
"""
from __future__ import annotations

import json

from . import actions

HEALTH, BUY = 'System health', 'Purchasing'


def _fixer(key: str) -> dict:
    f = actions.FIXERS[key]
    params = [{'name': p.name, 'label': p.label, 'default': p.default, 'min': p.minimum, 'max': p.maximum}
              for p in f.params]
    return {'key': f.key, 'label': f.label, 'params': params, 'params_json': json.dumps(params), 'single': f.single}


# (section, title, detail, fix keys, new?)
# Fix buttons exist only in ANALYST_AUTOFIX=ask mode; in auto mode there are none, and the Analyst
# itself
# leaves the card 'The Analyst did it itself: …' (see below).
_ROWS = [
    # ── System health ──
    (HEALTH, 'Disk is 91% full',
     '3.4 GB free out of 37.2 GB (/app). When the disk is full, rebuilding containers fails with "no space left on device".',
     ['host_disk_cleanup'], True),
    (HEALTH, 'The last database backup was 8 days ago',
     'Newest file: db_before_release_20261004.dump. Check the pg_dump cron job on the server.',
     ['host_backup_now'], True),
    (HEALTH, 'The scheduler has been silent for 6 h',
     'No regular task (payment reconciliation, bank statements, screenshots) has run. In August it likewise stood idle for 4 days. '
     'Restart the scheduler container.', ['host_restart_scheduler'], True),
    (HEALTH, 'The daily auto-backup failed',
     'Not enough space for a backup: 120.0 MB free, at least 200.0 MB needed. Free up space first.', [], True),
    (HEALTH, 'The SSL certificate of example.com expires in 12 days',
     'The certificate is reissued manually (acme.sh, DNS validation) — there is no auto-renewal.', [], True),
    (HEALTH, 'Task match_bank_transactions is failing',
     'InterfaceError: connection already closed', ['host_restart_scheduler'], True),
    (HEALTH, 'The bundle instruction queue is stuck: 4 tasks for over 3 h',
     'Probably the Claude subscription limit is exhausted or resolve_pending_instructions is not running.', [], True),
    # ── What the Analyst did itself (auto mode) ──
    (HEALTH, 'The Analyst did it itself: Free up space on the server',
     'Was: Disk is 91% full. Result: Done: 14.2 GB free out of 37.2 GB (62% used).', [], True),
    # ── Code errors and auto-fix ──
    (HEALTH, 'Code error: TypeError in catalog/api_views.py (product_price)',
     "unsupported operand type(s) for +: 'NoneType' and 'int' · 7 times in 24 h, last at 20.10 02:41 · URL /api/products/price/.",
     [], True),
    (HEALTH, 'The error fix is ready: a price without a value no longer breaks the API response',
     'Error: TypeError in catalog/api_views.py. Sandbox check passed (120 tests, the new test failed first, then passed). '
     'The reviewer approved.\n\nTo deploy to the site, press "Apply the fix" in the card window.\n\nChanges:\n'
     '--- a/backend/catalog/api_views.py\n+++ b/backend/catalog/api_views.py\n@@ -41,3 +41,3 @@\n'
     '-    price = int(item.price) + 1' + '\n+    price = int(item.price or 0) + 1',
     ['codefix_apply', 'codefix_reject'], True),
    (HEALTH, 'The Analyst fixed the error: a price without a value no longer breaks the API response',
     'Error: TypeError in catalog/api_views.py. Deployed 20.10 03:12 (commit 3f9a1c2d). Sandbox check passed. '
     'I am watching for a day: if the error returns, the fix will be reverted by itself. For now it exists only on the server: move it into git '
     'before the next manual deploy.', [], True),
    (HEALTH, 'The Analyst rolled back the fix: search by name',
     "Error: KeyError in catalog/views.py. The fix was deployed, but the health check failed, and everything was restored as it was."
     '\nReason: /api/products/?page_size=1 responds with 500.', [], False),
    (HEALTH, 'The server has 1 auto-fixes that are not in git yet',
     'The Analyst deployed fixes straight to the server. Before the next manual deploy, move them into the repository '
     '(on the VPS: git log origin/<branch>..HEAD), otherwise an update from git will wipe them.', [], True),
    (HEALTH, 'The Analyst could not run: Back up the database now',
     'Finding: The last database backup was 8 days ago. pg_dump finished with an error: connection refused.', [], True),
    (HEALTH, 'The Analyst failed to cope: The scheduler has been silent for 6 h',
     'Ran "Restart the scheduler" (19.10 09:12), but the problem did not go away. '
     'Your decision is needed; I will not repeat it on my own.', [], True),
    # ── Purchasing: reports only, no buttons (✓ = 'ordered') ──
    (BUY, 'Order 100 pcs: Plant Booster, 40 g',
     '10 days left. It sells every day, and it is exactly the season now.\n'
     'In stock: 22 pcs · over 2 mo. we expect to sell ≈124 pcs · enough for ≈10 days · '
     'other pack sizes: "Plant Booster, 500 ml" — 30 pcs in stock, sold in 60 days: 9',
     [], True),
    (BUY, 'Order 12.5 kg: Universal lawn seed (bulk bag "Bag")',
     'The bag is almost empty, and 4 buyers are waiting. The season is still on.\n'
     'In stock: 1.5 kg · over 2 mo. we expect to sell ≈11 kg · enough for ≈6 days · '
     'waiting: 3 subscriptions, 1 like', [], True),
    (BUY, 'Order 3 pcs: Pruning shears for conifers and lavender with a very long name that will not fit in one line of the table',
     'Out of stock, and one buyer is waiting.\nOut of stock since 01.09 · waiting: 1 subscription, 0 likes', [], True),
    # ── Processed (grey, ↩) ──
    (HEALTH, 'Disk is 94% full',
     'This has already been handled: the row stays in the table in grey until the problem goes away by itself.', [], False),
    (BUY, 'Ordered 3 pcs: Fungicide Energy, 10 ml',
     'Marked ✓ 19.10.2026. The Analyst does not advise this while the goods are on the way. '
     'Once they arrive the row disappears on its own; if you did not order, press ↩.',
     [], False),
    (HEALTH, 'The Analyst did it itself: Back up the database now',
     'Was: The last database backup was 4 days ago. Result: Done: db_auto_20261018_1432.dump (18.0 MB), checked.',
     [], False),
]

# Examples of the confirmation dialog for the buttons: what the owner sees between 'Fix'
# and 'Apply'.
_PLANS = {
    'host_disk_cleanup': ('Free up space on the server',
                          ['Disk: 3.4 GB free out of 37.2 GB (91% used).',
                           'Docker build cache (unused, older than 24h): 9.851GB in total, it will drop to 3.792GB. '
                           'The next build will be a little longer.',
                           'The system journal will shrink to 200M. Archived and active journals take up 794.9M.',
                           'The failed-logins log btmp.1 (146.6 MB) will be cleared.',
                           'I am not touching the rollback images (they are needed for a quick release rollback).'],
                          ['This is an action on the server itself: there is no rollback in the log.']),
    'host_backup_now': ('Back up the database now',
                        ['I will make a database dump (pg_dump) into /opt/shop/backups/db_auto_<date>.dump and check that it is readable.',
                         'I delete auto-backups older than 14 days (keeping at least 3); manual dumps are not touched.'],
                        ['This is an action on the server itself: there is no rollback in the log.']),
    'codefix_apply': ('Deploy the fix: a price without a value no longer breaks the API response',
                      ['A price without a value no longer breaks the API response.',
                       'Sandbox check passed: the new test failed without the fix and passes with it.',
                       'The reviewer approved.',
                       "A thin layer will be built on top of the running image and the site restarted (up to a minute without the site), then up to 3 minutes of health checks.",
                       'If anything goes wrong — automatic rollback to the previous state.', 'Changes:',
                       '--- a/backend/catalog/api_views.py', '+++ b/backend/catalog/api_views.py', '@@ -41,3 +41,3 @@',
                       '-    price = int(item.price) + 1', '+    price = int(item.price or 0) + 1'],
                      ['After deployment the fix exists only on the server: move it into git before a manual deploy.']),
    'codefix_reject': ('Reject the fix: a price without a value no longer breaks the API response',
                       ['The fix will be rejected, nothing on the site will change.'], []),
    'host_restart_scheduler': ('Restart the scheduler',
                               ['I will restart the "scheduler" container (docker compose restart scheduler). The background tasks '
                                'of this service will stop for a minute, data is not affected.'],
                               ['This is an action on the server itself: there is no rollback in the log.']),
}

def rows(many: bool = False, buttons: bool = True) -> list[dict]:
    """`buttons=False` — what the table will look like in ANALYST_AUTOFIX=auto mode: no fix buttons."""
    out = []
    for n, (section, title, body, fix_keys, is_new) in enumerate(_ROWS * (3 if many else 1), start=1):
        out.append({
            'id': -n, 'is_new': is_new, 'section': section,
            'title': title, 'body': body, 'link': '#' if n % 3 == 0 else '',
            'fixers': [_fixer(k) for k in fix_keys if buttons or actions.FIXERS[k].always] if is_new else [],
            'first_seen': f'{19 - (n % 6):02d}.10.2026 {8 + n % 12:02d}:{(n * 7) % 60:02d}',
        })
    # As on the live page: newest first by default (date in dd.mm.yyyy hh:mm format).
    out.sort(key=lambda r: (r['first_seen'][6:10], r['first_seen'][3:5], r['first_seen'][:2], r['first_seen'][11:]),
             reverse=True)
    return out


def plans() -> dict:
    """Ready-made 'plans' for the fix buttons: {action key: server response from /fix/plan/}."""
    result = {}
    for n, (key, (title, changes, notes)) in enumerate(_PLANS.items(), start=1):
        result[key] = {'run_id': -n, 'title': title, 'notes': notes, 'changes': changes, 'total': len(changes)}
    return result
