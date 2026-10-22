"""Collecting code errors from logs into the `ErrorEvent` table — the input for the Analyst's
auto-fixes.

How it works:
  * `ErrorEventHandler` is attached to the loggers in `config/settings.py` (web, scheduler,
    analyst). It sees
    only ERROR-level records with a traceback and NEVER raises an exception or delays a request: in
    the request thread
    it only does a CHEAP, bounded copy (type, truncated text, frames without line contents) and a
    write to a queue;
  * all the heavy work (reading code lines, sanitizing the text, fingerprint) and the DB write are
    done by a separate
    thread once every couple of seconds, over its own connection — so the write does not end up in
    the request's transaction and does not slow
    the response, and a long text from a client cannot hang a worker;
  * only the “SHAPE” of the error is stored: frames of our code (path in the repository, line,
    function, line text), without
    variables, headers or request body; the exception text is sanitized so that quoted values,
    e-mails, numbers and
    Postgres DETAIL disappear (only names from our code and built-in types remain);
  * noise (bots with a foreign Host, 404, dropped client connections) is not stored; environment
    failures (network, Redis,
    disk, dropped DB connection) are of kind `external`; `code` is only LOGIC errors
    (AttributeError, TypeError,
    KeyError, ValueError…), the only ones worth fixing with a code edit; the rest are `other` (a
    card without auto-fix).
"""
from __future__ import annotations

import builtins
import errno
import hashlib
import linecache
import logging
import queue
import re
import sys
import threading
import time
import traceback

THROTTLE_WINDOW = 2.0          # s: collapse repeats within this window into a single DB record
MAX_FRAMES = 12                # frames of our code in the stored traceback
CAPTURE_FRAMES = 60            # how many frames from the end are copied in the request thread
MAX_RECENT = 200
KEEP_SECONDS = 48 * 3600
KEEP_DAYS = 60                 # after how many days without repeats an event is deleted
QUEUE_SIZE = 500
RAW_MESSAGE = 1500             # characters of raw exception text that are taken at all
MESSAGE_LIMIT = 200            # characters in the stored text
QUOTE_SPAN = 200               # length of a quoted value that is still parsed

# Short exception names (any of the classes in the MRO) that are not worth attention: the error is
# not in our code.
NOISE = {
    'DisallowedHost', 'SuspiciousOperation', 'SuspiciousFileOperation', 'SuspiciousMultipartForm', 'BadRequest',
    'Http404', 'PermissionDenied', 'UnreadablePostError', 'RequestDataTooBig', 'TooManyFieldsSent',
    'MultiPartParserError', 'RawPostDataException', 'DisconnectedError', 'ClientDisconnect',
    'BrokenPipeError', 'ConnectionResetError', 'ConnectionAbortedError', 'CancelledError', 'KeyboardInterrupt',
    'SystemExit', 'GeneratorExit', 'StopAsyncIteration', 'InvalidHostHeader', 'ConnectionClosed',
    'ConnectionClosedError', 'ConnectionClosedOK', 'WebSocketDisconnect', 'StopConsumer', 'DenyConnection',
}
# An environment failure, not a code one: editing a file will not help here.
EXTERNAL = {
    'TimeoutError', 'ConnectionError', 'ConnectTimeout', 'ReadTimeout', 'Timeout', 'SSLError', 'RemoteDisconnected',
    'MaxRetryError', 'NewConnectionError', 'ProtocolError', 'OperationalError', 'InterfaceError', 'AdminShutdown',
    'ConnectionRefusedError', 'RedisError', 'BusyLoadingError', 'GaiError', 'gaierror', 'HTTPError', 'URLError',
    'MemoryError', 'APIConnectionError', 'RateLimitError', 'APIStatusError', 'APITimeoutError', 'IncompleteRead',
    'SMTPException', 'SMTPServerDisconnected', 'SMTPAuthenticationError', 'TimeoutExpired', 'BlockingIOError',
    'ResourceExhausted', 'ServiceUnavailable', 'DeadlineExceeded',
}
# Module prefixes (with the dot!) of external libraries: their exceptions are network/service
# failures.
EXTERNAL_MODULES = ('requests.', 'urllib3.', 'httpx.', 'redis.', 'socket.', 'ssl.', 'anthropic.', 'psycopg.',
                    'psycopg2.', 'claude_agent_sdk.', 'smtplib.', 'urllib.', 'http.client.', 'google.', 'websockets.',
                    'aiohttp.', 'asyncio.')
EXTERNAL_ERRNOS = {errno.ENOSPC, errno.EMFILE, errno.ENFILE, errno.ENOMEM, errno.EIO, errno.EROFS, errno.ENETUNREACH,
                   errno.EHOSTUNREACH, errno.ECONNREFUSED, errno.ETIMEDOUT, errno.ECONNRESET, errno.EPIPE,
                   getattr(errno, 'EDQUOT', -1)}
# Code LOGIC errors: AttributeError on None, wrong type, missing key… Only these are worth fixing by
# editing a file.
LOGIC = {
    'AttributeError', 'TypeError', 'KeyError', 'IndexError', 'ValueError', 'ZeroDivisionError', 'UnboundLocalError',
    'NameError', 'DoesNotExist', 'MultipleObjectsReturned', 'InvalidOperation', 'ValidationError', 'OverflowError',
    'LookupError', 'ArithmeticError', 'FieldError', 'FieldDoesNotExist', 'UnicodeError', 'UnicodeDecodeError',
    'UnicodeEncodeError', 'AssertionError', 'RecursionError', 'JSONDecodeError', 'ImportError', 'ModuleNotFoundError',
}

_CTRL = re.compile(r'[\x00-\x08\x0b-\x1f\x7f]')
# All regular expressions are linear (bounded quantifiers, no nested repeats) and are applied to
# text ≤ RAW_MESSAGE.
_EMAIL = re.compile(r'[\w.+-]{1,64}@[\w-]{1,63}(?:\.[\w-]{1,63}){1,5}')
_PHONE = re.compile(r'(?<![\w])\+?\d[\d\s().\-/]{6,20}\d')
_TOKEN = re.compile(r'[A-Za-z0-9_\-]{24,}')
_QUERY = re.compile(r'\?[^\s\'")]{0,200}')
_BIG_DIGITS = re.compile(r'\d{5,}')
_IDENT = re.compile(r'[A-Za-z_][A-Za-z0-9_]{0,60}\Z')
_WORDS = re.compile(r'[A-Za-z_][A-Za-z0-9_]*')
SAFE_WORDS = frozenset(dir(builtins)) | {'NoneType', 'Decimal', 'datetime', 'date', 'time', 'timedelta', 'UUID', 'QuerySet',
                                         'Manager', 'Q', 'F', 'OrderedDict', 'defaultdict'}


def scrub(text, limit: int = 200) -> str:
    """Truncates (BEFORE the regular expressions) and strips e-mails, phone numbers, long tokens and query
strings from the text."""
    text = _CTRL.sub(' ', str(text or '')[:max(limit * 4, 400)])
    text = _EMAIL.sub('<email>', text)
    text = _QUERY.sub('?…', text)
    text = _TOKEN.sub('<token>', text)
    text = _PHONE.sub('<number>', text)
    return ' '.join(text.split())[:limit]


def _mask_quotes(text: str, code_words: frozenset) -> str:
    """Replaces quoted values (name, address, key) with «…», leaving only names from our code and
    built-in types.
    Linear pass: an unpaired or overly long quote cuts off the rest of the text."""
    out, i, n = [], 0, len(text)
    while i < n:
        ch = text[i]
        if ch in '\'"`':
            j = text.find(ch, i + 1, i + QUOTE_SPAN + 2)
            if j == -1:
                out.append(ch + '…')
                break
            inner = text[i + 1:j]
            keep = bool(_IDENT.match(inner)) and (inner in SAFE_WORDS or inner in code_words)
            out.append(ch + (inner if keep else '…') + ch)
            i = j + 1
            continue
        out.append(ch)
        i += 1
    return ''.join(out)


def shape_message(message, code_words: frozenset = frozenset()) -> str:
    """The “shape” of exception text without values: no Postgres DETAIL, quoted values, e-mails, numbers or
long tokens."""
    text = _CTRL.sub(' ', str(message or '')[:RAW_MESSAGE])
    for marker in ('DETAIL:', 'Failing row', 'Key ('):
        cut = text.find(marker)
        if cut != -1:
            text = text[:cut] + '…'
    text = _mask_quotes(text[:600], code_words)
    text = _EMAIL.sub('<email>', text)
    text = _QUERY.sub('?…', text)
    text = _TOKEN.sub('<token>', text)
    text = _PHONE.sub('<number>', text)
    text = _BIG_DIGITS.sub('#', text)
    return ' '.join(text.split())[:MESSAGE_LIMIT]


def _shape_path(path_info: str, route: str) -> str:
    if route:
        return shape_message(route)[:150]
    parts = []
    for part in str(path_info or '')[:300].split('/'):
        keep = len(part) <= 32 and re.fullmatch(r'[a-z0-9_.\-]*', part) and not _BIG_DIGITS.search(part)
        parts.append(part if keep else '<…>')
    return '/'.join(parts)[:150]


def shape_path(request) -> str:
    """Route template (`api/products/<slug:slug>/reviews/`), not the actual address with client data."""
    if request is None:
        return ''
    match = getattr(request, 'resolver_match', None)
    route = str(getattr(match, 'route', '') or '') if match is not None else ''
    return _shape_path(str(getattr(request, 'path_info', '') or getattr(request, 'path', '') or ''), route)


def _service() -> str:
    argv = ' '.join(sys.argv)
    if 'run_scheduler' in argv:
        return 'scheduler'
    if 'run_analyst' in argv:
        return 'analyst'
    if 'run_advisor' in argv:
        return 'advisor'
    return 'web'


def _base_dir() -> str:
    from django.conf import settings
    return str(getattr(settings, 'BASE_DIR', '')).replace('\\', '/').rstrip('/')


def _names(exc) -> set[str]:
    return {c.__name__ for c in type(exc).__mro__}


def _is_external(exc, names: set[str]) -> bool:
    if names & EXTERNAL:
        return True
    module = (type(exc).__module__ or '') + '.'
    if any(module.startswith(p) for p in EXTERNAL_MODULES):
        return True
    return isinstance(exc, OSError) and getattr(exc, 'errno', None) in EXTERNAL_ERRNOS


def capture(exc_type, exc, tb, *, request=None) -> dict | None:
    """CHEAP copy in the request thread: reads nothing from disk, no regular expressions. None = noise."""
    if exc is None:
        return None
    names = _names(exc)
    if names & NOISE:
        return None
    frames = []
    for frame, lineno in traceback.walk_tb(tb):
        code = frame.f_code
        frames.append((code.co_filename, lineno, getattr(code, 'co_qualname', code.co_name)))
    try:
        message = str(exc)[:RAW_MESSAGE]
    except Exception:
        message = ''
    request_info = None
    if request is not None:
        match = getattr(request, 'resolver_match', None)
        request_info = (str(getattr(request, 'path_info', '') or getattr(request, 'path', '') or '')[:300],
                        str(getattr(match, 'route', '') or '')[:300] if match is not None else '')
    return {
        'exc_type': f'{exc_type.__module__}.{exc_type.__qualname__}'[:200],
        'exc_name': exc_type.__name__,
        'names': sorted(names),
        'external': _is_external(exc, names),
        'frames': frames[-CAPTURE_FRAMES:],
        'message': message,
        'request': request_info,
        'service': _service(),
    }


def process(raw: dict | None) -> dict | None:
    """The heavy part (writer thread): reads code lines, sanitizes the text, computes the fingerprint."""
    if raw is None:
        return None
    base = _base_dir()
    own = [(fn.replace('\\', '/'), lineno, name) for fn, lineno, name in raw['frames']
           if base and fn.replace('\\', '/').startswith(base + '/')
           and 'site-packages' not in fn and '/venv/' not in fn.replace('\\', '/')]
    names = set(raw['names'])
    external = raw['external'] or not own
    logic = bool(names & LOGIC)
    kind = 'external' if external else ('code' if logic else 'other')

    def repo_path(filename: str) -> str:
        return 'backend/' + filename[len(base) + 1:]

    source = [(fn, lineno, name, linecache.getline(fn, lineno).strip()) for fn, lineno, name in own[-MAX_FRAMES:]]
    code_words = frozenset(w for _fn, _l, _n, text in source for w in _WORDS.findall(text))
    message = shape_message(raw['message'], code_words)
    lines = []
    for fn, lineno, name, text in source:
        lines.append(f'{repo_path(fn)}:{lineno} in {name}')
        if text:
            lines.append('    ' + scrub(text, 160))
    lines.append(f'{raw["exc_name"]}: {message}')
    inner = own[-1] if own else None
    if kind == 'external':
        fingerprint = hashlib.sha1(f'{raw["exc_type"]}|external'.encode()).hexdigest()[:20]
    else:
        fingerprint = hashlib.sha1(f'{raw["exc_type"]}|{repo_path(inner[0])}|{inner[2]}'.encode()).hexdigest()[:20]
    path = _shape_path(*raw['request']) if raw['request'] is not None else ''
    return {
        'fingerprint': fingerprint,
        'kind': kind,
        'service': raw['service'],
        'exc_type': raw['exc_type'],
        'message': message,
        'where_path': repo_path(inner[0]) if kind != 'external' else '',
        'where_func': inner[2][:120] if kind != 'external' else '',
        'where_line': inner[1] if kind != 'external' else None,
        'request_path': path,
        'traceback': '\n'.join(lines),
    }


def describe(exc_type, exc, tb, *, request=None) -> dict | None:
    """Sanitized error description, or None if it is noise (convenience wrapper over capture + process)."""
    return process(capture(exc_type, exc, tb, request=request))


# ── write to the DB ──────────────────────────────────────────────────────────

def persist(info: dict, hits: int = 1, now=None) -> None:
    """Writes/updates ErrorEvent. Called from the background thread (or directly in tests)."""
    from django.db import IntegrityError, transaction
    from django.utils import timezone
    from analyst.models import ErrorEvent
    now = now or timezone.now()
    stamp = int(now.timestamp())
    fields = ('kind', 'service', 'exc_type', 'message', 'where_path', 'where_func', 'where_line', 'request_path', 'traceback')
    for _attempt in range(3):
        try:
            with transaction.atomic():
                event = ErrorEvent.objects.select_for_update().filter(fingerprint=info['fingerprint']).first()
                if event is None:
                    event = ErrorEvent(fingerprint=info['fingerprint'], count=0, recent=[], first_seen=now)
                for field in fields:                       # current appearance of the error (code lines changed between releases)
                    setattr(event, field, info.get(field))
                event.count += hits
                event.last_seen = now
                if event.status == ErrorEvent.FIXED:
                    event.status = ErrorEvent.OPEN         # 'fixed', but it happened again
                cutoff = stamp - KEEP_SECONDS
                recent = [t for t in (event.recent or []) if t >= cutoff] + [stamp] * min(hits, 50)
                event.recent = recent[-MAX_RECENT:]
                event.save()
            return
        except IntegrityError:
            continue                                       # another process has just created this fingerprint: update it


def purge(now=None) -> int:
    """Deletes events with no repeats for more than KEEP_DAYS days (except those currently being fixed)."""
    from datetime import timedelta
    from django.utils import timezone
    from analyst.models import ErrorEvent
    cutoff = (now or timezone.now()) - timedelta(days=KEEP_DAYS)
    deleted, _ = ErrorEvent.objects.filter(last_seen__lt=cutoff).exclude(status=ErrorEvent.FIXING).delete()
    return deleted


_queue: queue.Queue = queue.Queue(maxsize=QUEUE_SIZE)
_worker: threading.Thread | None = None
_worker_lock = threading.Lock()
SYNC = False       # tests turn this on: write in the same transaction, without a thread


def _drain(seed: dict) -> dict:
    """Collapses the queue's repeats by fingerprint: {fingerprint: [info, hits]}."""
    batch: dict[str, list] = {}

    def add(raw):
        try:
            info = process(raw)
        except Exception as exc:                           # a bad record must not bring down the whole batch
            print(f'analyst.errorlog: could not parse the error: {exc!r}'[:300], file=sys.stderr, flush=True)
            return
        if info is not None:
            slot = batch.setdefault(info['fingerprint'], [info, 0])
            slot[1] += 1

    add(seed)
    while True:
        try:
            add(_queue.get_nowait())
        except queue.Empty:
            return batch


def _work() -> None:
    from django.db import close_old_connections
    while True:
        try:
            first = _queue.get()
            time.sleep(THROTTLE_WINDOW)
            for info, hits in _drain(first).values():
                try:
                    close_old_connections()
                    persist(info, hits)
                except Exception as exc:     # DB unavailable or the table does not exist yet — don't stay completely silent, but don't crash either
                    print(f'analyst.errorlog: could not record the error: {exc!r}'[:300], file=sys.stderr, flush=True)
        except Exception as exc:
            print(f'analyst.errorlog: thread failure: {exc!r}'[:300], file=sys.stderr, flush=True)
            time.sleep(5)


def _ensure_worker() -> None:
    global _worker
    if _worker is not None and _worker.is_alive():
        return
    with _worker_lock:
        if _worker is None or not _worker.is_alive():
            _worker = threading.Thread(target=_work, name='analyst-errorlog', daemon=True)
            _worker.start()


class ErrorEventHandler(logging.Handler):
    """Attached in LOGGING. Raises no exceptions, waits for nothing and in the request thread does only a
cheap copy."""

    def __init__(self, level=logging.ERROR):
        super().__init__(level=level)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            if not record.exc_info or record.exc_info[0] is None:
                return
            raw = capture(record.exc_info[0], record.exc_info[1], record.exc_info[2],
                          request=getattr(record, 'request', None))
            if raw is None:
                return
            if SYNC:
                persist(process(raw))
                return
            _queue.put_nowait(raw)
            _ensure_worker()
        except Exception:
            pass
