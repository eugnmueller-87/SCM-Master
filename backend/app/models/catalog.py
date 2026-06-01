"""Catalog: the *what* and the *who we buy it from*.

This is the part of the OpenBoxes model genuinely worth scavenging. The key
idea — and the one IONOS specifically asked for ("we should be able to replace
suppliers") — is the separation of:

    Product          the spec  ("Supermicro AS-1015, 64GB, 2x NVMe")
    ProductSupplier  one row PER SOURCE of that product

A Product has many ProductSuppliers. Each ProductSupplier carries the
source-specific facts that matter under spiky demand: lead time, minimum order
quantity, contract price, and a preference rank. "Replacing a supplier" is then
just choosing a different ProductSupplier — without losing the product's
identity or its purchase history.
"""
from __future__ import annotations

from typing import Optional

from sqlalchemy import String, Numeric, Integer, Boolean, ForeignKey, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base, IdMixin, TimestampMixin


class Organization(IdMixin, TimestampMixin, Base):
    """A company we deal with: a supplier (Dell, Supermicro) and/or a
    manufacturer (Intel, Samsung). One org can be both, so we flag roles
    rather than splitting into separate tables (mirrors OpenBoxes' Party)."""

    __tablename__ = "organization"

    code: Mapped[Optional[str]] = mapped_column(String(64), unique=True)
    name: Mapped[str] = mapped_column(String(255))
    is_supplier: Mapped[bool] = mapped_column(Boolean, default=True)
    is_manufacturer: Mapped[bool] = mapped_column(Boolean, default=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True)

    # Source rows where this org is the seller.
    supplied_products: Mapped[list["ProductSupplier"]] = relationship(
        back_populates="supplier",
        foreign_keys="ProductSupplier.supplier_id",
    )


class Product(IdMixin, TimestampMixin, Base):
    """The spec — supplier-independent. A server model, a CPU SKU, a DIMM."""

    __tablename__ = "product"

    product_code: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(255))
    description: Mapped[Optional[str]] = mapped_column(Text)
    category: Mapped[Optional[str]] = mapped_column(String(128))  # e.g. server / cpu / storage / network
    # Hardware doesn't expire — so NO expiry field here, by design. The biggest
    # divergence from OpenBoxes' healthcare model lives in this one omission.
    active: Mapped[bool] = mapped_column(Boolean, default=True)

    suppliers: Mapped[list["ProductSupplier"]] = relationship(
        back_populates="product",
        cascade="all, delete-orphan",
    )


class ProductSupplier(IdMixin, TimestampMixin, Base):
    """One *source* for a Product. Multiple rows per product = multi-sourcing.

    Fields lifted from OpenBoxes' ProductSupplier because they are exactly the
    levers IONOS pulls during a demand spike:
      - standard_lead_time_days : the timing lever (chip lead times are brutal)
      - min_order_quantity      : the MOQ that bites when you only need a few
      - contract_price          : cost
      - preference_rank         : which source we'd pick first (1 = most preferred)

    manufacturer vs supplier are tracked separately so we can model BOTH
    "different reseller of the identical part" and "equivalent part from a
    different maker".
    """

    __tablename__ = "product_supplier"

    product_id: Mapped[str] = mapped_column(ForeignKey("product.id"), index=True)
    supplier_id: Mapped[str] = mapped_column(ForeignKey("organization.id"), index=True)
    manufacturer_id: Mapped[Optional[str]] = mapped_column(ForeignKey("organization.id"))

    supplier_product_code: Mapped[Optional[str]] = mapped_column(String(128))  # supplier's own SKU
    manufacturer_part_number: Mapped[Optional[str]] = mapped_column(String(128))

    standard_lead_time_days: Mapped[Optional[int]] = mapped_column(Integer)
    min_order_quantity: Mapped[Optional[int]] = mapped_column(Integer)
    contract_price: Mapped[Optional[float]] = mapped_column(Numeric(14, 2))
    currency_code: Mapped[str] = mapped_column(String(3), default="EUR")

    # Lower = preferred. The backbone of supplier-swapping: re-rank or
    # deactivate a source and sourcing decisions follow.
    preference_rank: Mapped[int] = mapped_column(Integer, default=100)
    active: Mapped[bool] = mapped_column(Boolean, default=True)

    product: Mapped["Product"] = relationship(back_populates="suppliers")
    supplier: Mapped["Organization"] = relationship(
        back_populates="supplied_products",
        foreign_keys=[supplier_id],
    )
    manufacturer: Mapped[Optional["Organization"]] = relationship(
        foreign_keys=[manufacturer_id],
    )
