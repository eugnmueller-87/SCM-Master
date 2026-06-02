"""Asset lifecycle routes: receiving, the asset register, transitions, moves,
the event log, and provenance.

Receiving is modelled as a sub-resource of a purchase order
(``POST /purchase-orders/{id}/receipts``) since a receipt always happens
against an order. Everything else hangs off ``/assets``.
"""
from __future__ import annotations

from typing import List, Optional

from fastapi import APIRouter, Depends, status
from sqlalchemy.orm import Session

from app.api.deps import get_db, require_role
from app.models.auth import Role
from app.models.flow import AssetStatus
from app.schemas.asset import (
    AssetEventRead, AssetRead, AssetTrace, MoveRequest, ReceiptRead,
    ReceiveRequest, TransitionRequest,
)
from app.services.asset import asset_service
from app.services import provenance

router = APIRouter(tags=["assets"])

# Receiving is warehouse work; lifecycle moves span warehouse + datacenter ops.
_receiving = require_role(Role.WAREHOUSE)
_ops = require_role(Role.WAREHOUSE, Role.DATACENTER)


# --- Receiving (against a purchase order) ---------------------------------

@router.post("/purchase-orders/{order_id}/receipts", response_model=ReceiptRead,
             status_code=status.HTTP_201_CREATED, dependencies=[Depends(_receiving)])
def receive_order(order_id: str, payload: ReceiveRequest, db: Session = Depends(get_db)):
    return asset_service.receive(
        db, order_id,
        location_id=payload.location_id,
        lines=[line.model_dump() for line in payload.lines],
        receipt_date=payload.receipt_date,
        actor=payload.actor,
    )


# --- Asset register -------------------------------------------------------

@router.get("/assets", response_model=List[AssetRead])
def list_assets(skip: int = 0, limit: int = 100,
                status: Optional[AssetStatus] = None,
                location_id: Optional[str] = None,
                db: Session = Depends(get_db)):
    return asset_service.list(db, skip=skip, limit=limit, status=status, location_id=location_id)


@router.get("/assets/{asset_id}", response_model=AssetRead)
def get_asset(asset_id: str, db: Session = Depends(get_db)):
    return asset_service.get_or_404(db, asset_id)


@router.get("/assets/{asset_id}/events", response_model=List[AssetEventRead])
def get_asset_events(asset_id: str, db: Session = Depends(get_db)):
    return asset_service.events(db, asset_id)


# --- Lifecycle actions ----------------------------------------------------

@router.post("/assets/{asset_id}/transition", response_model=AssetRead,
             dependencies=[Depends(_ops)])
def transition_asset(asset_id: str, payload: TransitionRequest, db: Session = Depends(get_db)):
    return asset_service.transition(
        db, asset_id, payload.target,
        location_id=payload.location_id, actor=payload.actor, note=payload.note,
    )


@router.post("/assets/{asset_id}/move", response_model=AssetRead,
             dependencies=[Depends(_ops)])
def move_asset(asset_id: str, payload: MoveRequest, db: Session = Depends(get_db)):
    return asset_service.move(
        db, asset_id, payload.location_id, actor=payload.actor, note=payload.note,
    )


# --- Provenance -----------------------------------------------------------

@router.get("/assets/{asset_id}/provenance", response_model=AssetTrace)
def asset_provenance(asset_id: str, db: Session = Depends(get_db)):
    return provenance.trace_asset(db, asset_id)


@router.get("/order-items/{order_item_id}/assets", response_model=List[AssetRead])
def assets_for_order_line(order_item_id: str, db: Session = Depends(get_db)):
    return provenance.assets_for_line(db, order_item_id)
