"""Auto-fix on the host: sandbox verification, rollout, health check, rollback.

Part of `shop-helper` (ops/shop_helper.py), standard library only. Runs as root on the host, so this
is the most critical place,
and the principle here is: the helper DOES NOT TRUST the web container, the analyst container, the
agent, or the reviewer.

  * a task request is accepted only with a SIGNATURE (HMAC with a key that exists only in the helper
    and in the analyst
    container): a compromised web can neither submit a patch nor order a rollout;
  * the patch is read from a file in the queue safely (no symlinks or FIFOs) and goes through the
    policy once more with the
    helper's own copy of `codefix_policy.py`, including a STRUCTURAL (AST) check of the new content
    of each file;
  * tests run in an isolated sandbox: NO network at all (Postgres in `--network none`, the app in
    its network namespace),
    no secrets, read-only filesystem, memory/process/output limits, timeouts; the new tests, the
    fixed code and the existing
    tests run in SEPARATE trees and processes, the verdict comes from the return code and the
    summary;
  * a rollout is possible only for a patch that passed verification right here, on THE SAME commit;
    a thin layer is built on top of
    the IMAGE that is running now (only the changed files, no network, no build cache);
  * window (night, Kyiv time), limit of 2 attempts per day with a 3 h pause (ALL attempts count),
    one pipeline at a time,
    a PAUSE flag file and a lock shared with the manual deploy;
  * after the rollout the site health is checked; if it fails - automatic rollback of images and
    code.

Long actions run in a background thread: `results/<id>.json` has `status: running` with the current
step, then
`done`/`failed`. The helper's heartbeat does not stop meanwhile.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import shutil
import tarfile
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

PG_IMAGE = 'postgres:18-alpine'
SERVICES = ('web', 'scheduler', 'analyst')
SANDBOX_MEMORY = '800m'
PG_MEMORY = '320m'
MiB, GiB = 2 ** 20, 2 ** 30
MIN_MEM_AVAILABLE = 1700 * MiB
MIN_FREE_DISK = 2 * GiB
TEST_TIMEOUT = 1500               # regression suite of the apps
NEW_TEST_TIMEOUT = 600            # new tests: several methods
MAX_OUTPUT = 8 * MiB              # more sandbox output than this and the run is aborted
BUILD_TIMEOUT = 600
UP_TIMEOUT = 300
PG_WAIT_SECONDS = 60
VERIFY_SECONDS = 180
VERIFY_INTERVAL = 10
DEPLOY_HOURS = (0, 7)             # rollout window (hour, Kyiv time); bypassed only by the owner's decision
MAX_DEPLOY_ATTEMPTS_PER_DAY = 2
MIN_DEPLOY_GAP = timedelta(hours=3)
VALIDATION_TTL = timedelta(hours=6)
RECOVER_MAX_AGE = timedelta(hours=3)
MAX_RECOVER_ATTEMPTS = 3
KEEP_ROLLBACK_TAGS = 3
SIGN_MAX_AHEAD = 3600
COMMIT_PREFIX = 'autofix '
BOT_NAME, BOT_EMAIL = 'Analyst', 'analyst@shop.local'
SITE_PATHS = ('/', '/api/products/?page_size=1', '/admin/login/')
SANDBOX_LABEL = 'shop-autofix'
DRILL_FILES = {'backend/core/canary.py'}
CORE_REGRESSION_APPS = ('core', 'catalog', 'orders', 'courses', 'users', 'blog')

HEX_ID = re.compile(r'^[a-f0-9]{8,32}$')
SHA = re.compile(r'^[a-f0-9]{40}$')
SHA256 = re.compile(r'^[a-f0-9]{64}$')
_HEADER = re.compile(r'^(FAIL|ERROR): (\S+) \(([\w.]+)\)(?:[ \t].*)?$', re.M)
_RAN = re.compile(r'^Ran (\d+) tests? in [0-9.]+s\s*$', re.M)
_VERDICT = re.compile(r'^(OK|FAILED)(?: \(([^)]*)\))?$')
INFRA_NAMES = ('setUpClass', 'setUpModule', 'tearDownClass', 'tearDownModule', 'setUpTestData')
TB_START = re.compile(r'^Traceback \(most recent call last\):')
COMPOSE_PREFIX = re.compile(r'^[\w.-]+\s+\|\s?')
# Exceptions that are NOT a sign of a broken fix: bot noise, client disconnects, external services.
HEALTH_IGNORE = {
    'DisallowedHost', 'Http404', 'PermissionDenied', 'SuspiciousOperation', 'BadRequest', 'ClientDisconnect', 'MultiPartParserError',
    'RequestDataTooBig', 'TooManyFieldsSent', 'UnreadablePostError', 'ConnectionResetError', 'BrokenPipeError', 'CancelledError',
    'WebSocketDisconnect', 'StopConsumer', 'ConnectionClosed', 'ConnectionClosedError', 'ConnectionClosedOK', 'TimeoutError',
    'OperationalError', 'InterfaceError', 'ConnectionError', 'ConnectTimeout', 'ReadTimeout', 'SSLError', 'gaierror', 'URLError',
    'HTTPError', 'SMTPException', 'SMTPServerDisconnected', 'RedisError', 'ChunkedEncodingError', 'ProtocolError',
}
# Signs of a logic error in the code (mirror of analyst/errorlog.LOGIC): only these count as 'new
# errors after the rollout'.
HEALTH_LOGIC = {
    'AttributeError', 'TypeError', 'KeyError', 'IndexError', 'ValueError', 'ZeroDivisionError', 'UnboundLocalError', 'NameError',
    'ImportError', 'ModuleNotFoundError', 'SyntaxError', 'IndentationError', 'DoesNotExist', 'MultipleObjectsReturned',
    'InvalidOperation', 'AssertionError', 'RecursionError', 'NotImplementedError', 'TemplateSyntaxError', 'NoReverseMatch',
    'FieldError', 'ImproperlyConfigured', 'ProgrammingError', 'FieldDoesNotExist',
}


def canonical(rid: str, action: str, params, exp: int) -> bytes:
    return json.dumps({'id': rid, 'action': action, 'params': params, 'exp': exp}, sort_keys=True,
                      separators=(',', ':'), ensure_ascii=True).encode('ascii')


def sign(key: bytes, rid: str, action: str, params, exp: int) -> str:
    return hmac.new(key, canonical(rid, action, params, exp), hashlib.sha256).hexdigest()


def _clip(text: str, limit: int = 300) -> str:
    text = ' '.join(str(text).split())
    return text if len(text) <= limit else text[:limit - 1] + '…'


def parse_test_output(stderr: str, stdout: str = '') -> dict:
    """Parsing the output of `manage.py test`. We trust ONLY stderr (that is where the runner's summary
    is) and only the LAST summary:
    the stdout of a hostile test decides nothing. `ok` means the summary is 'OK' and there is no
    FAIL/ERROR header."""
    ran_all = list(_RAN.finditer(stderr))
    last = ran_all[-1] if ran_all else None
    ran = int(last.group(1)) if last else 0
    summary = ''
    if last:
        for line in stderr[last.end():].splitlines():
            if line.strip():
                summary = line.strip()
                break
    verdict = _VERDICT.match(summary)
    headers = [(kind, name, path) for kind, name, path in _HEADER.findall(stderr[:last.start()] if last else stderr)]
    declared = None
    if verdict and verdict.group(1) == 'FAILED':
        counts = dict(re.findall(r'(\w+)=(\d+)', verdict.group(2) or ''))
        declared = int(counts.get('failures', 0)) + int(counts.get('errors', 0))
    failed = sorted({path for _kind, _name, path in headers})
    loader = sorted({path for _k, _n, path in headers if path.startswith('unittest.loader.')})
    infra = sorted({path for _k, name, path in headers if name in INFRA_NAMES})
    test_failed = sorted({path for _k, name, path in headers
                          if name not in INFRA_NAMES and not path.startswith('unittest.loader.')})
    consistent = declared is None or declared == len(headers)
    lines = [_clip(ln) for ln in stderr.strip().splitlines() if ln.strip()]
    return {'ran': ran, 'summary': summary, 'failed': failed, 'loader': loader, 'infra': infra, 'test_failed': test_failed,
            'consistent': consistent, 'ok': bool(verdict and verdict.group(1) == 'OK') and not headers and bool(last),
            'tail': lines[-25:]}


def new_logic_errors(text: str) -> list[str]:
    """New 'logic' tracebacks in the container logs: we parse blocks (rather than counting substrings),
    discard bot noise
    (DisallowedHost...), client disconnects and external-service failures. Returns short 'Type:
    message' lines."""
    lines = [COMPOSE_PREFIX.sub('', ln) for ln in text.splitlines()]
    found = []
    i = 0
    while i < len(lines):
        if not TB_START.match(lines[i].strip()):
            i += 1
            continue
        j = i + 1
        while j < len(lines) and (not lines[j].strip() or lines[j][:1] in (' ', '\t')):
            j += 1
        final = lines[j].strip() if j < len(lines) else ''
        name = re.match(r'^([A-Za-z_][\w.]*)', final)
        short = name.group(1).rsplit('.', 1)[-1] if name else ''
        if short in HEALTH_LOGIC and short not in HEALTH_IGNORE:
            found.append(_clip(final, 160))
        i = max(j, i + 1)
    return found


def sanitize_summary(text) -> str:
    return ' '.join(re.sub(r'[\x00-\x1f\x7f]', ' ', str(text or '')).split())[:160]


class Job:
    def __init__(self, rid: str, action: str, params: dict):
        self.id, self.action, self.params = rid, action, params
        self.lines: list[str] = []
        self.step = 'queued'
        self.data: dict = {}


class DeployLock:
    """Lock (flock) shared with the manual deploy: `flock /opt/shop-autofix/deploy.lock sh -c '…'`."""

    def __init__(self, path: Path, error):
        self.path, self.error, self.fd = path, error, None

    def __enter__(self):
        try:
            import fcntl
        except ImportError:                       # dev Windows: no lock
            return self
        import os
        self.fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(self.fd)
            self.fd = None
            exc = self.error('A manual deploy or another rollout is in progress (the deploy.lock lock is taken) — I will try later.')
            exc.code = 'wait'
            raise exc
        return self

    def __exit__(self, *exc):
        if self.fd is not None:
            import os
            os.close(self.fd)                      # the lock is released by closing the descriptor
            self.fd = None


class Codefix:
    def __init__(self, helper, policy, error, *, autofix_dir, key=None, site_url='https://magiyaroslyn.com', sync=False,
                 sleep=time.sleep, http=None, meminfo=None, kyiv_hour=None, mono=time.monotonic):
        self.h, self.policy, self.Error = helper, policy, error
        self.io = helper.io
        self.key = key if isinstance(key, bytes) and len(key) >= 32 else None
        self._mono = mono
        self.autofix_dir = Path(autofix_dir)
        self.site_url = site_url.rstrip('/')
        self.sync = sync
        self._sleep = sleep
        self._http = http or self._http_status
        self._meminfo = meminfo or self._read_meminfo
        self._kyiv_hour = kyiv_hour or self._local_kyiv_hour
        self._lock = threading.Lock()
        self._running: Job | None = None
        self._pending: Job | None = None
        self._head_cache: tuple[float, str] = (-1e9, "")
        for sub in ('wt', 'applied', 'rejected', 'build'):
            (self.autofix_dir / sub).mkdir(parents=True, exist_ok=True)

    # ── utility ─────────────────────────────────────────────────────────────
    def run(self, args, *, timeout=120, cwd=None, stdin=None, stdout=None, max_output=None):
        kw = {'max_output': max_output} if max_output else {}
        return self.h.runner.run(list(args), timeout=timeout, cwd=cwd, stdin=stdin, stdout=stdout, **kw)

    def git(self, *args, timeout=60, cwd=None):
        return self.run(['git', *args], timeout=timeout, cwd=str(cwd or self.h.project))

    def docker(self, *args, timeout=120, max_output=None):
        return self.run(['docker', *args], timeout=timeout, max_output=max_output)

    def compose(self, *args, timeout=120):
        return self.run([*self.h.compose, *args], cwd=str(self.h.project), timeout=timeout)

    def now(self) -> datetime:
        return self.h.now()

    def err(self, text: str, code: str = 'error'):
        """Error with a machine-readable code for the Analyst: 'wait' - try later, 'stale' - the patch is
outdated, 'error' - refusal."""
        exc = self.Error(text)
        exc.code = code
        return exc

    @property
    def state_path(self) -> Path:
        return self.autofix_dir / 'state.json'

    @property
    def paused(self) -> bool:
        return (self.autofix_dir / 'PAUSE').exists()

    def state(self) -> dict:
        data = self.h._read_json(self.state_path) or {}
        data.setdefault('attempts', [])
        data.setdefault('validated', {})
        data.setdefault('applied', [])
        return data

    def save_state(self, data: dict) -> None:
        self.h._write_json(self.state_path, data)

    def head(self) -> str:
        res = self.git('rev-parse', 'HEAD')
        return res.out.strip() if res.returncode == 0 else ''

    def tree_clean(self) -> bool:
        res = self.git('status', '--porcelain', '--untracked-files=no')
        return res.returncode == 0 and not res.out.strip()

    @staticmethod
    def _read_meminfo() -> int:
        try:
            for line in Path('/proc/meminfo').read_text().splitlines():
                if line.startswith('MemAvailable:'):
                    return int(line.split()[1]) * 1024
        except (OSError, ValueError):
            pass
        return 0

    @staticmethod
    def _local_kyiv_hour(dt: datetime) -> int:
        try:
            from zoneinfo import ZoneInfo
            return dt.astimezone(ZoneInfo('Europe/Kyiv')).hour
        except Exception:
            return dt.astimezone().hour

    @staticmethod
    def _http_status(url: str, timeout: int = 10) -> int:
        try:
            req = urllib.request.Request(url, headers={'User-Agent': 'shop-helper/health'})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.status
        except urllib.error.HTTPError as exc:
            return exc.code
        except Exception:
            return 0

    # ── status for status.json ──────────────────────────────────────────────
    def status_fields(self) -> dict:
        ts, cached = self._head_cache
        if self._mono() - ts > 30:
            cached = self.head()
            self._head_cache = (self._mono(), cached)
        job = self._running
        state = self.state()
        return {'policy_sha': self.policy.policy_sha(), 'head': cached, 'autofix_ahead': state.get('ahead', 0),
                'key_ok': self.key is not None, 'key_fp': hashlib.sha256(self.key).hexdigest()[:8] if self.key else '',
                'paused': self.paused, 'recovery': state.get('recovery'),
                'job': {'id': job.id, 'action': job.action, 'step': job.step} if job else None}

    def refresh_ahead(self) -> None:
        """How many of the server's auto-commits have not yet reached origin (the deploy key is read-only, so
this is a manual transfer)."""
        self.git('fetch', '--quiet', 'origin', timeout=90)
        res = self.git('log', '--format=%s', '@{upstream}..HEAD')
        ahead = sum(1 for ln in res.out.splitlines() if ln.startswith(COMMIT_PREFIX)) if res.returncode == 0 else 0
        data = self.state()
        data['ahead'] = ahead
        self.save_state(data)

    @property
    def busy_with_deploy(self) -> bool:
        job = self._running
        return job is not None and job.action in ('codefix_deploy', 'codefix_revert')

    # ── task intake and launch ──────────────────────────────────────────────
    def verify_auth(self, rid: str, action: str, params, auth) -> None:
        if self.key is None:
            raise self.err('Auto-fixes are not configured: the server has no signing key.')
        exp = auth.get('exp') if isinstance(auth, dict) else None
        mac = auth.get('mac') if isinstance(auth, dict) else None
        if not isinstance(exp, int) or isinstance(exp, bool) or not isinstance(mac, str):
            raise self.Error('The request is unsigned.')
        now_ts = int(self.now().timestamp())
        if exp < now_ts:
            raise self.Error('The request signature has expired.')
        if exp > now_ts + SIGN_MAX_AHEAD:
            raise self.Error('The request signature has an invalid time.')
        try:
            expected = sign(self.key, rid, action, params, exp)
        except (TypeError, ValueError):
            raise self.Error('The request parameters cannot be signed.')
        if not hmac.compare_digest(expected, mac):
            raise self.Error('The request signature is wrong.')

    def submit(self, rid: str, action: str, params, auth=None) -> dict:
        self.verify_auth(rid, action, params, auth)
        params = self._validate_params(action, params)
        if self.paused:
            raise self.err('Auto-fixes are paused (PAUSE file) — I will try later.', 'wait')
        with self._lock:
            if self._running is not None or self._pending is not None:
                raise self.err('Another auto-fix is already running — wait.', 'wait')
            job = Job(rid, action, params)
            self._pending = job
        return self._result(job, 'running')

    def launch(self) -> None:
        """Called AFTER the initial results/<id>.json is written: the thread will not overwrite it with an
older state."""
        with self._lock:
            job, self._pending = self._pending, None
            if job is None:
                return
            self._running = job
        if self.sync:
            self._execute(job)
        else:
            threading.Thread(target=self._execute, args=(job,), name=f'codefix-{job.id}', daemon=True).start()

    def _validate_params(self, action: str, raw) -> dict:
        raw = raw if isinstance(raw, dict) else {}
        rules = {
            'codefix_validate': {'patch_id': 'id', 'patch_sha': 'sha256'},
            'codefix_deploy': {'patch_id': 'id', 'patch_sha': 'sha256', 'base_sha': 'sha', 'summary': 'text', 'by_owner': 'bool?',
                               'simulate_failure': 'bool?', 'drill': 'bool?'},
            'codefix_revert': {'commit': 'sha', 'summary': 'text'},
        }
        if action not in rules:
            raise self.Error('Unknown auto-fix action.')
        schema = rules[action]
        extra = set(raw) - set(schema)
        if extra:
            raise self.Error(f'Unknown parameters: {", ".join(sorted(extra))}')
        out = {}
        for name, kind in schema.items():
            optional = kind.endswith('?')
            kind = kind.rstrip('?')
            if name not in raw:
                if optional:
                    out[name] = False
                    continue
                raise self.Error(f'The parameter "{name}" is not specified')
            value = raw[name]
            if kind == 'id' and not (isinstance(value, str) and HEX_ID.match(value)):
                raise self.Error(f'The parameter "{name}" is invalid.')
            if kind == 'sha' and not (isinstance(value, str) and SHA.match(value)):
                raise self.Error(f'The parameter "{name}" must be a full commit sha.')
            if kind == 'sha256' and not (isinstance(value, str) and SHA256.match(value)):
                raise self.Error(f'The parameter "{name}" must be a sha256 (64 hexadecimal characters).')
            if kind == 'bool' and not isinstance(value, bool):
                raise self.Error(f'The parameter "{name}" must be true/false.')
            out[name] = sanitize_summary(value) if kind == 'text' else value
        return out

    def _result(self, job: Job, status: str, **extra) -> dict:
        out = {'id': job.id, 'mode': 'job', 'action': job.action, 'status': status, 'step': job.step,
               'lines': job.lines[-40:], 'created': self.now().isoformat()}
        out.update(job.data)
        out.update(extra)
        return out

    def _publish(self, job: Job, status: str = 'running', **extra) -> None:
        try:
            self.h._write_json(self.h.queue / 'results' / f'{job.id}.json', self._result(job, status, **extra))
        except OSError as exc:
            print(f'helper: could not write the result {job.id}: {exc!r}', flush=True)

    def say(self, job: Job, step: str, line: str | None = None) -> None:
        job.step = step
        if line:
            job.lines.append(line)
        self._publish(job)

    def _execute(self, job: Job) -> None:
        try:
            getattr(self, f'_do_{job.action[len("codefix_"):]}')(job)
            self._publish(job, 'done')
        except self.Error as exc:
            job.lines.append(str(exc))
            self._publish(job, 'failed', error=str(exc), code=getattr(exc, 'code', 'error'))
        except Exception as exc:                  # the helper does not crash because of a single task
            job.lines.append(f'Internal error: {exc!r}'[:300])
            self._publish(job, 'failed', error='Internal helper error, details are in the service log.')
            print(f'helper: auto-fix error {job.id}: {exc!r}', flush=True)
        finally:
            with self._lock:
                self._running = None

    # ── input checks ────────────────────────────────────────────────────────
    def artifact(self, patch_id: str) -> tuple[str, str]:
        """Patch from the queue: safe read (no symlinks or FIFOs), size, UTF-8. Returns (text, sha256)."""
        data = self.io.read_regular(self.h.queue / 'artifacts' / f'{patch_id}.patch', self.policy.MAX_PATCH_BYTES + 1024)
        if data is None:
            raise self.Error('The patch was not found in the queue (or it is not a regular file of an allowed size).')
        try:
            return data.decode('utf-8'), hashlib.sha256(data).hexdigest()
        except UnicodeDecodeError:
            raise self.Error('The patch is not UTF-8 text.')

    def signed_artifact(self, job: Job) -> tuple[str, str]:
        """Patch from the queue whose sha256 matches the one SIGNED in the request: a swapped DB record
        pointing to a different artifact
        (say, an old rejected patch) does not pass. Otherwise returns (text, sha256)."""
        text, digest = self.artifact(job.params['patch_id'])
        if digest != job.params['patch_sha']:
            raise self.Error('The patch in the queue does not match the signed one (sha256) — the request was not carried out.')
        return text, digest

    def _reader(self, head: str):
        cache: dict = {}

        def read_old(path: str):
            if path not in cache:
                res = self.git('show', f'{head}:{path}', timeout=30)
                cache[path] = res.out if res.returncode == 0 else None
            return cache[path]
        return read_old

    def check_policy(self, text: str, head: str):
        try:
            return self.policy.check_patch(text, read_old=self._reader(head))
        except self.policy.PolicyViolation as exc:
            raise self.err(f'The policy rejected the patch: {exc}', 'policy')

    def _preflight(self, job: Job) -> None:
        free = self.h._free_bytes()
        if free < MIN_FREE_DISK:
            raise self.err(f'Not enough disk space: {free // MiB} MB free, at least {MIN_FREE_DISK // MiB} needed.', 'wait')
        mem = self._meminfo()
        if mem and mem < MIN_MEM_AVAILABLE:
            raise self.err(f'Not enough free memory: {mem // MiB} MB, at least {MIN_MEM_AVAILABLE // MiB} needed.', 'wait')
        if self.docker('version', '--format', '{{.Server.Version}}', timeout=30).returncode != 0:
            raise self.err('Docker is unavailable.', 'wait')

    # ── running images ──────────────────────────────────────────────────────
    def _service_images(self) -> dict:
        """{service: (container id, image id)} for RUNNING containers. Rollback and the base of the layer are taken from here, not from the
        :latest tag (it may point to an image that was never run)."""
        out = {}
        for svc in SERVICES:
            ids = self.compose('ps', '-q', svc, timeout=60).out.split()
            if len(ids) != 1:
                raise self.err(f'Exactly one running container "{svc}" was not found.', 'wait')
            info = self.docker('inspect', '--format', '{{.State.Running}} {{.Image}}', ids[0], timeout=30)
            parts = info.out.split()
            if info.returncode != 0 or len(parts) != 2 or parts[0] != 'true' or not parts[1].startswith('sha256:'):
                raise self.err(f'The container "{svc}" is not running.', 'wait')
            out[svc] = (ids[0], parts[1])
        return out

    def _image_matches_head(self, image: str, files: list[str], head: str) -> None:
        """The files we are patching must be THE SAME in the image as in git at `head`: otherwise the
        layer on top of the image will silently pull in
        someone else's changes (or the fix will not match what is running)."""
        reader = self._reader(head)
        for rel in files:
            inside = self.docker('run', '--rm', '--network', 'none', '--read-only', '--cap-drop', 'ALL', '--entrypoint', 'cat',
                                 image, '/app/' + rel[len('backend/'):], timeout=60)
            if inside.returncode != 0 or inside.out != reader(rel):
                raise self.err(f'The file {rel} in the running image does not match git — first synchronize the image and the code '
                               f'(a regular deploy), auto-fix is unsafe right now.', 'stale')

    # ── sandbox ─────────────────────────────────────────────────────────────
    def _export_tree(self, dest: Path, head: str) -> None:
        tar = dest.with_suffix('.tar')
        dest.mkdir(parents=True, exist_ok=True)
        res = self.git('archive', '--format=tar', '-o', str(tar), head, 'backend', timeout=120)
        if res.returncode != 0:
            raise self.Error(f'Could not export the code: {(res.err or res.out).strip()[:200]}')
        try:
            with tarfile.open(tar) as archive:
                archive.extractall(dest, filter='data')
        finally:
            tar.unlink(missing_ok=True)

    def _apply(self, tree: Path, patch_text: str, name: str) -> None:
        if not patch_text.strip():
            return
        patch_file = tree.parent / f'{name}.patch'
        patch_file.write_text(patch_text, encoding='utf-8', newline='\n')
        for args in (['apply', '--check', '--whitespace=nowarn', str(patch_file)], ['apply', '--whitespace=nowarn', str(patch_file)]):
            res = self.git(*args, cwd=tree)
            if res.returncode != 0:
                raise self.err(f'The patch does not apply to the current code: {(res.err or res.out).strip()[:300]}', 'policy')

    def _rm(self, name: str) -> None:
        self.docker('rm', '-f', name, timeout=60)

    def sweep(self) -> None:
        """Remove all sandbox containers (by label) - both those left over from a failure and the current ones."""
        res = self.docker('ps', '-aq', '--filter', f'label={SANDBOX_LABEL}', timeout=30)
        for cid in res.out.split():
            if re.fullmatch(r'[a-f0-9]{6,64}', cid):
                self._rm(cid)

    def _pg_up(self, job: Job, pg: str) -> None:
        args = ['run', '-d', '--name', pg, '--label', f'{SANDBOX_LABEL}={job.id}', '--network', 'none',
                '--memory', PG_MEMORY, '--memory-swap', PG_MEMORY, '--cpus', '1', '--cpu-shares', '256', '--pids-limit', '128',
                '--oom-score-adj', '1000', '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges', '--read-only',
                '--user', '70:70',
                '--tmpfs', '/var/lib/postgresql:rw,size=400m,uid=70,gid=70,mode=0700',
                '--tmpfs', '/var/run/postgresql:rw,size=8m,uid=70,gid=70,mode=0775', '--tmpfs', '/tmp:rw,noexec,nosuid,size=64m',
                '-e', 'POSTGRES_USER=cf', '-e', 'POSTGRES_PASSWORD=cf', '-e', 'POSTGRES_DB=cf', PG_IMAGE,
                '-c', 'listen_addresses=127.0.0.1', '-c', 'fsync=off', '-c', 'synchronous_commit=off', '-c', 'full_page_writes=off',
                '-c', 'shared_buffers=48MB', '-c', 'max_wal_size=128MB', '-c', 'max_connections=40']
        res = self.docker(*args, timeout=120)
        if res.returncode != 0:
            raise self.err(f'Could not start the temporary database: {(res.err or res.out).strip()[:200]}', 'wait')
        deadline = self._mono() + PG_WAIT_SECONDS
        while True:
            if self.docker('exec', pg, 'pg_isready', '-h', '127.0.0.1', '-U', 'cf', '-d', 'cf', timeout=15).returncode == 0:
                return
            if self._mono() > deadline:
                logs = self.docker('logs', '--tail', '8', pg, timeout=20)
                raise self.err(f'The temporary database did not become ready within a minute: {_clip(logs.out + logs.err, 200)}', 'wait')
            self._sleep(2)

    def _app_args(self, job: Job, name: str, pg: str, image: str, tree: Path, seconds: int, labels: list[str]) -> list[str]:
        return ['run', '--name', name, '--label', f'{SANDBOX_LABEL}={job.id}', '--network', f'container:{pg}',
                '--memory', SANDBOX_MEMORY, '--memory-swap', SANDBOX_MEMORY, '--cpus', '1', '--cpu-shares', '256',
                '--pids-limit', '256', '--oom-score-adj', '1000', '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges',
                '--read-only', '--ulimit', 'fsize=134217728', '--ulimit', 'core=0', '--log-driver', 'none',
                '-v', f'{tree}/backend:/app:ro', '--tmpfs', '/app/media:rw,noexec,nosuid,size=100m',
                '--tmpfs', '/tmp:rw,noexec,nosuid,size=128m', '-w', '/app',
                '-e', 'DEBUG=True', '-e', 'SECRET_KEY=sandbox-not-a-secret', '-e', 'DB_ENGINE=postgresql',
                '-e', 'DB_NAME=cf', '-e', 'DB_USER=cf', '-e', 'DB_PASSWORD=cf', '-e', 'DB_HOST=127.0.0.1', '-e', 'DB_PORT=5432',
                '-e', 'ALLOWED_HOSTS=testserver,localhost', '-e', 'ANALYST_ERRORLOG=off', '-e', 'PYTHONDONTWRITEBYTECODE=1',
                '--entrypoint', 'timeout', image, '-s', 'KILL', str(seconds), 'python', 'manage.py', 'test', '--noinput', *labels]

    def _sandbox_phase(self, job: Job, tag: str, image: str, tree: Path, labels: list[str], seconds: int) -> dict:
        """One run in a FRESH temporary database and a fresh container. Returns the parsed result + the reason
for an environment failure."""
        pg, name = f'cf-pg-{job.id}-{tag}', f'cf-run-{job.id}-{tag}'
        try:
            self._pg_up(job, pg)
            res = self.docker(*self._app_args(job, name, pg, image, tree, seconds, labels), timeout=seconds + 60, max_output=MAX_OUTPUT)
            oom = self.docker('inspect', '--format', '{{.State.OOMKilled}}', name, timeout=30).out.strip() == 'true'
        finally:
            self._rm(name)
            self._rm(pg)
        parsed = parse_test_output(res.err, res.out)
        parsed['returncode'] = res.returncode
        env = ''
        if getattr(res, 'over_limit', False):
            env = 'The test prints too much text (over 8 MB) — the run was aborted.'
        elif oom:
            env = 'The test exhausted the allotted memory — the run was aborted.'
        elif getattr(res, 'timed_out', False) or res.returncode == 137:
            env = f'The tests did not finish within {seconds // 60} min — the run was aborted.'
        elif res.returncode in (125, 126, 127):
            raise self.err(f'Docker could not start the sandbox: {_clip(res.err or res.out, 200)}', 'wait')
        parsed['env_problem'] = env
        return parsed

    def cleanup_stale(self) -> None:
        """After a service failure: remove the leftover sandbox containers and directories."""
        self.sweep()
        for line in self.docker('ps', '-a', '--filter', 'name=^cf-', '--format', '{{.Names}}', timeout=30).out.split():
            if re.fullmatch(r'cf-[a-z0-9-]+', line):
                self._rm(line)
        for net in self.docker('network', 'ls', '--filter', 'name=^cf-', '--format', '{{.Name}}', timeout=30).out.split():
            if re.fullmatch(r'cf-[a-z0-9]+', net):
                self.docker('network', 'rm', net, timeout=30)
        for sub in ('wt', 'build'):
            shutil.rmtree(self.autofix_dir / sub, ignore_errors=True)
            (self.autofix_dir / sub).mkdir(parents=True, exist_ok=True)

    @staticmethod
    def regression_labels(code_files: list[str]) -> list[str]:
        apps = {p.split('/')[1] for p in code_files}
        if 'core' in apps:
            apps |= set(CORE_REGRESSION_APPS)
        return sorted(apps)

    @staticmethod
    def judge_reproduces(a: dict, expected: int) -> str:
        """New tests WITHOUT the fix: they must start and fail specifically in the test methods (not on import
or setup)."""
        if a['env_problem']:
            return a['env_problem']
        if not a['consistent']:
            return 'The sandbox output does not match the runner summary — the result cannot be accepted.'
        if a['ran'] != expected:
            return f'Not all new tests started: {a["ran"]} of {expected} ran (an import or configuration error).'
        if a['loader'] or a['infra']:
            return 'The new tests do not reproduce the error: they fail on import or setup, not on the assertion.'
        if not a['test_failed']:
            return 'The new tests PASS even without the fix — they do not reproduce the error.'
        return ''

    @staticmethod
    def judge_passes(b: dict, expected: int) -> str:
        """New tests WITH the fix: all of them ran and passed (return code 0, summary OK)."""
        if b['env_problem']:
            return b['env_problem']
        if b['ran'] != expected:
            return f'Not all new tests started with the fix: {b["ran"]} of {expected} ran.'
        if b['returncode'] != 0 or not b['ok']:
            return 'The new test fails even with the fix.'
        return ''

    # ── action: sandbox verification ────────────────────────────────────────
    def _do_validate(self, job: Job) -> None:
        text, digest = self.signed_artifact(job)
        head = self.head()
        if not head:
            raise self.Error('Could not determine the current commit.')
        report = self.check_policy(text, head)
        self.say(job, 'policy', f'Policy passed: files {", ".join(report.files)}.')
        if not self.tree_clean():
            raise self.Error('The working tree on the server has uncommitted changes — the check is impossible.')
        self._preflight(job)
        images = self._service_images()
        image = images['web'][1]
        self._image_matches_head(image, report.code_files, head)
        tests_patch, code_patch = self.policy.split_patch(text)
        modules = sorted({p[len('backend/'):-3].replace('/', '.') for p in report.test_files})
        regression = self.regression_labels(report.code_files)
        work = self.autofix_dir / 'wt' / job.id
        trees = {name: work / name for name in ('base', 'with_tests', 'full', 'code_only')}
        verdict, reason = 'fail', ''
        phases: dict = {}
        started = self._mono()
        try:
            self.sweep()
            shutil.rmtree(work, ignore_errors=True)
            self.say(job, 'export', 'Exporting the code of the current commit…')
            self._export_tree(trees['base'], head)
            for name in ('with_tests', 'full', 'code_only'):
                shutil.copytree(trees['base'], trees[name])
            self._apply(trees['with_tests'], tests_patch, 'tests')
            self._apply(trees['code_only'], code_patch, 'code')
            self._apply(trees['full'], text, 'full')
            try:
                self.policy.check_applied(text, self._reader(head), lambda p: self._read_tree_file(trees['full'], p))
            except self.policy.PolicyViolation as exc:
                raise self.err(f'The structural check failed after applying the patch: {exc}', 'policy')
            for tree in trees.values():
                (tree / 'backend' / 'media').mkdir(exist_ok=True)         # tmpfs mount point: the code is mounted read-only
            self.say(job, 'baseline', 'The new tests on the code WITHOUT the fix (they must fail)…')
            a = phases['baseline'] = self._sandbox_phase(job, 'a', image, trees['with_tests'], modules, NEW_TEST_TIMEOUT)
            reason = self.judge_reproduces(a, report.tests_expected)
            if not reason:
                self.say(job, 'patched', 'The new tests with the fix (they must pass)…')
                b = phases['patched'] = self._sandbox_phase(job, 'b', image, trees['full'], modules, NEW_TEST_TIMEOUT)
                reason = self.judge_passes(b, report.tests_expected)
            if not reason:
                self.say(job, 'regression', f'The existing tests ({", ".join(regression)}) on the fixed code…')
                c = phases['regression'] = self._sandbox_phase(job, 'c', image, trees['code_only'], regression, TEST_TIMEOUT)
                if c['env_problem']:
                    reason = c['env_problem']
                elif not c['consistent'] or not c['summary']:
                    reason = 'The existing tests: the sandbox output does not match the runner summary.'
                elif c['returncode'] != 0 or not c['ok']:
                    broken = c['failed']
                    if not broken:
                        reason = 'The existing tests ended with an error without any named failure.'
                    else:
                        self.say(job, 'compare', f'{len(broken)} tests fail: checking whether they failed before the fix too…')
                        d = phases['base'] = self._sandbox_phase(job, 'd', image, trees['base'], broken, TEST_TIMEOUT)
                        already = set(d['failed'])
                        new_failures = sorted(set(broken) - already)
                        d['new_failures'] = new_failures
                        if d['env_problem']:
                            reason = d['env_problem']
                        elif new_failures:
                            reason = f'The fix breaks {len(new_failures)} existing tests: {", ".join(new_failures[:5])}.'
            if not reason:
                verdict = 'pass'
        finally:
            self.sweep()
            shutil.rmtree(work, ignore_errors=True)
        job.data.update(verdict=verdict, reason=reason, head=head, patch_sha=digest, tests_expected=report.tests_expected,
                        phases={k: {kk: vv for kk, vv in v.items() if kk in ('ran', 'failed', 'ok', 'tail', 'new_failures', 'returncode')}
                                for k, v in phases.items()}, seconds=round(self._mono() - started))
        if verdict == 'pass':
            data = self.state()
            data['validated'][digest] = {'head': head, 'at': self.now().isoformat(), 'files': report.files}
            data['validated'] = dict(list(data['validated'].items())[-20:])
            self.save_state(data)
            job.lines.append('The check passed.')
        else:
            job.lines.append(f'The check did NOT pass: {reason}')

    @staticmethod
    def _read_tree_file(tree: Path, rel: str):
        path = tree / rel
        if path.is_symlink() or not path.is_file():
            return None
        return path.read_text(encoding='utf-8')

    # ── action: rollout ─────────────────────────────────────────────────────
    def _check_window_and_limits(self, by_owner: bool, drill: bool) -> None:
        now = self.now()
        if not by_owner and not drill:
            hour = self._kyiv_hour(now)
            if not (DEPLOY_HOURS[0] <= hour < DEPLOY_HOURS[1]):
                raise self.err(f'Auto-fix deployment only at night ({DEPLOY_HOURS[0]:02d}:00–{DEPLOY_HOURS[1]:02d}:00 Kyiv time), '
                               f'it is {hour:02d}:00 now. Earlier — only by the owner\'s decision.', 'wait')
        if drill:
            return
        attempts = [datetime.fromisoformat(x) for x in self.state()['attempts']]
        recent = [d for d in attempts if now - d < timedelta(hours=24)]
        if len(recent) >= MAX_DEPLOY_ATTEMPTS_PER_DAY:
            raise self.err(f'Limit: no more than {MAX_DEPLOY_ATTEMPTS_PER_DAY} auto-fix deployment attempts per day '
                           f'(failed ones count too).', 'wait')
        if recent and now - max(recent) < MIN_DEPLOY_GAP:
            raise self.err('At least 3 hours must pass between auto-fix deployment attempts.', 'wait')

    def _tags(self, stamp: str) -> list[tuple[str, str]]:
        return [(f'shop-{svc}:latest', f'shop-{svc}:pre-autofix-{stamp}') for svc in SERVICES]

    def _retag_back(self, stamp: str) -> bool:
        ok = True
        for latest, backup in self._tags(stamp):
            if self.docker('tag', backup, latest, timeout=30).returncode != 0:
                ok = False
        return ok

    def _up(self) -> None:
        res = self.run([*self.h.compose, 'up', '-d', '--no-build', '--no-deps', *SERVICES], cwd=str(self.h.project), timeout=UP_TIMEOUT)
        if res.returncode != 0:
            raise self.Error(f'docker compose up failed: {(res.err or res.out).strip()[:200]}')

    def _healthy(self, since: datetime) -> tuple[bool, str]:
        """The site responds, the containers are running, there are no new LOGIC tracebacks in the logs."""
        for path in SITE_PATHS:
            code = self._http(f'{self.site_url}{path}')
            if code != 200:
                return False, f'{path} responds with {code or "nothing"}'
        ps = self.run([*self.h.compose, 'ps', '--format', '{{.Service}} {{.State}}', *SERVICES], cwd=str(self.h.project), timeout=60)
        states = dict(line.split(None, 1) for line in ps.out.splitlines() if ' ' in line)
        bad = [s for s in SERVICES if states.get(s, '').strip() != 'running']
        if bad:
            return False, f'not running: {", ".join(bad)}'
        logs = self.run([*self.h.compose, 'logs', '--since', since.strftime('%Y-%m-%dT%H:%M:%SZ'), '--no-color', *SERVICES],
                        cwd=str(self.h.project), timeout=60)
        errors = new_logic_errors(logs.out + logs.err)
        if errors:
            return False, f'the logs contain {len(errors)} new code errors, e.g.: {errors[0]}'
        return True, ''

    def _verify(self, since: datetime, simulate_failure: bool) -> tuple[bool, str]:
        deadline = self._mono() + VERIFY_SECONDS
        good_rounds, last = 0, 'health was not confirmed within the allotted time'
        while True:
            if simulate_failure:
                return False, 'a deliberate check failure (rollback drill)'
            ok, why = self._healthy(since)
            if ok:
                good_rounds += 1
                if good_rounds >= 3:
                    return True, ''
            else:
                good_rounds, last = 0, why
            if self._mono() > deadline:
                return False, last
            self._sleep(VERIFY_INTERVAL)

    def _abort(self, job: Job, head: str, stamp: str, stage: str, new_files: tuple = ()) -> tuple[bool, str]:
        """Restore the code and images. stage: 'pre' (nothing changed), 'committed' (code changed,
        containers not yet),
        'restarted' (containers already on the new image). Does not raise: each step is separate,
        the result is (clean, why not)."""
        self.say(job, 'rollback', 'Rollback: restoring the code and the images…')
        problems = []
        try:
            res = self.git('reset', '--hard', head, timeout=60)
            if res.returncode != 0:
                problems.append(f'git reset: {_clip(res.err or res.out, 120)}')
        except Exception as exc:
            problems.append(f'git reset: {exc!r}')
        if new_files:                                  # new patch files (tests) that managed to appear but did not make it into the commit
            try:
                self.git('clean', '-f', '--', *new_files, timeout=60)
            except Exception as exc:
                problems.append(f'git clean: {exc!r}')
        if stage == 'pre':
            return not problems, '; '.join(problems)
        try:
            if not self._retag_back(stamp):
                problems.append('could not restore the image tags')
        except Exception as exc:
            problems.append(f'tags: {exc!r}')
        if stage == 'restarted':
            try:
                self._up()
                self.say(job, 'rollback-verify', 'Waiting for the site to come up on the previous image…')
                ok, why = self._verify(self.now() - timedelta(seconds=30), False)      # web starts within a minute: one request is not enough
                job.lines.append('Rollback finished, the site is fine.' if ok else f'Rollback finished, but the check says: {why}')
                if not ok:
                    problems.append(f'the site is unhealthy after the rollback: {why}')
            except Exception as exc:
                problems.append(f'restart on the old image: {exc!r}'[:200])
        return not problems, '; '.join(problems)

    def _do_deploy(self, job: Job) -> None:
        p = job.params
        text, digest = self.signed_artifact(job)
        head = self.head()
        report = self.check_policy(text, head)
        validated = self.state()['validated'].get(digest)
        if not validated:
            raise self.err('This patch has not passed the sandbox check — run codefix_validate first.', 'stale')
        if self.now() - datetime.fromisoformat(validated['at']) > VALIDATION_TTL:
            raise self.err('The check is out of date (over 6 h) — repeat it.', 'stale')
        if head != p['base_sha'] or head != validated['head']:
            raise self.err('The code on the server changed after the check — the patch is out of date.', 'stale')
        if p['drill'] and set(report.code_files) != DRILL_FILES:
            raise self.Error('The drill flag is allowed only for the training canary.')
        with DeployLock(self.autofix_dir / 'deploy.lock', self.Error):
            self._deploy_locked(job, p, text, digest, head, report)

    def _deploy_locked(self, job: Job, p: dict, text: str, digest: str, head: str, report) -> None:
        if self.paused:
            raise self.err('Auto-fixes are paused (PAUSE file).', 'wait')
        if not self.tree_clean():
            raise self.Error('The working tree on the server has uncommitted changes — the deployment was cancelled.')
        self._check_window_and_limits(p['by_owner'], p['drill'])
        self._preflight(job)
        images = self._service_images()
        for svc, (_cid, image_id) in images.items():
            latest = self.docker('image', 'inspect', '--format', '{{.Id}}', f'shop-{svc}:latest', timeout=30).out.strip()
            if latest != image_id:
                raise self.err(f'The tag shop-{svc}:latest does not match the running image: someone built an image without a restart — '
                               f'sort it out manually.', 'stale')
        self._image_matches_head(images['web'][1], report.code_files, head)
        data = self.state()
        data['attempts'] = [a for a in data['attempts'] if self.now() - datetime.fromisoformat(a) < timedelta(days=2)] + [self.now().isoformat()]
        data['validated'].pop(digest, None)                  # one verification - one rollout attempt
        self.save_state(data)
        stamp = self.now().strftime('%Y%m%d%H%M%S')
        inflight = self.autofix_dir / 'inflight.json'
        self.say(job, 'backup', 'Making a fresh database dump before the deployment…')
        dump = self.h._make_backup()
        job.lines.append(f'Dump: {dump.name}.')
        for svc, (_cid, image_id) in images.items():
            res = self.docker('tag', image_id, f'shop-{svc}:pre-autofix-{stamp}', timeout=30)
            if res.returncode != 0:
                raise self.Error(f'Could not tag the rollback image shop-{svc}:pre-autofix-{stamp}: {(res.err or res.out).strip()[:150]}')
        self.h._write_json(inflight, {'job': job.id, 'base_sha': head, 'stamp': stamp, 'at': self.now().isoformat(), 'attempts': 0})
        started = self.now()
        stage, commit, why, ok = 'pre', '', '', False
        new_files = tuple(c.path for c in self.policy.parse_patch(text) if c.is_new)
        try:
            self.say(job, 'apply', 'Applying the patch to the working tree…')
            patch_file = self.autofix_dir / f'{job.id}.patch'
            patch_file.write_text(text, encoding='utf-8', newline='\n')
            try:
                for args in (['apply', '--check', '--whitespace=nowarn', str(patch_file)], ['apply', '--whitespace=nowarn', str(patch_file)]):
                    res = self.git(*args)
                    if res.returncode != 0:
                        raise self.Error(f'The patch does not apply: {(res.err or res.out).strip()[:300]}')
            finally:
                patch_file.unlink(missing_ok=True)                 # the patch copy is needed only by git apply; it is kept in applied/ or rejected/
            stage = 'committed'
            try:
                self.policy.check_applied(text, self._reader(head), lambda rel: self._read_tree_file(self.h.project, rel))
            except self.policy.PolicyViolation as exc:
                raise self.Error(f'The structural check failed after applying the patch: {exc}')
            self.git('add', '--', *report.files)
            msg = f'{COMMIT_PREFIX}{p["patch_id"]}: {p["summary"]}'
            res = self.git('-c', f'user.name={BOT_NAME}', '-c', f'user.email={BOT_EMAIL}', 'commit', '-q', '-m', msg)
            if res.returncode != 0:
                raise self.Error(f'git commit failed: {(res.err or res.out).strip()[:200]}')
            commit = self.head()
            if self.paused:
                raise self.Error('Auto-fix was paused in the middle of the deployment (PAUSE file).')
            self.say(job, 'build', 'Building a thin layer on top of the running image…')
            self._build_overlay(job, f'shop-web:pre-autofix-{stamp}', report.files, commit)
            self.say(job, 'restart', 'Restarting web, scheduler and analyst…')
            stage = 'restarted'
            self._up()
            self.say(job, 'verify', 'Checking the health of the site (up to 3 minutes)…')
            ok, why = self._verify(started, p['simulate_failure'])
        except Exception as exc:
            reason = str(exc) if isinstance(exc, self.Error) else f'failure during the deployment: {exc!r}'[:300]
            job.lines.append(reason)
            clean, trouble = self._abort(job, head, stamp, stage, new_files)
            self._keep_safe(text, p['patch_id'], 'rejected')
            if clean:
                inflight.unlink(missing_ok=True)
            else:
                job.lines.append(f'The rollback is incomplete: {trouble}. The marker is kept — the helper will repeat it at start.')
            if stage == 'restarted':
                job.data.update(outcome='rolled_back' if clean else 'rollback_unhealthy', reason=reason[:300], commit=commit,
                                base_sha=head, patch_sha=digest, rollback_problems=trouble[:300])
                return
            raise self.err(reason, 'error')
        if not ok:
            clean, trouble = self._abort(job, head, stamp, 'restarted', new_files)
            self._keep_safe(text, p['patch_id'], 'rejected')
            if clean:
                inflight.unlink(missing_ok=True)
            job.data.update(outcome='rolled_back' if clean else 'rollback_unhealthy', reason=why[:300], commit=commit,
                            base_sha=head, patch_sha=digest, rollback_problems=trouble[:300])
            job.lines.append(f'The deployment was rolled back: {why}.')
            return
        inflight.unlink(missing_ok=True)                       # rollout finished and healthy: the marker is no longer needed
        try:
            data = self.state()
            data['applied'] = (data['applied'] + [{'id': p['patch_id'], 'commit': commit, 'at': self.now().isoformat(),
                                                   'summary': p['summary'], 'stamp': stamp, 'files': report.files}])[-30:]
            self.save_state(data)
            self._keep_safe(text, p['patch_id'], 'applied')
            self._prune_rollback_tags()
            self.refresh_ahead()
        except Exception as exc:
            print(f'helper: the deployment bookkeeping was not completed: {exc!r}', flush=True)
        job.data.update(outcome='deployed', commit=commit, base_sha=head, stamp=stamp, patch_sha=digest)
        job.lines.append(f'Deployed: commit {commit[:10]}, the site is fine.')

    def _build_overlay(self, job: Job, base_tag: str, files: list[str], commit: str) -> None:
        """A thin layer on top of the RUNNING image: only the changed files, no network, no build cache, no
floating dependencies."""
        ctx = self.autofix_dir / 'build' / job.id
        shutil.rmtree(ctx, ignore_errors=True)
        try:
            for rel in files:
                src = self.h.project / rel
                if src.is_symlink() or not src.is_file():
                    raise self.Error(f'No file for the layer: {rel}')
                dst = ctx / 'files' / rel[len('backend/'):]
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(src, dst)
            (ctx / 'Dockerfile').write_text(f'FROM {base_tag}\nCOPY files/ /app/\nLABEL shop.autofix.commit={commit}\n', encoding='utf-8')
            res = self.docker('build', '--network', 'none', '-t', 'shop-web:latest', str(ctx), timeout=BUILD_TIMEOUT)
            if res.returncode != 0:
                raise self.Error(f'The layer build failed: {(res.err or res.out).strip()[-300:]}')
            for svc in SERVICES[1:]:
                tag = self.docker('tag', 'shop-web:latest', f'shop-{svc}:latest', timeout=30)
                if tag.returncode != 0:
                    raise self.Error(f'Could not tag the image shop-{svc}:latest')
        finally:
            shutil.rmtree(ctx, ignore_errors=True)

    def _keep_safe(self, text: str, patch_id: str, folder: str) -> None:
        try:
            (self.autofix_dir / folder / f'{patch_id}.patch').write_text(text, encoding='utf-8', newline='\n')
        except OSError as exc:
            print(f'helper: could not save the patch {patch_id}: {exc!r}', flush=True)

    def _prune_rollback_tags(self) -> None:
        listing = self.docker('images', '--format', '{{.Repository}}:{{.Tag}}', timeout=60).out.split()
        for svc in SERVICES:
            tags = sorted(t for t in listing if re.fullmatch(rf'shop-{svc}:pre-autofix-\d{{14}}', t))
            for old in tags[:-KEEP_ROLLBACK_TAGS]:
                self.docker('rmi', old, timeout=60)

    # ── action: rollback of a specific auto-fix ─────────────────────────────
    def _do_revert(self, job: Job) -> None:
        with DeployLock(self.autofix_dir / 'deploy.lock', self.Error):
            self._revert_locked(job)

    def _revert_locked(self, job: Job) -> None:
        p = job.params
        data = self.state()
        applied = {a['commit']: a for a in data['applied']}
        if p['commit'] not in applied:
            raise self.Error('This is not an Analyst auto-commit (or its record was lost).')
        if self.paused:
            raise self.err('Auto-fixes are paused (PAUSE file).', 'wait')
        if not self.tree_clean():
            raise self.Error('The working tree has uncommitted changes — the revert was cancelled.')
        self._preflight(job)
        images = self._service_images()
        head = self.head()
        stamp = self.now().strftime('%Y%m%d%H%M%S')
        for svc, (_cid, image_id) in images.items():
            if self.docker('tag', image_id, f'shop-{svc}:pre-autofix-{stamp}', timeout=30).returncode != 0:
                raise self.Error(f'Could not tag the rollback image shop-{svc}.')
        inflight = self.autofix_dir / 'inflight.json'
        self.h._write_json(inflight, {'job': job.id, 'base_sha': head, 'stamp': stamp, 'at': self.now().isoformat(), 'attempts': 0})
        started = self.now()
        self.say(job, 'revert', f'Reverting commit {p["commit"][:10]}…')
        stage = 'pre'
        try:
            res = self.git('-c', f'user.name={BOT_NAME}', '-c', f'user.email={BOT_EMAIL}', 'revert', '--no-edit', p['commit'])
            if res.returncode != 0:
                self.git('revert', '--abort')
                raise self.Error(f'git revert failed: {(res.err or res.out).strip()[:200]}')
            stage = 'committed'
            touched = [f for f in applied[p['commit']].get('files', []) if f.startswith('backend/')]
            if not touched:                                # old record without a file list: take it from the commit itself
                names = self.git('diff-tree', '--no-commit-id', '--name-only', '-r', p['commit']).out.split()
                touched = [f for f in names if f.startswith('backend/')]
            self.say(job, 'build', 'Building a thin layer on top of the running image…')
            self._build_overlay(job, f'shop-web:pre-autofix-{stamp}', [f for f in touched if (self.h.project / f).is_file()], self.head())
            stage = 'restarted'
            self._up()
            ok, why = self._verify(started, False)
        except Exception as exc:
            clean, trouble = self._abort(job, head, stamp, stage)
            if clean:
                inflight.unlink(missing_ok=True)
            raise self.Error(f'The auto-fix revert failed, the previous state was restored: {exc}' + ('' if clean else f' ({trouble})'))
        if not ok:
            clean, trouble = self._abort(job, head, stamp, 'restarted')
            if clean:
                inflight.unlink(missing_ok=True)
            raise self.Error(f'The site is unhealthy after the auto-fix revert ({why}); the previous state was restored.')
        inflight.unlink(missing_ok=True)
        try:
            data['applied'] = [a for a in data['applied'] if a['commit'] != p['commit']]
            self.save_state(data)
            self.refresh_ahead()
        except Exception as exc:
            print(f'helper: the revert bookkeeping was not completed: {exc!r}', flush=True)
        job.data.update(outcome='reverted', commit=self.head())
        job.lines.append('The auto-fix was reverted, the site is fine.')

    # ── recovery after a failure ────────────────────────────────────────────
    def mark_interrupted(self) -> int:
        """There are no live tasks at startup: all results with status=running are a consequence of a restart.
Rewrite them as failed."""
        count = 0
        results = self.h.queue / 'results'
        regular, _junk = self.io.scan(results, '.json', limit=2000)
        for name in regular:
            data = self.h._read_json(results / name)
            if isinstance(data, dict) and data.get('status') == 'running' and data.get('mode') == 'job':
                data.update(status='failed', code='interrupted', error='The helper restarted in the middle of the task.')
                self.h._write_json(results / name, data)
                count += 1
        return count

    def _is_autofix_child(self, head: str, base: str) -> bool:
        res = self.git('log', '-1', '--format=%P%n%s', head)
        lines = res.out.splitlines()
        return res.returncode == 0 and len(lines) >= 2 and lines[0].strip() == base and lines[1].startswith(COMMIT_PREFIX)

    def recover_inflight(self) -> None:
        path = self.autofix_dir / 'inflight.json'
        info = self.h._read_json(path)
        if not info:
            return
        data = self.state()
        base, stamp = str(info.get('base_sha', '')), str(info.get('stamp', ''))
        if not SHA.match(base) or not re.fullmatch(r'\d{14}', stamp):
            path.unlink(missing_ok=True)
            return
        try:
            age = self.now() - datetime.fromisoformat(info['at'])
        except (KeyError, ValueError):
            age = timedelta(days=99)
        head = self.head()
        if age > RECOVER_MAX_AGE or not (head == base or self._is_autofix_child(head, base)):
            path.unlink(missing_ok=True)
            data['recovery'] = {'ok': None, 'skipped': 'the marker is old or the code changed — nothing is rolled back',
                                'at': self.now().isoformat()}
            self.save_state(data)
            print('helper: the unfinished deployment is out of date, no rollback is done', flush=True)
            return
        attempts = int(info.get('attempts', 0)) + 1
        if attempts > MAX_RECOVER_ATTEMPTS:
            path.unlink(missing_ok=True)
            data['recovery'] = {'ok': False, 'error': 'recovery failed after several attempts — check the site manually',
                                'at': self.now().isoformat()}
            self.save_state(data)
            return
        info['attempts'] = attempts
        self.h._write_json(path, info)
        print(f'helper: found an unfinished deployment {info.get("job")}, recovering (attempt {attempts})', flush=True)
        problems = []
        if head != base:
            if self.git('reset', '--hard', base, timeout=60).returncode != 0:
                problems.append('git reset')
        if not self._retag_back(stamp):
            problems.append('image tags')
        try:
            self._up()
        except Exception as exc:
            problems.append(f'up: {exc!r}'[:100])
        if not problems:
            ok, why = self._verify(self.now() - timedelta(seconds=30), False)
            if not ok:
                problems.append(f'the site is unhealthy: {why}')
        data = self.state()
        if problems:
            data['recovery'] = {'ok': False, 'error': '; '.join(problems)[:300], 'at': self.now().isoformat()}
            self.save_state(data)
            return
        path.unlink(missing_ok=True)
        data['recovery'] = {'ok': True, 'at': self.now().isoformat()}
        self.save_state(data)
