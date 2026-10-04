"""Total Cost of Ownership (TCO) read API.

Read-only views over the TCO service:
  GET /assets/{id}/tco   — the per-asset cost waterfall + should-cost variance
  GET /tco/portfolio     — per-layer subtotals + total_cost_pct / tscmc_pct
  GET /tco/by-class      — the datacenter waterfall per product category
  GET /tco/devices       — the device fleet: cost per device and per month in service,
                           per class and per model, finished lives and the fleet to date
  GET /tco/devices/serial/{key}
                         — one serial's whole life: every layer, what its rentals earned,
                           the dated events behind both (key = serial number or asset id)
  GET /tco/devices/models/{product_id}/serials
                         — a few serials of one model to open: finished, rented, on hand

The first three accept ``exclude_landed_types`` (repeatable) for tariff/scenario
filtering (e.g. ?exclude_landed_types=DUTY) and belong to the datacenter's stored
layers; they answer at once, and empty, over a device fleet. The device view is the
fleet's own and answers the datacenter scenario with a reason, so the frontend can
branch on one call. Reads are open to any authenticated user; the cost math lives in
services/tco.py and services/tco_device.py. Errors map centrally via ServiceError.
"""
from __future__ import annotations

from decimal import Decimal
from typing import List, Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, get_db
from app.models.auth import User
from app.schemas.tco import AssetTCO, DeviceOne, DeviceSerials, DeviceTCO, PortfolioTCO, TCOByClassRow
from app.services import tco as svc
from app.services import tco_device

router = APIRouter(tags=["tco"], dependencies=[Depends(get_current_user)])


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


@router.get("/tco/by-class", response_model=List[TCOByClassRow])
def tco_by_class(exclude_landed_types: Optional[List[str]] = Query(None),
                 db: Session = Depends(get_db), _u: User = Depends(get_current_user)):
    """Per-category TCO breakdown (only assets with recorded cost layers)."""
    return svc.tco_by_class(db, exclude_landed_types=exclude_landed_types)


@router.get("/tco/devices", response_model=DeviceTCO)
def device_tco(db: Session = Depends(get_db), _u: User = Depends(get_current_user)):
    """The rented-device fleet's TCO: per class, per model and rolled up, for the finished
    lives and for the whole fleet to date. Every figure with the counts behind it."""
    return tco_device.overview(db)


@router.get("/tco/devices/serial/{key}", response_model=DeviceOne)
def device_tco_one(key: str, db: Session = Depends(get_db), _u: User = Depends(get_current_user)):
    """One device's TCO, through the same figures as the fleet: acquisition to resale, every
    rental with what it earned, every repair and refurbishment with its invoice."""
    return tco_device.device(db, key)


@router.get("/tco/devices/models/{product_id}/serials", response_model=DeviceSerials)
def device_tco_serials(product_id: str, per_group: int = Query(4, ge=1, le=20),
                       db: Session = Depends(get_db), _u: User = Depends(get_current_user)):
    """A few serials of one model to open one by one: finished lives first, then rented, then on hand."""
    return tco_device.serials(db, product_id, per_group=per_group)
