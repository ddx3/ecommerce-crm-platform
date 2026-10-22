"""Building and running the Claude agent for the Telegram bot.

The agent may use ONLY our tools (mcp__shop__*). Every built-in tool
(Bash/Read/Write/...) is forbidden by three layers:
  1. allowed_tools - a whitelist of mcp__shop__* only
  2. disallowed_tools - an explicit blacklist of built-in tools
  3. can_use_tool - a deny-by-default callback (the main, guaranteed layer)
  + permission_mode='dontAsk' (do not hang on a prompt in headless mode), strict_mcp_config,
    setting_sources=None (do not pull in third-party MCP servers / the project CLAUDE.md).

The 'subscription -> API fallback on rate limit' logic is reused from core/llm.py.
"""
from __future__ import annotations

import logging
import os

from django.conf import settings

from claude_agent_sdk import (
    AssistantMessage, ClaudeAgentOptions, ClaudeSDKClient, PermissionResultAllow,
    PermissionResultDeny, ResultMessage, TextBlock, ToolUseBlock,
    create_sdk_mcp_server,
)

from core.llm import _env_for, _is_rate_limit
from .context import ToolContext
from .tools import build_tools, TOOL_NAMES

logger = logging.getLogger(__name__)

ALLOWED_TOOLS = [f'mcp__shop__{n}' for n in TOOL_NAMES]
DISALLOWED_TOOLS = [
    'Bash', 'Read', 'Write', 'Edit', 'MultiEdit', 'Glob', 'Grep', 'WebFetch',
    'WebSearch', 'Task', 'NotebookEdit', 'KillBash', 'TodoWrite', 'ToolSearch',
]


def _load_system_prompt() -> str:
    try:
        path = settings.BASE_DIR / 'telegrambot' / 'prompts' / 'system.txt'
        return open(path, encoding='utf-8').read()
    except Exception:
        return ('You are the shop manager\'s assistant. Act only through the provided '
                'tools. Do not make anything up.')


async def _can_use_tool(tool_name, input_data, context):
    """Deny-by-default: only our mcp__shop__* tools are allowed through."""
    if tool_name in ALLOWED_TOOLS:
        return PermissionResultAllow()
    logger.warning('The Telegram agent tried a forbidden tool: %s', tool_name)
    return PermissionResultDeny(message=f'The tool {tool_name} is forbidden')


def _build_options(ctx: ToolContext, *, api_key, model) -> ClaudeAgentOptions:
    server = create_sdk_mcp_server(name='shop', version='1.0.0', tools=build_tools(ctx))
    opts = dict(
        system_prompt=_load_system_prompt(),
        model=model,
        mcp_servers={'shop': server},
        allowed_tools=ALLOWED_TOOLS,
        disallowed_tools=DISALLOWED_TOOLS,
        permission_mode='dontAsk',
        can_use_tool=_can_use_tool,
        strict_mcp_config=True,
        setting_sources=None,
        thinking={'type': 'disabled'},
        max_turns=12,
        env=_env_for(api_key),
    )
    explicit = getattr(settings, 'CLAUDE_BIN', '')
    if explicit and os.path.exists(explicit):
        opts['cli_path'] = explicit
    return ClaudeAgentOptions(**opts)


async def _image_input(prompt: str, image_parts: list):
    """Async entry point for the SDK with images (pattern from core/llm._query_image).

    image_parts is a list of (base64_str, media_type)."""
    content = [
        {'type': 'image', 'source': {'type': 'base64', 'media_type': mt, 'data': b64}}
        for b64, mt in image_parts
    ]
    content.append({'type': 'text', 'text': prompt})
    yield {'type': 'user', 'message': {'role': 'user', 'content': content}}


async def _drive(options: ClaudeAgentOptions, prompt: str, image_parts):
    text_chunks: list[str] = []
    tool_calls: list[dict] = []
    result_text: str | None = None

    async with ClaudeSDKClient(options=options) as client:
        if image_parts:
            await client.query(_image_input(prompt, image_parts))
        else:
            await client.query(prompt)

        async for message in client.receive_response():
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock):
                        text_chunks.append(block.text)
                    elif isinstance(block, ToolUseBlock):
                        tool_calls.append({
                            'tool': block.name,
                            'input': _truncate_input(block.input),
                        })
            elif isinstance(message, ResultMessage):
                if getattr(message, 'result', None):
                    result_text = message.result
                # Agent tokens (in+out over all turns) count toward the daily token budget.
                u = getattr(message, 'usage', None) or {}
                tok = int(u.get('input_tokens') or 0) + int(u.get('output_tokens') or 0)
                if tok:
                    from core.limits import record_llm_tokens
                    record_llm_tokens(tok)

    summary = (result_text if result_text is not None else '\n'.join(text_chunks)).strip()
    return summary, tool_calls


def _truncate_input(data) -> dict:
    """Images and huge values are not written to the log."""
    out = {}
    try:
        for k, v in (data or {}).items():
            s = str(v)
            out[k] = s if len(s) <= 200 else s[:200] + '…'
    except Exception:
        return {}
    return out


async def run_agent(ctx: ToolContext, prompt: str, image_parts: list | None = None):
    """Runs the agent. Returns (summary_text, tool_calls).

    The subscription is primary; on rate limit, with ANTHROPIC_API_KEY set, falls back to the
    paid API, but ONLY if no write operations have happened yet (otherwise a retry
    would duplicate the changes)."""
    model = getattr(settings, 'CLAUDE_MODEL', 'sonnet')
    reserve_key = settings.ANTHROPIC_API_KEY or None
    prefer_api = getattr(settings, 'LLM_PREFER_API', False)

    async def _once(api_key):
        return await _drive(_build_options(ctx, api_key=api_key, model=model),
                            prompt, image_parts)

    if prefer_api:
        if not reserve_key:
            raise RuntimeError('LLM_PREFER_API=True, but ANTHROPIC_API_KEY is not set')
        return await _once(reserve_key)

    try:
        return await _once(None)
    except Exception as e:
        if reserve_key and _is_rate_limit(e) and ctx._writes_done == 0:
            logger.warning('Subscription rate-limited, falling back to the paid API: %s', e)
            return await _once(reserve_key)
        raise


def run_agent_sync(ctx: ToolContext, prompt: str, image_parts: list | None = None):
    """Sync wrapper for the shell/management commands (outside the aiogram loop). A fresh
    ProactorEventLoop on Windows, since the SDK launches `claude` as a subprocess."""
    import asyncio
    import concurrent.futures
    import sys

    def _runner():
        if sys.platform == 'win32':
            loop = asyncio.ProactorEventLoop()
        else:
            loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(run_agent(ctx, prompt, image_parts))
        finally:
            loop.close()

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
        return ex.submit(_runner).result()
