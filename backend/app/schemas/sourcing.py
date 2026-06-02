"""Schemas for order status transitions, supplier-swap, sourcing, analytics."""
from __future__ import annotations

from decimal import Decimal
from typing import List, Optional

from pydantic import BaseModel

from app.models.procurement import OrderStatus


# --- Order status / supplier swap ----------------------------------------

class OrderStatusRequest(BaseModel):
    target: OrderStatus


class ResourceLineRequest(BaseModel):
    product_supplier_id: str


# --- Sourcing suggestions -------------------------------------------------

class SourceSuggestion(BaseModel):
    rank: int
    product_supplier_id: str
    supplier_id: str
    supplier_name: Optional[str]
    preference_rank: Optional[int]
    standard_lead_time_days: Optional[int]
    min_order_quantity: Optional[int]
    contract_price: Optional[Decimal]
    currency_code: Optional[str]
    active: bool


# --- Spend analytics ------------------------------------------------------

class SupplierSpend(BaseModel):
    supplier_id: str
    supplier_name: Optional[str]
    units: int
    spend: Decimal


class ProductSpend(BaseModel):
    product_id: str
    product_name: Optional[str]
    category: Optional[str]
    units: int
    spend: Decimal


class CategorySpend(BaseModel):
    category: str
    units: int
    spend: Decimal


class SpendSummary(BaseModel):
    total_units: int
    total_spend: Decimal
    by_supplier: List[SupplierSpend]
    by_category: List[CategorySpend]
