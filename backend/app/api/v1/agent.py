"""Agent routes: LLM-backed sourcing recommendation and portfolio insights.

Read-only and available to any authenticated user. Error mapping:
  - unknown product -> 404 (the underlying NotFoundError is mapped centrally by
    app.api.errors, same as every other route);
  - LLM/parse failure (AgentError) -> 502, raised here as an HTTPException.
"""
from __future__ import annotations

from datetime import datetime
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
from app.models.decision import DecisionLog
from app.services.exceptions import NotFoundError

router = APIRouter(tags=["agent"], prefix="/agent",
                   dependencies=[Depends(get_current_user)])

_INSIGHT_MIN = 5

# Per-day backstop on the on-demand AI commentary: even though it's click-only,
# cap the number of LLM calls per UTC day so a stuck client or a curious crowd
# can't run up the bill. In-process counter — fine for a single-instance demo.
_COMMENTARY_DAILY_CAP = 50
_commentary_calls: dict[str, int] = {}


def _commentary_allowed() -> bool:
    from datetime import datetime, timezone
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    n = _commentary_calls.get(day, 0)
    if n >= _COMMENTARY_DAILY_CAP:
        return False
    _commentary_calls.clear()          # only ever keep today's count
    _commentary_calls[day] = n + 1
    return True

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


class CommentaryFinding(BaseModel):
    title: str
    detail: Optional[str] = None
    metric: Optional[str] = None
    severity: Optional[str] = None


class CommentaryRequest(BaseModel):
    findings: List[CommentaryFinding]


class CommentaryResponse(BaseModel):
    commentary: str


@router.post("/commentary", response_model=CommentaryResponse)
def commentary(payload: CommentaryRequest, _user: User = Depends(get_current_user)):
    """Narrate OVER already-computed deterministic findings (on-demand, click-only).

    The deterministic rules engine computes the facts; this only synthesises a
    short read over them — the numbers come from the findings, never the model.
    Rate-limited per day as a cost backstop (429 when exceeded)."""
    if not payload.findings:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="no findings to narrate")
    if not _commentary_allowed():
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                            detail="Daily AI-commentary limit reached — try again tomorrow.")
    try:
        text = copilot.commentary_over_findings([f.model_dump() for f in payload.findings])
        return CommentaryResponse(commentary=text)
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


def _log_decisions(db: Session, result: PurchasingRunResult, actor: Optional[str]) -> None:
    """Persist each decision to the append-only DecisionLog — BEST EFFORT.

    A logging failure must never fail the run, so every write happens inside a
    SAVEPOINT (begin_nested) and the whole block is guarded: if it raises, we
    roll back only the log writes and swallow the error, leaving any placed POs
    and the run result untouched. Insert-only — rows are never updated.
    """
    try:
        for d in result.decisions:
            try:
                with db.begin_nested():
                    db.add(DecisionLog(
                        run_at=result.run_at.isoformat(),
                        dry_run=result.dry_run,
                        product_id=d.product_id,
                        supplier_id=d.supplier_id,
                        qty=d.qty,
                        unit_price=d.unit_price,
                        total=d.total,
                        trigger_type=getattr(d.trigger, "type", None),
                        evidence=getattr(d.trigger, "evidence", None),
                        tier=d.tier,
                        confidence=d.confidence,
                        rationale=d.rationale,
                        placed_po_id=d.placed_po_id,
                        actor=actor,
                    ))
            except Exception:  # noqa: BLE001  # nosec B112 — best-effort per-row audit; skip the bad row, never fail the run
                continue
    except Exception:  # noqa: BLE001  # nosec B110 — the audit write must never break a placed run
        pass


@router.post("/purchasing-run", response_model=PurchasingRunResult,
             dependencies=[Depends(_purchasing_role)])
def purchasing_run(payload: PurchasingRunRequest, db: Session = Depends(get_db),
                   user: User = Depends(get_current_user)):
    """Run the weekly purchasing automation. dry_run=True (default) places nothing."""
    result = purchasing.run_weekly_purchasing(
        db, dry_run=payload.dry_run, period_days=payload.period_days)
    _log_decisions(db, result, actor=user.email)   # best-effort audit write
    return result


@router.post("/purchasing-run/confirm", response_model=PurchasingRunResult,
             dependencies=[Depends(_purchasing_role)])
def purchasing_run_confirm(payload: PurchasingConfirmRequest, db: Session = Depends(get_db),
                           user: User = Depends(get_current_user)):
    """Approve->place: recompute the run and place POs only for approved suppliers
    whose recomputed bundle is placeable (act/propose). Escalate bundles are never
    placed here. Returns the recomputed run with placed_po_id set on confirmed ones."""
    result = purchasing.run_weekly_purchasing(
        db, period_days=payload.period_days,
        approve_suppliers=set(payload.approve_suppliers))
    _log_decisions(db, result, actor=user.email)   # best-effort audit write
    return result


# --- decision audit trail (read-only over the append-only DecisionLog) --------

class DecisionLogOut(BaseModel):
    id: str
    run_at: str
    dry_run: bool
    product_id: str
    supplier_id: Optional[str] = None
    qty: int
    unit_price: Optional[float] = None
    total: float
    trigger_type: Optional[str] = None
    evidence: Optional[dict] = None
    tier: str
    confidence: Optional[float] = None
    rationale: Optional[str] = None
    placed_po_id: Optional[str] = None
    actor: Optional[str] = None
    date_created: datetime

    model_config = {"from_attributes": True}


@router.get("/decisions", response_model=List[DecisionLogOut],
            dependencies=[Depends(_purchasing_role)])
def list_decisions(db: Session = Depends(get_db),
                   tier: Optional[str] = None,
                   product_id: Optional[str] = None,
                   supplier_id: Optional[str] = None,
                   run_at: Optional[str] = None,
                   placed_only: bool = False,
                   limit: int = 200):
    """The persistent decision audit trail, newest first. All filters optional."""
    q = db.query(DecisionLog)
    if tier:
        q = q.filter(DecisionLog.tier == tier)
    if product_id:
        q = q.filter(DecisionLog.product_id == product_id)
    if supplier_id:
        q = q.filter(DecisionLog.supplier_id == supplier_id)
    if run_at:
        q = q.filter(DecisionLog.run_at == run_at)
    if placed_only:
        q = q.filter(DecisionLog.placed_po_id.isnot(None))
    q = q.order_by(DecisionLog.date_created.desc()).limit(max(1, min(limit, 1000)))
    return list(q)


@router.get("/decisions/{decision_id}", response_model=DecisionLogOut,
            dependencies=[Depends(_purchasing_role)])
def get_decision(decision_id: str, db: Session = Depends(get_db)):
    """One decision by id (drill into its inputs; placed_po_id joins provenance)."""
    row = db.get(DecisionLog, decision_id)
    if row is None:
        raise NotFoundError(f"Decision {decision_id!r} not found")
    return row
