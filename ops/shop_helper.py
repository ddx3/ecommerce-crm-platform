#!/usr/bin/env python3
"""shop-helper: a narrow 'helper' on the VPS for the Analyst's buttons (free up space, backup,
scheduler restart). Runs on the HOST as a systemd service, on Python stdlib.

Why a separate service rather than access from the container: the web container does not have (and
must not have)
access to Docker/the host - that would be root on the whole server for anyone who compromises the
site.
So web only PUTS A REQUEST file into a shared folder, and the helper executes it - and only what
the whitelist below allows.

Protocol (QUEUE folder, on the host /opt/shop/ops-queue; ONLY subdirectories are mounted in the
containers, so that a container cannot
replace the directory itself with a symlink: requests/ - web and analyst (write), artifacts/ -
analyst (write), results/ - both
read-only):
  requests/<id>.json   request from web: {id, mode: plan|apply|job, action, params | plan_id}; for
  mode=job also
                       {auth: {exp, mac}} - an HMAC signature with a key that exists only in the helper and in analyst
  artifacts/<id>.patch auto-fix patches (put there by analyst; the helper itself checks them against
  the policy)
  results/<id>.json    the helper's response (written only by it)
  results/status.json  heartbeat and auto-backup state (read by the Analyst)
Everything in requests/ and artifacts/ is hostile data: it is read only via ops/safeio.py (no
symlinks or FIFOs).

  1. mode=plan  — the helper changes NOTHING, it only describes what exactly it will do, and
     responds with a
                  list of lines. A plan lives 30 min.
  2. mode=apply — web passes only plan_id. THE SAME plan is executed (the action and parameters come
     from the plan
                  result, not from the request), once.

Protection: the action key is from a whitelist; parameters strictly follow a schema; no strings into
shell
(argument lists only); apply without a fresh plan is rejected; a limit of applications per
hour and a pause between identical actions.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path

def _load_sibling(name: str, *extra_dirs):
    """Loads a module that lies alongside (on the VPS - /opt/shop-helper, in the repository - ops/ and
    backend/analyst/).
    Copies of the policy and the auto-fix code are installed by install.sh outside git, so the agent
    cannot reach them."""
    here = Path(__file__).resolve().parent
    for directory in (here, *extra_dirs):
        path = Path(directory) / f'{name}.py'
        if path.is_file():
            spec = importlib.util.spec_from_file_location(name, path)
            module = importlib.util.module_from_spec(spec)
            sys.modules[name] = module          # a dataclass in the module looks itself up in sys.modules
            spec.loader.exec_module(module)
            return module
    return None


safeio = _load_sibling('safeio')
if safeio is None:
    raise ImportError('safeio.py must lie next to shop_helper.py (ops/install.sh puts it there)')

ID_RE = re.compile(r'^[a-f0-9]{8,32}$')
# shop-web:pre-release-20261004 — images that the release playbook leaves behind for rollback.
ROLLBACK_RE = re.compile(r'^(shop-[a-z]+):(pre-[a-z0-9-]+-(\d{8,14}))$')
SERVICES = ('scheduler', 'advisor', 'analyst')

MAX_REQUEST_BYTES = 64 * 1024
MAX_JSON_BYTES = 2 * 1024 * 1024
PLAN_TTL = timedelta(minutes=30)
MAX_APPLIES_PER_HOUR = 6
COOLDOWN = timedelta(minutes=2)
POLL_SECONDS = 2
KEEP_RESULTS = timedelta(days=3)

JOURNAL_LIMIT = '200M'
BUILD_CACHE_MAX_AGE = '24h'

BACKUP_PREFIX = 'db_auto_'
BACKUP_KEEP_DAYS = 14
BACKUP_KEEP_MIN = 3
MIN_DUMP_BYTES = 50_000
MIN_FREE_FACTOR = 2           # free space must be ≥ 2 × the last dump,
MIN_FREE_BYTES = 200 * 1024 * 1024   # but not less than this (so an empty history does not zero the threshold)
AUTO_BACKUP_HOUR = 3          # host local time
AUTO_BACKUP_EVERY = timedelta(hours=20)
AUTO_BACKUP_RETRY = timedelta(hours=1)
AHEAD_REFRESH = timedelta(hours=1)
JOB_ACTIONS = ('codefix_validate', 'codefix_deploy', 'codefix_revert')

# action → {param: (min, max) | tuple-enum}. Anything not listed here is rejected.
SCHEMAS = {
    'disk_cleanup': {'rollback_images': (0, 1), 'keep_rollback': (1, 5)},
    'backup_now': {},
    'restart_service': {'service': SERVICES},
}
DEFAULTS = {'disk_cleanup': {'rollback_images': 0, 'keep_rollback': 2}}


class HelperError(Exception):
    """The text is shown to the owner in the 'Analyst'."""


class Result:
    def __init__(self, returncode, out='', err='', *, timed_out=False, over_limit=False):
        self.returncode, self.out, self.err = returncode, out, err
        self.timed_out, self.over_limit = timed_out, over_limit


TAIL_BYTES = 512 * 1024


class Runner:
    """The only place where external commands are run (argument list, no shell).
    Replaced in tests."""

    def run(self, args, *, timeout=120, cwd=None, stdin=None, stdout=None, max_output=None) -> Result:
        if max_output:
            return self._run_bounded(list(args), timeout, cwd, max_output)
        try:
            proc = subprocess.run(
                list(args), cwd=cwd, stdin=stdin, timeout=timeout,
                stdout=stdout if stdout is not None else subprocess.PIPE,
                stderr=subprocess.PIPE)
        except subprocess.TimeoutExpired:
            return Result(124, '', f'timeout {timeout} s', timed_out=True)
        except OSError as exc:
            return Result(127, '', str(exc))
        out = proc.stdout.decode('utf-8', 'replace') if proc.stdout else ''
        return Result(proc.returncode, out, proc.stderr.decode('utf-8', 'replace'))

    @staticmethod
    def _run_bounded(args, timeout, cwd, max_output) -> Result:
        """Output is read as a stream: only the tail (TAIL_BYTES) of each stream stays in memory, and
        when the total exceeds
        `max_output` the process is killed. A hostile test cannot fill the host's memory and disk
        with endless output."""
        try:
            proc = subprocess.Popen(args, cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        except OSError as exc:
            return Result(127, '', str(exc))
        total, over, tails = [0], [False], {}
        guard = threading.Lock()

        def pump(stream, key):
            keep = bytearray()
            while True:
                chunk = stream.read1(65536) if hasattr(stream, 'read1') else stream.read(65536)
                if not chunk:
                    break
                with guard:
                    total[0] += len(chunk)
                    if total[0] > max_output and not over[0]:
                        over[0] = True
                        try:
                            proc.kill()
                        except OSError:
                            pass
                keep += chunk
                if len(keep) > TAIL_BYTES:
                    del keep[:len(keep) - TAIL_BYTES]
            tails[key] = bytes(keep)

        threads = [threading.Thread(target=pump, args=(proc.stdout, 'out'), daemon=True),
                   threading.Thread(target=pump, args=(proc.stderr, 'err'), daemon=True)]
        for thread in threads:
            thread.start()
        timed_out = False
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            proc.kill()
            proc.wait()
        for thread in threads:
            thread.join(timeout=15)
        return Result(proc.returncode, tails.get('out', b'').decode('utf-8', 'replace'),
                      tails.get('err', b'').decode('utf-8', 'replace'), timed_out=timed_out, over_limit=over[0])


def fmt_bytes(n: float) -> str:
    n = float(n)
    for unit in ('B', 'KB', 'MB', 'GB'):
        if abs(n) < 1024 or unit == 'GB':
            return f'{n:.0f} {unit}' if unit == 'B' else f'{n:.1f} {unit}'
        n /= 1024
    return f'{n:.1f} GB'


def validate_params(action: str, raw) -> dict:
    schema = SCHEMAS[action]
    raw = raw if isinstance(raw, dict) else {}
    unknown = set(raw) - set(schema)
    if unknown:
        raise HelperError(f'Unknown parameters: {", ".join(sorted(unknown))}')
    params = dict(DEFAULTS.get(action, {}))
    for name, rule in schema.items():
        if name not in raw and name in params:
            continue
        if name not in raw:
            raise HelperError(f'The parameter "{name}" is not specified')
        value = raw[name]
        if isinstance(rule[0], str):               # enum
            if value not in rule:
                raise HelperError(f'Parameter "{name}": allowed values are {", ".join(rule)}')
            params[name] = value
        else:
            if isinstance(value, bool) or not isinstance(value, int) or not rule[0] <= value <= rule[1]:
                raise HelperError(f'Parameter "{name}" must be an integer from {rule[0]} to {rule[1]}')
            params[name] = value
    return params


class Helper:
    def __init__(self, queue, project, backup_dir, *, runner=None, clock=None, disk=None,
                 log_dir='/var/log', local_hour=None, autofix_dir=None, site_url=None, sync_jobs=False):
        self.queue, self.project, self.backup_dir = Path(queue), Path(project), Path(backup_dir)
        self._local_hour = local_hour or (lambda dt: dt.astimezone().hour)
        self.compose = ['docker', 'compose', '-f', str(self.project / 'docker-compose.yml')]
        self.runner = runner or Runner()
        self._clock = clock
        self._disk = disk or shutil.disk_usage
        self.log_dir = Path(log_dir)
        self.applies: deque = deque()
        self.last_apply: dict = {}
        self.last_auto_attempt = None
        self.last_auto_backup: dict = {}
        self._last_prune = None
        self._backup_lock = threading.Lock()
        self.io = safeio
        for sub in ('requests', 'results', 'done', 'artifacts'):
            path = self.queue / sub
            if os.path.islink(path) or (path.exists() and not path.is_dir()):
                raise RuntimeError(f'{path} must be a regular directory, not a link or a file')
            path.mkdir(parents=True, exist_ok=True)
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        self._last_ahead = None
        self.codefix = None
        policy = _load_sibling('codefix_policy', Path(__file__).resolve().parent.parent / 'backend' / 'analyst')
        host = _load_sibling('codefix_host')
        autofix_dir = autofix_dir or os.environ.get('SHOP_AUTOFIX_DIR')       # without the directory, auto-fix is disabled
        if policy is not None and host is not None and autofix_dir:
            raw_key = safeio.read_regular(Path(autofix_dir) / 'codefix.key', 512)
            self.codefix = host.Codefix(
                self, policy, HelperError, autofix_dir=autofix_dir, key=(raw_key or b'').strip() or None,
                site_url=site_url or os.environ.get('SHOP_SITE_URL', 'https://magiyaroslyn.com'), sync=sync_jobs)

    # ── utility ─────────────────────────────────────────────────────────────
    def now(self) -> datetime:
        return self._clock() if self._clock else datetime.now(timezone.utc)

    def _write_json(self, path: Path, data: dict) -> None:
        safeio.write_atomic(path, json.dumps(data, ensure_ascii=False).encode('utf-8'))

    def _read_json(self, path: Path):
        raw = safeio.read_regular(path, MAX_JSON_BYTES)
        if raw is None:
            return None
        try:
            return json.loads(raw.decode('utf-8'))
        except ValueError:
            return None

    def docker(self, *args, timeout=120, **kw) -> Result:
        return self.runner.run(['docker', *args], timeout=timeout, **kw)

    # ── queue ───────────────────────────────────────────────────────────────
    def process_queue(self) -> int:
        requests = self.queue / 'requests'
        if not safeio.is_real_dir(requests):
            return 0
        regular, junk = safeio.scan(requests, '.json', limit=200)
        for name in junk:                         # symlinks, FIFOs, sockets: delete without opening
            safeio.unlink_quiet(requests / name)
        for name in regular:
            self._handle(name)
        return len(regular)

    def _handle(self, name: str) -> None:
        path = self.queue / 'requests' / name
        rid = name[:-len('.json')]
        if not ID_RE.match(rid):
            safeio.unlink_quiet(path)
            return
        result_path = self.queue / 'results' / f'{rid}.json'
        if os.path.lexists(result_path):         # this request was already processed: ignore the repeat (replay)
            safeio.unlink_quiet(path)
            return
        raw = safeio.read_regular(path, MAX_REQUEST_BYTES)
        req = None
        try:
            if raw is None:
                raise HelperError('The request is not a regular file of an allowed size.')
            try:
                req = json.loads(raw.decode('utf-8'))
            except ValueError:
                raise HelperError('The request is not valid JSON.')
            if not isinstance(req, dict):
                raise HelperError('The request is not a valid JSON object.')
            result = self._dispatch(rid, req)
        except HelperError as exc:
            result = self._failure(rid, req, str(exc), getattr(exc, 'code', 'error'))
        except Exception as exc:    # the helper must not crash because of one bad request
            print(f'helper: request error {rid}: {exc!r}', file=sys.stderr, flush=True)
            result = self._failure(rid, req, 'Internal helper error, details are in the service log.')
        self._write_json(result_path, result)
        try:
            os.replace(path, self.queue / 'done' / name)
        except OSError:
            safeio.unlink_quiet(path)
        if self.codefix is not None:
            self.codefix.launch()                # starts the task only AFTER the initial response is written

    def _failure(self, rid, req, message, code='error') -> dict:
        mode = req.get('mode') if isinstance(req, dict) else None
        return {'id': rid, 'mode': mode, 'status': 'failed', 'lines': [], 'error': message, 'code': code,
                'created': self.now().isoformat()}

    def _dispatch(self, rid: str, req: dict) -> dict:
        if req.get('id') != rid:
            raise HelperError('The request id does not match the file name.')
        mode, action = req.get('mode'), req.get('action')
        if mode == 'plan':
            if action not in SCHEMAS:
                raise HelperError('Unknown action.')
            return self._plan(rid, action, validate_params(action, req.get('params')))
        if mode == 'apply':
            return self._apply(rid, req.get('plan_id'))
        if mode == 'job':
            if self.codefix is None:
                raise HelperError('Auto-fixes are not installed on this server.')
            if action not in JOB_ACTIONS:
                raise HelperError('Unknown action.')
            return self.codefix.submit(rid, action, req.get('params'), req.get('auth'))
        raise HelperError('Unknown request mode.')

    def _plan(self, rid, action, params) -> dict:
        lines, data = getattr(self, f'plan_{action}')(params)
        now = self.now()
        return {'id': rid, 'mode': 'plan', 'action': action, 'params': params, 'status': 'planned',
                'lines': lines, 'data': data, 'created': now.isoformat(),
                'expires': (now + PLAN_TTL).isoformat()}

    def _apply(self, rid, plan_id) -> dict:
        if not isinstance(plan_id, str) or not ID_RE.match(plan_id):
            raise HelperError('No plan to apply was specified.')
        plan_path = self.queue / 'results' / f'{plan_id}.json'
        plan = self._read_json(plan_path)
        if not plan or plan.get('mode') != 'plan' or plan.get('status') != 'planned':
            raise HelperError('The plan was not found or has already been applied — build it again.')
        if self.now() > datetime.fromisoformat(plan['expires']):
            raise HelperError('The plan is out of date — build it again.')
        action = plan['action']
        if self.codefix is not None and self.codefix.busy_with_deploy:
            raise HelperError('An auto-fix deployment or rollback is in progress right now — wait for it to finish (a few minutes).')
        self._check_rate(action)
        plan['status'] = 'consumed'          # one plan — one application, even if it fails
        self._write_json(plan_path, plan)
        lines, extra = getattr(self, f'apply_{action}')(plan['params'], plan.get('data') or {})
        self._register_apply(action)
        result = {'id': rid, 'mode': 'apply', 'action': action, 'status': 'done', 'lines': lines,
                  'created': self.now().isoformat()}
        result.update(extra)
        return result

    def _check_rate(self, action) -> None:
        now = self.now()
        while self.applies and now - self.applies[0] > timedelta(hours=1):
            self.applies.popleft()
        if len(self.applies) >= MAX_APPLIES_PER_HOUR:
            raise HelperError(f'Too many applications per hour (limit {MAX_APPLIES_PER_HOUR}).')
        last = self.last_apply.get(action)
        if last and now - last < COOLDOWN:
            raise HelperError('The same action has just been performed — wait a couple of minutes.')

    def _register_apply(self, action) -> None:
        now = self.now()
        self.applies.append(now)
        self.last_apply[action] = now

    # ── action: free up space ──────────────────────────────────────────────
    def _free_bytes(self) -> int:
        return self._disk('/').free

    def used_image_ids(self) -> set:
        ids = self.docker('ps', '-a', '-q').out.split()
        if not ids:
            return set()
        return set(self.docker('inspect', '--format', '{{.Image}}', *ids).out.split())

    def rollback_candidates(self, keep: int) -> list[dict]:
        """Rollback images of older releases. The newest `keep` releases and anything used by
        at least one container are kept."""
        listing = self.docker('images', '--no-trunc', '--format',
                              '{{.Repository}}:{{.Tag}}\t{{.ID}}\t{{.Size}}\t{{.CreatedAt}}')
        used = self.used_image_ids()
        rows = []
        for line in listing.out.splitlines():
            parts = line.split('\t')
            if len(parts) != 4:
                continue
            tag, image_id, size, created = parts
            m = ROLLBACK_RE.match(tag)
            if m:
                rows.append({'tag': tag, 'id': image_id, 'size': size, 'release': m.group(2),
                             'date': m.group(3), 'created': created})
        newest = {}
        for r in rows:
            newest[r['release']] = max(newest.get(r['release'], ''), r['created'])
        order = sorted(newest, key=lambda rel: (rel.rsplit('-', 1)[-1], newest[rel]), reverse=True)
        keep_releases = set(order[:keep])
        return [r for r in rows if r['release'] not in keep_releases and r['id'] not in used]

    def plan_disk_cleanup(self, params):
        total, used, free = self._disk('/')
        # The same percentage that df shows: the root reserve is not counted in the denominator.
        lines = [f'Disk: {fmt_bytes(free)} free out of {fmt_bytes(total)} ({used * 100 // max(used + free, 1)}% used).']
        for entry in self.docker('system', 'df', '--format', '{{json .}}').out.splitlines():
            try:
                d = json.loads(entry)
            except ValueError:
                continue
            if d.get('Type') == 'Build Cache':
                lines.append(f'Docker build cache (unused, older than {BUILD_CACHE_MAX_AGE}): '
                             f'{d.get("Size")} in total, it will drop to {d.get("Reclaimable")}. '
                             f'The next build will be a little longer.')
        journal = self.runner.run(['journalctl', '--disk-usage']).out.strip()
        lines.append(f'The system journal will shrink to {JOURNAL_LIMIT}. {journal}'.strip())
        for name in ('btmp', 'btmp.1'):
            p = self.log_dir / name
            if p.is_file() and not p.is_symlink() and p.stat().st_size > 1_000_000:
                lines.append(f'The failed-logins log {name} ({fmt_bytes(p.stat().st_size)}) will be cleared.')
        dangling = self.docker('images', '-f', 'dangling=true', '-q').out.split()
        if dangling:
            lines.append(f'Dangling Docker images: {len(dangling)} (unnamed, used by nobody).')
        tags = []
        if params['rollback_images']:
            candidates = self.rollback_candidates(params['keep_rollback'])
            tags = [c['tag'] for c in candidates]
            if candidates:
                lines.append(f'Rollback images of older releases (keeping the {params["keep_rollback"]} newest): '
                             + ', '.join(f'{c["tag"]} ({c["size"]})' for c in candidates)
                             + '. The layers are shared between images, so less will actually be freed than the sum.')
                lines.append('After this it will be impossible to roll back to these releases by image.')
            else:
                lines.append('There are no rollback images to delete.')
        else:
            lines.append('I am not touching the rollback images (they are needed for a quick release rollback).')
        return lines, {'rollback_tags': tags}

    def apply_disk_cleanup(self, params, data):
        before = self._free_bytes()
        lines, failures, steps = [], 0, 0

        def step(title, result: Result, tail=True):
            nonlocal failures, steps
            steps += 1
            if result.returncode == 0:
                last = (result.out.strip().splitlines() or [''])[-1] if tail else ''
                lines.append(f'Done: {title}' + (f' — {last}' if last else ''))
            else:
                failures += 1
                lines.append(f'Failed: {title} — {(result.err or result.out).strip()[:200]}')

        step('Docker build cache',
             self.docker('builder', 'prune', '-af', '--filter', f'until={BUILD_CACHE_MAX_AGE}', timeout=900))
        step('system journal',
             self.runner.run(['journalctl', f'--vacuum-size={JOURNAL_LIMIT}'], timeout=120))
        for name in ('btmp', 'btmp.1'):
            p = self.log_dir / name
            if p.is_file() and not p.is_symlink():
                steps += 1
                try:
                    p.write_text('')
                    lines.append(f'Done: cleared {name}')
                except OSError as exc:
                    failures += 1
                    lines.append(f'Failed: {name} — {exc}')
        step('dangling images', self.docker('image', 'prune', '-f'))
        used = self.used_image_ids() if data.get('rollback_tags') else set()
        for tag in data.get('rollback_tags') or []:
            if not ROLLBACK_RE.match(tag):
                continue
            inspected = self.docker('inspect', '--format', '{{.Id}}', tag)
            if inspected.returncode == 0 and inspected.out.strip() in used:
                lines.append(f'Skipped {tag}: the image is already used by a container.')
                continue
            step(f'rollback image {tag}', self.docker('rmi', tag), tail=False)
        after = self._free_bytes()
        lines.append(f'Freed {fmt_bytes(max(after - before, 0))}; now {fmt_bytes(after)} free.')
        if steps and failures == steps:
            raise HelperError('No cleanup step succeeded: ' + ' '.join(lines))
        return lines, {'freed_bytes': max(after - before, 0)}

    # ── action: backup ─────────────────────────────────────────────────────
    def backup_files(self) -> list[Path]:
        files = [p for p in self.backup_dir.glob(f'{BACKUP_PREFIX}*.dump') if p.is_file()]
        return sorted(files, key=lambda p: p.stat().st_mtime, reverse=True)

    def plan_backup_now(self, params):
        files = self.backup_files()
        lines = [f'I will make a database dump (pg_dump) into {self.backup_dir}/{BACKUP_PREFIX}<date>.dump and check that it is readable.',
                 f'I delete auto-backups older than {BACKUP_KEEP_DAYS} days (keeping at least {BACKUP_KEEP_MIN}); manual dumps are not touched.']
        lines.append(f'Auto-backups right now: {len(files)}.')
        return lines, {}

    def apply_backup_now(self, params, data):
        path = self._make_backup()
        removed = self._prune_backups()
        lines = [f'Done: {path.name} ({fmt_bytes(path.stat().st_size)}), checked.']
        if removed:
            lines.append(f'Old auto-backups deleted: {removed}.')
        return lines, {'file': path.name}

    def _make_backup(self, wait: bool = True) -> Path:
        """Database dump. One dump at a time (an auto-fix rollout and the daily auto-backup must not
        write to the same file):
        with wait=False a busy lock means HelperError('busy') without waiting."""
        if not self._backup_lock.acquire(blocking=wait):
            raise HelperError('busy')
        try:
            newest = self.backup_files()
            need = max((newest[0].stat().st_size if newest else 0) * MIN_FREE_FACTOR, MIN_FREE_BYTES)
            if self._free_bytes() < need:
                raise HelperError(f'Not enough space for a backup: {fmt_bytes(self._free_bytes())} free, '
                                  f'at least {fmt_bytes(need)} needed. Free up space first.')
            stamp = self.now().astimezone().strftime('%Y%m%d_%H%M')
            tmp = self.backup_dir / f'.{BACKUP_PREFIX}{stamp}.part'
            final = self.backup_dir / f'{BACKUP_PREFIX}{stamp}.dump'
            try:
                with open(tmp, 'wb') as out:
                    dump = self.runner.run(
                        [*self.compose, 'exec', '-T', 'db', 'sh', '-c',
                         'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -F c'],
                        cwd=str(self.project), stdout=out, timeout=1800)
                if dump.returncode != 0:
                    raise HelperError(f'pg_dump finished with an error: {dump.err.strip()[:200]}')
                if tmp.stat().st_size < MIN_DUMP_BYTES:
                    raise HelperError('The dump is suspiciously small — not saving it.')
                with open(tmp, 'rb') as src:
                    listing = self.runner.run([*self.compose, 'exec', '-T', 'db', 'pg_restore', '-l'],
                                              cwd=str(self.project), stdin=src, timeout=300)
                if listing.returncode != 0 or len(listing.out.splitlines()) < 10:
                    raise HelperError('The dump failed the pg_restore check — not saving it.')
                os.replace(tmp, final)
            finally:
                if tmp.exists():
                    tmp.unlink()
            return final
        finally:
            self._backup_lock.release()

    def _prune_backups(self) -> int:
        files = self.backup_files()
        cutoff = self.now().timestamp() - BACKUP_KEEP_DAYS * 86400
        removed = 0
        for p in files[BACKUP_KEEP_MIN:]:
            if p.stat().st_mtime < cutoff:
                p.unlink()
                removed += 1
        return removed

    def maybe_auto_backup(self) -> None:
        """Daily auto-backup: after 03:00 local time, if there is no fresh one."""
        now = self.now()
        if self._local_hour(now) < AUTO_BACKUP_HOUR:
            return
        files = self.backup_files()
        if files and now.timestamp() - files[0].stat().st_mtime < AUTO_BACKUP_EVERY.total_seconds():
            return
        if self.last_auto_attempt and now - self.last_auto_attempt < AUTO_BACKUP_RETRY:
            return
        if self.codefix is not None and self.codefix.busy_with_deploy:
            return                                  # the rollout makes its own dump; we will try the auto-backup next time
        self.last_auto_attempt = now
        try:
            path = self._make_backup(wait=False)
            self._prune_backups()
            self.last_auto_backup = {'ok': True, 'file': path.name, 'at': now.isoformat()}
            print(f'helper: auto-backup {path.name}', flush=True)
        except HelperError as exc:
            if str(exc) == 'busy':
                self.last_auto_attempt = None
                return
            self.last_auto_backup = {'ok': False, 'error': str(exc)[:300], 'at': now.isoformat()}
            print(f'helper: auto-backup failed: {exc}', file=sys.stderr, flush=True)
        except Exception as exc:
            self.last_auto_backup = {'ok': False, 'error': str(exc)[:300], 'at': now.isoformat()}
            print(f'helper: auto-backup failed: {exc}', file=sys.stderr, flush=True)

    # ── action: restart service ────────────────────────────────────────────
    def plan_restart_service(self, params):
        svc = params['service']
        return [f'I will restart the "{svc}" container (docker compose restart {svc}). '
                f'The background tasks of this service will stop for a minute, data is not affected.'], {}

    def apply_restart_service(self, params, data):
        svc = params['service']
        if svc not in SERVICES:
            raise HelperError('The service is not allowed.')
        res = self.runner.run([*self.compose, 'restart', svc], cwd=str(self.project), timeout=180)
        if res.returncode != 0:
            raise HelperError(f'The restart failed: {(res.err or res.out).strip()[:200]}')
        return [f'Done: "{svc}" restarted.'], {}

    # ── heartbeat and cleanup ──────────────────────────────────────────────
    def write_status(self) -> None:
        status = {'heartbeat': self.now().isoformat(), 'version': 3, 'last_auto_backup': self.last_auto_backup}
        if self.codefix is not None:
            try:
                status.update(self.codefix.status_fields())
            except Exception as exc:
                print(f'helper: auto-fix status: {exc!r}', file=sys.stderr, flush=True)
        self._write_json(self.queue / 'results' / 'status.json', status)
        self._write_json(self.queue / 'status.json', status)            # old location: needed only while the containers are being moved over

    def startup(self) -> None:
        """After start: mark interrupted tasks, finish an unfinished rollout (roll it back), clean up sandbox
leftovers."""
        if self.codefix is None:
            return
        for step in (self.codefix.mark_interrupted, self.codefix.recover_inflight, self.codefix.cleanup_stale):
            try:
                step()
            except Exception as exc:
                print(f'helper: recovery after a failure: {exc!r}', file=sys.stderr, flush=True)

    def maybe_refresh_ahead(self) -> None:
        """Once an hour (in the background) counts how many of the server's auto-commits are not yet in origin."""
        if self.codefix is None:
            return
        now = self.now()
        if self._last_ahead and now - self._last_ahead < AHEAD_REFRESH:
            return
        self._last_ahead = now
        if self.codefix._running is not None:
            return

        def work():
            try:
                self.codefix.refresh_ahead()
            except Exception as exc:
                print(f'helper: git fetch: {exc!r}', file=sys.stderr, flush=True)
        threading.Thread(target=work, name='codefix-ahead', daemon=True).start()

    def prune_queue(self) -> None:
        now = self.now()
        if self._last_prune and now - self._last_prune < timedelta(hours=1):
            return
        self._last_prune = now
        cutoff = now.timestamp() - KEEP_RESULTS.total_seconds()
        for sub, keep in (('results', ('status.json',)), ('done', ()), ('requests', ()), ('artifacts', ())):
            safeio.prune_old(self.queue / sub, cutoff, keep=keep)       # without following symlinks

    def loop(self, stop) -> None:
        print(f'helper: start, queue {self.queue}', flush=True)
        self.startup()
        while not stop():
            try:
                self.process_queue()
                self.maybe_auto_backup()
                self.maybe_refresh_ahead()
                self.prune_queue()
                self.write_status()
            except Exception as exc:
                print(f'helper: loop failure: {exc!r}', file=sys.stderr, flush=True)
            time.sleep(POLL_SECONDS)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    parser.add_argument('--queue', default=os.environ.get('SHOP_OPS_QUEUE', '/opt/shop/ops-queue'))
    parser.add_argument('--project', default=os.environ.get('SHOP_PROJECT', '/opt/shop'))
    parser.add_argument('--backups', default=os.environ.get('SHOP_BACKUP_DIR', '/opt/shop/backups'))
    parser.add_argument('--autofix-dir', default=os.environ.get('SHOP_AUTOFIX_DIR', '/opt/shop-autofix'))
    parser.add_argument('--site-url', default=os.environ.get('SHOP_SITE_URL', 'https://magiyaroslyn.com'))
    parser.add_argument('--once', action='store_true', help='Process the queue once and exit')
    args = parser.parse_args(argv)
    helper = Helper(args.queue, args.project, args.backups, autofix_dir=args.autofix_dir, site_url=args.site_url)
    if args.once:
        helper.process_queue()
        helper.write_status()
        return 0
    state = {'stop': False}
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: state.update(stop=True))
    helper.loop(lambda: state['stop'])
    return 0


if __name__ == '__main__':
    sys.exit(main())
