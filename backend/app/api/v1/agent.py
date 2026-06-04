"""Agent routes: LLM-backed sourcing recommendation and portfolio insights.

Read-only and available to any authenticated user. Error mapping:
  - unknown product -> 404 (the underlying NotFoundError is mapped centrally by
    app.api.errors, same as every other route);
  - LLM/parse failure (AgentError) -> 502, raised here as an HTTPException.
"""
from __future__ import annotations

from typing import List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.agent import copilot, purchasing
from app.agent.copilot import AgentError
from app.agent.schemas import AgentInsight, PurchasingRunResult, SourcingRecommendation
from app.api.deps import get_current_user, get_db, require_role
from app.models.auth import Role, User

router = APIRouter(tags=["agent"], prefix="/agent")

_INSIGHT_MIN = 5

# Running the purchasing automation is a procurement action, not a read.
_purchasing_role = require_role(Role.PROCUREMENT)


class SourcingRequest(BaseModel):
    product_id: UUID
    desired_qty: Optional[int] = None


class PurchasingRunRequest(BaseModel):
    dry_run: bool = True
    period_days: int = 7


class ChatTurn(BaseModel):
    role: str
    content: str


class AskRequest(BaseModel):
    question: str
    history: Optional[List[ChatTurn]] = None


class AskResponse(BaseModel):
    answer: str


class PurchasingConfirmRequest(BaseModel):
    """Approve specific suppliers' bundles from a prior preview, then place them.

    The run is recomputed from live data on confirm, so an approval that is no
    longer justified (or has become escalate-tier) will not place a PO.
    """
    approve_suppliers: list[str]
    period_days: int = 7


@router.post("/sourcing-recommendation", response_model=SourcingRecommendation)
def sourcing_recommendation(payload: SourcingRequest, db: Session = Depends(get_db),
                            _user: User = Depends(get_current_user)):
    try:
        return copilot.recommend_sourcing(db, str(payload.product_id), payload.desired_qty)
    except AgentError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc))


@router.get("/insights", response_model=List[AgentInsight])
def insights(db: Session = Depends(get_db), _user: User = Depends(get_current_user)):
    try:
        return copilot.generate_insights(db, min_count=_INSIGHT_MIN)
    except AgentError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc))


@router.post("/ask", response_model=AskResponse)
def ask(payload: AskRequest, db: Session = Depends(get_db),
        _user: User = Depends(get_current_user)):
    """Chat: answer a use-case question grounded in a live snapshot of the system."""
    history = [t.model_dump() for t in payload.history] if payload.history else None
    try:
        return AskResponse(answer=copilot.ask(db, payload.question, history))
    except AgentError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc))


@router.post("/purchasing-run", response_model=PurchasingRunResult,
             dependencies=[Depends(_purchasing_role)])
def purchasing_run(payload: PurchasingRunRequest, db: Session = Depends(get_db)):
    """Run the weekly purchasing automation. dry_run=True (default) places nothing."""
    return purchasing.run_weekly_purchasing(
        db, dry_run=payload.dry_run, period_days=payload.period_days)


@router.post("/purchasing-run/confirm", response_model=PurchasingRunResult,
             dependencies=[Depends(_purchasing_role)])
def purchasing_run_confirm(payload: PurchasingConfirmRequest, db: Session = Depends(get_db)):
    """Approve->place: recompute the run and place POs only for approved suppliers
    whose recomputed bundle is placeable (act/propose). Escalate bundles are never
    placed here. Returns the recomputed run with placed_po_id set on confirmed ones."""
    return purchasing.run_weekly_purchasing(
        db, period_days=payload.period_days,
        approve_suppliers=set(payload.approve_suppliers))
