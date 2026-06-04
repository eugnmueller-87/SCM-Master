"""The copilot: orchestrate signals -> Claude -> validated structured output.

Both entry points follow the same shape:
  1. gather signals (pure, in-process);
  2. build a user message that is the signals JSON;
  3. call_claude(system, user);
  4. strip stray markdown fences, parse JSON, validate against the schema;
  5. on any failure, retry ONCE with a "valid JSON only" nudge, then raise.

Failures raise ``AgentError`` so the API layer can map them to a 502.
"""
from __future__ import annotations

import json
import re
from typing import Optional

from sqlalchemy.orm import Session

from app.agent import prompts
from app.agent.client import call_claude
from app.agent.schemas import AgentInsight, SourcingRecommendation
from app.agent.signals import gather_insight_signals, gather_sourcing_signals

_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.IGNORECASE)
_RETRY_NUDGE = (
    "\n\nYour previous reply could not be parsed. Reply with VALID JSON ONLY — "
    "no prose, no markdown fences — matching the required schema exactly."
)


class AgentError(RuntimeError):
    """Raised when the agent cannot produce valid structured output."""


def _strip_fences(text: str) -> str:
    """Remove a leading ```json / trailing ``` fence if the model added one."""
    text = text.strip()
    text = _FENCE_RE.sub("", text)
    # Also handle a fence only at the very start or end.
    if text.startswith("```"):
        text = text[3:]
    if text.endswith("```"):
        text = text[:-3]
    return text.strip()


def _raw_is_error(raw: str) -> bool:
    return raw.startswith("[agent-error]")


def recommend_sourcing(db: Session, product_id: str,
                       desired_qty: Optional[int] = None) -> SourcingRecommendation:
    sig = gather_sourcing_signals(db, product_id, desired_qty)
    user = json.dumps({"signals": sig}, default=str)

    for attempt in range(2):
        system = prompts.SOURCING_SYSTEM + (_RETRY_NUDGE if attempt else "")
        raw = call_claude(system, user)
        if _raw_is_error(raw):
            if attempt == 0:
                continue
            raise AgentError(raw)
        try:
            data = json.loads(_strip_fences(raw))
            return SourcingRecommendation.model_validate(data)
        except Exception as exc:  # noqa: BLE001
            if attempt == 0:
                continue
            raise AgentError(f"could not parse sourcing recommendation: {exc}") from exc
    raise AgentError("sourcing recommendation failed")  # unreachable


def generate_insights(db: Session, min_count: int = 5) -> list[AgentInsight]:
    sig = gather_insight_signals(db)
    user = json.dumps({
        "signals": sig,
        "instruction": f"Produce at least {min_count} insights.",
    }, default=str)

    for attempt in range(2):
        system = prompts.INSIGHTS_SYSTEM + (_RETRY_NUDGE if attempt else "")
        raw = call_claude(system, user)
        if _raw_is_error(raw):
            if attempt == 0:
                continue
            raise AgentError(raw)
        try:
            data = json.loads(_strip_fences(raw))
            if not isinstance(data, list):
                raise ValueError("expected a JSON array of insights")
            insights = [AgentInsight.model_validate(item) for item in data]
            if len(insights) < min_count:
                raise ValueError(f"expected >= {min_count} insights, got {len(insights)}")
            return insights
        except Exception as exc:  # noqa: BLE001
            if attempt == 0:
                continue
            raise AgentError(f"could not parse insights: {exc}") from exc
    raise AgentError("insight generation failed")  # unreachable
