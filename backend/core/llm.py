"""Single entry point for LLM calls for the admin's text features.

Text generation (descriptions, characteristics, cleanup, block extraction, bundle composition) goes
through the **Claude Code subscription** (Max/Pro): the official `claude-agent-sdk` launches the
local `claude` binary and streams the reply. `ANTHROPIC_API_KEY` is STRIPPED from the child process
environment so the CLI goes via the OAuth subscription (`claude setup-token`) rather than paid API
billing.

Fallback (only if `ANTHROPIC_API_KEY` is set): on a subscription rate limit the same SDK, but with
the key in env -> paid API. `LLM_PREFER_API=True` -> go straight to the API.

Why the SDK and not `subprocess.run([claude, ...])`: launching raw `claude.CMD` as a subprocess from
a web process (daphne/ASGI, especially on Windows) hung and died on a 180s timeout. The SDK drives
the child process through an async stream correctly - no pipe deadlocks.

Embeddings (invoice/bundle matching) are NOT handled here - that is Gemini.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import json
import logging
import os
import re
import shutil

from django.conf import settings

logger = logging.getLogger(__name__)


def _env_for(api_key: str | None) -> dict:
    """Environment for the child `claude`.

    No key → subscription: ANTHROPIC_API_KEY is EXPLICITLY blanked. Simply not putting it in the
    dict is not enough: the SDK builds the environment
    as {**os.environ, **options.env}, so the key from the container environment would remain and the
    CLI would go to the paid API.
    With a key → deliberately the paid API (fallback)."""
    env = dict(os.environ)
    env['ANTHROPIC_API_KEY'] = api_key or ''
    return env


def claude_available() -> bool:
    """The SDK launches the `claude` binary - check that it exists."""
    explicit = getattr(settings, 'CLAUDE_BIN', '')
    if explicit and os.path.exists(explicit):
        return True
    return bool(shutil.which('claude') or shutil.which('claude.cmd'))


_FENCE_RE = re.compile(r'^```(?:json)?\s*|\s*```$', re.IGNORECASE)


def _extract_json(text: str):
    """Extract JSON from the model's response, even if it wrapped it in a ```-fence
    or appended an explanation."""
    text = text.strip()
    text = _FENCE_RE.sub('', text).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    start = min((i for i in (text.find('{'), text.find('[')) if i != -1), default=-1)
    if start == -1:
        raise ValueError('No JSON found in the LLM response')
    return json.loads(text[start:text.rfind('}') + 1] if text[start] == '{'
                      else text[start:text.rfind(']') + 1])


_SYSTEM_JSON = (
    "You are a text-processing tool in an API. Follow the user's instruction and "
    "return ONLY a valid JSON object in response — without markdown fences, "
    "without explanations, without any text before or after the JSON."
)

# The CLI subscription rate limit is reported as text - we catch it by substrings (best-effort).
_RATE_LIMIT_SIGNS = (
    'rate limit', 'rate_limit', 'ratelimit', 'usage limit', 'usage_limit',
    '429', 'overloaded', 'quota', 'too many requests', 'hit your limit',
)


class SubscriptionLimit(RuntimeError):
    """Claude subscription limit exhausted (5-hour or weekly window).

    Nothing is remembered: the next call simply goes to the CLI and succeeds as soon as
    the window resets — no manual «unlock». The reset time is taken from the CLI itself
    (if it has none — the text has no time)."""

    def __init__(self, resets_at: int | None = None):
        self.resets_at = resets_at
        super().__init__(self._text(resets_at))

    @staticmethod
    def _text(resets_at: int | None) -> str:
        head = 'The Claude subscription limit is exhausted.'
        if not resets_at:
            return f'{head} Try pressing the button again a bit later.'
        from datetime import datetime, timezone as dt_tz
        from django.utils import timezone
        at = timezone.localtime(datetime.fromtimestamp(resets_at, tz=dt_tz.utc))
        when = f'at {at:%H:%M}' if at.date() == timezone.localdate() else f'{at:%d.%m} at {at:%H:%M}'
        return f'{head} It resets {when} — then press the button again.'


class _LimitWatch:
    """Looks at the SDK messages and raises SubscriptionLimit when the CLI responded with an
    actual limit refusal (not just «approaching»: allowed_warning is ignored).

    Without this the refusal arrived as the text «You've hit your limit…» and was disguised as
    "no JSON" (3 useless retries) or "the agent did not
    finish processing"."""

    def __init__(self):
        self.resets_at: int | None = None

    def feed(self, message) -> None:
        from claude_agent_sdk import AssistantMessage, RateLimitEvent, ResultMessage
        if isinstance(message, RateLimitEvent):
            info = message.rate_limit_info
            if info.status == 'rejected':
                self.resets_at = info.resets_at or self.resets_at
        elif isinstance(message, AssistantMessage):
            if message.error == 'rate_limit':
                raise SubscriptionLimit(self.resets_at)
        elif isinstance(message, ResultMessage) and message.is_error:
            text = (message.result or '').lower()
            if message.api_error_status == 429 or any(s in text for s in _RATE_LIMIT_SIGNS):
                raise SubscriptionLimit(self.resets_at)


def _is_rate_limit(exc: Exception) -> bool:
    if isinstance(exc, SubscriptionLimit):
        return True
    s = str(exc).lower()
    return any(sign in s for sign in _RATE_LIMIT_SIGNS)


def _tokens_of(message) -> int:
    """The 'weight' of a usage message for the daily token budget: input + output + cache write ×1.25 +
    cache read ×0.1 (the API bills it the same way).
    Previously the cache was not counted, and in a multi-turn session almost all input is cached —
    the budget limited almost nothing."""
    u = getattr(message, 'usage', None) or {}

    def part(name: str) -> int:
        return int(u.get(name) or 0)
    return int(part('input_tokens') + part('output_tokens') + 1.25 * part('cache_creation_input_tokens')
               + 0.1 * part('cache_read_input_tokens'))


async def _query_text(prompt: str, *, system: str, model: str,
                      api_key: str | None, timeout: int) -> tuple[str, int]:
    """A single tool-less call to the model via the SDK. Returns (text, tokens)."""
    from claude_agent_sdk import (
        AssistantMessage, ClaudeAgentOptions, ResultMessage, TextBlock, query,
    )
    opts = dict(
        system_prompt=system,
        model=model,
        max_turns=1,
        # Turn off extended thinking: these tasks don't need it, and it slows the
        # call down ~8x (41s → 5s on a bundle). The old direct API didn't think
        # either — hence «fast and crisp». Doesn't affect answer quality.
        thinking={'type': 'disabled'},
        allowed_tools=[],               # pure generation without Read/Bash/MCP ->
                                        # there are no permission prompts, so
        # we do NOT set bypassPermissions: its flag --dangerously-skip-permissions is forbidden by
        # the CLI under root (production container), and there are no tools anyway.
        setting_sources=None,           # do not pull in the project CLAUDE.md/settings
        env=_env_for(api_key),          # <- this is where subscription vs API is decided
    )
    explicit = getattr(settings, 'CLAUDE_BIN', '')
    if explicit and os.path.exists(explicit):
        opts['cli_path'] = explicit
    options = ClaudeAgentOptions(**opts)

    chunks: list[str] = []
    result_text: str | None = None
    tokens = 0
    watch = _LimitWatch()

    async def _consume():
        nonlocal result_text, tokens
        async for message in query(prompt=prompt, options=options):
            watch.feed(message)
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock):
                        chunks.append(block.text)
            elif isinstance(message, ResultMessage):
                if getattr(message, 'result', None):
                    result_text = message.result
                tokens = _tokens_of(message)

    try:
        await asyncio.wait_for(_consume(), timeout=timeout)
    except asyncio.TimeoutError:
        raise RuntimeError(
            f'Timeout {timeout}s — the model did not manage to generate a response '
            f'(request too large or slow subscription)'
        ) from None
    text = (result_text if result_text is not None else ''.join(chunks)).strip()
    return text, tokens


def _run_sync(coro):
    """Runs an async coroutine from sync code (Django view).

    Always in a separate thread with a FRESH event loop - this way we do not clash with the active
    ASGI loop ('loop already running') and can pick ProactorEventLoop on Windows (SelectorEventLoop
    there can NOT do subprocess, which the SDK needs to launch `claude`)."""
    import sys

    def _runner():
        if sys.platform == 'win32':
            loop = asyncio.ProactorEventLoop()
        else:
            loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(coro)
        finally:
            # Unclosed async generators of the SDK (an exception inside `async for`) would leave the
            # `claude` process alive until the container
            # restarts: the asyncio finalizer no longer closes them at loop.close().
            try:
                loop.run_until_complete(loop.shutdown_asyncgens())
            except Exception:
                logger.warning('shutdown_asyncgens failed', exc_info=True)
            loop.close()

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
        return ex.submit(_runner).result()


def text_to_json(prompt: str, *, system: str | None = None,
                 model: str | None = None, timeout: int = 280) -> dict:
    """Main entry point for text LLM tasks (admin buttons, moderation, blog).

    `system` — custom system prompt (by default a strict JSON-only one).
    Subscription (no key). On rate limit with `ANTHROPIC_API_KEY` set —
    fallback to the paid API. `LLM_PREFER_API=True` → API right away."""
    from .limits import enforce_llm, record_llm_tokens
    enforce_llm()  # protect the subscription/API from a loop bug
    system = system or _SYSTEM_JSON
    model = model or getattr(settings, 'CLAUDE_MODEL', 'sonnet')
    reserve_key = settings.ANTHROPIC_API_KEY or None
    prefer_api = getattr(settings, 'LLM_PREFER_API', False)

    if prefer_api:
        if not reserve_key:
            raise RuntimeError('LLM_PREFER_API=True, but ANTHROPIC_API_KEY is not set')
        raw, tok = _run_sync(_query_text(prompt, system=system, model=model,
                                         api_key=reserve_key, timeout=timeout))
        record_llm_tokens(tok)
        return _extract_json(raw)

    # Broken JSON is not a code error but a model slip: the same request a second time usually
    # returns a valid reply. Without a retry, batch runs (dozens of instructions in a row) lost a
    # few items for nothing.
    last_bad = None
    for attempt in range(3):
        try:
            raw, tok = _run_sync(_query_text(prompt, system=system, model=model,
                                             api_key=None, timeout=timeout))
            record_llm_tokens(tok)
            return _extract_json(raw)
        except (json.JSONDecodeError, ValueError) as e:
            last_bad = e
            logger.warning('The LLM returned invalid JSON (attempt %s of 3): %s',
                           attempt + 1, e)
            continue
        except Exception as e:
            if reserve_key and _is_rate_limit(e):
                logger.warning('Claude subscription rate-limited, falling back to the paid API: %s', e)
                raw, tok = _run_sync(_query_text(prompt, system=system, model=model,
                                                 api_key=reserve_key, timeout=timeout))
                record_llm_tokens(tok)
                return _extract_json(raw)
            raise
    raise last_bad


async def _query_image(images: list, prompt: str, *,
                       system: str, model: str, api_key: str | None, timeout: int) -> tuple[str, int]:
    """Vision call: one or several images + text -> (text, tokens).

    images - list of (base64_str, media_type). Via the SDK's stream-json input."""
    from claude_agent_sdk import (
        AssistantMessage, ClaudeAgentOptions, ResultMessage, TextBlock, query,
    )

    async def _input():
        content = [
            {'type': 'image', 'source': {'type': 'base64', 'media_type': mt, 'data': b64}}
            for b64, mt in images
        ]
        content.append({'type': 'text', 'text': prompt})
        yield {'type': 'user', 'message': {'role': 'user', 'content': content}}

    opts = dict(
        system_prompt=system, model=model, max_turns=1,
        thinking={'type': 'disabled'}, allowed_tools=[], setting_sources=None,
        env=_env_for(api_key),
    )
    explicit = getattr(settings, 'CLAUDE_BIN', '')
    if explicit and os.path.exists(explicit):
        opts['cli_path'] = explicit
    options = ClaudeAgentOptions(**opts)

    chunks: list[str] = []
    result_text: str | None = None
    tokens = 0
    watch = _LimitWatch()

    async def _consume():
        nonlocal result_text, tokens
        async for message in query(prompt=_input(), options=options):
            watch.feed(message)
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock):
                        chunks.append(block.text)
            elif isinstance(message, ResultMessage):
                if getattr(message, 'result', None):
                    result_text = message.result
                tokens = _tokens_of(message)

    try:
        await asyncio.wait_for(_consume(), timeout=timeout)
    except asyncio.TimeoutError:
        raise RuntimeError(f'Timeout {timeout}s — the vision request did not finish') from None
    text = (result_text if result_text is not None else ''.join(chunks)).strip()
    return text, tokens


def images_to_json(images: list, *, prompt: str,
                   system: str | None = None, model: str | None = None, timeout: int = 280) -> dict:
    """Vision on the Claude subscription: one or several images -> JSON.

    images - list of (image_bytes, media_type). media_type: image/jpeg|png|webp|gif.
    Subscription is primary, the paid API is the fallback on a rate limit."""
    from .limits import enforce_llm, record_llm_tokens
    enforce_llm()  # protect the subscription/API from a loop bug
    import base64 as _b64
    system = system or _SYSTEM_JSON
    model = model or getattr(settings, 'CLAUDE_MODEL', 'sonnet')
    parts = [(_b64.b64encode(b).decode(), mt) for b, mt in images]
    reserve_key = settings.ANTHROPIC_API_KEY or None
    prefer_api = getattr(settings, 'LLM_PREFER_API', False)

    if prefer_api and reserve_key:
        raw, tok = _run_sync(_query_image(parts, prompt, system=system,
                                          model=model, api_key=reserve_key, timeout=timeout))
        record_llm_tokens(tok)
        return _extract_json(raw)
    try:
        raw, tok = _run_sync(_query_image(parts, prompt, system=system,
                                          model=model, api_key=None, timeout=timeout))
        record_llm_tokens(tok)
        return _extract_json(raw)
    except Exception as e:
        if reserve_key and _is_rate_limit(e):
            logger.warning('Subscription rate-limited, vision fallback — API: %s', e)
            raw, tok = _run_sync(_query_image(parts, prompt, system=system,
                                              model=model, api_key=reserve_key, timeout=timeout))
            record_llm_tokens(tok)
            return _extract_json(raw)
        raise


def image_to_json(image_bytes: bytes, *, prompt: str, media_type: str = 'image/jpeg',
                  **kwargs) -> dict:
    """Convenient wrapper around images_to_json for a single image."""
    return images_to_json([(image_bytes, media_type)], prompt=prompt, **kwargs)
