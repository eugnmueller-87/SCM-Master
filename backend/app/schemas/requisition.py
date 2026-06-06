"""Schemas for Purchase Requisitions (the editable cart) + edit/decision inputs."""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import List, Optional

from pydantic import BaseModel, Field

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
