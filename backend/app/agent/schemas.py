"""Pydantic v2 schemas for agent output — the contract the LLM must return."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class AgentInsight(BaseModel):
    title: str
    finding: str
    evidence: list[str] = Field(default_factory=list)
    assumption: str
    limitation: str
    confidence: float = Field(ge=0.0, le=1.0)
    severity: Literal["info", "watch", "action"]


class SourcingRecommendation(BaseModel):
    product_id: str
    recommended_source_id: str
    recommended_qty: int
    rationale: str
    signals: dict = Field(default_factory=dict)
    assumptions: list[str] = Field(default_factory=list)
    uncertainties: list[str] = Field(default_factory=list)
    confidence: float = Field(ge=0.0, le=1.0)
    decision: Literal["act", "recommend", "escalate"]
