"""Requisition routes — the agent's shopping cart and its approval workflow.

  POST /requisitions/run        run the cycle: stage PRs, auto-place the trusted ones
  GET  /requisitions            list (optionally by status: STAGED/PLACED/REJECTED)
  GET  /requisitions/{id}       one requisition with its lines
  PATCH /requisitions/{id}/lines/{lineId}   edit a staged line (qty / include)
  POST /requisitions/{id}/approve           convert PR -> PO (records feedback)
  POST /requisitions/{id}/reject            dismiss PR (records feedback)
  GET  /requisitions/calibration            what the agent has learned (the bars)

All mutations are a PROCUREMENT action (ADMIN passes); reads need any authed user.
"""
from __future__ import annotations

from typing import List, Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.agent import purchasing
from app.api.deps import get_current_user, get_db, require_role
from app.models.auth import Role, User
from app.models.requisition import RequisitionStatus
from app.schemas.requisition import (
    DecisionInput,
    LineEdit,
    RequisitionRead,
    RunCycleInput,
)
from app.services import calibration, ordering
from app.services.requisition import requisition_service

router = APIRouter(tags=["requisitions"], prefix="/requisitions")

_proc = require_role(Role.PROCUREMENT)


@router.post("/run", dependencies=[Depends(_proc)])
def run_cycle(payload: RunCycleInput, db: Session = Depends(get_db),
              user: User = Depends(get_current_user)):
    """Detect demand -> stage PRs -> auto-place those clearing their calibrated bar."""
    return purchasing.run_requisition_cycle(db, period_days=payload.period_days,
                                            actor=user.email)


# --- packages + manual order (the buyer-initiated order path) ---------------

class ManualOrderLine(BaseModel):
    product_id: str
    quantity: int = Field(gt=0)


class ManualOrderInput(BaseModel):
    """Order EITHER explicit product lines OR a package (expanded ×packs)."""
    lines: Optional[List[ManualOrderLine]] = None
    package_id: Optional[str] = None
    packs: int = Field(1, ge=1)


@router.get("/packages")
def list_packages(db: Session = Depends(get_db), _user: User = Depends(get_current_user)):
    """Reusable order bundles (e.g. 'Compute rack') a buyer can order in one click."""
    return [{"id": p.id, "code": p.code, "name": p.name, "description": p.description,
             "lines": [{"product_id": ln.product_id, "quantity": ln.quantity} for ln in p.lines]}
            for p in ordering.list_packages(db)]


@router.post("/manual", dependencies=[Depends(_proc)])
def manual_order(payload: ManualOrderInput, db: Session = Depends(get_db),
                 user: User = Depends(get_current_user)):
    """Place a manual order (products or a package). Capacity-guarded server-side:
    an order that can't fit the warehouse is REFUSED (422). Stages a requisition
    per supplier for approval — never auto-places."""
    return ordering.stage_manual_order(
        db, lines=[ln.model_dump() for ln in payload.lines] if payload.lines else None,
        package_id=payload.package_id, packs=payload.packs, actor=user.email)


@router.get("", response_model=List[RequisitionRead])
def list_requisitions(status: Optional[RequisitionStatus] = None,
                      skip: int = 0, limit: int = 100,
                      db: Session = Depends(get_db),
                      _user: User = Depends(get_current_user)):
    return requisition_service.list(db, status=status, skip=skip, limit=limit)


@router.get("/calibration")
def calibration_view(db: Session = Depends(get_db),
                     _user: User = Depends(get_current_user)):
    """Per (product, supplier) auto-place bars the agent has learned from feedback."""
    return calibration.calibration_overview(db)


@router.get("/{requisition_id}", response_model=RequisitionRead)
def get_requisition(requisition_id: str, db: Session = Depends(get_db),
                    _user: User = Depends(get_current_user)):
    return requisition_service.get_or_404(db, requisition_id)


@router.patch("/{requisition_id}/lines/{line_id}", response_model=RequisitionRead,
              dependencies=[Depends(_proc)])
def edit_line(requisition_id: str, line_id: str, payload: LineEdit,
              db: Session = Depends(get_db)):
    return requisition_service.edit_line(db, requisition_id, line_id,
                                         qty=payload.qty, included=payload.included)


@router.post("/{requisition_id}/approve", response_model=RequisitionRead,
             dependencies=[Depends(_proc)])
def approve(requisition_id: str, db: Session = Depends(get_db),
            user: User = Depends(get_current_user)):
    return requisition_service.approve(db, requisition_id, actor=user.email, auto=False)


@router.post("/{requisition_id}/reject", response_model=RequisitionRead,
             dependencies=[Depends(_proc)])
def reject(requisition_id: str, payload: DecisionInput, db: Session = Depends(get_db),
           user: User = Depends(get_current_user)):
    return requisition_service.reject(db, requisition_id, actor=user.email, reason=payload.reason)
