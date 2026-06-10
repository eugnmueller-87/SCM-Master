"""Catalog: the *what* and the *who we buy it from*.

This is the part of the OpenBoxes model genuinely worth scavenging. The key
idea — being able to replace suppliers without losing a product's history — is
the separation of:

    Product          the spec  ("Supermicro AS-1015, 64GB, 2x NVMe")
    ProductSupplier  one row PER SOURCE of that product

A Product has many ProductSuppliers. Each ProductSupplier carries the
source-specific facts that matter under spiky demand: lead time, minimum order
quantity, contract price, and a preference rank. "Replacing a supplier" is then
just choosing a different ProductSupplier — without losing the product's
identity or its purchase history.
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Optional

from sqlalchemy import Boolean, Date, DateTime, ForeignKey, Integer, Numeric, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base, ExternalRefMixin, IdMixin, TimestampMixin, _now


class Organization(IdMixin, TimestampMixin, ExternalRefMixin, Base):
    """A company we deal with: a supplier (Dell, Supermicro) and/or a
    manufacturer (Intel, Samsung). One org can be both, so we flag roles
    rather than splitting into separate tables (mirrors OpenBoxes' Party)."""

    __tablename__ = "organization"

    code: Mapped[Optional[str]] = mapped_column(String(64), unique=True)
    name: Mapped[str] = mapped_column(String(255))
    is_supplier: Mapped[bool] = mapped_column(Boolean, default=True)
    is_manufacturer: Mapped[bool] = mapped_column(Boolean, default=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True)

    # --- Supplier onboarding / compliance gate -------------------------------
    # A supplier must clear onboarding before it can be ordered from: a quick
    # risk assessment plus a signed DPA and NDA on record. Documents are tracked
    # as metadata only (signer + date) — a document of record, not file storage.
    # ``onboarding_status`` is the gate; ``is_orderable`` derives the verdict.
    # Legacy/seeded suppliers default to APPROVED so existing flows are unaffected.
    onboarding_status: Mapped[str] = mapped_column(String(16), default="APPROVED")  # DRAFT/IN_REVIEW/APPROVED/REJECTED
    risk_level: Mapped[Optional[str]] = mapped_column(String(8))  # LOW/MEDIUM/HIGH
    risk_notes: Mapped[Optional[str]] = mapped_column(Text)
    risk_assessed_at: Mapped[Optional[date]] = mapped_column(Date)
    dpa_signed: Mapped[bool] = mapped_column(Boolean, default=False)
    dpa_signed_at: Mapped[Optional[date]] = mapped_column(Date)
    dpa_reference: Mapped[Optional[str]] = mapped_column(String(255))  # doc-of-record: filename/ref/signer
    nda_signed: Mapped[bool] = mapped_column(Boolean, default=False)
    nda_signed_at: Mapped[Optional[date]] = mapped_column(Date)
    nda_reference: Mapped[Optional[str]] = mapped_column(String(255))

    @property
    def onboarding_complete(self) -> bool:
        """The hard gate: risk assessed AND both agreements signed."""
        return (
            self.risk_level is not None
            and self.dpa_signed
            and self.nda_signed
        )

    @property
    def is_orderable(self) -> bool:
        """A supplier can be ordered from only when active and APPROVED."""
        return self.is_supplier and self.active and self.onboarding_status == "APPROVED"

    # Source rows where this org is the seller.
    supplied_products: Mapped[list["ProductSupplier"]] = relationship(
        back_populates="supplier",
        foreign_keys="ProductSupplier.supplier_id",
    )


class Product(IdMixin, TimestampMixin, ExternalRefMixin, Base):
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
    levers you pull during a demand spike:
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

    # Contract lifecycle (a ProductSupplier IS the sourcing contract here).
    # contract_status is nullable: when unset, it is derived from term dates /
    # `active` at read time. term_start/end drive the renewal countdown;
    # annual_budget drives the budget-burn bar (ytd_spend is computed live from
    # received-asset cost, not stored).
    contract_status: Mapped[Optional[str]] = mapped_column(String(16))  # DRAFT/ACTIVE/RENEWAL_DUE/EXPIRING/EXPIRED/SUPERSEDED
    term_start: Mapped[Optional[date]] = mapped_column(Date)
    term_end: Mapped[Optional[date]] = mapped_column(Date)
    annual_budget: Mapped[Optional[float]] = mapped_column(Numeric(14, 2))

    product: Mapped["Product"] = relationship(back_populates="suppliers")
    supplier: Mapped["Organization"] = relationship(
        back_populates="supplied_products",
        foreign_keys=[supplier_id],
    )
    manufacturer: Mapped[Optional["Organization"]] = relationship(
        foreign_keys=[manufacturer_id],
    )


class ContractDocument(IdMixin, TimestampMixin, Base):
    """An uploaded contract file attached to a supplier (Organization).

    This holds the ACTUAL document bytes' location — distinct from the supplier's
    document *metadata of record* (``Organization.dpa_reference`` / ``nda_reference``),
    which only note that an agreement was signed. A supplier may have any number of
    these, or none: the repository is entirely optional.

    The bytes live in a pluggable ``ContractStore`` (a Railway volume today, an
    SAP/S3 backend tomorrow). ``storage_key`` is the opaque, server-generated key
    the store uses — never the user's filename, and never exposed to clients.
    ``kind`` is a free-text hint (NDA/DPA/POC/MSA) and is intentionally NOT
    enforced; classification/compliance is deliberately out of scope here.
    """

    __tablename__ = "contract_document"

    organization_id: Mapped[str] = mapped_column(
        ForeignKey("organization.id"), index=True)
    original_filename: Mapped[str] = mapped_column(String(255))
    content_type: Mapped[str] = mapped_column(String(128))
    size_bytes: Mapped[int] = mapped_column(Integer)
    # Opaque store key (e.g. "<org_id>/<uuid>.pdf"); a UNIQUE INDEX so a row maps
    # to exactly one blob (unique) and lookups are fast. index=True + unique=True
    # makes SQLAlchemy emit a unique index — matching the migration exactly (an
    # `# autogenerate must be empty` CI gate enforces model==migration).
    storage_key: Mapped[str] = mapped_column(String(512), unique=True, index=True)
    # Free-text label (NDA/DPA/POC/MSA …) — a hint, not a constraint.
    kind: Mapped[Optional[str]] = mapped_column(String(32))
    uploaded_at: Mapped[datetime] = mapped_column(DateTime, default=_now)

    organization = relationship("Organization")
