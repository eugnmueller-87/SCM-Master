"""Outcome-feedback calibration — how the agent learns to auto-place more safely.

The auto-place gate is a confidence bar: a staged requisition whose confidence is
at or above the bar converts to a PO automatically; below it, a human approves.
A fixed bar is dumb — it ignores that humans rubber-stamp some (product, supplier)
buys every time while editing or rejecting others. This service moves the bar per
(product, supplier) based on that track record:

  - sources humans consistently **approve unchanged** earn TRUST -> the bar drops
    (up to ``calibration_max_delta``), so more of their buys auto-place;
  - sources often **edited, dropped, or rejected** earn DISTRUST -> the bar rises,
    so they keep going to a human until the agent's proposals improve.

The signal is :class:`RequisitionFeedback` rows (one per decided line). Until a
source has at least ``calibration_min_samples`` of history, the bar is the global
default — we don't move it on noise. Everything here is a transparent, auditable
rule (no opaque model): ``adjusted_floor`` and the counts behind it are returned
so the UI can explain exactly why a buy auto-placed or waited.
"""
from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.requisition import RequisitionFeedback

# Per-action weight toward trust (+) or distrust (−). Approving unchanged is the
# strongest positive signal; a reject is the strongest negative.
_ACTION_SCORE = {
    "approved": 1.0,    # accepted the agent's proposal as-is
    "edited": -0.5,     # changed the quantity — agent was close but off
    "dropped": -1.0,    # removed the line — agent shouldn't have proposed it
    "rejected": -1.0,   # killed the whole PR
}


@dataclass
class Calibration:
    """The bar for one (product, supplier) and the evidence behind it."""

    product_id: str
    supplier_id: str
    base_floor: float
    adjusted_floor: float
    samples: int
    approval_rate: float        # share of feedback that was 'approved' unchanged
    trust_score: float          # weighted signal in [-1, 1]
    reason: str

    def as_dict(self) -> dict:
        return {
            "product_id": self.product_id,
            "supplier_id": self.supplier_id,
            "base_floor": round(self.base_floor, 3),
            "adjusted_floor": round(self.adjusted_floor, 3),
            "samples": self.samples,
            "approval_rate": round(self.approval_rate, 3),
            "trust_score": round(self.trust_score, 3),
            "reason": self.reason,
        }


def _feedback_rows(db: Session, product_id: str, supplier_id: str) -> list[RequisitionFeedback]:
    return list(db.scalars(
        select(RequisitionFeedback).where(
            RequisitionFeedback.product_id == product_id,
            RequisitionFeedback.supplier_id == supplier_id,
        )
    ).all())


def calibrate(db: Session, product_id: str, supplier_id: str) -> Calibration:
    """Compute the adjusted auto-place bar for one (product, supplier)."""
    base = settings.auto_place_confidence
    rows = _feedback_rows(db, product_id, supplier_id)
    n = len(rows)

    if n < settings.calibration_min_samples:
        return Calibration(
            product_id=product_id, supplier_id=supplier_id,
            base_floor=base, adjusted_floor=base, samples=n,
            approval_rate=0.0, trust_score=0.0,
            reason=f"Not enough history ({n}/{settings.calibration_min_samples}) — default bar.",
        )

    score = sum(_ACTION_SCORE.get(r.action, 0.0) for r in rows) / n  # mean signal in [-1,1]
    approvals = sum(1 for r in rows if r.action == "approved")
    approval_rate = approvals / n

    # Trust LOWERS the bar (auto-place more); distrust RAISES it. Clamp the move.
    delta = -score * settings.calibration_max_delta
    adjusted = max(0.5, min(0.99, base + delta))  # never below 0.5, never above 0.99

    if score > 0.2:
        reason = (f"Trusted: {approvals}/{n} approved unchanged — bar lowered "
                  f"{base:.0%} -> {adjusted:.0%}.")
    elif score < -0.2:
        reason = (f"Caution: frequently edited/rejected ({approval_rate:.0%} clean) — "
                  f"bar raised {base:.0%} -> {adjusted:.0%}.")
    else:
        reason = f"Mixed history ({approval_rate:.0%} clean) — bar near default ({adjusted:.0%})."

    return Calibration(
        product_id=product_id, supplier_id=supplier_id,
        base_floor=base, adjusted_floor=adjusted, samples=n,
        approval_rate=approval_rate, trust_score=score, reason=reason,
    )


def record_line_feedback(db: Session, *, requisition_id: str, product_id: str,
                         supplier_id: str, action: str, proposed_qty: int,
                         final_qty: int, confidence: float, auto_placed: bool) -> None:
    """Append one feedback row — the learning input for future calibration."""
    db.add(RequisitionFeedback(
        requisition_id=requisition_id, product_id=product_id, supplier_id=supplier_id,
        action=action, proposed_qty=proposed_qty, final_qty=final_qty,
        confidence=confidence, auto_placed=auto_placed,
    ))
    db.flush()


def calibration_overview(db: Session) -> list[dict]:
    """Every (product, supplier) pair that has feedback, with its current bar.

    Powers a 'what the agent has learned' view — proof the threshold adapts.
    """
    pairs = db.execute(
        select(
            RequisitionFeedback.product_id,
            RequisitionFeedback.supplier_id,
            func.count(RequisitionFeedback.id),
        ).group_by(RequisitionFeedback.product_id, RequisitionFeedback.supplier_id)
    ).all()
    return [calibrate(db, pid, sid).as_dict() for pid, sid, _n in pairs]
