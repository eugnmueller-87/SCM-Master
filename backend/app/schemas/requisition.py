"""Schemas for Purchase Requisitions (the editable cart) + edit/decision inputs."""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import List, Optional

from pydantic import BaseModel, Field, model_validator

from app.models.requisition import RequisitionStatus
from app.schemas.base import ReadBase


class RequisitionLineRead(ReadBase):
    requisition_id: str
    product_id: str
    product_supplier_id: Optional[str]
    proposed_qty: int
    qty: int
    unit_price: Optional[Decimal]
    included: bool
    trigger_type: Optional[str]
    line_confidence: float
    rationale: Optional[str]
    # Contracted lead time of the resolved source, surfaced so the card's "Lead"
    # column shows the real supplier lead time instead of dashing when no live
    # coverage/landing metric is available. Read-through the source relationship
    # at serialization time; None when the line has no resolved ProductSupplier.
    standard_lead_time_days: Optional[int] = None

    @model_validator(mode="before")
    @classmethod
    def _pull_lead_time(cls, obj):
        # On read, `obj` is the ORM RequisitionLine: hop through the source
        # relationship to lift its lead time onto the payload WITHOUT mutating the
        # ORM row. A plain dict (tests) or a source-less line passes through with
        # the field left as its declared default (None).
        if isinstance(obj, dict) or obj is None:
            return obj
        src = getattr(obj, "product_supplier", None)
        if src is not None and getattr(src, "standard_lead_time_days", None) is not None:
            data = {c.key: getattr(obj, c.key)
                    for c in obj.__mapper__.column_attrs}  # ORM columns only
            data["standard_lead_time_days"] = src.standard_lead_time_days
            return data
        return obj


class RequisitionRead(ReadBase):
    supplier_id: str
    status: RequisitionStatus
    confidence: float
    confidence_floor: float
    tier: str
    rationale: Optional[str]
    auto_placed: bool
    po_id: Optional[str]
    decided_by: Optional[str]
    decided_at: Optional[datetime]
    order_by: Optional[date]
    lines: List[RequisitionLineRead]


class LineEdit(BaseModel):
    qty: Optional[int] = Field(default=None, gt=0)
    included: Optional[bool] = None


class DecisionInput(BaseModel):
    reason: Optional[str] = None


class RunCycleInput(BaseModel):
    period_days: int = 7
