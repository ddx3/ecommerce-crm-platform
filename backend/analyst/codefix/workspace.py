"""Working copy of the code for the fixer agent: everything it may see and change.

The agent gets no shell, no network and no access to live files: only these methods over a TEMPORARY
copy
of the `backend/` directory. The copy has no .env, keys, media, virtual environment or symlinks.
Changes are allowed only to files that the policy permits (`analyst/codefix_policy.py`); the patch
is the difference
between the copy and the live code, so whatever is unchanged is not in the patch.
"""
from __future__ import annotations

import fnmatch
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from analyst import codefix_policy as policy

READ_SUFFIXES = {'.py', '.html', '.txt', '.md', '.json', '.css', '.js', '.yml', '.yaml', '.toml', '.cfg', '.ini'}
SKIP_DIRS = {'media', 'venv', 'env', '__pycache__', 'staticfiles', 'node_modules', 'backups', '.git', 'static', 'scripts',
             'migrations_backup'}
SKIP_FILES = ('*.pyc', '*.pyo', '*.sqlite3', '*.dump', '*.pem', '*.key', '*.p12', '*.crt', '.env*', '*.env', '*secret*',
              '*credential*')
MAX_COPY_BYTES = 400_000
MAX_READ_LINES = 250
MAX_EDIT_BYTES = policy.MAX_FILE_BYTES
MAX_TEST_BYTES = 12_000
MAX_LIST = 300
MAX_GREP_RESULTS = 60
MAX_GREP_FILES = 3000
GREP_TIMEOUT = 8
GREP_LINE_CHARS = 2000
TEST_APPS = ('catalog', 'blog', 'courses', 'core')


class WorkspaceError(Exception):
    """The text is returned to the agent as the tool response."""


# Search child process: reads JSON from stdin, prints a JSON list of matches. A separate process can
# be killed at the deadline.
# (no backslashes in the text: chr(92) is `\`, chr(10) is `\n`)
_GREP_CHILD = """
import json, re, sys
cfg = json.loads(sys.stdin.read())
rx = re.compile(cfg['pattern'])
BS, NL = chr(92), chr(10)
base, found = cfg['base'].replace(BS, '/').rstrip('/') + '/', []
for name in cfg['files']:
    try:
        text = open(name, 'rb').read().decode('utf-8', 'replace')
    except OSError:
        continue
    rel = name.replace(BS, '/')
    rel = rel[len(base):] if rel.startswith(base) else rel
    for n, line in enumerate(text.split(NL), 1):
        if len(line) > cfg['line_chars']:
            continue
        if rx.search(line):
            found.append('backend/%s:%d: %s' % (rel, n, line.strip()[:200]))
            if len(found) >= cfg['limit']:
                print(json.dumps(found))
                sys.exit(0)
print(json.dumps(found))
"""


class Workspace:
    def __init__(self, source, root=None):
        self.source = Path(source).resolve()
        self.root = Path(root or tempfile.mkdtemp(prefix='codefix-')).resolve()
        self.dir = self.root / 'backend'
        self.touched: dict[str, bool] = {}           # path in the repository → created as new

    # ── lifecycle ──
    def create(self) -> 'Workspace':
        def ignore(directory, names):
            skipped = []
            for name in names:
                full = os.path.join(directory, name)
                if os.path.islink(full):
                    skipped.append(name)
                elif os.path.isdir(full):
                    if name in SKIP_DIRS:
                        skipped.append(name)
                elif any(fnmatch.fnmatch(name.lower(), pat) for pat in SKIP_FILES) or os.path.getsize(full) > MAX_COPY_BYTES:
                    skipped.append(name)
            return skipped
        shutil.copytree(self.source, self.dir, ignore=ignore)
        return self

    def cleanup(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def __enter__(self) -> 'Workspace':
        return self.create()

    def __exit__(self, *exc) -> None:
        self.cleanup()

    # ── paths ──
    def _resolve(self, path: str) -> tuple[str, Path]:
        repo = policy.normalize(path)
        if not repo or not (repo == 'backend' or repo.startswith('backend/')):
            raise WorkspaceError('The path must be from the repository root and start with backend/, e.g. backend/catalog/views.py.')
        parts = repo[len('backend/'):].split('/') if repo != 'backend' else []
        if any(fnmatch.fnmatch(part.lower(), pat) for part in parts for pat in SKIP_FILES) \
                or any(part in SKIP_DIRS for part in parts):
            raise WorkspaceError(f'The file {repo} is not available.')
        target = self.dir.joinpath(*parts)
        probe = self.dir
        for part in parts:
            probe = probe / part
            if probe.is_symlink():
                raise WorkspaceError('Symbolic links are not available.')
        if not target.resolve().is_relative_to(self.dir.resolve()):
            raise WorkspaceError('The path goes outside the project.')
        return repo, target

    @staticmethod
    def _text(path: Path) -> str:
        if path.suffix not in READ_SUFFIXES:
            raise WorkspaceError('This file type cannot be read.')
        data = path.read_bytes()
        if b'\r\n' in data:
            raise WorkspaceError('A file with Windows line endings (CRLF) is not supported.')
        return data.decode('utf-8', errors='replace')

    # ── reading ──
    def list_files(self, path: str = 'backend') -> dict:
        repo, target = self._resolve(path)
        if not target.is_dir():
            raise WorkspaceError(f'{repo} is not a directory.')
        entries = []
        for child in sorted(target.iterdir(), key=lambda c: (not c.is_dir(), c.name)):
            if child.name in SKIP_DIRS or child.is_symlink():
                continue
            entries.append(child.name + ('/' if child.is_dir() else ''))
        return {'path': repo, 'entries': entries[:MAX_LIST], 'truncated': len(entries) > MAX_LIST}

    def read_file(self, path: str, start: int = 1, end: int | None = None) -> dict:
        repo, target = self._resolve(path)
        if not target.is_file():
            raise WorkspaceError(f'The file {repo} does not exist.')
        lines = self._text(target).split('\n')
        total = len(lines)
        start = max(1, int(start or 1))
        end = min(int(end or start + MAX_READ_LINES - 1), start + MAX_READ_LINES - 1, total)
        body = '\n'.join(f'{n:>5}| {lines[n - 1]}' for n in range(start, end + 1))
        return {'path': repo, 'total_lines': total, 'from': start, 'to': end, 'text': body}

    def grep(self, pattern: str, path: str = 'backend', glob: str = '*.py', limit: int = 40) -> dict:
        """Regex search. The expression from the model may be 'catastrophic' (nested quantifiers): `re`
        holds the GIL and can't be interrupted,
        so the search runs in a separate process with a hard deadline — the analyst service doesn't
        hang."""
        if not isinstance(pattern, str) or not pattern or len(pattern) > 300:
            raise WorkspaceError('The pattern must be a string of up to 300 characters.')
        try:
            re.compile(pattern)
        except re.error as exc:
            raise WorkspaceError(f'Invalid regular expression: {exc}')
        repo, root = self._resolve(path)
        files = [root] if root.is_file() else sorted(root.rglob('*'))
        limit = max(1, min(int(limit or 40), MAX_GREP_RESULTS))
        picked, scanned = [], 0
        for f in files:
            if scanned >= MAX_GREP_FILES:
                break
            if not f.is_file() or f.is_symlink() or f.suffix not in READ_SUFFIXES or not fnmatch.fnmatch(f.name, str(glob or '*.py')):
                continue
            if any(part in SKIP_DIRS for part in f.relative_to(self.dir).parts):
                continue
            scanned += 1
            picked.append(f)
        payload = json.dumps({'pattern': pattern, 'limit': limit, 'line_chars': GREP_LINE_CHARS,
                              'files': [str(f) for f in picked], 'base': str(self.dir)})
        try:
            proc = subprocess.run([sys.executable, '-I', '-c', _GREP_CHILD], input=payload, capture_output=True, text=True,
                                  timeout=GREP_TIMEOUT)
        except subprocess.TimeoutExpired:
            raise WorkspaceError('The search was aborted: this regular expression is too slow (catastrophic). Simplify it or '
                                 'search with a plain string.')
        if proc.returncode != 0:
            raise WorkspaceError('The search failed.')
        try:
            return {'pattern': pattern, 'matches': json.loads(proc.stdout)[:limit]}
        except ValueError:
            raise WorkspaceError('The search failed.')

    # ── changes ──
    def edit_file(self, path: str, old: str, new: str) -> dict:
        repo, target = self._resolve(path)
        kind = policy.classify(repo)
        if kind is None:
            raise WorkspaceError(f'The Analyst has no right to change the file {repo} (policy: only selected modules of the catalog, '
                                 f'blog, courses and helper core/* files, and their tests are allowed). If the cause of the error is in such a file — '
                                 f'call give_up.')
        if not target.is_file():
            raise WorkspaceError(f'The file {repo} does not exist.')
        if kind == 'test' and not self.touched.get(repo):
            raise WorkspaceError(f'Existing tests cannot be changed ({repo}): add a new test through add_test.')
        if not isinstance(old, str) or not old:
            raise WorkspaceError('old cannot be empty.')
        live = self.live_text(repo)
        problem = policy.file_problem(live) if live is not None else ''
        if problem:
            raise WorkspaceError(f'This file cannot be fixed automatically ({problem}). Call give_up.')
        text = self._text(target)
        count = text.count(old)
        if count == 0:
            raise WorkspaceError('The fragment old was not found (an exact match is needed, including indentation).')
        if count > 1:
            raise WorkspaceError(f'The fragment old occurs {count} times — add lines of context to make it unique.')
        updated = text.replace(old, str(new), 1)
        if updated == text:
            raise WorkspaceError('Nothing changed.')
        self._validate(repo, kind, updated, str(new).split('\n'))
        target.write_text(updated, encoding='utf-8', newline='\n')
        self.touched.setdefault(repo, False)
        return {'ok': True, 'path': repo, 'lines_in_file': updated.count('\n') + 1}

    def live_text(self, repo: str) -> str | None:
        """File content in the LIVE code (before any agent edits) or None if there is no such file. No newline
translation."""
        path = self.source / repo[len('backend/'):]
        if path.is_symlink() or not path.is_file():
            return None
        return path.read_bytes().decode('utf-8', errors='replace')

    def add_test(self, app: str, name: str, content: str) -> dict:
        if app not in TEST_APPS:
            raise WorkspaceError(f'app must be one of: {", ".join(TEST_APPS)}.')
        if not re.fullmatch(r'[a-z0-9_]{1,30}', str(name or '')):
            raise WorkspaceError('name: Latin letters, digits and _, up to 30 characters.')
        repo = f'backend/{app}/tests_autofix_{name}.py'
        repo, target = self._resolve(repo)
        if policy.classify(repo) != 'test':
            raise WorkspaceError('The test path is not allowed.')
        if target.exists():
            raise WorkspaceError(f'{repo} already exists.')
        content = str(content or '')
        if not content.endswith('\n'):
            content += '\n'
        if len(content.encode('utf-8')) > MAX_TEST_BYTES:
            raise WorkspaceError(f'The test is too large (over {MAX_TEST_BYTES // 1000} KB).')
        self._validate(repo, 'test', content, content.split('\n'))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding='utf-8', newline='\n')
        self.touched[repo] = True
        return {'ok': True, 'path': repo}

    def _validate(self, repo: str, kind: str, text: str, added: list[str]) -> None:
        if len(text.encode('utf-8')) > MAX_EDIT_BYTES:
            raise WorkspaceError('The file is too large after the edit.')
        try:
            compile(text[1:] if text.startswith(chr(0xFEFF)) else text, repo, 'exec')       # a BOM at the start of a file is a normal thing
        except (SyntaxError, ValueError) as exc:
            raise WorkspaceError(f'The file does not compile after the edit: {getattr(exc, "msg", exc)} '
                                 f'(line {getattr(exc, "lineno", "?")}). The edit was not saved.')
        old = '' if kind == 'test' else (self.live_text(repo) or '')
        try:
            policy.check_added(kind, repo, added)
            policy.check_structure(repo, kind, old, text)
        except policy.PolicyViolation as exc:
            raise WorkspaceError(f'The policy forbids it: {exc} The edit was not saved.')

    # ── patch ──
    def diff(self) -> str:
        """Unified diff of changes against the live code (paths a/backend/…, b/backend/…). Lines are
        separated only by \\n: that is how both our patch
        parser and git apply understand them (unlike splitlines, which also splits on U+2028, \\x0c,
        \\x85).
        Code first, then tests: so the diff in the card and for the reviewer isn't crowded out by a
        test."""
        order = sorted(self.touched, key=lambda repo: (policy.classify(repo) != 'code', repo))
        parts = []
        for repo in order:
            rel = repo[len('backend/'):]
            new_text = (self.dir / rel).read_bytes().decode('utf-8')
            is_new = self.touched[repo]
            old_text = '' if is_new else (self.live_text(repo) or '')
            try:
                parts.append(policy.diff_text(repo, old_text, new_text))
            except policy.PolicyViolation as exc:
                raise WorkspaceError(str(exc))
        return ''.join(parts)
