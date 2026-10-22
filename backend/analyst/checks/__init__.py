"""Registry of Analyst checks.

A check is a plain function `fn(now) -> list[Item]` with no side effects: it only reads the DB
and returns findings. Writing, deduplication and closing of vanished findings is done by the runner.
A check that failed does not close its findings (see runner.apply).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable


@dataclass
class Item:
    """A single finding. `check` is set by the runner from the check's name."""
    key: str
    severity: int
    title: str
    body: str = ''
    data: dict = field(default_factory=dict)
    link: str = ''
    check: str = ''
    section: str = ''


CheckFn = Callable[[datetime], 'list[Item]']
REGISTRY: dict[str, tuple[str, CheckFn]] = {}


def check(name: str, section: str):
    """Registers a check under the name `name` in section `section` (Finding.SECTION_*)."""
    def deco(fn: CheckFn) -> CheckFn:
        REGISTRY[name] = (section, fn)
        return fn
    return deco


def all_checks() -> dict[str, tuple[str, CheckFn]]:
    from . import code_errors, health  # noqa: F401 — the import registers the checks
    return REGISTRY
