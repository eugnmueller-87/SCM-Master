"""Total Cost of Ownership (TCO) read API.

Read-only views over the TCO service:
  GET /assets/{id}/tco   — the per-asset cost waterfall + should-cost variance
  GET /tco/portfolio     — per-layer subtotals + total_cost_pct / tscmc_pct

Both accept ``exclude_landed_types`` (repeatable) for tariff/scenario filtering
(e.g. ?exclude_landed_types=DUTY). Reads are open to any authenticated user; the
cost math lives in services/tco.py. Errors map centrally via ServiceError.
"""
from __future__ import annotations

from decimal import Decimal
from typing import List, Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, get_db
from app.models.auth import User
from app.schemas.tco import AssetTCO, PortfolioTCO
from app.services import tco as svc

router = APIRouter(tags=["tco"])


@router.get("/assets/{asset_id}/tco", response_model=AssetTCO)
def asset_tco(asset_id: str,
              exclude_landed_types: Optional[List[str]] = Query(None),
              db: Session = Depends(get_db), _u: User = Depends(get_current_user)):
    return svc.asset_tco(db, asset_id, exclude_landed_types=exclude_landed_types)


@router.get("/tco/portfolio", response_model=PortfolioTCO)
def portfolio_tco(baseline: float = Query(..., gt=0, description="revenue/cost baseline for the ratios"),
                  exclude_landed_types: Optional[List[str]] = Query(None),
                  db: Session = Depends(get_db), _u: User = Depends(get_current_user)):
    return svc.portfolio_tco(db, Decimal(str(baseline)), exclude_landed_types=exclude_landed_types)
