"""Requisition service — the shopping-cart lifecycle around the agent's proposals.

Responsibilities:
  - **stage**   : create a STAGED PurchaseRequisition (one per supplier) from a
                  bundle the purchasing run produced;
  - **edit**    : while STAGED, adjust a line's quantity or drop/keep it (a PR is
                  mutable; a PO is not);
  - **approve** : convert a STAGED PR into a real PO via the existing PO service
                  (fixed document, one PO per supplier for invoice matching), and
                  record per-line feedback so the agent learns;
  - **reject**  : dismiss a STAGED PR, recording the negative signal.

The transaction boundary is the caller's (the route's ``get_db``); this service
only flushes. Approval records feedback BEFORE converting, so the learning signal
is captured even though the requisition's lines are about to be read into a PO.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.requisition import (
    PurchaseRequisition,
    RequisitionLine,
    RequisitionStatus,
)
from app.services import calibration
from app.services.crud import CRUDService
from app.services.exceptions import ValidationError
from app.services.procurement import purchase_order_service


def _now() -> datetime:
    return datetime.now(timezone.utc)


class RequisitionService(CRUDService[PurchaseRequisition]):
    def __init__(self):
        super().__init__(PurchaseRequisition)

    # --- queries ----------------------------------------------------------

    def list(self, db: Session, *, status: Optional[RequisitionStatus] = None,
             skip: int = 0, limit: int = 100):
        stmt = select(PurchaseRequisition)
        if status is not None:
            stmt = stmt.where(PurchaseRequisition.status == status)
        stmt = stmt.order_by(PurchaseRequisition.date_created.desc()).offset(skip).limit(limit)
        return db.scalars(stmt).all()

    # --- staging (called by the purchasing run) ---------------------------

    def stage(self, db: Session, *, supplier_id: str, confidence: float,
              confidence_floor: float, tier: str, rationale: str,
              order_by: Optional[date], lines: list[dict]) -> PurchaseRequisition:
        """Create a STAGED PR + its lines from a supplier bundle.

        ``lines`` items: product_id, product_supplier_id, qty, unit_price,
        trigger_type, line_confidence, rationale.
        """
        pr = PurchaseRequisition(
            supplier_id=supplier_id, status=RequisitionStatus.STAGED,
            confidence=confidence, confidence_floor=confidence_floor,
            tier=tier, rationale=rationale, order_by=order_by,
        )
        db.add(pr)
        db.flush()
        for ln in lines:
            db.add(RequisitionLine(
                requisition_id=pr.id,
                product_id=ln["product_id"],
                product_supplier_id=ln.get("product_supplier_id"),
                proposed_qty=ln["qty"], qty=ln["qty"],
                unit_price=ln.get("unit_price"), included=True,
                trigger_type=ln.get("trigger_type"),
                line_confidence=ln.get("line_confidence", confidence),
                rationale=ln.get("rationale"),
            ))
        db.flush()
        db.refresh(pr)
        return pr

    # --- editing (only while STAGED) --------------------------------------

    def _assert_staged(self, pr: PurchaseRequisition) -> None:
        if pr.status is not RequisitionStatus.STAGED:
            raise ValidationError(
                f"Requisition is {pr.status.value}; only STAGED requisitions are editable")

    def edit_line(self, db: Session, requisition_id: str, line_id: str, *,
                  qty: Optional[int] = None, included: Optional[bool] = None) -> PurchaseRequisition:
        """Adjust a staged line's quantity and/or include flag."""
        pr = self.get_or_404(db, requisition_id)
        self._assert_staged(pr)
        line = db.get(RequisitionLine, line_id)
        if line is None or line.requisition_id != pr.id:
            raise ValidationError("Line is not on this requisition")
        if qty is not None:
            if qty <= 0:
                raise ValidationError("Quantity must be positive (drop the line instead)")
            line.qty = qty
        if included is not None:
            line.included = included
        db.flush()
        db.refresh(pr)
        return pr

    # --- approve -> convert to a PO ---------------------------------------

    def approve(self, db: Session, requisition_id: str, *, actor: Optional[str] = None,
                auto: bool = False) -> PurchaseRequisition:
        """Convert a STAGED PR into a fixed PO. Records per-line feedback first
        (the learning signal), then places ONE multi-line PO for the supplier.

        Raises if no line is included (nothing to order)."""
        pr = self.get_or_404(db, requisition_id)
        self._assert_staged(pr)

        included = [ln for ln in pr.lines if ln.included]
        if not included:
            raise ValidationError("No lines included — nothing to order. Reject it instead.")

        # 1. Capture feedback per decided line BEFORE we build the PO.
        for ln in pr.lines:
            if not ln.included:
                action = "dropped"
            elif ln.qty != ln.proposed_qty:
                action = "edited"
            else:
                action = "approved"
            calibration.record_line_feedback(
                db, requisition_id=pr.id, product_id=ln.product_id,
                supplier_id=pr.supplier_id, action=action,
                proposed_qty=ln.proposed_qty, final_qty=ln.qty if ln.included else 0,
                confidence=ln.line_confidence, auto_placed=auto)

        # 2. Place ONE PO per supplier (fixed document, invoice-matching unit).
        run_at = _now()
        prefix = "AUTO" if auto else "PR"
        order = purchase_order_service.create(db, {
            "order_number": f"{prefix}-{run_at.strftime('%Y%m%d%H%M%S')}-{pr.supplier_id[:8]}",
            "supplier_id": pr.supplier_id,
            "date_ordered": run_at.date(),
            "items": [{
                "product_id": ln.product_id,
                "product_supplier_id": ln.product_supplier_id,
                "quantity": ln.qty,
                "unit_price": ln.unit_price,
            } for ln in included],
        })

        pr.status = RequisitionStatus.PLACED
        pr.po_id = order.id
        pr.auto_placed = auto
        pr.decided_by = actor or ("agent" if auto else None)
        pr.decided_at = run_at
        db.flush()
        db.refresh(pr)
        return pr

    # --- reject -----------------------------------------------------------

    def reject(self, db: Session, requisition_id: str, *, actor: Optional[str] = None,
               reason: Optional[str] = None) -> PurchaseRequisition:
        """Dismiss a STAGED PR, recording the negative learning signal per line."""
        pr = self.get_or_404(db, requisition_id)
        self._assert_staged(pr)
        for ln in pr.lines:
            calibration.record_line_feedback(
                db, requisition_id=pr.id, product_id=ln.product_id,
                supplier_id=pr.supplier_id, action="rejected",
                proposed_qty=ln.proposed_qty, final_qty=0,
                confidence=ln.line_confidence, auto_placed=False)
        pr.status = RequisitionStatus.REJECTED
        pr.decided_by = actor
        pr.decided_at = _now()
        if reason:
            pr.rationale = f"{pr.rationale or ''}\n[rejected] {reason}".strip()
        db.flush()
        db.refresh(pr)
        return pr


requisition_service = RequisitionService()
