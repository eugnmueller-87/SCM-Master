"""Schemas for the logistics control-tower reads."""
from __future__ import annotations

from datetime import date, datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict


class OrderTracking(BaseModel):
    po_id: str
    supplier: str
    country: str
    shipment_id: str
    mode: str
    current_status: str
    progress_idx: int
    current_location: Optional[str]
    current_lat: Optional[float]
    current_lng: Optional[float]
    last_event_at: Optional[datetime]
    eta_original: Optional[date]
    eta_current: Optional[date]
    delay_days: int
    exception_flag: bool
    total_value: Optional[float]
    currency: Optional[str]
    status_label: str


class ShipmentEventRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    seq: int
    status: str
    location_name: str
    lat: Optional[float]
    lng: Optional[float]
    event_ts: Optional[datetime]
    notes: Optional[str]
