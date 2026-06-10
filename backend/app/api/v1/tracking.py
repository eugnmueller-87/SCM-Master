"""Logistics control-tower routes (read-only, any authenticated user).

Endpoint names mirror the handoff's PostgREST contract so the Tracking screen
needs no path changes:
  GET /v_order_tracking
  GET /shipment_events?shipment_id=eq.<id>&order=seq
The PostgREST-style ``shipment_id=eq.<id>`` filter is accepted (the ``eq.``
prefix is stripped); ``order`` is ignored (events are always returned by seq).
"""
from __future__ import annotations

from typing import List, Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, get_db
from app.models.auth import User
from app.schemas.tracking import OrderTracking, ShipmentEventRead
from app.services import tracking

router = APIRouter(tags=["tracking"], dependencies=[Depends(get_current_user)])


@router.get("/v_order_tracking", response_model=List[OrderTracking])
def v_order_tracking(db: Session = Depends(get_db), _user: User = Depends(get_current_user)):
    return tracking.order_tracking(db)


@router.get("/shipment_events", response_model=List[ShipmentEventRead])
def shipment_events(shipment_id: str = Query(...), order: Optional[str] = None,
                    db: Session = Depends(get_db), _user: User = Depends(get_current_user)):
    sid = shipment_id.split("eq.", 1)[-1] if shipment_id.startswith("eq.") else shipment_id
    return tracking.shipment_events(db, sid)
