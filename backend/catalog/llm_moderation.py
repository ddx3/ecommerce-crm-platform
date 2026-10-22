"""LLM moderation of reviews and questions via a Claude subscription (core.llm).

Called from signals after a Review/Question is created.
Returns a verdict (`pass` / `flag`) and a short reason.

Principles:
  • Criticism and negative feedback — let through.
  • Profanity, insults, spam/advertising, phone numbers/links to other shops,
    gibberish, an obvious mismatch between text and rating — flag.
  • API error → `error` (the manager sees it manually, nothing is published automatically).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Literal, Optional

from django.conf import settings

logger = logging.getLogger(__name__)

Verdict = Literal['pass', 'flag', 'error']


@dataclass
class ModerationResult:
    verdict: Verdict
    reason: str = ''


# NOTE: the reviews are written by Ukrainian customers and the moderation reason is read by the
# Ukrainian-speaking manager, so the prompt keeps the demand for the reason in Ukrainian.
SYSTEM_PROMPT = """You are a moderator of reviews and comments for an online shop selling garden goods and fertilizers.
Your task is to decide whether a review/question can be published on the site automatically.

PASS (verdict: "pass"):
  • Any honest criticism, negative feedback, complaints — even harsh in tone.
  • Questions about the product, its composition, dosage, delivery.
  • Short reviews ("liked it", "fine", "ok").
  • Grammar/spelling mistakes are not a reason to reject.

FLAG (verdict: "flag"):
  • Profanity, insults aimed at the shop, the seller, other customers.
  • Spam, advertising: links to other shops, phone numbers, "buy from us".
  • Meaningless strings of characters ("asdfasdf", "qwerty", test messages).
  • An obvious mismatch between the text and the rating (the text praises, the rating is 1; or vice versa).

Answer with ONLY valid JSON in the format:
{"verdict": "pass" | "flag", "reason": "a short reason in Ukrainian, up to 80 characters"}

If verdict="pass", reason may be an empty string."""


def _build_user_prompt(*, kind: str, text: str, rating: Optional[int] = None) -> str:
    parts = [f'Type: {kind}']
    if rating is not None:
        parts.append(f'Rating: {rating}/5')
    parts.append(f'Text: """{text.strip()}"""')
    return '\n'.join(parts)


def moderate(*, kind: str, text: str, rating: Optional[int] = None) -> ModerationResult:
    """Moderates the text. kind = 'review' or 'question'."""
    text = (text or '').strip()
    if not text:
        return ModerationResult(verdict='flag', reason='Empty text')

    # Moderation goes through a Claude subscription (core.llm); JSON parsing and the fallback to the
    # paid API are inside text_to_json. Error → 'error' (manual review).
    try:
        from core import llm
        data = llm.text_to_json(
            _build_user_prompt(kind=kind, text=text, rating=rating),
            system=SYSTEM_PROMPT,
        )
    except Exception as e:
        logger.exception('LLM moderation failed')
        return ModerationResult(verdict='error', reason=f'LLM error: {e}'[:200])

    if not isinstance(data, dict):
        return ModerationResult(verdict='error', reason='Invalid LLM response')
    verdict = data.get('verdict')
    reason = (data.get('reason') or '')[:200]
    if verdict not in ('pass', 'flag'):
        return ModerationResult(verdict='error', reason=f'Unknown verdict: {verdict}')

    return ModerationResult(verdict=verdict, reason=reason)
