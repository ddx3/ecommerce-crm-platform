"""Context of a single agent run.

Passed to the tools via closure (build_tools(ctx)), NOT via the agent's
arguments, so that the agent cannot forge telegram_user_id or exceed the
write-operation limit.
"""
from __future__ import annotations

from dataclasses import dataclass


class WriteLimitExceeded(Exception):
    pass


@dataclass
class ToolContext:
    command_log_id: int
    telegram_user_id: int
    max_writes: int = 10
    # Key of the photo uploaded from Telegram, in ProductImage(temp_key=...).
    # Empty if the message had no photo.
    image_temp_key: str = ''
    _writes_done: int = 0

    def bump_write(self) -> None:
        self._writes_done += 1
        if self._writes_done > self.max_writes:
            raise WriteLimitExceeded(
                f'The limit of changes per command was exceeded ({self.max_writes}). '
                f'Stopped for safety.'
            )
