"""Schemas for capacity & flow planning views."""
from __future__ import annotations

from datetime import date
from typing import Optional

from pydantic import BaseModel

from app.models.flow import LocationType
from app.models.procurement import OrderStatus


class InboundLine(BaseModel):
    order_id: str
    order_number: str
    order_status: OrderStatus
    order_item_id: str
    product_id: str
    ordered: int
    received: int
    outstanding: int
    estimated_delivery_date: Optional[date]
    overdue: bool


class LocationCapacity(BaseModel):
    location_id: str
    code: str
    name: str
    location_type: LocationType
    capacity: Optional[int]
    used: int
    free: Optional[int]
    utilisation: Optional[float]
    over_capacity: bool


class DeploymentForecast(BaseModel):
    on_hand: int
    inbound: int
    deployed: int
    forecast_deployable: int
