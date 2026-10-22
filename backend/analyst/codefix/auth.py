"""Signing of auto-fix jobs (HMAC-SHA256).

The key is a single file (`ANALYST_CODEFIX_KEY_FILE`, on the VPS /opt/shop-autofix/codefix.key,
created by ops/install.sh): it exists only for the helper on the host and in the analyst container
(docker-compose mounts it only there), NOT in web. Therefore:
  * the helper executes an auto-fix job only with a valid signature: a compromised web can neither
    submit a patch nor order a deployment;
  * the patch (together with the artifact name and the drill/simulate_failure flags) and the
    reviewer's verdict in the `CodeFix` table are 'sealed'
    (`patch_mac`, `review_mac`): web has access to the same DB but without the key cannot swap the
    patch or set the verdict to approved —
    before each dispatch the pipeline checks the seals, and puts the sha256 of the patch into the
    signed job, which the helper checks against the artifact.
    Only `owner_approved` is NOT sealed (the owner's button goes through web): its effect is limited
    — it allows deploying earlier than the
    night window a fix that the independent reviewer has already approved; it cannot override the
    reviewer's verdict.
The canonical form of the job signature MUST match `ops/codefix_host.py::sign` (there is a test).
"""
from __future__ import annotations

import hashlib
import hmac
import json
import time
from pathlib import Path

from django.conf import settings

SIGN_TTL = 1800


def key() -> bytes | None:
    path = getattr(settings, 'ANALYST_CODEFIX_KEY_FILE', '') or ''
    if not path:
        return None
    try:
        raw = Path(path).read_bytes().strip()
    except OSError:
        return None
    return raw if len(raw) >= 32 else None


def key_fingerprint() -> str:
    k = key()
    return hashlib.sha256(k).hexdigest()[:8] if k else ''


def canonical(rid: str, action: str, params, exp: int) -> bytes:
    return json.dumps({'id': rid, 'action': action, 'params': params, 'exp': exp}, sort_keys=True,
                      separators=(',', ':'), ensure_ascii=True).encode('ascii')


def sign_request(rid: str, action: str, params, *, now: float | None = None) -> dict | None:
    """{'exp', 'mac'} for a request, or None if there is no key."""
    k = key()
    if k is None:
        return None
    exp = int((now if now is not None else time.time())) + SIGN_TTL
    return {'exp': exp, 'mac': hmac.new(k, canonical(rid, action, params, exp), hashlib.sha256).hexdigest()}


def _seal(*parts: str) -> str:
    k = key()
    if k is None:
        return ''
    return hmac.new(k, '\x1f'.join(parts).encode('utf-8'), hashlib.sha256).hexdigest()


def patch_sha(patch: str) -> str:
    return hashlib.sha256(patch.encode('utf-8')).hexdigest()


def seal_patch(patch: str, patch_id: str = '', simulate_failure: bool = False, drill: bool = False) -> str:
    """Patch seal: the content, the artifact name in the queue and the drill flags. Everything web
    could swap in a `CodeFix` record (another
    artifact, 'deliberate failure', 'drill') goes into the seal, so tampering breaks it."""
    return _seal('patch', patch_sha(patch), patch_id, '1' if simulate_failure else '0', '1' if drill else '0')


def seal_review(patch: str, approved: bool) -> str:
    return _seal('review', patch_sha(patch), '1' if approved else '0')


def valid(stored: str, expected: str) -> bool:
    """The seal matches (and a key exists at all). An empty seal is always invalid."""
    return bool(stored) and bool(expected) and hmac.compare_digest(stored, expected)
