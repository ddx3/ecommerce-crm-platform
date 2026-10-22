"""Shared launch of the auto-fix LLM agents (fixer and reviewer).

The same pattern, proven in production, as in the purchasing agent (`analyst/agent.py`): no built-in
CLI tools
(`tools=[]`), only our in-process MCP server, deny-by-default (`permission_mode='dontAsk'`), the
Claude subscription first,
the paid API only as a fallback on a limit, a separate daily token budget of the Analyst.

Failures of the LLM itself (subscription limit, exhausted budget, expired token, API error) are NOT
an agent decision: they are raised as
`LLMUnavailable` with a hint of when to retry, and the pipeline postpones the step without counting
a fix attempt.
"""
from __future__ import annotations

import json
import logging
import os
import time

from asgiref.sync import sync_to_async
from django.conf import settings

log = logging.getLogger('analyst')

# ResultMessage subtypes where is_error=True means an infrastructure failure, not exhausted turns or
# session budget.
INFRA_SUBTYPES = ('success', 'error_during_execution')


class LLMUnavailable(RuntimeError):
    """LLM temporarily unavailable (subscription/budget limit, authentication, API error). `wait` — how
many seconds until retrying."""

    def __init__(self, message: str, wait: int = 3600):
        super().__init__(message)
        self.wait = wait


def content(obj) -> dict:
    return {'content': [{'type': 'text', 'text': json.dumps(obj, ensure_ascii=False, default=str)}]}


def error(msg: str) -> dict:
    return {'content': [{'type': 'text', 'text': msg}], 'is_error': True}


def tool_runner():
    """Tool wrapper: runs the session's sync method in a thread, closes its DB connection, a failure
doesn't crash the run."""
    def in_thread(fn, *args, **kw):
        from django.db import connections
        try:
            return fn(*args, **kw)
        finally:
            connections.close_all()

    async def run(fn, *args, **kw):
        try:
            return content(await sync_to_async(in_thread)(fn, *args, **kw))
        except Exception as exc:
            log.exception('analyst codefix tool failed')
            return error(f'Tool error: {exc}')
    return run


def _options(system_prompt, server, tools, names, *, api_key, model, max_turns):
    from claude_agent_sdk import ClaudeAgentOptions, create_sdk_mcp_server
    from core.llm import _env_for
    from analyst.agent import _thinking
    opts = dict(
        system_prompt=system_prompt, model=model, tools=[],
        mcp_servers={server: create_sdk_mcp_server(name=server, version='1.0.0', tools=tools)},
        allowed_tools=[f'mcp__{server}__{n}' for n in names],
        permission_mode='dontAsk', strict_mcp_config=True, setting_sources=None,
        thinking=_thinking(), max_turns=max_turns, env=_env_for(api_key),
        max_budget_usd=float(getattr(settings, 'ANALYST_SESSION_MAX_USD', 8.0)),
    )
    explicit = getattr(settings, 'CLAUDE_BIN', '')
    if explicit and os.path.exists(explicit):
        opts['cli_path'] = explicit
    return ClaudeAgentOptions(**opts)


def _wait_for(exc) -> int:
    """How long to wait after a subscription limit: until the reset time if the CLI named it (with a
margin), otherwise an hour."""
    resets_at = getattr(exc, 'resets_at', None)
    if resets_at:
        return max(300, min(int(resets_at - time.time()) + 120, 8 * 3600))
    return 3600


def run_session(system_prompt: str, prompt: str, server: str, tools: list, names: list[str], *,
                timeout: int, max_turns: int) -> dict:
    """One agent run. Returns statistics. Raises LLMUnavailable if the LLM is unavailable (postpone);
    any other exception is a failure
    of the run itself (timeout, etc.)."""
    from analyst.agent import _drive
    from core.limits import LimitExceeded, enforce_analyst_llm, record_analyst_llm_tokens
    from core.llm import SubscriptionLimit, _is_rate_limit, _run_sync

    try:
        enforce_analyst_llm()
    except LimitExceeded as exc:
        raise LLMUnavailable(str(exc), 3600) from exc
    model = getattr(settings, 'ANALYST_MODEL', '') or getattr(settings, 'CLAUDE_MODEL', 'sonnet')
    reserve_key = settings.ANTHROPIC_API_KEY or None

    def attempt(api_key):
        t0, stats = time.time(), {}
        try:
            opts = _options(system_prompt, server, tools, names, api_key=api_key, model=model, max_turns=max_turns)
            _run_sync(_drive(None, opts, prompt, timeout, stats))
        finally:
            if stats.get('tokens'):                       # also for failed sessions: the tokens have already been spent
                record_analyst_llm_tokens(stats['tokens'])
        stats['seconds'] = round(time.time() - t0, 1)
        if stats.get('is_error') and stats.get('subtype') in INFRA_SUBTYPES:
            raise LLMUnavailable(f'The LLM returned an error: {stats.get("result_text") or "no explanation"}', 3600)
        return stats

    try:
        if getattr(settings, 'LLM_PREFER_API', False):
            if not reserve_key:
                raise RuntimeError('LLM_PREFER_API=True, but ANTHROPIC_API_KEY is not set')
            return attempt(reserve_key)
        try:
            return attempt(None)
        except Exception as exc:
            if reserve_key and _is_rate_limit(exc):
                log.warning('Subscription rate-limited, the auto-fix agent uses the paid API: %s', exc)
                return attempt(reserve_key)
            raise
    except SubscriptionLimit as exc:
        raise LLMUnavailable(str(exc), _wait_for(exc)) from exc
