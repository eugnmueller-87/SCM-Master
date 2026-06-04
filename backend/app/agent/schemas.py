"""Pydantic v2 schemas for agent output — the contract the LLM must return."""
from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional

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


class DemandTrigger(BaseModel):
    """Why a buy is justified — the run never proposes a PO without one."""
    type: Literal["lifecycle_replacement", "forecast_shortfall", "reorder_floor"]
    evidence: dict = Field(default_factory=dict)  # the numbers behind the trigger


class PurchasingDecision(BaseModel):
    product_id: str
    supplier_id: Optional[str]            # None when no contracted source exists
    qty: int
    unit_price: Optional[float]
    total: float
    trigger: DemandTrigger
    tier: Literal["act", "propose", "escalate"]
    confidence: float = Field(ge=0.0, le=1.0)
    rationale: str
    placed_po_id: Optional[str] = None    # set only when actually placed


class DemandReasoning(BaseModel):
    """AI reasoning over the deterministic demand forecast for one product."""
    product_id: str
    name: Optional[str] = None
    computed_shortfall: float                 # echo of the deterministic figure
    recommended_qty: int                      # AI's adjusted recommendation
    adjustment: Literal["raise", "hold", "lower", "defer"]  # vs the computed qty
    risks: list[str] = Field(default_factory=list)          # what the math misses
    rationale: str                            # why, grounded in the signals
    confidence: float = Field(ge=0.0, le=1.0)
    urgency: Literal["routine", "soon", "urgent"]


class DemandReasoningResult(BaseModel):
    horizon_days: int
    items: list[DemandReasoning] = Field(default_factory=list)
    summary: str                              # portfolio-level read


class PurchasingRunResult(BaseModel):
    run_at: datetime
    dry_run: bool
    period_days: int
    decisions: list[PurchasingDecision] = Field(default_factory=list)
    summary: dict = Field(default_factory=dict)  # placed, proposed, escalated, total_committed
