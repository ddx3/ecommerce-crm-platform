"""Safe handling of files in the `shop-helper` queue. Standard library only.

The queue's `requests/` and `artifacts/` directories are mounted writable into the web/analyst
containers, so everything that lies there is
HOSTILE data: a container (root in its own namespace) can put there a symlink to any host file, a
named pipe (FIFO) that blocks
reads forever, or a giant file. The helper runs as root on the host, so such paths must not be
trusted. Rules of this module:

  * reading - only via `os.open(O_NOFOLLOW | O_NONBLOCK)` + `fstat`: a regular file, bounded size,
    NO following of
    symlinks and no blocking on a FIFO;
  * writing - only via a temporary file with a random name, created with `O_EXCL | O_NOFOLLOW`, then
    `os.replace`
    (rename replaces the directory entry itself rather than writing through a link);
  * directory traversal - `os.scandir` without following symlinks; symlinks, FIFOs and sockets are
    treated as junk.
"""
from __future__ import annotations

import os
import stat
import uuid

O_NOFOLLOW = getattr(os, 'O_NOFOLLOW', 0)
O_NONBLOCK = getattr(os, 'O_NONBLOCK', 0)
O_CLOEXEC = getattr(os, 'O_CLOEXEC', 0)
O_BINARY = getattr(os, 'O_BINARY', 0)


def is_real_dir(path) -> bool:
    """A real directory (not a symlink to a directory)."""
    try:
        return stat.S_ISDIR(os.lstat(path).st_mode)
    except OSError:
        return False


def read_regular(path, max_bytes: int) -> bytes | None:
    """Contents of a regular file or None: missing, symlink, FIFO, directory, larger than `max_bytes`."""
    path = os.fspath(path)
    if not O_NOFOLLOW:                       # platform without O_NOFOLLOW (dev Windows only)
        try:
            if stat.S_ISLNK(os.lstat(path).st_mode):
                return None
        except OSError:
            return None
    try:
        fd = os.open(path, os.O_RDONLY | O_NOFOLLOW | O_NONBLOCK | O_CLOEXEC | O_BINARY)
    except OSError:
        return None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > max_bytes:
            return None
        chunks, left = [], max_bytes + 1
        while left > 0:
            part = os.read(fd, min(65536, left))
            if not part:
                break
            chunks.append(part)
            left -= len(part)
        data = b''.join(chunks)
        return data if len(data) <= max_bytes else None
    except OSError:
        return None
    finally:
        os.close(fd)


def write_atomic(path, data: bytes, mode: int = 0o644) -> None:
    """Atomic write: a random temporary file next to the target (O_EXCL, without following symlinks) +
os.replace."""
    path = os.fspath(path)
    directory, name = os.path.split(path)
    tmp = os.path.join(directory, f'.{name}.{uuid.uuid4().hex}.tmp')
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | O_NOFOLLOW | O_CLOEXEC | O_BINARY, mode)
    try:
        view = memoryview(data)
        while len(view):
            view = view[os.write(fd, view):]
    except BaseException:
        os.close(fd)
        unlink_quiet(tmp)
        raise
    os.close(fd)
    try:
        os.replace(tmp, path)
    except BaseException:
        unlink_quiet(tmp)
        raise


def unlink_quiet(path) -> bool:
    try:
        os.unlink(path)
        return True
    except OSError:
        return False


def scan(dirpath, suffix: str = '', limit: int = 500) -> tuple[list[str], list[str]]:
    """(names of regular files, names of junk): symlinks, FIFOs, sockets, devices. Directories are
    ignored.
    Hidden files (starting with a dot) are skipped - these are the helper's own temporary files."""
    regular, junk = [], []
    try:
        with os.scandir(dirpath) as entries:
            for entry in entries:
                if entry.name.startswith('.') or (suffix and not entry.name.endswith(suffix)):
                    continue
                try:
                    if entry.is_file(follow_symlinks=False):
                        regular.append(entry.name)
                    elif not entry.is_dir(follow_symlinks=False):
                        junk.append(entry.name)
                except OSError:
                    junk.append(entry.name)
    except OSError:
        return [], []
    return sorted(regular)[:limit], sorted(junk)[:limit]


def prune_old(dirpath, cutoff_ts: float, keep: tuple = ()) -> int:
    """Deletes regular files older than `cutoff_ts` (mtime) and any junk. Symlinks are deleted
    themselves (unlink does not
    follow them), directories are left alone. Returns how many were deleted."""
    if not is_real_dir(dirpath):
        return 0
    removed = 0
    try:
        with os.scandir(dirpath) as entries:
            for entry in entries:
                if entry.name in keep:
                    continue
                try:
                    if entry.is_dir(follow_symlinks=False):
                        continue
                    if entry.is_file(follow_symlinks=False) and entry.stat(follow_symlinks=False).st_mtime >= cutoff_ts:
                        continue
                    if unlink_quiet(entry.path):
                        removed += 1
                except OSError:
                    continue
    except OSError:
        return removed
    return removed
