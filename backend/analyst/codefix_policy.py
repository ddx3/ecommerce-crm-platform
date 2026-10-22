"""Auto-fix policy: what the Analyst IS ALLOWED to change in the code by itself, and what it may never
change.

This is the single source of the rules. Two parties read it:
  * the Django part (`analyst/codefix/*`) — early rejection, before the sandbox;
  * the helper on the VPS (`ops/shop_helper.py`) — the AUTHORITATIVE check using its own copy of
    this file, which
    `ops/install.sh` puts in /opt/shop-helper outside git. The agent can't reach that copy, and the
    file itself
    lies in `analyst/`, which the policy forbids changing.

The file uses the standard library only (the host has no Django). The rules are in code, not in the
prompt: the agent's prompt
can be fooled by error text, code verification can't.

Principle: default-deny. Only what is listed in ALLOW_CODE / ALLOW_TESTS is allowed; everything else
is rejected.
The check has two layers:
  1. text layer: files, limits, forbidden words in the ADDED lines (after Unicode NFKC
     normalization);
  2. STRUCTURAL layer (`check_structure`, the ast module): compares the old and new code of a file
     and forbids NEW risky
     constructs — dynamic name resolution, writing to attributes of foreign objects
     (monkey-patching; foreign objects include
     parameters whose default value is a class, and the function parameters themselves), methods
     that write to the DB through a relation manager
     (`obj.variants.update(…)`), new imports outside the whitelist (each imported name is checked
     too), access to other
     apps and personal data, swallowing exceptions, removal or change of existing guard checks and
     decorators.
     For ACCESS checks, DATA FLOW is checked as well: you can't change how a value that ends up in
     one is computed (`owns = … or
     request.GET…`), change a helper function out of its condition, or add an early return next to
     it that returns something other than a constant.
     Regular expressions don't catch this: string concatenation bypasses them.
"""
from __future__ import annotations

import ast
import difflib
import hashlib
import io
import re
import tokenize
import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

POLICY_VERSION = 2
BOM = chr(0xFEFF)

# ── Limits ────────────────────────────────────────────────────────────────────
MAX_CODE_FILES = 3          # code files (not tests) in a single fix
MAX_CODE_LINES = 60         # added + removed lines in code files
MAX_TEST_FILES = 2
MAX_TEST_LINES = 200
MAX_PATCH_BYTES = 60_000
MAX_LINE_CHARS = 400        # a longer line never occurs in added code: there is nothing to hide in it

# ── Where code may be changed ────────────────────────────────────────────────
# Paths from the repository root, with forward slashes. Deliberately narrow: the storefront and
# utility helper modules,
# and NOT payments/orders (orders), authorization (users), settings (config), models, signals,
# migrations,
# stock, customer emails, keys, the Analyst itself, the Telegram bot, templates and the frontend.
ALLOW_CODE = [re.compile(p) for p in (
    r'backend/catalog/(api_views|serializers|views|flora_feed|description_images|image_standardize|'
    r'printout_match|printout_thumb|llm_moderation|widgets|context_processors)\.py',
    r'backend/catalog/instruction_(?!agent\.py)[a-z_]+\.py',   # except instruction_agent.py: it assembles the LLM agent's permissions (tools, permission_mode)
    r'backend/blog/(api_views|serializers|views)\.py',
    r'backend/courses/(api_views|serializers)\.py',
    r'backend/core/(jsonutils|media|robots|sitemaps|admin_filters|embeddings)\.py',
    r'backend/core/canary\.py',                       # training canary for drills, not called in production code
)]
ALLOW_TESTS = [re.compile(p) for p in (
    r'backend/(catalog|blog|courses|core)/tests[a-z0-9_]*\.py',
)]

# Storefront files that decide WHAT is visible and to WHOM (API, serializers, CRM views, feed,
# moderation, media, sitemap, embeddings):
# the Analyst prepares fixes in them all the way through (sandbox, two reviews), but rolls them out
# ONLY after the owner's 'Apply', even in auto mode.
# Static analysis cannot prove that a change to such code is safe (as the many policy checks show):
# the final say belongs to a human, and
# only helper modules with no access logic and no writes (formatting, images, parsing, the canary)
# are rolled out automatically, without a human.
OWNER_ONLY = [re.compile(p) for p in (
    r'backend/(catalog|blog|courses)/(api_views|serializers|views)\.py',
    r'backend/catalog/(flora_feed|llm_moderation|context_processors)\.py',
    r'backend/catalog/instruction_[a-z_]+\.py',          # doses and composition in bundle instructions: a wrong change harms customers
    r'backend/core/(sitemaps|embeddings|media)\.py',
)]

# New files are allowed only for tests. Existing tests must not be modified.

# ── Text layer: forbidden constructs in ADDED lines ───────────────────
# (regular expression, explanation for the owner/agent). All expressions are linear and run on a
# line of at most MAX_LINE_CHARS.
_CODE_BANNED = [
    (r'\bsubprocess\b|\bos\.(system|popen|exec\w*|spawn\w*|fork|kill|_exit)\b|\bpty\b', 'running commands'),
    (r'\b(eval|exec|compile)\s*\(|\b__import__\b|__(builtins|globals|subclasses|class|code|dict|loader|spec)__',
     'dynamic code execution'),
    (r'\b(socket|urllib|urllib3|requests|httpx|aiohttp|http\.client|smtplib|ftplib|telnetlib|paramiko|websockets?)\b',
     'network'),
    (r'\b(pickle|marshal|ctypes|cffi|importlib|shelve|dill|builtins|signal|inspect|gc|tempfile|pkgutil|runpy|zipimport)\b',
     'dangerous modules'),
    (r'\b(import|from)\s+(os|sys)\b|(?<![\w.])(os|sys)\s*\.', 'system access'),
    (r'''\.open\s*\(\s*['"][^'"]*[wax+]''', 'writing to a file'),
    (r'\b(threading|multiprocessing|concurrent\.futures|asyncio\.create_subprocess\w*)\b', 'threads and processes'),
    (r'\bshutil\.\w+|\.(unlink|rmdir|rmtree|chmod|chown|write_text|write_bytes|read_text|read_bytes)\s*\(|\bos\.(remove|unlink|'
     r'rmdir|removedirs|rename|replace|chmod|chown|makedirs|mkdir|environ|putenv|getenv|chdir)\b', 'file system and environment'),
    (r'''\bopen\s*\(''', 'opening files'),
    (r'''['"]/(proc|etc|dev|sys|root|var|run|opt|home|usr|bin|tmp|mnt)\b''', 'system directory paths'),
    (r'(secret|api_?key|apikey|token|passw(or)?d|private_?key|credential|authorization|bearer)|\bDATABASES\b|\.env\b',
     'keys and secrets'),
    (r'\.(delete|raw|extra|execute|executemany|bulk_create|bulk_update|update_or_create|get_or_create|create|save|truncate)\s*\(|'
     r'\bcursor\s*\(|django\.db\s+import[^\n]*\bconnections?\b|\bTRUNCATE\b|\bDROP\s+(TABLE|DATABASE)\b|\)\s*\.update\s*\(|'
     r'\bobjects\s*\.update\s*\(|\.update\s*\(\s*(\w+\s*=[^=]|\*\*|$)', 'writing and deleting data'),
    (r'\bsend_mail\b|\bsend_mass_mail\b|\bEmailMessage\b|\bEmailMultiAlternatives\b', 'e-mails to customers'),
    (r'\bmark_safe\b|\|\s*safe\b|\bSafeString\b|\bcsrf_exempt\b|\bAllowAny\b|\bpermission_classes\b|\bIsAdminUser\b|'
     r'\bIsAuthenticated\b|\blogin_required\b|\bstaff_member_required\b|\buser_passes_test\b|\bis_staff\b|\bis_superuser\b|'
     r'\bhas_perm\b|\bauthentication_classes\b|\bthrottle\w*\b|\bowner_only\b', 'authorization, permissions and abuse protection'),
    (r'\blogging\.disable\b|\.setLevel\s*\(|\.disabled\b|\bfilterwarnings\b|\bsimplefilter\b|\bglobal\s+\w|\bnonlocal\b',
     'disabling logs and global state'),
    (r'(^|\s)except\s*:|except\s+(Exception|BaseException)\b', 'too broad except'),
    (r'coding\s*[:=]\s*[-\w.]+', 'file encoding declaration (PEP 263)'),
]
# In tests the same 'escapes' apply, but without the ORM/secrets/network rules (a test runs against
# a temporary database with no network).
_TEST_BANNED = [
    (r'\bsubprocess\b|\bos\.(system|popen|exec\w*|spawn\w*|fork|kill|_exit)\b', 'running commands'),
    (r'\b(eval|exec|compile)\s*\(|\b__import__\b', 'dynamic code execution'),
    (r'\b(socket|ctypes|cffi|pickle|marshal|tempfile|importlib|builtins)\b', 'dangerous modules'),
    (r'\bshutil\b|\bos\.(remove|unlink|rmdir|removedirs|environ|getenv|putenv|chdir)\b|\.(unlink|rmdir|write_text|write_bytes)\s*\(',
     'file system'),
    (r'''\.open\s*\(\s*['"][^'"]*[wax+]''', 'writing to a file'),
    (r'''\bopen\s*\([^)]*,\s*(mode\s*=\s*)?['"][^'"]*[wax+][^'"]*['"]''', 'writing to a file'),
    (r'\bSystemExit\b|\bsys\.(exit|stdout|stderr)\b|\bprint\s*\(|\bTextTestResult\b|\bTextTestRunner\b', 'replacing output and terminating tests'),
    (r'coding\s*[:=]\s*[-\w.]+', 'file encoding declaration (PEP 263)'),
]
CODE_BANNED = [(re.compile(p, re.I), why) for p, why in _CODE_BANNED]
TEST_BANNED = [(re.compile(p), why) for p, why in _TEST_BANNED]

# Lines that must NOT be removed (weakening protection or the log trail), even if nothing dangerous
# is being added.
# This is a coarse fallback guard: the structural layer guards more precisely (R7: decorators, early
# exits, function names).
PROTECTED_REMOVALS = re.compile(
    r'permission|authenticat|authoriz|login_required|staff_member_required|user_passes_test|csrf|is_staff|is_superuser|has_perm|'
    r'throttle|owner_only|authentication_classes|is_active|is_approved|is_published|is_visible|is_public|is_preview|'
    r'\buser\b|\.user_id|\bowner|customer|purchas|enroll|\bsession\b|'               # filtering data by owner: `.filter(user=request.user)`
    r'raise\s+(PermissionDenied|Http404|SuspiciousOperation)|status\s*=\s*(401|403|404)|HTTP_40[134]|'
    r'compare_digest|hmac|escape|strip_tags|bleach|sanitiz|'
    r'\b(log|logger|logging|_log|_logger)\.(exception|error|critical|warning)\b', re.I)


class PolicyViolation(Exception):
    """The text is shown to the owner and returned to the agent as the reason for the rejection."""


@dataclass
class Hunk:
    old_start: int
    old_len: int
    lines: list = field(default_factory=list)       # [(tag ' ' | '-' | '+', text)]


@dataclass(eq=False)
class FileChange:
    path: str
    is_new: bool = False
    added: list = field(default_factory=list)
    removed: list = field(default_factory=list)
    raw: list = field(default_factory=list)       # this file's lines in the patch (to split the patch without re-parsing)
    hunks: list = field(default_factory=list)

    @property
    def lines(self) -> int:
        return len(self.added) + len(self.removed)


@dataclass
class Report:
    files: list
    code_files: list
    test_files: list
    code_lines: int
    test_lines: int
    tests_expected: int = 0       # how many test methods the patch adds (the host checks it against «Ran N tests»)
    owner_only: list = field(default_factory=list)      # code files whose rollout only the owner decides


# ── Path classification ───────────────────────────────────────────────────────

def normalize(path: str) -> str:
    """Path relative to the repository root, or an empty string if it is suspicious (escapes the
repository)."""
    if not isinstance(path, str) or not path or '\x00' in path or '\\' in path:
        return ''
    if path.startswith('/') or re.match(r'^[A-Za-z]:', path):
        return ''
    parts = path.split('/')
    if any(p in ('', '.', '..') for p in parts) or any(p.startswith('.') for p in parts):
        return ''
    return '/'.join(parts)


def owner_only(path: str) -> bool:
    """Whether only the owner decides the rollout of changes in this file (see OWNER_ONLY)."""
    return any(rx.fullmatch(normalize(path)) for rx in OWNER_ONLY)


def needs_owner(patch: str) -> bool:
    """Whether the patch touches a file whose rollout only the owner decides. A patch that cannot be parsed
is also handed to a human."""
    try:
        return any(owner_only(c.path) for c in parse_patch(patch))
    except PolicyViolation:
        return True


def classify(path: str) -> str | None:
    """'code' | 'test' | None (None = the Analyst cannot change this file)."""
    path = normalize(path)
    if not path or '/migrations/' in path:
        return None
    if any(rx.fullmatch(path) for rx in ALLOW_TESTS):
        return 'test'
    if any(rx.fullmatch(path) for rx in ALLOW_CODE):
        return 'code'
    return None


def repo_path_from_traceback(filename: str) -> str:
    """'/app/catalog/views.py' (path in the container) → 'backend/catalog/views.py' (path in the
repository)."""
    if isinstance(filename, str) and filename.startswith('/app/'):
        return normalize('backend/' + filename[len('/app/'):])
    return ''


def is_fixable_file(filename: str) -> bool:
    return classify(repo_path_from_traceback(filename)) == 'code'


def app_of(path: str) -> str:
    parts = path.split('/')
    return parts[1] if len(parts) > 2 and parts[0] == 'backend' else ''


# ── Whether a file is fit for editing ──────────────────────────────────────
MAX_FILE_BYTES = 400_000


def file_problem(text: str) -> str:
    """Why a file cannot be edited automatically (empty string = it can). All our tools (diff, patch
    parsing, git apply)
    split text into lines only by \\n, while the Python tokenizer also treats \\r as a line break,
    so CR, NUL and a missing
    trailing \\n make a file unfit. U+2028/U+2029 and BOM are harmless in existing code (in ADDED
    lines they are forbidden)."""
    if len(text.encode('utf-8', 'replace')) > MAX_FILE_BYTES:
        return 'the file is too large'
    if '\r' in text:
        return 'the file has Windows line endings (CR)'
    if '\x00' in text:
        return 'the file contains NUL'
    if text and not text.endswith('\n'):
        return 'the file has no trailing newline'
    return ''


def diff_text(path: str, old: str, new: str) -> str:
    """Unified diff in which lines are separated ONLY by \\n (unlike str.splitlines, which also splits
    on U+2028, \x0c, \x85…).
    This is exactly the parsing that parse_patch/apply_hunks and git apply assume. old='' means a
    new file."""
    for label, text in (('old', old), ('new', new)):
        if text and not text.endswith('\n'):
            raise PolicyViolation(f'{path}: {label} content without a trailing newline.')
    a = old.split('\n')[:-1] if old else []
    b = new.split('\n')[:-1] if new else []
    out = list(difflib.unified_diff(a, b, '/dev/null' if not old else 'a/' + path, 'b/' + path, lineterm=''))
    return ('\n'.join(out) + '\n') if out else ''


# ── Patch parsing ─────────────────────────────────────────────────────────────
_HUNK = re.compile(r'^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@')


def parse_patch(text: str) -> list[FileChange]:
    """Strict unified diff parsing (only `--- a/…`/`+++ b/…` and hunks). Anything else is rejected:
    renames, mode changes, binary data, symlinks, file deletions, «No newline at end of file»."""
    if not isinstance(text, str) or not text.strip():
        raise PolicyViolation('Empty patch.')
    if len(text.encode('utf-8', 'replace')) > MAX_PATCH_BYTES:
        raise PolicyViolation(f'The patch is too large (over {MAX_PATCH_BYTES // 1000} KB).')
    if '\r' in text:
        raise PolicyViolation('The patch contains a carriage return (CR): not supported.')
    lines = text.split('\n')
    if lines and lines[-1] == '':
        lines.pop()
    changes: list[FileChange] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if not line.startswith('--- '):
            raise PolicyViolation('Unexpected line in the patch. Only ordinary unified-diff edits are allowed.')
        start = i
        old = line[4:].strip()
        if i + 1 >= len(lines) or not lines[i + 1].startswith('+++ '):
            raise PolicyViolation('"---" must be followed by "+++".')
        new = lines[i + 1][4:].strip()
        i += 2
        is_new = old == '/dev/null'
        if new == '/dev/null':
            raise PolicyViolation('Deleting files is forbidden.')
        if not new.startswith('b/') or (not is_new and old != 'a/' + new[2:]):
            raise PolicyViolation('The paths in the patch header must be a/<path> and b/<the same path>.')
        change = FileChange(path=new[2:], is_new=is_new)
        while i < len(lines) and lines[i].startswith('@@'):
            m = _HUNK.match(lines[i])
            if not m:
                raise PolicyViolation('Corrupted hunk header.')
            old_left = int(m.group(2) if m.group(2) is not None else 1)
            new_left = int(m.group(4) if m.group(4) is not None else 1)
            hunk = Hunk(old_start=int(m.group(1)), old_len=old_left)
            i += 1
            while old_left > 0 or new_left > 0:
                if i >= len(lines):
                    raise PolicyViolation('The patch is cut off in the middle of a hunk.')
                body = lines[i]
                tag, content = body[:1], body[1:]
                if tag == ' ':
                    old_left, new_left = old_left - 1, new_left - 1
                elif tag == '-':
                    old_left -= 1
                    change.removed.append(content)
                elif tag == '+':
                    new_left -= 1
                    change.added.append(content)
                elif body == '':
                    tag, content = ' ', ''
                    old_left, new_left = old_left - 1, new_left - 1     # empty context line without a space
                else:
                    raise PolicyViolation('A line that is not allowed inside a hunk.')
                if old_left < 0 or new_left < 0:
                    raise PolicyViolation('The number of lines in the hunk does not match the header.')
                hunk.lines.append((tag, content))
                i += 1
            if i < len(lines) and lines[i].startswith('\\'):
                raise PolicyViolation('A file without a trailing newline is not supported.')
            change.hunks.append(hunk)
        if not change.hunks:
            raise PolicyViolation(f'There is no hunk for {change.path}.')
        change.raw = lines[start:i]
        changes.append(change)
    return changes


def apply_hunks(old_text: str, hunks: list) -> str:
    """Applies hunks to the text (pure-Python implementation). Context and removed lines must match
    verbatim,
    and the position must match the hunk header (no 'floating' search, which `git apply` could make
    use of)."""
    if '\r' in old_text:
        raise PolicyViolation('The file contains CR (Windows line endings): not supported.')
    ends_nl = old_text.endswith('\n') or old_text == ''
    lines = old_text.split('\n')
    if old_text.endswith('\n') or old_text == '':
        lines.pop()
    out, pos = [], 0
    for hunk in hunks:
        target = hunk.old_start if hunk.old_len == 0 else hunk.old_start - 1
        if target < pos or target > len(lines):
            raise PolicyViolation('The patch hunks overlap or go beyond the file.')
        out.extend(lines[pos:target])
        pos = target
        for tag, content in hunk.lines:
            if tag == '+':
                out.append(content)
                continue
            if pos >= len(lines) or lines[pos] != content:
                raise PolicyViolation('The patch does not match the current content of the file (the context differs).')
            if tag == ' ':
                out.append(content)
            pos += 1
    out.extend(lines[pos:])
    return '\n'.join(out) + ('\n' if out and ends_nl else '')


# ── Text layer ────────────────────────────────────────────────────────────

def _bad_char(text: str) -> str:
    for ch in text:
        cat = unicodedata.category(ch)
        if cat in ('Cf', 'Co', 'Cn', 'Cs', 'Zl', 'Zp') or (cat == 'Cc' and ch != '\t'):
            return f'U+{ord(ch):04X}'
    return ''


def _scan(lines: list[str], banned, path: str, verb: str) -> None:
    for text in lines:
        if len(text) > MAX_LINE_CHARS:
            raise PolicyViolation(f'{path}: {verb} line is too long (over {MAX_LINE_CHARS} characters).')
        bad = _bad_char(text)
        if bad:
            raise PolicyViolation(f'{path}: {verb} line contains an invisible or control Unicode character ({bad}).')
        norm = unicodedata.normalize('NFKC', text)          # Python itself folds identifiers to NFKC: «ｏｓ» = «os»
        for candidate in ((text, norm) if norm != text else (text,)):
            for rx, why in banned:
                if rx.search(candidate):
                    raise PolicyViolation(f'{path}: {verb} line with a forbidden construct ({why}): "{text.strip()[:80]}".')


def check_added(kind: str, path: str, lines: list[str]) -> None:
    """Early check of just-added lines (for the agent's tools), before the patch is assembled."""
    _scan(lines, CODE_BANNED if kind == 'code' else TEST_BANNED, path, 'added')


# ── Structural layer (AST) ────────────────────────────────────────────────
# Calls forbidden everywhere (tests too).
CALL_NAMES = {'setattr', 'delattr', 'vars', 'globals', 'locals', 'exec', 'eval', 'compile', '__import__', 'open', 'input',
              'breakpoint', 'import_string', 'import_module', 'print'}
# Additionally forbidden only in code (in tests get_user_model()/get_model() is routine).
CALL_NAMES_CODE = {'dir', 'get_model', 'get_user_model', 'receiver'}
# References to these names (without a call: reduce(getattr, …)) are also forbidden.
REF_NAMES = {'getattr', 'hasattr', 'setattr', 'delattr', 'vars', 'globals', 'locals', 'exec', 'eval', 'compile', '__import__',
             'open', 'import_string', 'import_module'}
ATTR_CALLS = {'import_string', 'import_module', 'attrgetter', 'methodcaller', 'system', 'popen', 'exec_module', 'load_module',
              'write_text', 'write_bytes', 'unlink', 'rmdir', 'rmtree', 'chmod', 'chown', 'mro', '__getattribute__',
              '__subclasses__'}
ATTR_CALLS_CODE = {'get_model', 'get_user_model', 'read_text', 'read_bytes', 'delete', 'raw', 'extra', 'execute', 'executemany',
                   'bulk_create', 'bulk_update', 'update_or_create', 'get_or_create', 'create', 'save', 'truncate', 'cursor',
                   'send_mail', 'send_mass_mail', 'mark_safe',
                   # signals, registries, messages, cache and internal ORM writers: everything that
                   # leaves a trace outside the function
                   'send', 'send_robust', 'connect', 'disconnect', 'register', 'unregister', 'add_to_class', 'contribute_to_class',
                   'set_many', 'delete_many', 'get_or_set', 'incr', 'decr', 'get_compiler', 'execute_sql', 'delete_batch',
                   '_raw_delete', '_insert', '_update', '_batched_insert', '_do_insert', '_do_update', '_save_table', '_save_parents'}
# Methods that WRITE to the database via a relation manager (`obj.variants.update(stock=0)`,
# `obj.categories.clear()`) or to the cache/session, but
# are harmless on ordinary Python collections. Allowed only on local 'simple' collections and in the
# form `dict.update(other_dict)`.
MUTATORS = {'update', 'add', 'remove', 'clear', 'set'}
MUTATOR_CALLS = MUTATORS | {'append', 'extend', 'insert', 'pop', 'popitem', 'setdefault', 'discard', 'sort', 'reverse'}
PLAIN_CTORS = {'dict', 'set', 'list', 'frozenset', 'sorted', 'tuple', 'defaultdict', 'OrderedDict', 'Counter', 'deque', 'Q'}
LOG_SUPPRESS = {'disable', 'setLevel', 'filterwarnings', 'simplefilter', 'removeHandler', 'addFilter', 'addHandler'}
LOG_OWNERS = {'logging', 'logger', 'log', 'warnings', '_logger', 'LOGGER', '_log'}
LOG_ATTRS = {'handlers', 'propagate', 'loggerDict'}
SAFE_DUNDER = {'__name__', '__doc__', '__qualname__', '__module__', '__init__', '__str__', '__repr__', '__eq__', '__hash__',
               '__len__', '__iter__', '__contains__', '__call__', '__enter__', '__exit__'}
DUNDER_IN_STRING = re.compile(r'__[A-Za-z_]+__')
BANNED_NAMES = {'os', 'sys', 'subprocess', 'builtins', '__builtins__', 'socket', 'requests', 'urllib', 'urllib3', 'httpx',
                'shutil', 'pickle', 'marshal', 'ctypes', 'importlib', 'threading', 'multiprocessing', 'signal', 'gc',
                'inspect', 'tempfile', 'pathlib', 'Path', 'ssl', 'smtplib', 'asyncio', 'pkgutil'}
# New imports: only these (plus anything the file ALREADY imported verbatim before).
ALLOWED_IMPORTS = ('re', 'json', 'math', 'decimal', 'datetime', 'itertools', 'functools', 'collections', 'typing', 'logging',
                   'html', 'string', 'textwrap', 'unicodedata', 'uuid', 'copy', 'enum', 'django.utils',
                   'django.http', 'django.core.exceptions', 'django.shortcuts', 'django.urls', 'django.template.defaultfilters',
                   'rest_framework.response', 'rest_framework.exceptions', 'rest_framework.status',
                   'django.db.models.functions')
ALLOWED_EXACT = ('django.db.models',)        # Q/F/Count… themselves; submodules (sql, deletion, query) are not
# Both the module itself and each imported name are checked (`from django.db import connection` =
# django.db.connection).
DENIED_IMPORTS = ('django.utils.module_loading', 'django.utils.autoreload', 'django.utils.safestring', 'django.utils.crypto',
                  'django.utils.log', 'django.utils.connection', 'django.core.management', 'django.db.connection',
                  'django.db.connections', 'django.db.transaction', 'django.db.models.sql', 'django.db.models.deletion',
                  'django.db.models.query', 'django.db.backends', 'django.db.migrations', 'django.dispatch',
                  'core.llm', 'core.limits', 'core.email', 'core.antibot', 'core.shell', 'core.seo', 'orders', 'users',
                  'telegrambot', 'chat', 'analyst', 'config')
# Operator calls that raise an exception themselves if something is wrong
# (`_require_owner(request.user, course)`): also 'protective'
CHECK_CALL = re.compile(r'^_*(check|require|ensure|assert|verify|validate|authori[sz]e|authenticate|enforce|guard|deny|forbid|refuse|restrict|'
                        r'protect|abort)|perm|auth|owner', re.I)
# Imports from our apps: new ones only models, serializers and a couple of utility modules.
# Everything else (stock_audit, notifications,
# signals, revert, video_access…) consists of functions with side effects that a fix has no right to
# pull in. Modules the file already
# imports remain available.
LOCAL_APPS = ('catalog', 'courses', 'blog', 'core')
PROJECT_APPS = LOCAL_APPS + ('analyst', 'orders', 'users', 'chat', 'telegrambot', 'config', 'formats')
LOCAL_SAFE = ('catalog.models', 'courses.models', 'blog.models', 'catalog.serializers', 'courses.serializers', 'blog.serializers',
              'core.jsonutils', 'core.media')
# CALL ALLOWLIST in added code (R14): new calls only to built-in functions and methods of 'pure'
# types, functions of modules on the
# import allowlist, whatever the file ALREADY called, and functions defined by the fix itself. An
# unknown method on a foreign object
# (`variant.restock(100)`, `notification.dispatch()`) is rejected: such methods with side effects
# cannot be enumerated for the project,
# while the list of safe ones is short.
SAFE_NAME_CALLS = frozenset({
    'len', 'str', 'int', 'float', 'bool', 'list', 'dict', 'set', 'tuple', 'frozenset', 'sorted', 'reversed', 'min', 'max', 'sum', 'abs',
    'round', 'any', 'all', 'isinstance', 'issubclass', 'enumerate', 'zip', 'range', 'map', 'filter', 'repr', 'format', 'divmod', 'pow',
    'ord', 'chr', 'bytes', 'iter', 'next', 'hash', 'callable', 'slice', 'super', 'getattr', 'hasattr', 'object', 'complex',
    'property', 'staticmethod', 'classmethod'})
SAFE_EXC_SUFFIX = ('Error', 'Exception', 'Warning')
SAFE_METHODS = frozenset({
    # strings
    'strip', 'lstrip', 'rstrip', 'lower', 'upper', 'title', 'capitalize', 'swapcase', 'casefold', 'split', 'rsplit', 'splitlines',
    'join', 'replace', 'startswith', 'endswith', 'find', 'rfind', 'index', 'rindex', 'count', 'format', 'format_map', 'encode',
    'decode', 'isdigit', 'isalpha', 'isalnum', 'isnumeric', 'isdecimal', 'isspace', 'islower', 'isupper', 'istitle', 'isidentifier',
    'isascii', 'zfill', 'ljust', 'rjust', 'center', 'partition', 'rpartition', 'translate', 'maketrans', 'expandtabs', 'removeprefix',
    'removesuffix',
    # lists, dicts, sets (writes to them are cut off by rules R11/R2d)
    'get', 'items', 'keys', 'values', 'copy', 'append', 'extend', 'insert', 'pop', 'popitem', 'sort', 'reverse', 'setdefault',
    'union', 'intersection', 'difference', 'symmetric_difference', 'issubset', 'issuperset', 'isdisjoint', 'fromkeys', 'most_common',
    'add', 'discard', 'remove', 'clear', 'update', 'set',
    # numbers, Decimal, math
    'quantize', 'normalize', 'is_finite', 'is_nan', 'is_infinite', 'to_integral_value', 'as_tuple', 'sqrt', 'ln', 'log10', 'exp',
    'compare', 'bit_length', 'is_integer', 'as_integer_ratio', 'floor', 'ceil', 'isfinite', 'isnan', 'isinf', 'fabs', 'trunc', 'log',
    'log2', 'gcd', 'hypot',
    # re, json
    'sub', 'subn', 'match', 'search', 'fullmatch', 'findall', 'finditer', 'escape', 'group', 'groups', 'groupdict', 'start', 'end',
    'span', 'loads', 'dumps',
    # date and time
    'now', 'utcnow', 'today', 'date', 'time', 'isoformat', 'strftime', 'strptime', 'timestamp', 'astimezone', 'weekday', 'isoweekday',
    'total_seconds', 'fromisoformat', 'fromtimestamp', 'combine', 'utcoffset', 'tzname', 'timetuple', 'toordinal',
    # reading from the database (writes are covered by separate rules)
    'filter', 'exclude', 'all', 'none', 'first', 'last', 'exists', 'order_by', 'values_list', 'distinct', 'annotate', 'aggregate',
    'select_related', 'prefetch_related', 'only', 'defer', 'iterator', 'in_bulk', 'get_queryset', 'refresh_from_db',
    'desc', 'asc', 'nulls_first', 'nulls_last',
    # requests, responses, serializers, log
    'is_valid', 'build_absolute_uri', 'get_host', 'get_full_path', 'getlist', 'urlencode', 'to_representation', 'to_internal_value',
    'debug', 'info', 'warning', 'error', 'exception', 'critical', 'getLogger', 'isEnabledFor',
    # utility
    'setter', 'getter', 'deleter', '__init__'})
SAFE_METHOD_PREFIX = ('get_', 'is_', 'has_', 'can_')          # reading by name: get_stock(), is_available(), has_images()
# ...but not when the name suggests a write or an action: get_or_create_cart(), is_paid_then_ship(),
# get_and_set_stock()
WRITEY_WORDS = frozenset({
    'create', 'update', 'delete', 'remove', 'save', 'set', 'add', 'send', 'sync', 'reset', 'clear', 'mark', 'apply', 'write', 'push',
    'publish', 'notify', 'dispatch', 'commit', 'flush', 'invalidate', 'touch', 'activate', 'deactivate', 'cancel', 'pay', 'charge',
    'refund', 'reserve', 'assign', 'trigger', 'fire', 'emit', 'enqueue', 'schedule', 'increment', 'decrement', 'restock', 'adjust',
    'move', 'merge', 'purge', 'wipe', 'reindex', 'rebuild', 'register', 'subscribe', 'persist', 'execute', 'exec', 'insert', 'upsert',
    'ship', 'kill', 'retry', 'resend', 'resync', 'attach', 'detach', 'grant', 'revoke', 'approve', 'reject', 'submit', 'enable',
    'disable', 'toggle', 'bump', 'lock', 'unlock', 'restore', 'clone', 'promote', 'broadcast', 'confirm', 'cancel', 'enqueue'})


class _Writey:
    """`WRITEY.search(name)`: whether the method name contains a word that sounds like a write or an action
(get_or_create_cart → create)."""

    @staticmethod
    def search(name: str) -> bool:
        return bool(set(name.lower().strip('_').split('_')) & WRITEY_WORDS)


WRITEY = _Writey()
# Functions that call whatever is passed to them: positions of call arguments (map(obj.m, xs),
# partial(obj.m), iter(obj.m, 0)…) and `key=`
HOF_ARGS = {'map': (0,), 'filter': (0,), 'reduce': (0,), 'partial': (0,), 'partialmethod': (0,), 'starmap': (0,), 'takewhile': (0,),
            'dropwhile': (0,), 'filterfalse': (0,), 'iter': (0,), 'defaultdict': (0,), 'cmp_to_key': (0,), 'accumulate': (1,)}
HOF_KEY = frozenset({'sorted', 'min', 'max', 'groupby', 'nlargest', 'nsmallest'})
# ORM queries: field names must be LITERALS in the patch itself (R14b). A field name taken from a
# query string or a dict amounts to reading arbitrary data.
ORM_LOOKUP = frozenset({'filter', 'exclude', 'get', 'annotate', 'alias', 'aggregate', 'order_by', 'values', 'values_list', 'only', 'defer',
                        'select_related', 'prefetch_related', 'distinct', 'latest', 'earliest', 'get_object_or_404', 'get_list_or_404'})
ORM_FIELD_LISTS = frozenset({'order_by', 'values', 'values_list', 'only', 'defer', 'select_related', 'prefetch_related', 'distinct',
                             'latest', 'earliest'})
DB_EXPR_FIELD = frozenset({'F', 'Count', 'Sum', 'Avg', 'Min', 'Max', 'Coalesce', 'Lower', 'Upper', 'Length', 'Cast', 'OuterRef', 'Prefetch',
                           'StdDev', 'Variance', 'Concat', 'Substr', 'Trim', 'Greatest', 'Least'})
DB_EXPR = DB_EXPR_FIELD | {'Q'}
# STRONG access words: if a function's INNER check (or the direct assignments to its variables)
# contains such words, the function
# is automatically not edited at all (R7e) — it is access code, and humans edit it. No
# 'token'/'signature': there they concern text parsing.
STRONG_ACL = re.compile(r'(\buser|authent|authori[sz]|\bperm|staff|\bowner|\bowns?\b|\bsession|\bpaid\b|purchas|enroll|hmac|csrf|\blogin|'
                        r'cookie|secret|passw|\brole\b|\badmin\b)', re.I)
# Words by which a check is considered an ACCESS check (see check_structure/R7b-R7d)
ACL_WORDS = re.compile(r'(user|auth|perm|owner|\bowns?\b|staff|admin|session|token|access|paid|purchas|enroll|allow|\bcan_|\bhas_|publish|'
                       r'visible|public|private|hidden|signature|hmac|csrf|\brole|subscri|premium|buyer|login|cookie|is_active|\bactive\b)', re.I)
# LANGUAGE DATA (Ukrainian): `закуп` ('purchasing', as in закупівля) and `собівартіст` ('cost price', as in
# собівартість) are Ukrainian stems of the shop's commercial terms; they are matched in identifiers and
# strings next to the English words for the same notions (supplier, wholesale, purchase_price, cost_price).
SECRET_WORDS = re.compile(r'(secret|passw|token|credential|private_?key|api_?key|apikey|authorization|bearer|database|\bdsn\b|salt|'
                          r'supplier|wholesale|purchase_?price|cost_?price|margin|закуп|собівартіст)', re.I)
PII_WORDS = re.compile(r'(e-?mail|phone|\btel\b|telephone|address|iban|card_?num|birth|customer|delivery|recipient|viber|telegram|'
                       r'ip_?addr|session_?key|passport|full_?name|first_?name|last_?name|surname|patronymic|nova_?poshta|\bnp_|'
                       r'remote_?addr|x_?forwarded|user_?agent|guest_?(name|email|phone)|client_?ip|\bip\b)', re.I)
QS_METHODS = {'filter', 'exclude', 'all', 'annotate', 'order_by', 'select_related', 'prefetch_related', 'values', 'values_list',
              'distinct', 'only', 'defer', 'none', 'using', 'select_for_update'}
NOOP_STMTS = (ast.Pass, ast.Continue)
STORED_CALLABLE_CTORS = {'staticmethod', 'classmethod', 'property', 'cached_property', 'partial', 'partialmethod', 'MethodType',
                         'wraps', 'lru_cache', 'cache'}


def _root(node):
    while isinstance(node, (ast.Attribute, ast.Subscript, ast.Call)):
        node = node.value if isinstance(node, (ast.Attribute, ast.Subscript)) else node.func
    return node


def _chain_depth(node) -> int:
    """How many links (.attribute / [index]) lie between the write target and the root name: `a.b.c` → 2."""
    depth = 0
    while isinstance(node, (ast.Attribute, ast.Subscript)):
        depth += 1
        node = node.value
    return depth


def _only_subscripts(node) -> bool:
    """The chain from the write target to the root consists of indexes only (`out['a']['b'] = …`), with no
attributes."""
    while isinstance(node, (ast.Attribute, ast.Subscript)):
        if isinstance(node, ast.Attribute):
            return False
        node = node.value
    return True


def _orm_literal(node) -> bool:
    """An ORM query argument in which field names are written as literals: 'name', '-price', F('x'),
Count('y'), F('a') * 2, Q(x=1) | Q(y=2)."""
    if isinstance(node, ast.Constant):
        return True
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd, ast.Invert)):
        return _orm_literal(node.operand)
    if isinstance(node, ast.BinOp):
        return _orm_literal(node.left) and _orm_literal(node.right)
    if isinstance(node, (ast.Tuple, ast.List)):
        return all(_orm_literal(e) for e in node.elts)
    if isinstance(node, ast.Call):
        f = node.func
        if isinstance(f, ast.Name) and f.id == 'Q':          # the value in Q(x=value) is data; field names are the keyword argument names
            return all(k.arg is not None for k in node.keywords) and all(_orm_literal(a) for a in node.args)
        if isinstance(f, ast.Name) and f.id in DB_EXPR_FIELD:
            return (all(_orm_literal(a) for a in node.args)
                    and all(k.arg is not None and (k.arg in ('output_field', 'filter', 'distinct', 'default') or _orm_literal(k.value))
                            for k in node.keywords))
        if isinstance(f, ast.Attribute) and f.attr in ('desc', 'asc', 'nulls_first', 'nulls_last'):
            return _orm_literal(f.value)
    return False


def _is_plain_expr(node) -> bool:
    """An expression that yields a NEW plain Python collection or value (not an ORM object, not a class,
not foreign state)."""
    if isinstance(node, (ast.List, ast.Dict, ast.Set, ast.Tuple, ast.ListComp, ast.DictComp, ast.SetComp, ast.GeneratorExp,
                         ast.JoinedStr, ast.Constant)):
        return True
    return isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in PLAIN_CTORS


def _literal_like(node) -> bool:
    """A value from which protected data cannot be extracted: a constant or a collection of constants
(variables, calls, queries are not)."""
    if isinstance(node, ast.Constant):
        return True
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd, ast.Not)):
        return _literal_like(node.operand)
    if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        return all(_literal_like(e) for e in node.elts)
    if isinstance(node, ast.Dict):
        return all(k is not None and _literal_like(k) for k in node.keys) and all(_literal_like(v) for v in node.values)
    if isinstance(node, ast.JoinedStr):
        return not any(isinstance(v, ast.FormattedValue) for v in node.values)
    if isinstance(node, ast.Attribute):
        return isinstance(node.value, ast.Name) and node.value.id == 'status' and node.attr.isupper()
    if isinstance(node, ast.Call):
        name = node.func.id if isinstance(node.func, ast.Name) else node.func.attr if isinstance(node.func, ast.Attribute) else ''
        if name in ('Response', 'JsonResponse', 'HttpResponse') or name.startswith('HttpResponse') or name == 'redirect':
            for kw in node.keywords:
                if kw.arg == 'status' and _error_status(kw.value):
                    # error response (4xx/5xx): the body is a literal or validation errors
                    # (`serializer.errors`), but not arbitrary data
                    return all(_literal_like(a) or (isinstance(a, ast.Attribute) and a.attr == 'errors') for a in node.args)
            if name.startswith('HttpResponse') and any(w in name for w in ('BadRequest', 'NotFound', 'Forbidden', 'NotAllowed', 'ServerError', 'Gone')):
                return True
            return all(_literal_like(a) for a in node.args) and all(_literal_like(k.value) for k in node.keywords)
    return False


def _error_status(node) -> bool:
    if isinstance(node, ast.Constant):
        return isinstance(node.value, int) and not isinstance(node.value, bool) and node.value >= 400
    return (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == 'status'
            and (node.attr.startswith('HTTP_4') or node.attr.startswith('HTTP_5')))


def _returns_in(stmts):
    """Return statements in a statement list (without nested functions, classes and nested ifs — those are
checked separately)."""
    stack = list(stmts)
    while stack:
        n = stack.pop()
        if isinstance(n, ast.Return):
            yield n
        elif not isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda, ast.If)):
            stack.extend(ast.iter_child_nodes(n))


def _is_qs_expr(node) -> bool:
    while isinstance(node, (ast.Attribute, ast.Call, ast.Subscript)):
        if isinstance(node, ast.Attribute) and node.attr == 'objects':
            return True
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in QS_METHODS:
            return True
        node = node.value if isinstance(node, (ast.Attribute, ast.Subscript)) else node.func
    return False


def _imports_of(tree, app: str = '') -> set:
    """Modules the file imports (absolute names; `from .models import X` in the catalog app gives
`catalog.models`)."""
    out = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            out.update(a.name for a in n.names)
        elif isinstance(n, ast.ImportFrom):
            if not n.level:
                if n.module:
                    out.add(n.module)
                    out.update(f'{n.module}.{a.name}' for a in n.names)
            elif n.level == 1 and app:
                base = app + ('.' + n.module if n.module else '')
                out.add(base)
                out.update(f'{base}.{a.name}' for a in n.names)
    return out


def _callables_of(tree) -> tuple:
    """(names the file calls, defines or imports; names of methods the file calls or defines) — for the R14
allowlist."""
    names, attrs = set(), set()
    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(n.name)
            attrs.add(n.name)
        elif isinstance(n, ast.alias):
            names.add((n.asname or n.name).split('.')[0])
        elif isinstance(n, ast.Call):
            if isinstance(n.func, ast.Name):
                names.add(n.func.id)
            elif isinstance(n.func, ast.Attribute):
                attrs.add(n.func.attr)
    return names, attrs


class _Scope:
    """Scope: kind, local names (everything bound in the body, plus parameters), parameters, 'fresh'
    names (always hold a NEW value:
    writing to them is safe), query names and names bound to lambdas."""
    __slots__ = ('kind', 'local', 'params', 'fresh', 'qs', 'lambdas')

    def __init__(self, kind: str, names=()):
        self.kind = kind
        self.local = set(names)
        self.params = set(names) if kind == 'func' else set()
        self.fresh: dict = {}                  # name → 'plain' (new collection or value) | 'obj' (new instance/query)
        self.qs: set = set()
        self.lambdas: set = set()


def _model_names(tree, app: str) -> frozenset:
    """Names imported from the allowlisted models and serializers of our apps (everywhere in the file,
including inside functions)."""
    out = set()

    def safe(full: str) -> bool:
        return any(full == m or full.startswith(m + '.') for m in LOCAL_SAFE)

    defined = {n.name for n in ast.walk(tree) if isinstance(n, ast.ClassDef)}
    for n in ast.walk(tree):
        if isinstance(n, ast.ImportFrom):
            if n.level == 1 and app:
                base = app + ('.' + n.module if n.module else '')
            elif not n.level and n.module:
                base = n.module
            else:
                continue
            for a in n.names:
                if safe(f'{base}.{a.name}') or safe(base):
                    out.add(a.asname or a.name)
        elif isinstance(n, ast.Import):
            for a in n.names:
                if safe(a.name):
                    out.add((a.asname or a.name).split('.')[0])
    return frozenset(out - defined)


def _callback_funcs(tree) -> dict:
    """{function name: (call argument positions, parameter names) that it CALLS ITSELF}: passing a foreign method there means calling
    it (`def _run(fn, *args): return fn(*args)`; `_run(obj.restock, 1)`)."""
    out: dict = {}
    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            a = n.args
            params = [x.arg for x in a.posonlyargs + a.args]
            invoked = {c.func.id for c in ast.walk(n) if isinstance(c, ast.Call) and isinstance(c.func, ast.Name) and c.func.id in params}
            if invoked:
                shift = 1 if params and params[0] in ('self', 'cls') else 0
                positions, names = out.setdefault(n.name, (set(), set()))
                positions.update(params.index(x) - shift for x in invoked if params.index(x) - shift >= 0)
                names.update(invoked)
    return out


def _param_defaults(a) -> dict:
    """{parameter name: default value expression or None}."""
    pos = a.posonlyargs + a.args
    out = {x.arg: None for x in pos + a.kwonlyargs + [v for v in (a.vararg, a.kwarg) if v]}
    for x, default in zip(pos[len(pos) - len(a.defaults):], a.defaults):
        out[x.arg] = default
    for x, default in zip(a.kwonlyargs, a.kw_defaults):
        if default is not None:
            out[x.arg] = default
    return out


# Built-in functions that return a NEW scalar value (foreign state cannot be kept on it); min/max
# return one of their arguments, so they do not qualify.
SCALAR_CALLS = frozenset({'str', 'int', 'float', 'bool', 'len', 'round', 'abs', 'sum', 'repr', 'ord', 'chr', 'format', 'divmod', 'pow', 'hash'})
INSTANCE_ACCESSORS = frozenset({'get', 'first', 'last', 'latest', 'earliest'})
ITER_PASSTHROUGH = frozenset({'list', 'tuple', 'set', 'frozenset', 'sorted', 'reversed', 'enumerate', 'iter', 'zip', 'filter'})
QS_CHAIN = QS_METHODS | INSTANCE_ACCESSORS | {'get_queryset', 'in_bulk', 'count', 'exists'}


def _scope_nodes(nodes):
    """All nodes of a function body, excluding nested functions, classes and lambdas (they have their own
names)."""
    stack = list(nodes)
    while stack:
        n = stack.pop()
        yield n
        for c in ast.iter_child_nodes(n):
            if not isinstance(c, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
                stack.append(c)


def _qs_like(value, qs_names, model_names=frozenset()) -> bool:
    """A query or an instance from it that ALWAYS originates from the manager of an allowlisted
    imported model (`Model.objects…`, `qs.filter(…)`,
    `qs.first()`), or a name holding such a query. The form `something.filter(…)` proves nothing by
    itself: a patch may define its own
    class with an `all()` method that returns a foreign object — so the root must be a model, not
    just any object."""
    node = value
    while True:
        if isinstance(node, ast.Name):
            return node.id in qs_names
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in QS_CHAIN:
            node = node.func.value
            continue
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in ('get_object_or_404', 'get_list_or_404'):
            first = node.args[0] if node.args else None
            root = _root(first) if first is not None else None
            return isinstance(root, ast.Name) and root.id in model_names
        if isinstance(node, ast.Subscript):
            node = node.value
            continue
        if isinstance(node, ast.Attribute) and node.attr in ('objects', '_default_manager', '_base_manager'):
            root = _root(node.value)
            return isinstance(root, ast.Name) and root.id in model_names
        return False


def _instance_expr(value, qs_names, model_names=frozenset()) -> bool:
    return _qs_like(value, qs_names, model_names)


def _fresh_kind(value, fresh: dict, qs_names, model_names=frozenset()):
    """'plain' | 'obj' | None: whether the value is a NEW object that can be written to without
    touching classes, modules or global state.
    Fresh ones are only: literals and comprehensions, collection constructors, instances
    (`Class(…)`, query rows), scalar results."""
    if value is None:
        return None
    if isinstance(value, tuple):                       # ('iter', expression): an iteration element
        it = value[1]
        if _qs_like(it, qs_names, model_names) or (isinstance(it, ast.Call) and isinstance(it.func, ast.Name) and it.func.id in ITER_PASSTHROUGH
                                                   and any(_qs_like(a, qs_names, model_names) for a in it.args)):
            return 'obj'
        if isinstance(it, ast.Call) and isinstance(it.func, ast.Name) and it.func.id == 'range':
            return 'plain'
        return None
    if _is_plain_expr(value):
        return 'plain'
    if isinstance(value, ast.Name):
        return fresh.get(value.id)
    if isinstance(value, ast.Lambda):
        return 'obj'
    if isinstance(value, ast.Compare) or (isinstance(value, ast.UnaryOp) and isinstance(value.op, ast.Not)):
        return 'plain'
    if isinstance(value, ast.BinOp):
        kinds = (_fresh_kind(value.left, fresh, qs_names, model_names), _fresh_kind(value.right, fresh, qs_names, model_names))
        return 'plain' if kinds == ('plain', 'plain') else None
    if isinstance(value, (ast.IfExp, ast.BoolOp)):
        parts = [value.body, value.orelse] if isinstance(value, ast.IfExp) else list(value.values)
        kinds = [_fresh_kind(p, fresh, qs_names, model_names) for p in parts]
        return None if None in kinds else ('plain' if all(k == 'plain' for k in kinds) else 'obj')
    if isinstance(value, ast.Call):
        f = value.func
        if isinstance(f, ast.Name) and f.id in SCALAR_CALLS:
            return 'plain'
        if isinstance(f, ast.Name) and f.id[:1].isupper():
            return 'obj'                                # a class constructor yields a new instance
        if _qs_like(value, qs_names, model_names):
            return 'obj'
    return None


def _analyze_locals(body, params, plain_params=(), model_names=frozenset()):
    """Facts about a function's names (ignoring execution order — more reliable than tracking the
    flow):
    (all names bound in it; which of them ALWAYS hold a fresh value; which are queries; which are
    lambdas).
    A parameter is a foreign object passed by the call (it may be a class or a module), so it is
    never fresh; `*args`/`**kwargs` are containers
    created by the call itself."""
    bind: dict = {}

    def add(name, value):
        bind.setdefault(name, []).append(value)

    def target(t, value):
        if isinstance(t, ast.Name):
            add(t.id, value)
        elif isinstance(t, ast.Starred):
            target(t.value, None)
        elif isinstance(t, (ast.Tuple, ast.List)):
            pair = (isinstance(value, (ast.Tuple, ast.List)) and len(value.elts) == len(t.elts)
                    and not any(isinstance(e, ast.Starred) for e in list(t.elts) + list(value.elts)))
            for i, e in enumerate(t.elts):
                target(e, value.elts[i] if pair else None)

    for p in params:
        add(p, None)
    for p in plain_params:
        add(p, ast.Dict(keys=[], values=[]))
    for n in _scope_nodes(body):
        if isinstance(n, ast.Assign):
            for t in n.targets:
                target(t, n.value)
        elif isinstance(n, ast.AnnAssign):
            if n.value is not None:
                target(n.target, n.value)
        elif isinstance(n, (ast.For, ast.AsyncFor, ast.comprehension)):
            target(n.target, ('iter', n.iter))
        elif isinstance(n, (ast.With, ast.AsyncWith)):
            for it in n.items:
                if it.optional_vars is not None:
                    target(it.optional_vars, None)
        elif isinstance(n, ast.NamedExpr):
            target(n.target, n.value)
        elif isinstance(n, ast.ExceptHandler) and n.name:
            add(n.name, None)
        elif isinstance(n, ast.AugAssign) and isinstance(n.target, ast.Name):
            bind.setdefault(n.target.id, [])

    qs_names = {name for name, vals in bind.items() if vals}
    changed = True
    while changed:
        changed = False
        for name in list(qs_names):
            if not all(isinstance(v, ast.AST) and _qs_like(v, qs_names, model_names) for v in bind[name]):
                qs_names.discard(name)
                changed = True
    fresh = {name: 'plain' for name, vals in bind.items() if vals}
    changed = True
    while changed:
        changed = False
        for name, vals in bind.items():
            if name not in fresh:
                continue
            kinds = [_fresh_kind(v, fresh, qs_names, model_names) for v in vals]
            if None in kinds:
                del fresh[name]
                changed = True
            else:
                kind = 'plain' if all(k == 'plain' for k in kinds) else 'obj'
                if fresh[name] != kind:
                    fresh[name] = kind
                    changed = True
    lambdas = {name for name, vals in bind.items() if vals and all(isinstance(v, ast.Lambda) for v in vals)}
    return set(bind) | set(params), fresh, qs_names, lambdas


class _Gate(ast.NodeVisitor):
    """Collects a file's 'risky signatures'. New code is acceptable only if the set of signatures has not
grown."""

    def __init__(self, kind: str, file_imports: set, app: str, old_calls: tuple | None = None):
        self.kind, self.app, self.file_imports = kind, app, file_imports
        self.code = kind == 'code'
        self.old_calls = old_calls if self.code else None       # (names, methods) of the old file; None — the call allowlist is off
        self.found: Counter = Counter()
        self.scopes: list = []                 # _Scope stack
        self.docstrings: set = set()
        self.call_funcs: set = set()           # ids of nodes sitting in the function position of a call
        self.func_names: set = set()           # names of all functions and classes in the file
        self.imported_ok: set = set()          # names imported from allowed modules
        self.imported_listed: set = set()      # of those, only the allowlisted ones (not merely 'the file already imported this')
        self.imported_modules: set = set()     # of those, modules (`import re`, `from django.utils import timezone`): their functions are safe
        self.model_names: frozenset = frozenset()   # names of allowlisted models/serializers (root of `Model.objects…`)
        self.callback_funcs: dict = {}         # file functions that call their own parameters: name → positions of such parameters

    def add(self, rule: str, node, text: str = '') -> None:
        self.found[(rule, text or ast.unparse(node)[:100])] += 1

    # -- scopes
    def _enter(self, kind, names=()):
        self.scopes.append(_Scope(kind, names))

    def _enter_func(self, names, body, args) -> None:
        self._enter('func', names)
        s = self.scopes[-1]
        plain = [x.arg for x in (args.vararg, args.kwarg) if x]
        local, s.fresh, s.qs, s.lambdas = _analyze_locals(body, [n for n in names if n not in plain], plain, self.model_names)
        s.local |= local

    def _plain_receiver(self, value) -> bool:
        """Whether the expression is a NEW local collection (`.add/.update/.append/.clear` on it are
        harmless; on a foreign object, an ORM manager,
        cache, session or settings they are not)."""
        if isinstance(value, ast.Name):
            s = self.scopes[-1]
            return s.kind == 'func' and s.fresh.get(value.id) == 'plain'
        return _is_plain_expr(value)

    def _method_refs(self, value) -> tuple:
        """Attribute names that the expression points to as a REFERENCE to a method: `obj.m`, `a if c else
obj.m`, `(obj.a, obj.b)`, `getattr(o, 'm')`."""
        if isinstance(value, ast.Attribute):
            return (value.attr,)
        if isinstance(value, ast.IfExp):
            return self._method_refs(value.body) + self._method_refs(value.orelse)
        if isinstance(value, ast.BoolOp):
            return tuple(r for v in value.values for r in self._method_refs(v))
        if isinstance(value, (ast.Tuple, ast.List, ast.Set)):
            return tuple(r for e in value.elts for r in self._method_refs(e))
        if (isinstance(value, ast.Call) and isinstance(value.func, ast.Name) and value.func.id in ('getattr', 'hasattr')
                and len(value.args) >= 2 and isinstance(value.args[1], ast.Constant) and isinstance(value.args[1].value, str)):
            return (value.args[1].value,)
        return ()

    # -- module: what runs at import
    def visit_Module(self, node):
        self._enter('module')
        self.func_names = {n.name for n in ast.walk(node) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))}
        self.model_names = _model_names(node, self.app) - self.func_names
        self.callback_funcs = _callback_funcs(node)
        for st in node.body:
            if isinstance(st, ast.Expr) and isinstance(st.value, ast.Constant):
                self.docstrings.add(id(st.value))
                continue
            if isinstance(st, (ast.Import, ast.ImportFrom, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Pass)):
                continue
            if isinstance(st, ast.Assign) and all(isinstance(t, ast.Name) for t in st.targets):
                continue
            if isinstance(st, ast.AnnAssign) and isinstance(st.target, ast.Name):
                continue
            if isinstance(st, ast.If) and ast.unparse(st.test).startswith(('TYPE_CHECKING', '__name__')):
                continue
            self.add('R1-code-at-import', st)
        self.generic_visit(node)

    def visit_ClassDef(self, node):
        for d in node.decorator_list:
            if not isinstance(d, ast.Call):
                self._callee(d, d)
        self._enter('class')
        for st in node.body:
            if isinstance(st, ast.Expr) and isinstance(st.value, ast.Constant):
                self.docstrings.add(id(st.value))
        self._ident(node.name, node)
        self.generic_visit(node)
        self.scopes.pop()

    def _annotations(self, node) -> None:
        """Annotations are evaluated when the function is defined (before Python 3.14 — immediately): the
expression in them is executed like any other."""
        a = node.args
        for x in a.posonlyargs + a.args + a.kwonlyargs + [v for v in (a.vararg, a.kwarg) if v]:
            if x.annotation is not None:
                self.visit(x.annotation)
        if node.returns is not None:
            self.visit(node.returns)
        for tp in getattr(node, 'type_params', None) or ():
            for attr in ('bound', 'default_value'):
                inner = getattr(tp, attr, None)
                if inner is not None:
                    self.visit(inner)

    def _func(self, node):
        a = node.args
        names = [x.arg for x in a.posonlyargs + a.args + a.kwonlyargs] + [x.arg for x in (a.vararg, a.kwarg) if x]
        for n in names:
            self._ident(n, node)
        self._ident(node.name, node)
        if node.body and isinstance(node.body[0], ast.Expr) and isinstance(node.body[0].value, ast.Constant):
            self.docstrings.add(id(node.body[0].value))
        for d in node.decorator_list:
            self._decorator(d)
        for default in a.defaults + [d for d in a.kw_defaults if d is not None]:
            self.visit(default)
        self._annotations(node)
        self._enter_func(names, node.body, a)
        for st in node.body:
            self.visit(st)
        self.scopes.pop()

    visit_FunctionDef = visit_AsyncFunctionDef = _func

    def _decorator(self, d) -> None:
        """A decorator runs at import: `@something` is a call. A call `@something(…)` is checked by visit_Call,
a bare `@something` — here."""
        if not isinstance(d, ast.Call):
            self._callee(d, d)
        self.visit(d)

    def visit_Lambda(self, node):
        a = node.args
        for default in a.defaults + [d for d in a.kw_defaults if d is not None]:
            self.visit(default)
        self._enter_func([x.arg for x in a.posonlyargs + a.args + a.kwonlyargs] + [x.arg for x in (a.vararg, a.kwarg) if x], [node.body], a)
        self.visit(node.body)
        self.scopes.pop()

    def visit_TypeAlias(self, node):            # Python 3.12: `type X = …`
        self.visit(node.value)

    # -- writes to attributes and items. Allowed only into FRESH local objects (new collection,
    # instance, query row) and `self.x = …`:
    # a write into a class, module, settings or anything read from a parameter, attribute, call or
    # container is a change of foreign state
    # (monkey-patching), and recognizing every way a foreign object can end up in a local variable
    # is impossible — so only what is proven is allowed.
    def _target(self, t, node):
        if isinstance(t, (ast.Attribute, ast.Subscript)):
            r = _root(t)
            s = self.scopes[-1]
            if not self.code:
                # in tests (sandbox) it is enough not to touch module-level and imported names
                if not isinstance(r, ast.Name) or s.kind != 'func' or (r.id not in s.local and r.id not in s.params):
                    self.add('R2-write-to-foreign-object', node)
                return
            ok = False
            if isinstance(r, ast.Name) and s.kind == 'func':
                kind = s.fresh.get(r.id)
                if r.id == 'self' and 'self' in s.params:
                    ok = isinstance(t, ast.Attribute) and _chain_depth(t) == 1
                elif kind is not None:
                    ok = _chain_depth(t) == 1 or (kind == 'plain' and _only_subscripts(t))
            if not ok:
                if (isinstance(r, ast.Name) and r.id in s.params and isinstance(t, ast.Attribute)
                        and (r.id != 'self' or _chain_depth(t) >= 2)):
                    self.add('R2d-write-to-parameter-attribute', node)
                else:
                    self.add('R2-write-to-foreign-object', node)
        elif isinstance(t, (ast.Tuple, ast.List)):
            for e in t.elts:
                self._target(e, node)
        elif isinstance(t, ast.Starred):
            self._target(t.value, node)

    def _stored_callable(self, node, targets, value) -> None:
        """`obj.method = lambda …` / `= file function` / `= partial(…)`: replacing behavior, not writing data."""
        if not self.code or not any(isinstance(t, (ast.Attribute, ast.Subscript)) for t in targets):
            return
        callable_value = (isinstance(value, ast.Lambda)
                          or (isinstance(value, ast.Name) and value.id in self.func_names)
                          or (isinstance(value, ast.Call) and (
                              (isinstance(value.func, ast.Name) and value.func.id in STORED_CALLABLE_CTORS)
                              or (isinstance(value.func, ast.Attribute) and value.func.attr in STORED_CALLABLE_CTORS))))
        if callable_value:
            self.add('R2c-write-function-to-attribute', node)

    def visit_Assign(self, node):
        for t in node.targets:
            self._target(t, node)
        self._stored_callable(node, node.targets, node.value)
        self.generic_visit(node)

    def visit_AugAssign(self, node):
        self._target(node.target, node)
        self.generic_visit(node)

    def visit_AnnAssign(self, node):
        self._target(node.target, node)
        if node.value is not None:
            self._stored_callable(node, [node.target], node.value)
        self.generic_visit(node)                 # including the annotation: it is executed too

    def visit_Delete(self, node):
        for t in node.targets:
            self._target(t, node)
        self.generic_visit(node)

    def visit_While(self, node):
        t = node.test
        if self.code and isinstance(t, ast.Constant) and t.value:
            self.add('R15-infinite-loop', node, 'while ' + repr(t.value))
        self.generic_visit(node)

    def visit_Global(self, node):
        self.add('R12-global-state', node)

    visit_Nonlocal = visit_Global

    # -- exceptions: swallowing an error instead of fixing it
    def visit_ExceptHandler(self, node):
        if self.code:
            broad = node.type is None or (isinstance(node.type, ast.Name) and node.type.id in ('Exception', 'BaseException'))
            reraises = any(isinstance(n, ast.Raise) for n in ast.walk(node))
            if broad and not reraises:
                self.add('R5-broad-except-without-raise', node, 'except ' + (ast.unparse(node.type) if node.type else ''))
            only_noop = all(isinstance(s, NOOP_STMTS) or (isinstance(s, ast.Expr) and isinstance(s.value, ast.Constant))
                            for s in node.body)
            if only_noop:
                self.add('R5-empty-except', node, 'except …: pass')
        self.generic_visit(node)

    # -- imports
    def _imp(self, node, mods, relative_level=0, also_names=()):
        for a in node.names:
            self._ident(a.asname or a.name, node)
        if not self.code:
            return
        bound = [(a.asname or a.name).split('.')[0] for a in node.names]
        ok, listed = self._import_ok(node, mods, relative_level, also_names)
        if not ok:
            return
        self.imported_ok.update(bound)
        if listed:
            self.imported_listed.update(bound)
        if not relative_level:
            # `import re` / `from django.utils import timezone` — a module from the import list: its
            # functions (re.sub, timezone.now)
            # are safe. Modules of our apps are not included: the class `models.Product` has its own
            # methods with side effects.
            for a in node.names:
                full = a.name if isinstance(node, ast.Import) else mods[0]
                in_list = full in ALLOWED_EXACT or any(full == ok_ or full.startswith(ok_ + '.') for ok_ in ALLOWED_IMPORTS)
                name = (a.asname or a.name).split('.')[0]
                if in_list and (isinstance(node, ast.Import) or name[:1].islower()):
                    self.imported_modules.add(name)

    def _local_ok(self, full: str) -> bool:
        return any(full == m or full.startswith(m + '.') for m in LOCAL_SAFE)

    def _import_ok(self, node, mods, relative_level, also_names) -> tuple:
        """(allowed, from_allowlist); adds the violation itself. Allowed: allowlisted modules,
        models/serializers/utility modules
        of our apps, and everything the file ALREADY imports (that does not count as 'from the
        allowlist': calls at import time do not rely on it)."""
        if any(a.name == '*' for a in node.names):
            self.add('R4-star-import', node)
            return False, False
        if relative_level:
            if relative_level > 1:
                self.add('R4-import-outside-app', node)
                return False, False
            module = node.module or ''
            base = self.app + ('.' + module if module else '')
            fulls = [base] if module else [f'{base}.{a.name}' for a in node.names]
            named = [f'{base}.{a.name}' for a in node.names]
            if any(f == bad or f.startswith(bad + '.') for f in fulls + named for bad in DENIED_IMPORTS):
                self.add('R4-forbidden-import', node)
                return False, False
            if all(self._local_ok(f) for f in fulls):
                return True, True
            if all(f in self.file_imports for f in fulls):
                return True, False
            self.add('R4-import-outside-whitelist', node)
            return False, False
        # the forbidden list is checked against both the module and each imported name: `from
        # django.db import connection` = django.db.connection
        if any(m == bad or m.startswith(bad + '.') for m in list(mods) + list(also_names) for bad in DENIED_IMPORTS):
            self.add('R4-forbidden-import', node)
            return False, False
        # `from catalog import models` — we check full names (catalog.models), not the bare package
        checks = list(also_names) if (len(mods) == 1 and mods[0] in LOCAL_APPS and also_names) else list(mods)
        listed = True
        for m in checks:
            if m in ALLOWED_EXACT or any(m == ok or m.startswith(ok + '.') for ok in ALLOWED_IMPORTS) or self._local_ok(m):
                continue
            if m in self.file_imports:                                  # the file already imports this same module verbatim
                listed = False
                continue
            self.add('R4-import-outside-whitelist', node)
            return False, False
        return True, listed

    def visit_Import(self, node):
        self._imp(node, [a.name for a in node.names])

    def visit_ImportFrom(self, node):
        module = node.module or ''
        self._imp(node, [module], node.level or 0, [f'{module}.{a.name}' for a in node.names])

    # -- calls
    def visit_Call(self, node):
        f = node.func
        self.call_funcs.add(id(f))
        if isinstance(f, ast.Name):
            if f.id in CALL_NAMES or (self.code and f.id in CALL_NAMES_CODE):
                self.add('R3-reflection-or-execution', node)
            elif f.id == 'type' and (len(node.args) >= 3 or self.code):
                self.add('R3-type()', node)
            elif f.id in ('getattr', 'hasattr'):
                second = node.args[1] if len(node.args) >= 2 else None
                if not (isinstance(second, ast.Constant) and isinstance(second.value, str) and not second.value.startswith('__')):
                    self.add('R3-dynamic-getattr', node)
                else:
                    self._dangerous_name(second.value, node, node.args[0])      # getattr(x, 'delete') yields the same method as x.delete
        elif isinstance(f, ast.Attribute):
            if self.code and f.attr == 'update':
                recv = f.value
                if _is_qs_expr(recv) or (isinstance(recv, ast.Name) and any(recv.id in s.qs for s in self.scopes)):
                    self.add('R11-bulk-data-update', node)
            root = _root(f.value)
            if f.attr in LOG_SUPPRESS and isinstance(root, ast.Name) and root.id in LOG_OWNERS:
                self.add('R6-disabling-the-log', node)
        self.generic_visit(node)
        self._callee(f, node)             # after traversing the nested node: more precise rules (R3, R11) fire earlier and are therefore shown first
        self._higher_order(f, node)
        self._orm_args(f, node)

    def _import_time(self) -> bool:
        """Module code and class bodies, default values and decorators run at IMPORT — with no request and no
access check."""
        return self.scopes[-1].kind in ('module', 'class')

    def _callee(self, f, node) -> None:
        """Call allowlist (R14): see SAFE_NAME_CALLS/SAFE_METHODS. Disabled for tests and when there is
        no old file.
        A call of a VARIABLE (parameter, loop variable, assignment result) is always rejected,
        except lambdas: a variable may hold any method
        taken by a roundabout route (from a dict, a call, a container), and tracking all roundabout
        routes is impossible."""
        if self.old_calls is None:
            return
        names, _attrs = self.old_calls
        s = self.scopes[-1]
        if isinstance(f, ast.Name):
            n = f.id
            if s.kind == 'func' and n in s.local and n not in s.lambdas:
                self.add('R14-call-of-a-variable', node, n + '(…)')
                return
            if n in SAFE_NAME_CALLS or n.endswith(SAFE_EXC_SUFFIX) or n in s.lambdas:
                return
            if self._import_time():
                if n not in self.imported_listed:
                    self.add('R14-unknown-call-at-import', node, n + '(…)')
                return
            if n in names or n in self.func_names or n in self.imported_ok:
                return
            self.add('R14-unknown-call', node, n + '(…)')
        elif isinstance(f, ast.Attribute):
            self._method_ok(f.attr, node, _root(f.value))
        elif not isinstance(f, ast.Lambda):
            self.add('R14-call-of-a-result', node, ast.unparse(f)[:60])

    def _method_ok(self, attr: str, node, root) -> None:
        _names, attrs = self.old_calls
        if attr in SAFE_METHODS or (attr.startswith(SAFE_METHOD_PREFIX) and not WRITEY.search(attr)):
            return
        if isinstance(root, ast.Name) and root.id in self.imported_modules:
            return
        if not self._import_time() and (attr in attrs or attr in self.func_names):
            return
        self.add('R14-unknown-method', node, '.' + attr + '(…)')

    def _higher_order(self, f, node) -> None:
        """`map(obj.method, xs)`, `partial(obj.method)`, `sorted(xs, key=obj.method)`,
        `iter(obj.method, 0)` call the method without writing
        a call: we check these too."""
        if self.old_calls is None:
            return
        name = f.id if isinstance(f, ast.Name) else f.attr if isinstance(f, ast.Attribute) else ''
        positions, kw_names = self.callback_funcs.get(name, (set(), set()))
        refs = [node.args[i] for i in set(HOF_ARGS.get(name, ())) | positions if i < len(node.args)]
        refs.extend(kw.value for kw in node.keywords if kw.arg in kw_names)
        if name in HOF_KEY or name == 'sort':
            refs.extend(kw.value for kw in node.keywords if kw.arg in ('key', 'cmp'))
        for ref in refs:
            self._check_ref(ref, node)

    def _check_ref(self, ref, node) -> None:
        """A value that someone will call (map/partial argument, key=…, callback parameter): must be an allowed
callable."""
        if isinstance(ref, ast.Name):
            self._callee(ref, node)
        elif isinstance(ref, ast.Lambda):
            return
        elif isinstance(ref, ast.Call):
            if isinstance(ref.func, ast.Name) and ref.func.id in ('partial', 'partialmethod') and ref.args:
                self._check_ref(ref.args[0], node)
            elif not (isinstance(ref.func, ast.Name) and ref.func.id in ('getattr', 'hasattr')):
                self.add('R14-call-of-a-result', node, ast.unparse(ref)[:60])
            for attr in self._method_refs(ref):
                self._method_ok(attr, node, None)
        else:
            for attr in self._method_refs(ref):
                self._method_ok(attr, node, _root(ref) if isinstance(ref, ast.Attribute) else None)

    def _orm_args(self, f, node) -> None:
        """Field names in queries (`filter`, `order_by`, `values`, `Count`…) must be literals in the
        patch itself: a field name cannot come from a query
        string (`?f_purchases__user__password__startswith=…`) or a dict — that is reading arbitrary
        data through the ORM."""
        if not self.code:
            return
        name = f.id if isinstance(f, ast.Name) else f.attr if isinstance(f, ast.Attribute) else ''
        if name not in ORM_LOOKUP and name not in DB_EXPR:
            return
        for kw in node.keywords:
            if kw.arg is None and not (isinstance(kw.value, ast.Dict) and all(isinstance(k, ast.Constant) and isinstance(k.value, str)
                                                                              for k in kw.value.keys)):
                self.add('R14b-field-names-not-literals', node, f'{name}(**…)')
                return
        if name in ORM_FIELD_LISTS or name in DB_EXPR:
            for arg in node.args:
                if not _orm_literal(arg):
                    self.add('R14b-field-names-not-literals', node, f'{name}({ast.unparse(arg)[:40]})')
                    return

    def _dangerous_name(self, attr: str, node, receiver) -> None:
        """A dangerous method/attribute by NAME — even without a call (`f = qs.delete`, `map(qs.delete, …)`,
`getattr(qs, 'update')`)."""
        if attr in ATTR_CALLS or (self.code and attr in ATTR_CALLS_CODE):
            self.add('R3-dangerous-call', node, '.' + attr + '(…)')
        elif self.code and attr in MUTATOR_CALLS and not self._plain_receiver(receiver):
            self.add('R11-data-change-via-manager-or-session', node, '.' + attr)

    def visit_Attribute(self, node):
        self._ident(node.attr, node)
        if node.attr.startswith('__') and node.attr.endswith('__') and node.attr not in SAFE_DUNDER:
            self.add('R3-dunder', node)
        if node.attr in LOG_ATTRS:
            self.add('R6-disabling-the-log', node, '.' + node.attr)
        self._dangerous_name(node.attr, node, node.value)
        if self.code:
            self._words(node.attr, node)
        self.generic_visit(node)

    def visit_Name(self, node):
        self._ident(node.id, node)
        if node.id.startswith('__') and node.id.endswith('__') and node.id not in SAFE_DUNDER:
            self.add('R3-dunder', node)
        if node.id in BANNED_NAMES:
            self.add('R10-dangerous-name', node, node.id)
        if node.id in REF_NAMES and id(node) not in self.call_funcs:
            self.add('R3-reference-to-a-dangerous-function', node, node.id)
        if self.code:
            self._words(node.id, node)

    def visit_keyword(self, node):
        if node.arg:
            self._ident(node.arg, node)
            if self.code:
                self._words(node.arg, node)
        self.generic_visit(node)

    def visit_Constant(self, node):
        if isinstance(node.value, str) and id(node) not in self.docstrings:
            if DUNDER_IN_STRING.search(node.value):
                self.add('R3-dunder-in-string', node, node.value[:60])
            if self.code:
                self._words(node.value[:200], node)

    # -- helpers
    def _ident(self, name: str, node):
        if isinstance(name, str) and not name.isascii():
            self.add('R9-non-ASCII-identifier', node, name[:40])

    def _words(self, text: str, node):
        for rx, rule in ((SECRET_WORDS, 'R8-secrets'), (PII_WORDS, 'R8-personal-data')):
            m = rx.search(text)
            if m:
                self.add(rule, node, m.group(0).lower())


WRITE_METHODS = frozenset({'save', 'create', 'get_or_create', 'update_or_create', 'bulk_create', 'bulk_update', 'delete', 'raw', 'execute',
                           'executemany', 'cursor', 'truncate', 'send_mail', 'send_mass_mail', 'send', 'send_robust', 'call_command',
                           'unlink', 'rmdir', 'rmtree', 'write_text', 'write_bytes', 'mkdir', 'touch', 'rename', 'open'})
NETWORK_NAMES = frozenset({'requests', 'urllib', 'urllib3', 'httpx', 'socket', 'smtplib', 'aiohttp', 'websockets'})
COLLECTION_VERBS = frozenset({'add', 'remove', 'update', 'clear', 'set', 'insert', 'append', 'extend', 'pop', 'discard', 'sort', 'reverse',
                              'copy', 'get', 'items', 'keys', 'values', 'setdefault', 'popitem'})
# Keyword arguments and words showing that a function selects data by VISIBILITY or OWNER (R7i-R7k)
VIS_KW = frozenset({'is_active', 'is_approved', 'is_published', 'is_hidden', 'is_public', 'is_visible', 'is_preview', 'is_rejected',
                    'user', 'user_id', 'owner', 'owner_id', 'author', 'author_id'})
VIS_TOKEN = re.compile(r'is_(active|approved|published|visible|public|hidden|rejected|preview)|approved|published|moderat|draft|hidden', re.I)
# Functions and classes responsible, by name, for access or abuse protection: automatically not
# edited (R7e)
ACCESS_NAME = re.compile(r'(access|authori[sz]|auth(?!or)|allowed|permission|\bowns?\b|_owns?_|owner|^_?can_|is_staff|is_admin|purchas|paid|'
                         r'entitle|has_perm|login|password|secret|csrf)', re.I)
CLASS_FREEZE = re.compile(r'(Throttle|Permission|Authentication|Pagination)')
# Constants that by name define what is visible or who is allowed: not changed (R7g)
SENSITIVE_CONST = re.compile(r'(VISIBLE|ALLOW|PUBLIC|FREE|HIDDEN|DRAFT|WHITELIST|BLACKLIST|DENY|ROLE|PERMISSION|STAFF|OWNER|SECRET|PRIVATE|'
                             r'_DIR|_PATH|_ROOT|FOLDER)', re.I)


def _writey_callee(name: str) -> bool:
    return name not in COLLECTION_VERBS and WRITEY.search(name)


def _unlisted_local_names(tree, app: str) -> set:
    """Names imported from modules of OUR apps outside the allowlist (revert, notifications, signals,
    video_access…): these are functions
    with side effects, and calling them is 'risky' (R7h)."""
    out = set()

    def safe(full: str) -> bool:
        return any(full == m or full.startswith(m + '.') for m in LOCAL_SAFE)

    for n in ast.walk(tree):
        if isinstance(n, ast.ImportFrom):
            if n.level == 1 and app:
                base = app + ('.' + n.module if n.module else '')
            elif not n.level and n.module:
                base = n.module
            else:
                continue
            if base.split('.')[0] not in PROJECT_APPS:
                continue
            for a in n.names:
                if not safe(f'{base}.{a.name}') and not safe(base):
                    out.add(a.asname or a.name)
        elif isinstance(n, ast.Import):
            for a in n.names:
                if a.name.split('.')[0] in PROJECT_APPS and not safe(a.name):
                    out.add((a.asname or a.name).split('.')[0])
    return out


def _risk_calls(fn, unlisted: set, fresh: dict | None = None) -> Counter:
    """Calls inside a function that write to the database, send, or change state outside it (by name):
    ORM writes, management commands,
    network, functions from our modules outside the allowlist, action names (apply_*, grant_*,
    sync_*…). Key — the call name."""
    out: Counter = Counter()
    for n in ast.walk(fn):
        if isinstance(n, ast.Name) and n.id in NETWORK_NAMES:
            out[f'<network:{n.id}>'] += 1
        if isinstance(n, ast.Name) and n.id in unlisted and isinstance(n.ctx, ast.Load):
            out[n.id] += 1                           # both a call and passing as a value (`map(apply_snapshot, …)`)
        if not isinstance(n, ast.Call):
            continue
        f = n.func
        if isinstance(f, ast.Attribute):
            root = _root(f.value)
            local_dict = isinstance(f.value, ast.Name) and (fresh or {}).get(f.value.id) == 'plain'
            if f.attr in WRITE_METHODS or (f.attr == 'update' and not n.args and n.keywords and not local_dict):
                out[f.attr] += 1
            elif isinstance(root, ast.Name) and root.id in unlisted:
                out[f'{root.id}.{f.attr}'] += 1
            elif _writey_callee(f.attr):
                out[f.attr] += 1
        elif isinstance(f, ast.Name):
            if f.id in WRITE_METHODS or _writey_callee(f.id):
                out[f.id] += 1
    return out


def _manager_roots(fn) -> Counter:
    """Where a function gets its records: `Course.objects`, `X._default_manager`, `get_object_or_404(X,
…)`."""
    out: Counter = Counter()
    for n in ast.walk(fn):
        if isinstance(n, ast.Attribute) and n.attr in ('objects', '_default_manager', '_base_manager'):
            out[ast.unparse(n.value)[:60]] += 1
        elif isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id in ('get_object_or_404', 'get_list_or_404') and n.args:
            out[ast.unparse(n.args[0])[:60]] += 1
    return out


def _vis_info(fn) -> tuple:
    """(selects by visibility/owner?, count of 'visibility words' in the body, all names and attributes of
the body)."""
    scoped = False
    vis: Counter = Counter()
    names: set = set()
    for n in ast.walk(fn):
        if isinstance(n, ast.keyword) and n.arg:
            if n.arg.split('__')[-1] in VIS_KW or any(
                    (isinstance(x, ast.Attribute) and x.attr in ('user', 'user_id')) or (isinstance(x, ast.Name) and x.id in ('user', 'user_id'))
                    for x in ast.walk(n.value)):
                scoped = True
            if VIS_TOKEN.search(n.arg):
                vis[n.arg] += 1
        elif isinstance(n, ast.Name):
            names.add(n.id)
            if VIS_TOKEN.search(n.id):
                vis[n.id] += 1
        elif isinstance(n, ast.Attribute):
            names.add(n.attr)
            if VIS_TOKEN.search(n.attr):
                vis[n.attr] += 1
        elif isinstance(n, ast.Constant) and isinstance(n.value, str) and len(n.value) <= 40 and VIS_TOKEN.search(n.value):
            vis['"' + n.value + '"'] += 1
    return scoped, vis, names


def _call_counts(fn) -> Counter:
    """How many times a function mentions each name (a call or passing as a value: Name or attribute)."""
    out: Counter = Counter()
    for n in ast.walk(fn):
        if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load):
            out[n.id] += 1
        elif isinstance(n, ast.Attribute) and isinstance(n.ctx, ast.Load):
            out[n.attr] += 1
    return out


def _qual_info(tree, app: str = '') -> dict:
    """{full name: {'n': how many times defined, 'dec': decorators, 'bases': class bases, 'guards': early exits (key counter),
    'records': each check separately (key, line, names/tokens/calls in the condition), 'loops':
    loops, 'events': all assignments of the function,
    'returns': early exits with a non-constant, 'dump': its full form, 'risky': risky calls (direct
    and through file functions that
    write), 'writes': whether it writes (directly or through such functions), 'roots': sources of
    writes, 'scoped'/'vis'/'names': visibility}}."""
    out: dict = {}
    unlisted = _unlisted_local_names(tree, app)
    models = _model_names(tree, app)

    def walk(node, prefix):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                q = f'{prefix}{child.name}'
                info = out.setdefault(q, {'n': 0, 'dec': Counter(), 'guards': Counter(), 'bases': Counter(), 'records': [], 'loops': [],
                                          'events': [], 'returns': Counter(), 'dump': Counter(), 'writes': False, 'risk': Counter(),
                                          'calls': Counter(), 'risky': Counter(), 'roots': Counter(), 'scoped': False, 'vis': Counter(),
                                          'names': set(), 'is_class': isinstance(child, ast.ClassDef)})
                info['n'] += 1
                info['dec'].update(ast.unparse(d) for d in child.decorator_list)
                info['dump'][ast.dump(child)] += 1
                if isinstance(child, ast.ClassDef):
                    info['bases'].update(ast.unparse(b) for b in child.bases)
                else:
                    guards, records = _guards(child)
                    info['guards'].update(guards)
                    info['records'].extend(records)
                    info['returns'].update(_early_returns(child))
                    info['loops'].extend((n.lineno, n.end_lineno or n.lineno) for n in ast.walk(child)
                                         if isinstance(n, (ast.For, ast.AsyncFor, ast.While)))
                    info['events'].extend(_events(child))
                    args = child.args
                    plain_args = [x.arg for x in (args.vararg, args.kwarg) if x]
                    arg_names = [x.arg for x in args.posonlyargs + args.args + args.kwonlyargs]
                    _loc, fresh, _qs, _lam = _analyze_locals(child.body, arg_names, plain_args, models)
                    info['risk'].update(_risk_calls(child, unlisted, fresh))
                    info['calls'].update(_call_counts(child))
                    info['roots'].update(_manager_roots(child))
                    scoped, vis, names = _vis_info(child)
                    info['scoped'] = info['scoped'] or scoped
                    info['vis'].update(vis)
                    info['names'] |= names
                walk(child, q + '.')
            else:
                walk(child, prefix)
    walk(tree, '')
    # a function “writes” if it writes itself or calls a function from this same file that writes
    # (transitively)
    by_simple: dict = {}
    for q in out:
        by_simple.setdefault(q.rsplit('.', 1)[-1], []).append(q)
    writers = {q for q, i in out.items() if i['risk']}
    changed = True
    while changed:
        changed = False
        for q, i in out.items():
            if q in writers or i['is_class']:
                continue
            if any(h in writers and h != q for c in i['calls'] for h in by_simple.get(c, ())):
                writers.add(q)
                changed = True
    for q, i in out.items():
        if i['is_class']:
            continue
        i['risky'] = Counter(i['risk'])
        for c, count in i['calls'].items():
            if any(h in writers and h != q for h in by_simple.get(c, ())):
                i['risky'][c] += count
        i['writes'] = q in writers
    return out


GUARD_EXITS = (ast.Return, ast.Raise, ast.Continue, ast.Break)
TRY_NODES = tuple(t for t in (ast.Try, getattr(ast, 'TryStar', None)) if t)


def _loaded_names(node) -> set:
    """Names the expression depends on; `self.method(…)` yields the method name too (its body also affects
the value)."""
    out = set()
    for n in ast.walk(node):
        if isinstance(n, ast.Name):
            out.add(n.id)
        elif isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) and n.value.id in ('self', 'cls'):
            out.add(n.attr)
    return out


def _guards(fn):
    """Early exits on checks: `if …: return/raise/continue/break` and assert — the function's “guard”
    constructs.
    The key is (kind, condition, body, ENCLOSURE): you cannot change the condition or the body, or
    wrap an existing guard in a new condition/try/with; you can
    only add a new one. The second value is a record for each check: line, names from its condition
    (and from the conditions enclosing it), tokens for
    searching access words, and names of the functions called in the condition."""
    out: Counter = Counter()
    records: list = []

    def record(key, line, tests):
        out[key] += 1
        names: set = set()
        calls: set = set()
        tokens: set = set()
        for test in tests:
            names.update(_loaded_names(test))
            for n in ast.walk(test):
                if isinstance(n, ast.Name):
                    tokens.add(n.id)
                elif isinstance(n, ast.Attribute):
                    tokens.add(n.attr)
                elif isinstance(n, ast.Constant) and isinstance(n.value, str) and len(n.value) <= 40:
                    tokens.add(n.value)
                if isinstance(n, ast.Call):
                    f = n.func
                    if isinstance(f, ast.Name):
                        calls.add(f.id)
                    elif isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name) and f.value.id in ('self', 'cls'):
                        calls.add(f.attr)
        records.append({'key': key, 'line': line, 'names': names, 'tokens': tokens, 'calls': calls})

    def walk(stmts, ctx, nodes):
        for n in stmts:
            visit(n, ctx, nodes)

    def visit(n, ctx, nodes):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            return
        if isinstance(n, ast.If):
            test = ast.unparse(n.test)
            if n.body and isinstance(n.body[-1], GUARD_EXITS):
                record(('if', test, '; '.join(ast.unparse(s) for s in n.body), ctx), n.lineno, nodes + [n.test])
            walk(n.body, ctx + (('if', test),), nodes + [n.test])
            walk(n.orelse, ctx + (('else', test),), nodes + [n.test])
        elif isinstance(n, ast.Assert):
            record(('assert', ast.unparse(n.test), '', ctx), n.lineno, nodes + [n.test])
        elif isinstance(n, ast.Expr):
            call = n.value.value if isinstance(n.value, ast.Await) else n.value
            if isinstance(call, ast.Call):
                f = call.func
                name = f.id if isinstance(f, ast.Name) else f.attr if isinstance(f, ast.Attribute) else ''
                if CHECK_CALL.search(name):           # `self.check_object_permissions(request, obj)` — a check that raises an exception by itself
                    record(('call', ast.unparse(call), '', ctx), n.lineno, nodes + [call])
        elif isinstance(n, (ast.For, ast.AsyncFor)):
            it = ast.unparse(n.iter)
            walk(n.body, ctx + (('for', it),), nodes + [n.iter])
            walk(n.orelse, ctx + (('for-else', it),), nodes + [n.iter])
        elif isinstance(n, ast.While):
            test = ast.unparse(n.test)
            walk(n.body, ctx + (('while', test),), nodes + [n.test])
            walk(n.orelse, ctx + (('while-else', test),), nodes + [n.test])
        elif TRY_NODES and isinstance(n, TRY_NODES):
            kinds = ', '.join(ast.unparse(h.type) if h.type else '' for h in n.handlers)
            walk(n.body, ctx + (('try', kinds),), nodes)
            for h in n.handlers:
                walk(h.body, ctx + (('except', ast.unparse(h.type) if h.type else ''),), nodes)
            walk(n.orelse, ctx + (('try-else', kinds),), nodes)
            walk(n.finalbody, ctx + (('finally', kinds),), nodes)
        elif isinstance(n, (ast.With, ast.AsyncWith)):
            walk(n.body, ctx + (('with', ', '.join(ast.unparse(i.context_expr) for i in n.items)),), nodes)
        elif isinstance(n, ast.Match):
            for case in n.cases:
                walk(case.body, ctx + (('case', ast.unparse(case.pattern)),), nodes)

    walk(fn.body, (), [])
    return out, records


def _early_returns(fn) -> Counter:
    """All `return`s with a non-constant that are NOT at the top level of the function body (inside
    if/for/while/try/with) are “early” exits that
    hand back something other than intended. Nested functions and lambdas are not counted (they are
    separate)."""
    out: Counter = Counter()
    top = {id(st) for st in fn.body}
    stack = list(fn.body)
    while stack:
        n = stack.pop()
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
            continue
        if isinstance(n, ast.Return) and id(n) not in top and n.value is not None and not _literal_like(n.value):
            out[f'return {ast.unparse(n.value)[:70]}'] += 1
        stack.extend(ast.iter_child_nodes(n))
    return out


def _target_roots(t) -> list:
    """Names whose value an assignment changes: the variable itself, and for `a.b = …`/`a[i] = …` the root
of the chain."""
    if isinstance(t, ast.Name):
        return [t.id]
    if isinstance(t, (ast.Tuple, ast.List)):
        return [r for e in t.elts for r in _target_roots(e)]
    if isinstance(t, ast.Starred):
        return _target_roots(t.value)
    if isinstance(t, (ast.Attribute, ast.Subscript)):
        r = _root(t)
        return [r.id] if isinstance(r, ast.Name) else []
    return []


def _events(fn) -> list:
    """Everything that can change the value of a function's local variable: [(variable, names it
    depends on, event “signature”)].
    Assignments (including to attributes/items), loops, with, walrus, comprehensions, exception
    names, imports, deletions, calls of
    mutating methods and default values of parameters."""
    out = []

    def add(roots, value, key, line):
        names = _loaded_names(value) if value is not None else set()
        for r in roots:
            out.append((r, names, f'{r}|{key}'[:300], line))

    for name, default in _param_defaults(fn.args).items():
        add([name], default, 'param ' + (ast.unparse(default) if default is not None else ''), 0)
    for n in ast.walk(fn):
        if n is fn:
            continue
        line = getattr(n, 'lineno', 0)
        if isinstance(n, ast.Assign):
            add([r for t in n.targets for r in _target_roots(t)], n.value, ast.unparse(n), line)
        elif isinstance(n, (ast.AugAssign, ast.AnnAssign)):
            add(_target_roots(n.target), n.value, ast.unparse(n), line)
        elif isinstance(n, (ast.For, ast.AsyncFor)):
            add(_target_roots(n.target), n.iter, f'for {ast.unparse(n.target)} in {ast.unparse(n.iter)}', line)
        elif isinstance(n, (ast.With, ast.AsyncWith)):
            for it in n.items:
                if it.optional_vars is not None:
                    add(_target_roots(it.optional_vars), it.context_expr,
                        f'with {ast.unparse(it.context_expr)} as {ast.unparse(it.optional_vars)}', line)
        elif isinstance(n, ast.NamedExpr):
            add(_target_roots(n.target), n.value, ast.unparse(n), line)
        elif isinstance(n, ast.comprehension):
            add(_target_roots(n.target), n.iter, f'comp {ast.unparse(n.target)} in {ast.unparse(n.iter)}', getattr(n.target, 'lineno', 0))
        elif isinstance(n, ast.ExceptHandler) and n.name:
            add([n.name], n.type, 'except ' + (ast.unparse(n.type) if n.type is not None else ''), line)
        elif isinstance(n, (ast.Import, ast.ImportFrom)):
            for al in n.names:
                add([(al.asname or al.name).split('.')[0]], None, ast.unparse(n), line)
        elif isinstance(n, ast.Delete):
            add([r for t in n.targets for r in _target_roots(t)], None, ast.unparse(n), line)
        elif isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr in MUTATOR_CALLS:
            root = _root(n.func.value)
            if isinstance(root, ast.Name):
                add([root.id], n, 'mutate ' + ast.unparse(n), line)
    return out


def _window(info: dict, line: int) -> list:
    """Function events that can happen BEFORE the check on line `line`: everything located in the text
    at or above it, and everything in the loops it
    sits in (the next iteration sees the changes of the previous one). After the check the value may
    be changed — it no longer affects the check."""
    spans = [(a, b) for a, b in info['loops'] if a <= line <= b]
    return [ev for ev in info['events'] if ev[3] <= line or any(a <= ev[3] <= b for a, b in spans)]


def _closure(seed: set, events: list) -> set:
    """Names on which the result of the checks depends (directly or through a chain of assignments)."""
    out = set(seed)
    changed = True
    while changed:
        changed = False
        for root, names, _key, _line in events:
            if root in out and not names <= out:
                out |= names
                changed = True
    return out


def _parse(src: str, path: str):
    try:
        return ast.parse(src[1:] if src.startswith(BOM) else src)
    except SyntaxError as exc:
        raise PolicyViolation(f'{path}: the file does not compile: {exc.msg} (line {exc.lineno}).')
    except (ValueError, RecursionError, MemoryError):
        raise PolicyViolation(f'{path}: the file is too complex to parse.')


def encoding_problem(src: str) -> str:
    """Why Python will read this file NOT as plain UTF-8 (empty means it is read as plain). A `#
    coding: utf-7` line on the first or second
    line (PEP 263) makes the interpreter decode the WHOLE file with a different codec, while our
    checks (and `ast.parse(str)`) see
    only the raw text: `+AAo-` is harmless characters to them, but to Python it is a line break.
    This way code can be hidden in a “comment” or a string."""
    try:
        encoding, _lines = tokenize.detect_encoding(io.BytesIO(src.encode('utf-8', 'replace')).readline)
    except (SyntaxError, LookupError, ValueError) as exc:
        return f'invalid encoding declaration in the file ({exc})'
    return '' if encoding in ('utf-8', 'utf-8-sig') else f'the file declares the encoding "{encoding}", but it must stay UTF-8'


def check_structure(path: str, kind: str, old_src: str, new_src: str) -> None:
    """Structural layer: compares the risky constructs of the old and new versions of a file. kind: 'code'
| 'test'."""
    if '\r' in new_src or '\r' in old_src:
        raise PolicyViolation(f'{path}: the file contains CR (Windows line endings): not supported.')
    problem = encoding_problem(new_src)
    if problem:
        raise PolicyViolation(f'{path}: structural check (R16): {problem}. Adding an encoding line (`# coding: …`) is forbidden.')
    new_tree = _parse(new_src, path)
    old_tree = _parse(old_src, path) if old_src else ast.parse('')
    try:
        app = app_of(path)
        base_imports = _imports_of(old_tree, app)
        callables = _callables_of(old_tree) if old_src else None          # whitelist of calls: whatever the old file already calls is allowed
        new_gate = _Gate(kind, base_imports, app, callables)
        old_gate = _Gate(kind, base_imports, app, callables)             # the old file is counted the same way: `f(x)(y)` in it is not “new”
        new_gate.visit(new_tree)
        old_gate.visit(old_tree)
        added = new_gate.found - old_gate.found
        old_info, new_info = (_qual_info(old_tree, app), _qual_info(new_tree, app)) if kind == 'code' and old_src else ({}, {})
        extras = ((_global_defs(old_tree), _global_defs(new_tree), _meta_contracts(old_tree), _meta_contracts(new_tree),
                   _binding_counts(old_tree), _binding_counts(new_tree), _dead_code(old_tree), _dead_code(new_tree))
                  if old_info else ({}, {}, {}, {}, Counter(), Counter(), 0, 0))
    except RecursionError:
        raise PolicyViolation(f'{path}: the file is too complex to parse.')
    try:
        _check_preserved(path, old_info, new_info, extras, phase=1)     # first “existing guard changed” — a more precise explanation than “new construct”
        if added:
            (rule, text), _n = next(iter(added.items()))
            raise PolicyViolation(f'{path}: structural check ({rule}): "{text}". Such a construct is forbidden in an auto-fix.')
        _check_preserved(path, old_info, new_info, extras, phase=2)     # then the general rules: new calls that write, data sources, contracts
    except RecursionError:
        raise PolicyViolation(f'{path}: the file is too complex to parse.')


def _dead_code(tree) -> int:
    """How many blocks have statements AFTER an unconditional return/raise/continue/break: code that
    will never run. Honest code has none of this,
    while a `return OK` at the start of a function quietly disables everything that follows (check,
    filter, restriction)."""
    count = 0
    for node in ast.walk(tree):
        for field in ('body', 'orelse', 'finalbody'):
            block = getattr(node, field, None)
            if isinstance(block, list):
                for i, st in enumerate(block[:-1]):
                    if isinstance(st, GUARD_EXITS):
                        count += 1
                        break
        if isinstance(node, ast.Try) or (TRY_NODES and isinstance(node, TRY_NODES)):
            for h in node.handlers:
                for i, st in enumerate(h.body[:-1]):
                    if isinstance(st, GUARD_EXITS):
                        count += 1
                        break
    return count


META_ATTRS = ('fields', 'exclude', 'read_only_fields', 'depth', 'extra_kwargs', 'model')


def _meta_contracts(tree) -> dict:
    """{'Owner.Meta': {attribute: (counter of string literals or None, text)}}: serializer/form contract — which fields are exposed and how."""
    out: dict = {}

    def literal(v):
        if isinstance(v, (ast.List, ast.Tuple, ast.Set)) and all(isinstance(e, ast.Constant) and isinstance(e.value, str) for e in v.elts):
            return Counter(e.value for e in v.elts)
        return None

    def walk(node, prefix):
        for ch in ast.iter_child_nodes(node):
            if isinstance(ch, ast.ClassDef):
                q = f'{prefix}{ch.name}'
                if ch.name == 'Meta' and prefix:
                    data = {}
                    for st in ch.body:
                        if isinstance(st, ast.Assign):
                            names, value = [t.id for t in st.targets if isinstance(t, ast.Name)], st.value
                        elif isinstance(st, ast.AnnAssign) and isinstance(st.target, ast.Name):
                            names, value = [st.target.id], st.value
                        else:
                            continue
                        for name in names:
                            if name in META_ATTRS and value is not None:
                                data[name] = (literal(value), ast.unparse(value)[:300])
                    out[q] = data
                walk(ch, q + '.')
            elif isinstance(ch, (ast.FunctionDef, ast.AsyncFunctionDef)):
                walk(ch, f'{prefix}{ch.name}.')
            else:
                walk(ch, prefix)
    walk(tree, '')
    return out


def _binding_counts(tree) -> Counter:
    """How many times each name is bound at module or class-body level (def, class, assignment,
    import): rebinding an
    existing name (`_helper = lambda …` after `def _helper`) replaces the function, and nothing else
    sees it."""
    out: Counter = Counter()

    def names_of(t):
        if isinstance(t, ast.Name):
            return [t.id]
        if isinstance(t, (ast.Tuple, ast.List)):
            return [n for e in t.elts for n in names_of(e)]
        return []

    def walk(body, prefix):
        for st in body:
            if isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef)):
                out[prefix + st.name] += 1
            elif isinstance(st, ast.ClassDef):
                out[prefix + st.name] += 1
                walk(st.body, f'{prefix}{st.name}.')
            elif isinstance(st, ast.Assign):
                for t in st.targets:
                    for n in names_of(t):
                        out[prefix + n] += 1
            elif isinstance(st, (ast.AnnAssign, ast.AugAssign)):
                for n in names_of(st.target):
                    out[prefix + n] += 1
            elif isinstance(st, (ast.Import, ast.ImportFrom)):
                for a in st.names:
                    out[prefix + (a.asname or a.name).split('.')[0]] += 1
            elif isinstance(st, ast.If):
                walk(st.body + st.orelse, prefix)
            elif TRY_NODES and isinstance(st, TRY_NODES):
                walk(st.body + [s for h in st.handlers for s in h.body] + st.orelse + st.finalbody, prefix)
    walk(tree.body, '')
    return out


def _check_preserved(path: str, old_info: dict, new_info: dict, extras: tuple = ({}, {}, {}, {}, Counter(), Counter(), 0, 0),
                     phase: int = 0) -> None:
    """R7: existing functions, decorators, class bases and guard checks (and everything they depend on)
    stay as they were; new calls of
    functions that write, new data sources, loss of visibility filters, and changes to serializer
    contracts are forbidden."""
    old_globals, new_globals, old_meta, new_meta, old_bind, new_bind, old_dead, new_dead = extras
    by_simple: dict = {}
    for q in old_info:
        by_simple.setdefault(q.rsplit('.', 1)[-1], []).append(q)
    file_roots = set()
    for info in old_info.values():
        file_roots |= set(info['roots'])
    for q, old in old_info.items():
        new = new_info.get(q)
        if phase in (0, 1):
            if new is None or new['n'] < old['n']:
                raise PolicyViolation(f'{path}: structural check (R7): "{q}" disappeared or was renamed — an auto-fix may only edit.')
            if old['dec'] - new['dec']:
                raise PolicyViolation(f'{path}: structural check (R7): in "{q}" a decorator was removed or changed '
                                      f'"{next(iter(old["dec"] - new["dec"]))}".')
            if old['bases'] - new['bases']:
                raise PolicyViolation(f'{path}: structural check (R7): the base classes of the class "{q}" were changed.')
            lost = old['guards'] - new['guards']
            if lost:
                test = next(iter(lost))[1]
                raise PolicyViolation(f'{path}: structural check (R7): in "{q}" an existing '
                                      f'early-exit check ("if {test[:60]}") was changed, removed or wrapped in a new condition/try/with. Do not touch existing guard checks — add a new one next to them.')
        if phase == 1:
            continue
        parts = q.split('.')
        if old['dump'] != new['dump'] and (ACCESS_NAME.search(parts[-1]) or any(CLASS_FREEZE.search(p) for p in parts)):
            raise PolicyViolation(f'{path}: structural check (R7e): by its name "{q}" is responsible for access or abuse protection '
                                  f'(permissions, ownership, rate limiting, pagination) — such code is not edited automatically, only manually.')
        if not new['is_class']:
            _check_sources(path, q, old, new, old_info, by_simple)
        _check_dataflow(path, q, old, new, old_info, new_info, by_simple, old_globals, new_globals)
    if phase == 1:
        return
    for q, new in new_info.items():
        if q in old_info or new['is_class']:
            continue
        if new['risky']:
            raise PolicyViolation(f'{path}: structural check (R7h): the new function "{q}" calls "{next(iter(new["risky"]))}", which writes to the database, '
                                  f'sends something or changes state outside itself — such calls must not be added in an auto-fix.')
        if set(new['roots']) - file_roots:
            raise PolicyViolation(f'{path}: structural check (R7j): the new function "{q}" takes data from a model the file has not used so far '
                                  f'("{next(iter(set(new["roots"]) - file_roots))}").')
    _check_meta(path, old_meta, new_meta)
    if new_dead > old_dead:
        raise PolicyViolation(f'{path}: structural check (R18): code appeared after an unconditional return/raise/continue/break — it will never '
                              f'run. This is how checks are quietly disabled; fix the cause itself.')
    for name in old_globals:
        if SENSITIVE_CONST.search(name) and old_globals[name] != new_globals.get(name, Counter()):
            raise PolicyViolation(f'{path}: structural check (R7g): by its name the value "{name}" defines what is visible or who is allowed — '
                                  f'such a value is not changed automatically.')
    for name, count in old_bind.items():
        if new_bind[name] > count:
            raise PolicyViolation(f'{path}: structural check (R7l): the name "{name}" is already defined in the file, and the patch binds it once more — '
                                  f'this is how an existing function or value is replaced. Edit the definition itself.')


def _check_sources(path: str, q: str, old: dict, new: dict, old_info: dict, by_simple: dict) -> None:
    """R7h/R7i/R7j/R7k for an existing function: no new calls of functions that write appear; no new data
sources; visibility filters are not lost."""
    extra = new['risky'] - old['risky']
    if extra:
        raise PolicyViolation(f'{path}: structural check (R7h): in "{q}" a new call "{next(iter(extra))}" appeared, which writes to the database, '
                              f'sends something or changes state outside the function — such calls must not be added in an auto-fix.')
    new_models = set(new['roots']) - set(old['roots'])
    if new_models:
        raise PolicyViolation(f'{path}: structural check (R7j): "{q}" started taking data from a model it did not use before '
                              f'("{next(iter(new_models))}") — widening the data source is not done automatically.')
    if old['scoped'] and (new['roots'] - old['roots']):
        raise PolicyViolation(f'{path}: structural check (R7i): "{q}" selects records by visibility or owner, and the patch adds one more '
                              f'query from the same manager (`{next(iter(new["roots"] - old["roots"]))}.objects…`) — this is how a filter is bypassed.')
    if old['vis'] - new['vis']:
        raise PolicyViolation(f'{path}: structural check (R7k): in "{q}" the number of mentions of visibility/moderation decreased '
                              f'("{next(iter(old["vis"] - new["vis"]))}") — visibility filters must not be removed.')
    for name in old['calls'] - new['calls']:
        for helper in by_simple.get(name, ()):
            if helper != q and old_info[helper]['vis']:
                raise PolicyViolation(f'{path}: structural check (R7k): "{q}" now calls "{name}", which carries a visibility filter, less often.')


def _check_meta(path: str, old_meta: dict, new_meta: dict) -> None:
    """R17: serializer/form contract. Fields can only be removed; exclude/read_only_fields can only be
    added to; the rest (depth, extra_kwargs,
    model, non-literal values) stays unchanged."""
    for key, old_data in old_meta.items():
        new_data = new_meta.get(key)
        if new_data is None:
            raise PolicyViolation(f'{path}: structural check (R17): the class "{key}" disappeared or changed.')
        if 'fields' in old_data and 'fields' not in new_data:
            raise PolicyViolation(f'{path}: structural check (R17): `fields` was removed in "{key}" — without it all fields are exposed.')
        if 'exclude' in new_data and 'exclude' not in old_data:
            raise PolicyViolation(f'{path}: structural check (R17): `exclude` appeared in "{key}" — the field contract is not changed automatically.')
        for attr in set(old_data) | set(new_data):
            o, n = old_data.get(attr), new_data.get(attr)
            o_set, o_txt = o if o else (Counter(), '')
            n_set, n_txt = n if n else (Counter(), '')
            literal = (o is None or o_set is not None) and (n is None or n_set is not None)
            if attr in ('fields', 'exclude', 'read_only_fields') and literal:
                widened = (n_set - o_set) if attr == 'fields' else (o_set - n_set)
                if widened:
                    what = 'added the field' if attr == 'fields' else 'removed from forbidden/read-only'
                    raise PolicyViolation(f'{path}: structural check (R17): in "{key}.{attr}" {what} "{next(iter(widened))}" — the contract '
                                          f'of what is exposed outward is not widened automatically.')
            elif o_txt != n_txt:
                raise PolicyViolation(f'{path}: structural check (R17): in "{key}" "{attr}" was changed — the serializer contract is not '
                                      f'changed automatically.')


def _global_defs(tree) -> dict:
    """{name: counter of assignment texts} for names assigned at module level or in a class body (constants, class attributes):
    access checks may depend on them (`if slug in FREE_COURSES`), and these assignments are not
    visible inside a function body."""
    out: dict = {}

    def add(name, stmt):
        out.setdefault(name, Counter())[ast.unparse(stmt)[:300]] += 1

    def names_of(t):
        if isinstance(t, ast.Name):
            return [t.id]
        if isinstance(t, (ast.Tuple, ast.List)):
            return [n for e in t.elts for n in names_of(e)]
        return []

    def walk(body):
        for st in body:
            if isinstance(st, ast.Assign):
                for t in st.targets:
                    for n in names_of(t):
                        add(n, st)
            elif isinstance(st, (ast.AnnAssign, ast.AugAssign)):
                for n in names_of(st.target):
                    add(n, st)
            elif isinstance(st, ast.ClassDef):
                walk(st.body)
            elif isinstance(st, ast.If):
                walk(st.body + st.orelse)
            elif TRY_NODES and isinstance(st, TRY_NODES):
                walk(st.body + [s for h in st.handlers for s in h.body] + st.orelse + st.finalbody)
    walk(tree.body)
    return out


def _record_words(info: dict, rec: dict) -> str:
    """Words that show whether a check concerns access: the condition itself and the assignments of its
    immediate variables (one step, not the whole
    chain: otherwise any function where something is filtered by is_active somewhere would become
    “guarding” as a whole)."""
    return ' '.join(rec['tokens']) + ' ' + ' '.join(key for root, _n, key, _l in _window(info, rec['line']) if root in rec['names'])


def _function_chain(tree, line: int) -> list:
    """Full names of the functions enclosing the line (from the outermost to the innermost)."""
    best: list = []

    def walk(node, prefix, chain):
        nonlocal best
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                q = f'{prefix}{child.name}'
                inside = child.lineno <= line <= (child.end_lineno or child.lineno)
                funcs = chain + [q] if inside and not isinstance(child, ast.ClassDef) else chain
                if inside and len(funcs) > len(best):
                    best = funcs
                if inside:
                    walk(child, q + '.', funcs)
            else:
                walk(child, prefix, chain)
    walk(tree, '', [])
    return best


def frozen_reason(src: str, line: int, path: str = '') -> str:
    """Why auto-fix will not edit the function enclosing line `line` (empty means it can be edited).
    For selecting errors and cards:
    do not spend the LLM on what the policy will reject anyway (R7e/R7f)."""
    try:
        tree = _parse(src, '')
    except PolicyViolation:
        return ''
    info = _qual_info(tree, app_of(path) if path else '')
    for q in _function_chain(tree, line):
        inf = info.get(q)
        if inf is None:
            continue
        parts = q.split('.')
        if ACCESS_NAME.search(parts[-1]) or any(CLASS_FREEZE.search(p) for p in parts):
            return 'by its name the function is responsible for access or abuse protection'
        if inf['writes']:
            return 'the function writes to the database or sends something'
        if any(STRONG_ACL.search(_record_words(inf, rec)) for rec in inf['records']):
            return 'the function itself checks access (user, permissions, ownership, payment)'
    return ''


def _check_dataflow(path: str, q: str, old: dict, new: dict, old_info: dict, new_info: dict, by_simple: dict,
                    old_globals: dict | None = None, new_globals: dict | None = None) -> None:
    """R7b–R7d for each existing ACCESS check (user, permissions, ownership, visibility, payment…): you
    cannot change how what its condition depends on
    is computed (R7b), change helper functions in its condition (R7d), or add next to it an early
    exit that returns a non-constant
    (R7c). Input-format checks (`if not email`) are not covered by these rules: simple computations
    next to them are normal
    candidates for a fix."""
    def words_of(rec) -> str:
        return _record_words(old, rec)

    if old['dump'] != new['dump'] and any(STRONG_ACL.search(words_of(rec)) for rec in old['records']):
        raise PolicyViolation(f'{path}: structural check (R7e): "{q}" itself checks access (user, permissions, ownership, payment…) — '
                              f'such functions are not edited automatically, only manually.')
    if old['dump'] != new['dump'] and old['writes']:
        raise PolicyViolation(f'{path}: structural check (R7f): "{q}" itself writes to the database or sends something (save/create/delete/update/send…) — '
                              f'such functions are not edited automatically, only manually: any change there can alter WHAT exactly is written.')
    new_lines: dict = {}
    for rec in sorted(new['records'], key=lambda r: r['line']):
        new_lines.setdefault(rec['key'], []).append(rec['line'])
    seen: Counter = Counter()
    sensitive_function = False
    for rec in sorted(old['records'], key=lambda r: r['line']):
        index = seen[rec['key']]
        seen[rec['key']] += 1
        lines = new_lines.get(rec['key'], [])
        if index >= len(lines):
            continue                                        # the vanished check was already rejected by R7
        old_events = _window(old, rec['line'])
        text = words_of(rec)
        if not (ACL_WORDS.search(text) or STRONG_ACL.search(text)):
            continue
        closure = _closure(rec['names'], old_events)
        sensitive_function = True
        before = Counter(key for root, _n, key, _l in old_events if root in closure)
        after = Counter(key for root, _n, key, _l in _window(new, lines[index]) if root in closure)
        if before != after:
            key = next(iter((after - before) or (before - after)))
            name, _sep, stmt = key.partition('|')
            raise PolicyViolation(f'{path}: structural check (R7b): in "{q}" the assignment of the variable "{name}" was changed or added, on which '
                                  f'an existing guard check depends ("{stmt[:80]}"). Do not change how it is computed — add a new '
                                  f'check next to it.')
        for name in closure & set(old_globals or ()):                       # a constant the check depends on is not changed
            if old_globals[name] != (new_globals or {}).get(name, Counter()):
                raise PolicyViolation(f'{path}: structural check (R7g): the value "{name}", on which the access check in "{q}" depends, '
                                      f'was changed — this is not edited automatically.')
        for name in (closure | rec['calls']) - {q.rsplit('.', 1)[-1]}:      # replacing the result of a helper function bypasses the condition
            for helper in by_simple.get(name, ()):
                if helper != q and old_info[helper]['dump'] != new_info.get(helper, {}).get('dump'):
                    raise PolicyViolation(f'{path}: structural check (R7d): "{helper}" takes part in the condition of the guard check "{q}" — '
                                          f'it must not be changed automatically.')
    added_returns = new['returns'] - old['returns']
    if (sensitive_function or old['scoped']) and added_returns:
        raise PolicyViolation(f'{path}: structural check (R7c): in "{q}" (it has access checks or a selection by visibility) a new early exit returns a non-'
                              f'constant: "{next(iter(added_returns))}". New early exits here may return only constants, '
                              f'empty collections or an error response (4xx).')


def count_tests(src: str) -> int:
    """How many test* methods are in the file's classes (the host compares with the sandbox's “Ran N
tests”)."""
    tree = _parse(src, 'test')
    total = 0
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            total += sum(1 for st in node.body if isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef)) and st.name.startswith('test'))
    return total


# ── Full patch check ─────────────────────────────────────────────────────────

def check_patch(text: str, *, require_test: bool = True, read_old=None) -> Report:
    """Full check of a patch against the policy. Raises PolicyViolation with a human-readable
    explanation.

    `read_old(path) -> str | None` returns the CURRENT contents of the file in the repository; with
    it the structural layer also works
    (the new version of the file is reconstructed from the hunks). All production calls (agent,
    sandbox, rollout) pass `read_old`;
    without it only the text layer is checked (handy for quick tests)."""
    changes = parse_patch(text)
    seen = set()
    code, tests = [], []
    for ch in changes:
        path = normalize(ch.path)
        if not path or path != ch.path:
            raise PolicyViolation('Suspicious path in the patch.')
        if path in seen:
            raise PolicyViolation(f'The file {path} is listed twice.')
        seen.add(path)
        kind = classify(path)
        if kind is None:
            raise PolicyViolation(f'The Analyst has no right to change the file {path} (it is outside the allowed list).')
        if ch.is_new and kind != 'test':
            raise PolicyViolation(f'New files are allowed only for tests, not {path}.')
        if kind == 'test' and not ch.is_new:
            raise PolicyViolation(f'Existing tests cannot be changed ({path}): only new test files can be added. '
                                  f'Otherwise a test could be "fixed" instead of the error.')
        (code if kind == 'code' else tests).append(ch)
    if not code:
        raise PolicyViolation('A fix without a code change is not needed: the patch contains only tests.')
    if require_test and not tests:
        raise PolicyViolation('A test that reproduces the error must be added to the fix.')
    if len(code) > MAX_CODE_FILES:
        raise PolicyViolation(f'Too many code files: {len(code)} (allowed {MAX_CODE_FILES}).')
    if len(tests) > MAX_TEST_FILES:
        raise PolicyViolation(f'Too many test files: {len(tests)} (allowed {MAX_TEST_FILES}).')
    code_lines, test_lines = sum(c.lines for c in code), sum(c.lines for c in tests)
    if code_lines > MAX_CODE_LINES:
        raise PolicyViolation(f'The fix is too large: {code_lines} lines of code (allowed {MAX_CODE_LINES}).')
    if test_lines > MAX_TEST_LINES:
        raise PolicyViolation(f'The tests are too large: {test_lines} lines (allowed {MAX_TEST_LINES}).')
    for ch in code:
        _scan(ch.added, CODE_BANNED, ch.path, 'added')
        for text_line in ch.removed:
            if PROTECTED_REMOVALS.search(text_line):
                raise PolicyViolation(f'{ch.path}: a line with protection or an access check, with error logging '
                                      f'or with escaping was changed or removed: "{text_line.strip()[:80]}".')
    for ch in tests:
        _scan(ch.added, TEST_BANNED, ch.path, 'added')
    expected = 0
    if read_old is not None:
        for ch in code + tests:
            old = '' if ch.is_new else read_old(ch.path)
            if old is None:
                raise PolicyViolation(f'{ch.path}: there is no current file content for the structural check.')
            kind = classify(ch.path)
            problem = file_problem(old) if not ch.is_new else ''
            if problem:
                raise PolicyViolation(f'{ch.path}: {problem} — such a file cannot be fixed automatically.')
            new = apply_hunks(old, ch.hunks)
            check_structure(ch.path, kind, old, new)
            if kind == 'test':
                expected += count_tests(new)
        if tests and expected < 1:
            raise PolicyViolation('The added test files contain no test* method.')
    return Report(files=[c.path for c in changes], code_files=[c.path for c in code], test_files=[c.path for c in tests],
                  code_lines=code_lines, test_lines=test_lines, tests_expected=expected,
                  owner_only=[c.path for c in code if owner_only(c.path)])


def check_applied(text: str, read_old, read_new) -> None:
    """Check of what ACTUALLY came out after `git apply`: the structure of every changed file — old
    contents versus new contents
    from disk. Guards against a mismatch between our model of applying hunks and real git."""
    for ch in parse_patch(text):
        kind = classify(ch.path)
        if kind is None:
            raise PolicyViolation(f'The Analyst has no right to change the file {ch.path}.')
        new = read_new(ch.path)
        if new is None:
            raise PolicyViolation(f'{ch.path}: the file does not exist after applying.')
        old = '' if ch.is_new else read_old(ch.path)
        if old is None:
            raise PolicyViolation(f'{ch.path}: there is no original file content.')
        check_structure(ch.path, kind, old, new)


def split_patch(text: str) -> tuple[str, str]:
    """(test part only, code part only) — for the “base + new tests, no fix yet” run."""
    tests, code = [], []
    for ch in parse_patch(text):
        (tests if classify(ch.path) == 'test' else code).append('\n'.join(ch.raw))
    join = lambda parts: ('\n'.join(parts) + '\n') if parts else ''   # noqa: E731
    return join(tests), join(code)


def _compute_sha() -> str:
    data = Path(__file__).read_bytes().replace(b'\r\n', b'\n')
    return hashlib.sha256(data).hexdigest()[:16]


_SHA = _compute_sha()           # once at load time: the fingerprint describes the RULES IN MEMORY, not a file that could have been swapped


def policy_sha() -> str:
    """Fingerprint of this file at load time (CRLF → LF, so that it matches across Windows, the image and
the VPS)."""
    return _SHA
