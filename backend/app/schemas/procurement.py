"""Procurement schemas: PurchaseOrder + OrderItem.

An order is created with its lines nested in one request, and reads embed the
lines. Status is NOT settable on create — a new order always starts PENDING and
advances through the service layer (Phase 2/3), never by direct client write.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import List, Optional

from pydantic import BaseModel, Field

from app.models.procurement import OrderStatus
from app.schemas.base import ReadBase


# --- OrderItem ------------------------------------------------------------

class OrderItemCreate(BaseModel):
    product_id: str
    product_supplier_id: Optional[str] = None  # the chosen source for this line
    quantity: int = Field(gt=0)
    unit_price: Optional[Decimal] = Field(default=None, ge=0)
    estimated_delivery_date: Optional[date] = None


class OrderItemUpdate(BaseModel):
    product_supplier_id: Optional[str] = None
    quantity: Optional[int] = Field(default=None, gt=0)
    unit_price: Optional[Decimal] = Field(default=None, ge=0)
    estimated_delivery_date: Optional[date] = None
    actual_delivery_date: Optional[date] = None


class OrderItemRead(ReadBase):
    order_id: str
    product_id: str
    product_supplier_id: Optional[str]
    quantity: int
    unit_price: Optional[Decimal]
    estimated_delivery_date: Optional[date]
    actual_delivery_date: Optional[date]


# --- PurchaseOrder --------------------------------------------------------

class PurchaseOrderCreate(BaseModel):
    order_number: str
    supplier_id: str
    destination_id: Optional[str] = None
    currency_code: str = "EUR"
    date_ordered: Optional[date] = None
    items: List[OrderItemCreate] = Field(default_factory=list)


class PurchaseOrderUpdate(BaseModel):
    supplier_id: Optional[str] = None
    destination_id: Optional[str] = None
    currency_code: Optional[str] = None
    date_ordered: Optional[date] = None


class PurchaseOrderRead(ReadBase):
    order_number: str
    status: OrderStatus
    supplier_id: str
    destination_id: Optional[str]
    currency_code: str
    date_ordered: Optional[date]
    items: List[OrderItemRead]
