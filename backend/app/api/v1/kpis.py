"""KPIs tab API.

  GET /kpis                    — every KPI: live value, targets Y1/Y2/Y3, owner, status, trend
  GET /kpis/{id}/history       — the daily snapshots of one KPI
  PUT /kpis/{id}/target        — set the targets and the owner (PROCUREMENT; ADMIN passes)
  GET /kpis/export.csv         — every KPI flat: today, goal and its origin, half-year steps, plan need,
                                 public value with source, owner, levers, the data counted

Reads write one snapshot per KPI per day (that is how the trend exists) and reuse
that day's measurement on later reads; ``?refresh=true`` measures again. Any
authenticated user may read. Values are computed in services/kpis.py, never typed in.
"""
from __future__ import annotations

from typing import List

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, get_db, require_role
from app.models.auth import Role, User
from app.schemas.kpi import KpiPoint, KpiRead, KpiTargetWrite
from app.services import kpis as svc

router = APIRouter(prefix="/kpis", tags=["kpis"], dependencies=[Depends(get_current_user)])
_proc = require_role(Role.PROCUREMENT)


@router.get("", response_model=List[KpiRead])
def list_kpis(refresh: bool = Query(False, description="measure again now instead of reusing today's measurement"),
              db: Session = Depends(get_db), _u: User = Depends(get_current_user)):
    rows = svc.compute_all(db, refresh=refresh)
    db.commit()   # the day's snapshot and the goal model's targets
    return rows


@router.get("/export.csv", response_class=Response)
def export_kpis(db: Session = Depends(get_db), _u: User = Depends(get_current_user)):
    """Every KPI as one flat row for a spreadsheet, Power BI or a deck: today, the goal and
    where it comes from, the goal at each half-year end, the plan's need at each milestone,
    the public value with its source, the owner, the levers, and what is counted from which
    data. The same record /kpis serves, flattened; nothing is computed twice."""
    rows = svc.compute_all(db)
    db.commit()
    return Response(content=svc.export_csv(rows), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": 'attachment; filename="kpis.csv"'})


@router.get("/{kpi_id}/history", response_model=List[KpiPoint])
def kpi_history(kpi_id: str, days: int = Query(365, ge=1, le=3650),
                db: Session = Depends(get_db), _u: User = Depends(get_current_user)):
    if kpi_id not in svc.KPI_BY_ID:
        from app.services.exceptions import NotFoundError
        raise NotFoundError(f"unknown KPI {kpi_id}")
    return svc.history(db, kpi_id, days=days)


@router.put("/{kpi_id}/target", response_model=KpiRead, dependencies=[Depends(_proc)])
def set_kpi_target(kpi_id: str, payload: KpiTargetWrite,
                   db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    svc.set_target(db, kpi_id, y1=payload.target_y1, y2=payload.target_y2, y3=payload.target_y3,
                   owner=payload.owner, note=payload.note, actor=getattr(user, "email", None))
    db.commit()
    rows = svc.compute_all(db, snapshot=False)
    return next(r for r in rows if r["id"] == kpi_id)
