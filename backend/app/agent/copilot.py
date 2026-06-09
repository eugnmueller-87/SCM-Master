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
from app.agent.context import build_context, demand_signals
from app.agent.schemas import (
    AgentInsight,
    DemandReasoning,
    DemandReasoningResult,
    SourcingRecommendation,
)
from app.agent.signals import gather_insight_signals, gather_sourcing_signals

_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.IGNORECASE)
_RETRY_NUDGE = (
    "\n\nYour previous reply could not be parsed. Reply with VALID JSON ONLY — "
    "no prose, no markdown fences — matching the required schema exactly."
)


class AgentError(RuntimeError):
    """Raised when the agent cannot produce valid structured output."""


def ask(db: Session, question: str, history: Optional[list[dict]] = None) -> str:
    """Free-form chat answer grounded in a live snapshot of the operation.

    Returns plain text (the chat bubble renders it). ``history`` is an optional
    list of prior turns [{"role": "user"|"assistant", "content": str}] folded
    into the prompt for follow-ups. Raises AgentError on LLM failure.
    """
    context = build_context(db)
    convo = ""
    for turn in (history or [])[-6:]:
        who = "User" if turn.get("role") == "user" else "Assistant"
        convo += f"\n{who}: {turn.get('content', '')}"
    user = (
        "CURRENT SNAPSHOT (live system state):\n"
        + json.dumps(context, default=str)
        + (f"\n\nEarlier in this conversation:{convo}" if convo else "")
        + f"\n\nQuestion: {question}"
    )
    raw = call_claude(prompts.CHAT_SYSTEM, user)
    if raw.startswith("[agent-error]"):
        raise AgentError(raw)
    return raw.strip()


def commentary_over_findings(findings: list[dict]) -> str:
    """Narrate OVER already-computed deterministic findings (plain text).

    The findings carry the correct numbers; the model only synthesises a short
    executive read and must not recompute or invent figures. Small structured
    input -> tiny token cost. Raises AgentError on LLM failure.
    """
    user = (
        "Findings (already computed, numbers are final):\n"
        + json.dumps(findings, default=str)
        + "\n\nWrite the 2–4 sentence executive read."
    )
    raw = call_claude(prompts.COMMENTARY_SYSTEM, user, max_tokens=400)
    if raw.startswith("[agent-error]"):
        raise AgentError(raw)
    return raw.strip()


def reason_demand(db: Session, *, horizon_days: int = 90) -> DemandReasoningResult:
    """AI reasoning ON TOP OF the deterministic demand forecast.

    The numbers (usage rate, EOL, shortfall, recommended qty) are computed live
    and passed in; the model interprets them per product — adjusts the
    recommendation, flags risks the math misses (expiring contract, single
    source, overdue inbound, no capacity), and explains why with a confidence.
    Returns a validated structured result; retries once, then raises AgentError.
    """
    signals = demand_signals(db)
    user = (
        "Deterministic demand forecast + planning signals (live):\n"
        + json.dumps(signals, default=str)
        + "\n\nReturn a JSON object with keys: horizon_days (int), summary (string),"
        " and items (array). Each item: product_id, name, computed_shortfall (number),"
        " recommended_qty (int), adjustment ('raise'|'hold'|'lower'|'defer'),"
        " risks (array of AT MOST 2 short strings), rationale (ONE concise sentence),"
        " confidence (0..1), urgency ('routine'|'soon'|'urgent'). Keep it terse."
        " Cover every product in the signals."
    )
    for attempt in range(2):
        system = prompts.DEMAND_SYSTEM + (_RETRY_NUDGE if attempt else "")
        # Larger budget: per-product reasoning across the catalog is verbose and
        # must not truncate mid-JSON.
        raw = call_claude(system, user, max_tokens=4000)
        if _raw_is_error(raw):
            if attempt == 0:
                continue
            raise AgentError(raw)
        try:
            data = json.loads(_strip_fences(raw))
            items = [DemandReasoning.model_validate(i) for i in data.get("items", [])]
            return DemandReasoningResult(
                horizon_days=int(data.get("horizon_days", horizon_days)),
                items=items, summary=str(data.get("summary", "")),
            )
        except Exception as exc:  # noqa: BLE001
            if attempt == 0:
                continue
            raise AgentError(f"could not parse demand reasoning: {exc}") from exc
    raise AgentError("demand reasoning failed")  # unreachable


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


def _recommend_sourcing_from_signals(sig: dict) -> SourcingRecommendation:
    """The LLM half of recommend_sourcing — pure network, NO database access.

    Split out so a batch run can gather every product's signals on the main
    thread (DB-bound, sequential, session-safe) and then fire these LLM calls
    concurrently (network-bound). Takes pre-gathered signals; touches no Session.
    """
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


def recommend_sourcing(db: Session, product_id: str,
                       desired_qty: Optional[int] = None,
                       *, signals: Optional[dict] = None) -> SourcingRecommendation:
    """Sourcing verdict for a product. Pass pre-gathered ``signals`` to skip the
    DB read (used by the concurrent batch path, which gathers signals on the main
    thread). This stays the single mockable seam — tests patch THIS function, so
    the batch path goes through it too."""
    sig = signals if signals is not None else gather_sourcing_signals(db, product_id, desired_qty)
    return _recommend_sourcing_from_signals(sig)


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
