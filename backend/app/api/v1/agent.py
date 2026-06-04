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


@router.post("/purchasing-run", response_model=PurchasingRunResult,
             dependencies=[Depends(_purchasing_role)])
def purchasing_run(payload: PurchasingRunRequest, db: Session = Depends(get_db)):
    """Run the weekly purchasing automation. dry_run=True (default) places nothing."""
    return purchasing.run_weekly_purchasing(
        db, dry_run=payload.dry_run, period_days=payload.period_days)
