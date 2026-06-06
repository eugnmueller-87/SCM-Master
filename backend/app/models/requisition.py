"""Purchase Requisition (PR) — the editable pre-approval document.

The key domain distinction this models:

    PR (PurchaseRequisition)  the agent's PROPOSAL. Mutable while STAGED: a human
                              can change quantities, drop or keep lines. It is NOT
                              an order — nothing is committed to a supplier.
    PO (PurchaseOrder)        the FIXED document. Created only when a PR is
                              approved (automatically at high confidence, or by a
                              human). Once it's a PO it is immutable — that's what
                              invoice matching reconciles against.

Lifecycle:  STAGED ──approve──▶ PLACED (a real PO now exists)
                   └─reject───▶ REJECTED

The agent stages PRs from the purchasing run. A PR whose calibrated confidence
clears the auto-place bar is approved in the same run (status PLACED, with
``auto_placed=True`` so the UI can show it acted on its own and still offer an
undo while the PO hasn't been received). Everything below the bar waits in STAGED
as the human's shopping cart.
"""
from __future__ import annotations

import enum
from datetime import date, datetime
from typing import Optional

from sqlalchemy import Boolean, Date, DateTime, Float, ForeignKey, Integer, Numeric, String, Text
from sqlalchemy import Enum as SAEnum
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base, IdMixin, TimestampMixin


class RequisitionStatus(str, enum.Enum):
    STAGED = "STAGED"          # editable; awaiting approval (the cart)
    PLACED = "PLACED"          # approved -> converted to a PO (po_id set)
    REJECTED = "REJECTED"      # dismissed by a human; no PO created


class PurchaseRequisition(IdMixin, TimestampMixin, Base):
    """One supplier's proposed buy, awaiting approval. One PR -> at most one PO
    (one PO per supplier, for invoice matching), mirroring the bundle the run
    builds."""

    __tablename__ = "purchase_requisition"

    supplier_id: Mapped[str] = mapped_column(ForeignKey("organization.id"), index=True)
    status: Mapped[RequisitionStatus] = mapped_column(
        SAEnum(RequisitionStatus), default=RequisitionStatus.STAGED, index=True)

    # The agent's assessment at staging time.
    confidence: Mapped[float] = mapped_column(Float, default=0.0)        # 0..1
    # The calibrated bar this PR was judged against (so the UI can explain the
    # auto/manual decision and show how learning moved the threshold).
    confidence_floor: Mapped[float] = mapped_column(Float, default=0.0)
    tier: Mapped[str] = mapped_column(String(16), default="propose")     # act/propose/escalate
    rationale: Mapped[Optional[str]] = mapped_column(Text)

    # Outcome of approval.
    auto_placed: Mapped[bool] = mapped_column(Boolean, default=False)    # cleared the bar
    po_id: Mapped[Optional[str]] = mapped_column(ForeignKey("purchase_order.id"))
    decided_by: Mapped[Optional[str]] = mapped_column(String(128))       # actor on approve/reject
    decided_at: Mapped[Optional[datetime]] = mapped_column(DateTime)

    # When the proposed buy should be ordered by (earliest line order_by).
    order_by: Mapped[Optional[date]] = mapped_column(Date)

    supplier = relationship("Organization")
    purchase_order = relationship("PurchaseOrder")
    lines: Mapped[list["RequisitionLine"]] = relationship(
        back_populates="requisition",
        cascade="all, delete-orphan",
    )


class RequisitionLine(IdMixin, TimestampMixin, Base):
    """A proposed line on a PR. Quantity is editable while the PR is STAGED;
    a line can also be dropped (``included=False``) without losing the agent's
    original proposal, so the rationale/history stays intact."""

    __tablename__ = "requisition_line"

    requisition_id: Mapped[str] = mapped_column(ForeignKey("purchase_requisition.id"), index=True)
    product_id: Mapped[str] = mapped_column(ForeignKey("product.id"), index=True)
    product_supplier_id: Mapped[Optional[str]] = mapped_column(ForeignKey("product_supplier.id"))

    # The agent's proposal (immutable record of what it suggested).
    proposed_qty: Mapped[int] = mapped_column(Integer)
    # The current (human-editable) quantity that will become the PO line.
    qty: Mapped[int] = mapped_column(Integer)
    unit_price: Mapped[Optional[float]] = mapped_column(Numeric(14, 2))
    included: Mapped[bool] = mapped_column(Boolean, default=True)

    # Why this line was proposed (trigger type + per-line confidence).
    trigger_type: Mapped[Optional[str]] = mapped_column(String(32))
    line_confidence: Mapped[float] = mapped_column(Float, default=0.0)
    rationale: Mapped[Optional[str]] = mapped_column(Text)

    requisition: Mapped["PurchaseRequisition"] = relationship(back_populates="lines")
    product = relationship("Product")
    product_supplier = relationship("ProductSupplier")


class RequisitionFeedback(IdMixin, TimestampMixin, Base):
    """The learning signal: what a human did with a staged PR, per (product,
    supplier). Drives outcome-feedback calibration — sources humans consistently
    approve as-is earn a lower confidence bar (more auto-placing); ones often
    edited or rejected earn a higher bar (fewer surprises). See
    ``services/calibration.py``.

    One row per decided line so the signal is product+supplier specific, not just
    per requisition.
    """

    __tablename__ = "requisition_feedback"

    requisition_id: Mapped[str] = mapped_column(ForeignKey("purchase_requisition.id"), index=True)
    product_id: Mapped[str] = mapped_column(ForeignKey("product.id"), index=True)
    supplier_id: Mapped[str] = mapped_column(ForeignKey("organization.id"), index=True)

    # What happened to the line: approved unchanged / quantity edited / dropped /
    # whole PR rejected. This is the calibration input.
    action: Mapped[str] = mapped_column(String(16), index=True)  # approved/edited/dropped/rejected
    proposed_qty: Mapped[int] = mapped_column(Integer)
    final_qty: Mapped[int] = mapped_column(Integer)
    confidence: Mapped[float] = mapped_column(Float, default=0.0)  # agent confidence at the time
    auto_placed: Mapped[bool] = mapped_column(Boolean, default=False)
