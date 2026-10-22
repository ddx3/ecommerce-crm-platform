"""Protection against 'burning' LLM resources through a bug (loop, retry storm, spam).

Counters are kept in the Django cache (in production Redis, shared between processes; locally
locmem, best-effort per process). Each key has a TTL equal to the limit window (minute / hour /
day), so the count resets itself.

Three protected resources:
  - Claude Code LLM subscription (core/llm, telegrambot) - not money, but a bug can eat the daily
    Max/Pro limit and take work down for hours;
  - Gemini embeddings - the ONLY real money (invoice/bundle matching);
  - Telegram bot - per-chat anti-spam (loop/flood -> many calls).

Limits are configured via env (see settings) - they can be tuned without a deploy.
"""
from __future__ import annotations

from django.conf import settings
from django.core.cache import cache


class LimitExceeded(RuntimeError):
    """Usage limit exhausted. Caught in views/the bot and shown to the user."""


def _hit(key: str, limit: int, ttl: int, amount: int = 1) -> bool:
    """Increments counter `key` by `amount` in a window of `ttl` seconds.

    Returns True if still within `limit`, False if exceeded.
    cache.add sets the key with a TTL only if it does not exist (the window 'rolls' from the first
    call); after that incr is atomic in Redis."""
    cache.add(key, 0, ttl)
    try:
        total = cache.incr(key, amount)
    except ValueError:
        # The key may have expired between add and incr (rare) - start the window anew.
        cache.set(key, amount, ttl)
        total = amount
    return total <= limit


def enforce_llm() -> None:
    """Guard BEFORE a Claude subscription call (core/llm, bot).

    Two levels of protection for the daily Max/Pro limit:
      1) rate (requests/min) - instantly catches a loop/retry storm, before any tokens;
      2) daily token budget - if already exhausted, do not start a new call
         (actual tokens are added by record_llm_tokens() after the reply)."""
    if not _hit('limit:llm:rate', settings.LLM_RATE_LIMIT, settings.LLM_RATE_WINDOW):
        mins = max(1, settings.LLM_RATE_WINDOW // 60)
        raise LimitExceeded(
            f'Too many AI requests ({settings.LLM_RATE_LIMIT} per {mins} min). '
            f'Please wait a moment.'
        )
    if cache.get('limit:llm:tokens:day', 0) >= settings.LLM_TOKENS_PER_DAY:
        raise LimitExceeded(
            f'Daily AI token limit exhausted ({settings.LLM_TOKENS_PER_DAY:,}). '
            f'It resets tomorrow.'
        )


def record_llm_tokens(tokens: int) -> None:
    """AFTER the call - add the actually spent tokens (input+output) to the daily counter. A breach is
    caught by the next enforce_llm()."""
    if tokens <= 0:
        return
    cache.add('limit:llm:tokens:day', 0, 86400)
    try:
        cache.incr('limit:llm:tokens:day', tokens)
    except ValueError:
        cache.set('limit:llm:tokens:day', tokens, 86400)


def enforce_analyst_llm() -> None:
    """Guard before an LLM call of the Analyst (the daily purchasing agent) — a separate budget: the
    analyst must not
    eat the subscription limit that the CRM buttons and chat depend on. The same two-layer
    protection (rate + daily
    token budget)."""
    if not _hit('limit:analyst:llm:rate', settings.ANALYST_LLM_RATE_LIMIT, settings.ANALYST_LLM_RATE_WINDOW):
        raise LimitExceeded('Too many LLM requests from the "Analyst". Please wait a moment.')
    if (cache.get('limit:analyst:llm:tokens:day') or 0) >= settings.ANALYST_LLM_TOKENS_PER_DAY:
        raise LimitExceeded(f'The daily token limit of the "Analyst" is exhausted ({settings.ANALYST_LLM_TOKENS_PER_DAY:,}).')


def record_analyst_llm_tokens(tokens: int) -> None:
    if tokens <= 0:
        return
    cache.add('limit:analyst:llm:tokens:day', 0, 86400)
    try:
        cache.incr('limit:analyst:llm:tokens:day', tokens)
    except ValueError:
        cache.set('limit:analyst:llm:tokens:day', tokens, 86400)


def enforce_gemini(count: int = 1) -> None:
    """Guard before Gemini embeddings - the only real money.

    `count` - how many texts are in the batch (we count texts, not calls)."""
    if not _hit('limit:gemini:day', settings.GEMINI_LIMIT_PER_DAY, 86400, amount=count):
        raise LimitExceeded(
            f'Daily Gemini embedding limit exhausted ({settings.GEMINI_LIMIT_PER_DAY}).'
        )


def enforce_tg_chat(chat_id) -> None:
    """Telegram bot anti-spam: N messages/hour per chat."""
    if not _hit(f'limit:tg:{chat_id}', settings.TG_MSGS_PER_HOUR, 3600):
        raise LimitExceeded(
            f'Too many messages. Please wait (limit {settings.TG_MSGS_PER_HOUR}/h).'
        )
