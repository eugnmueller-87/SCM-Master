"""Canonical, source-agnostic records that every adapter maps onto.

These are the *contract* between an adapter (which knows a specific upstream wire
format) and the sync engine (which knows how to persist). An adapter's whole job
is: raw feed -> these objects. Add a new upstream by writing a new adapter that
produces the same three records; the sync engine never changes.

They are plain Pydantic models, NOT the ORM models — keeping the wire/mapping
layer decoupled from persistence. ``external_ref`` on each is the upstream
system's own identifier for that entity, which the sync keys its upsert on.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Optional

from pydantic import BaseModel, Field


class SupplierRecord(BaseModel):
    """A supplier as the upstream system knows it -> our Organization."""

    external_ref: str = Field(..., description="Upstream supplier id (e.g. Coupa supplier number)")
    name: str
    code: Optional[str] = None
    currency_code: str = "EUR"
    active: bool = True


class MaterialRecord(BaseModel):
    """A material / catalog item -> our Product."""

    external_ref: str = Field(..., description="Upstream material/item id")
    product_code: str
    name: str
    category: Optional[str] = None
    description: Optional[str] = None


class PoLineRecord(BaseModel):
    """One line of a purchase order, referencing its material by external ref."""

    material_external_ref: str
    quantity: int
    unit_price: Optional[Decimal] = None
    expected_delivery_date: Optional[date] = None


class PurchaseOrderRecord(BaseModel):
    """A purchase order header + lines -> our PurchaseOrder + OrderItems."""

    external_ref: str = Field(..., description="Upstream PO number")
    supplier_external_ref: str
    order_number: str
    currency_code: str = "EUR"
    date_ordered: Optional[date] = None
    status: Optional[str] = None  # upstream status, mapped to our OrderStatus by the sync
    lines: list[PoLineRecord] = Field(default_factory=list)


class FeedBatch(BaseModel):
    """Everything one feed file resolves to, ready for the sync engine.

    A single Coupa/SAP export typically carries all three in a denormalised
    form; the adapter de-duplicates suppliers and materials so each appears once.
    """

    suppliers: list[SupplierRecord] = Field(default_factory=list)
    materials: list[MaterialRecord] = Field(default_factory=list)
    purchase_orders: list[PurchaseOrderRecord] = Field(default_factory=list)
