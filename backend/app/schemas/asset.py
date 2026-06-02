"""Asset, lifecycle event, receiving, transition, and provenance schemas."""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import List, Optional

from pydantic import BaseModel, Field

from app.models.flow import AssetEventType, AssetStatus
from app.schemas.base import ReadBase

# --- Asset ----------------------------------------------------------------

class AssetRead(ReadBase):
    serial_number: str
    product_id: str
    status: AssetStatus
    current_location_id: Optional[str]
    source_order_item_id: Optional[str]
    received_date: Optional[date]
    deployed_date: Optional[date]
    warranty_end_date: Optional[date]
    decommissioned_date: Optional[date]
    notes: Optional[str]


class AssetEventRead(ReadBase):
    asset_id: str
    event_type: AssetEventType
    from_status: Optional[AssetStatus]
    to_status: Optional[AssetStatus]
    from_location_id: Optional[str]
    to_location_id: Optional[str]
    actor: Optional[str]
    note: Optional[str]


# --- Receiving ------------------------------------------------------------

class ReceiptLine(BaseModel):
    order_item_id: str
    quantity: int = Field(gt=0)


class ReceiveRequest(BaseModel):
    location_id: str
    lines: List[ReceiptLine] = Field(min_length=1)
    receipt_date: Optional[date] = None
    actor: Optional[str] = None


class ReceiptItemRead(ReadBase):
    receipt_id: str
    order_item_id: str
    quantity_received: int


class ReceiptRead(ReadBase):
    purchase_order_id: str
    received_at_id: str
    receipt_date: Optional[date]
    items: List[ReceiptItemRead]


# --- Transitions / moves --------------------------------------------------

class TransitionRequest(BaseModel):
    target: AssetStatus
    location_id: Optional[str] = None  # e.g. the rack when deploying
    actor: Optional[str] = None
    note: Optional[str] = None


class MoveRequest(BaseModel):
    location_id: str
    actor: Optional[str] = None
    note: Optional[str] = None


# --- Provenance -----------------------------------------------------------

class AssetTrace(BaseModel):
    asset_id: str
    serial_number: str
    status: AssetStatus
    product_id: str
    product_name: Optional[str]
    order_item_id: Optional[str]
    order_id: Optional[str]
    order_number: Optional[str]
    supplier_id: Optional[str]
    supplier_name: Optional[str]
    unit_price: Optional[Decimal]
