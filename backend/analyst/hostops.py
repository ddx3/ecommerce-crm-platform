"""Channel to the 'helper' on the VPS (ops/shop_helper.py) via a shared queue folder.

web only drops a request file and waits for the reply; the helper on the host executes it, and only
actions from its allowlist. More on the protocol and protection — in the header of
ops/shop_helper.py.
"""
from __future__ import annotations

import json
import os
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from django.conf import settings

PLAN_WAIT = 90        # s: data collection by the helper (docker system df, etc.)
APPLY_WAIT = 240      # s: within the nginx timeout for /admin/ (300 s)
POLL = 0.4
HEARTBEAT_MAX_AGE = timedelta(seconds=90)


class HostError(Exception):
    """The text is shown to the owner."""


def ops_dir() -> Path | None:
    value = getattr(settings, 'ANALYST_OPS_DIR', '') or ''
    path = Path(value) if value else None
    return path if path is not None and path.is_dir() else None


def configured() -> bool:
    return ops_dir() is not None


def status() -> dict | None:
    root = ops_dir()
    if root is None:
        return None
    for name in ('results/status.json', 'status.json'):          # the second is the old location, only for the transition period
        try:
            return json.loads((root / name).read_text(encoding='utf-8'))
        except (OSError, ValueError):
            continue
    return None


def helper_alive(now: datetime | None = None) -> bool:
    st = status()
    if not st or not st.get('heartbeat'):
        return False
    try:
        beat = datetime.fromisoformat(st['heartbeat'])
    except ValueError:
        return False
    if beat.tzinfo is None:
        beat = beat.replace(tzinfo=timezone.utc)
    return (now or datetime.now(timezone.utc)) - beat <= HEARTBEAT_MAX_AGE


def _submit(payload: dict, *, signed: bool = False) -> str:
    root = ops_dir()
    if root is None:
        raise HostError('The channel to the server is not configured.')
    rid = uuid.uuid4().hex[:16]
    payload = {**payload, 'id': rid}
    if signed:
        from analyst.codefix import auth
        mac = auth.sign_request(rid, payload.get('action'), payload.get('params'))
        if mac is None:
            raise HostError('No signing key for auto-fixes (ANALYST_CODEFIX_KEY_FILE) — the task was not sent.')
        payload['auth'] = mac
    target = root / 'requests' / f'{rid}.json'
    tmp = target.with_name(f'{rid}.json.tmp')      # the helper reads only *.json, so it will not pick up a half-written file
    try:
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding='utf-8')
        os.replace(tmp, target)
    except OSError as exc:
        raise HostError(f'Could not hand the request to the helper ({exc.strerror or exc}). '
                        f'Is the queue folder mounted read-only?')
    return rid


def _wait(rid: str, timeout: float) -> dict:
    root = ops_dir()
    deadline = time.monotonic() + timeout
    path = root / 'results' / f'{rid}.json'
    while True:
        try:
            return json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            pass
        if time.monotonic() >= deadline:
            raise HostError('The helper on the server did not respond in time. Check the shop-helper service.')
        time.sleep(POLL)


def _ask(payload: dict, timeout: float) -> dict:
    if not helper_alive():
        raise HostError('The helper on the server is not responding (is the shop-helper service not running?).')
    result = _wait(_submit(payload), timeout)
    if result.get('status') == 'failed':
        raise HostError(result.get('error') or 'The helper refused without explanation.')
    return result


def plan(action: str, params: dict) -> dict:
    return _ask({'mode': 'plan', 'action': action, 'params': params}, PLAN_WAIT)


def apply(plan_id: str) -> dict:
    return _ask({'mode': 'apply', 'plan_id': plan_id}, APPLY_WAIT)


# ── long auto-fix jobs ───────────────────────────────────────────────────────
# Sandbox verification and rollout take minutes, so this is not plan/apply with waiting: a
# `mode=job` request returns
# immediately, and progress is read from results/<id>.json (status: running → done | failed).

def put_artifact(patch_id: str, text: str) -> None:
    root = ops_dir()
    if root is None:
        raise HostError('The channel to the server is not configured.')
    target = root / 'artifacts' / f'{patch_id}.patch'
    tmp = target.with_name(f'{patch_id}.patch.tmp')
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(text, encoding='utf-8', newline='\n')
        os.replace(tmp, target)
    except OSError as exc:
        raise HostError(f'Could not hand the patch to the helper ({exc.strerror or exc}).')


def submit_job(action: str, params: dict) -> str:
    if not helper_alive():
        raise HostError('The helper on the server is not responding (is the shop-helper service not running?).')
    return _submit({'mode': 'job', 'action': action, 'params': params}, signed=True)


def job_result(rid: str) -> dict | None:
    root = ops_dir()
    if root is None:
        return None
    try:
        return json.loads((root / 'results' / f'{rid}.json').read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None
