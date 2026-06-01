"""Procurement: the *buying*. PurchaseOrder + OrderItem.

Trimmed from OpenBoxes' Order/OrderItem to what IONOS needs. Crucially, each
OrderItem points at a ProductSupplier (the chosen source) — so re-sourcing a
line during a spike is literally repointing this FK to a different
ProductSupplier of the same Product. The estimated dates are the inbound-timing
data the future capacity/flow layer will plan against.
"""
from __future__ import annotations

import enum
from datetime import date
from typing import Optional

from sqlalchemy import String, Integer, Numeric, ForeignKey, Date, Enum as SAEnum
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base, IdMixin, TimestampMixin


class OrderStatus(str, enum.Enum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    PLACED = "PLACED"
    PARTIALLY_RECEIVED = "PARTIALLY_RECEIVED"
    RECEIVED = "RECEIVED"
    CANCELLED = "CANCELLED"


class PurchaseOrder(IdMixin, TimestampMixin, Base):
    __tablename__ = "purchase_order"

    order_number: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    status: Mapped[OrderStatus] = mapped_column(SAEnum(OrderStatus), default=OrderStatus.PENDING)
    supplier_id: Mapped[str] = mapped_column(ForeignKey("organization.id"), index=True)
    # Where the goods should land — the transit warehouse, usually.
    destination_id: Mapped[Optional[str]] = mapped_column(ForeignKey("location.id"))

    currency_code: Mapped[str] = mapped_column(String(3), default="EUR")
    date_ordered: Mapped[Optional[date]] = mapped_column(Date)

    supplier = relationship("Organization")
    destination = relationship("Location")
    items: Mapped[list["OrderItem"]] = relationship(
        back_populates="order",
        cascade="all, delete-orphan",
    )


class OrderItem(IdMixin, TimestampMixin, Base):
    __tablename__ = "order_item"

    order_id: Mapped[str] = mapped_column(ForeignKey("purchase_order.id"), index=True)
    product_id: Mapped[str] = mapped_column(ForeignKey("product.id"), index=True)
    # The chosen source for this line. Swap-supplier = change this FK.
    product_supplier_id: Mapped[Optional[str]] = mapped_column(ForeignKey("product_supplier.id"))

    quantity: Mapped[int] = mapped_column(Integer)
    unit_price: Mapped[Optional[float]] = mapped_column(Numeric(14, 2))

    # Inbound-timing data the flow/capacity planner will consume.
    estimated_delivery_date: Mapped[Optional[date]] = mapped_column(Date)
    actual_delivery_date: Mapped[Optional[date]] = mapped_column(Date)

    order: Mapped["PurchaseOrder"] = relationship(back_populates="items")
    product = relationship("Product")
    product_supplier = relationship("ProductSupplier")
