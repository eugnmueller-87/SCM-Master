"""Order packages — named, reusable bundles of products you order as one unit.

A ``Package`` is a procurement template (e.g. "Compute rack" = 1 server + 2 CPUs
+ 8 DIMMs). Ordering a package expands it into one requisition line per
``PackageLine``, sourced + capacity-checked like any manual order. Distinct from
a costing BOM (which decomposes ONE product into components for should-cost): a
package groups SEPARATELY-STOCKED products a buyer orders together.
"""
from __future__ import annotations

from typing import Optional

from sqlalchemy import Boolean, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base, IdMixin, TimestampMixin


class Package(IdMixin, TimestampMixin, Base):
    """A named, reusable bundle of products ordered together."""

    __tablename__ = "package"

    code: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(255))
    description: Mapped[Optional[str]] = mapped_column(Text)
    active: Mapped[bool] = mapped_column(Boolean, default=True)

    lines: Mapped[list["PackageLine"]] = relationship(
        back_populates="package", cascade="all, delete-orphan")


class PackageLine(IdMixin, TimestampMixin, Base):
    """One product + quantity within a package."""

    __tablename__ = "package_line"

    package_id: Mapped[str] = mapped_column(ForeignKey("package.id"), index=True)
    product_id: Mapped[str] = mapped_column(ForeignKey("product.id"), index=True)
    quantity: Mapped[int] = mapped_column(Integer, default=1)

    package: Mapped["Package"] = relationship(back_populates="lines")
