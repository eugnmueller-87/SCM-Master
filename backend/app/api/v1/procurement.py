"""Procurement routes: purchase orders (with nested lines).

Status is read-only here — an order is created PENDING and advanced via the
dedicated transitions added in Phase 3, never by a raw PATCH.
"""
from __future__ import annotations

from typing import List

from fastapi import APIRouter, Depends, status
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.schemas.procurement import (
    PurchaseOrderCreate, PurchaseOrderRead, PurchaseOrderUpdate,
)
from app.services.procurement import purchase_order_service

router = APIRouter(tags=["procurement"])


@router.post("/purchase-orders", response_model=PurchaseOrderRead, status_code=status.HTTP_201_CREATED)
def create_purchase_order(payload: PurchaseOrderCreate, db: Session = Depends(get_db)):
    return purchase_order_service.create(db, payload.model_dump())


@router.get("/purchase-orders", response_model=List[PurchaseOrderRead])
def list_purchase_orders(skip: int = 0, limit: int = 100, db: Session = Depends(get_db)):
    return purchase_order_service.list(db, skip=skip, limit=limit)


@router.get("/purchase-orders/{order_id}", response_model=PurchaseOrderRead)
def get_purchase_order(order_id: str, db: Session = Depends(get_db)):
    return purchase_order_service.get_or_404(db, order_id)


@router.patch("/purchase-orders/{order_id}", response_model=PurchaseOrderRead)
def update_purchase_order(order_id: str, payload: PurchaseOrderUpdate, db: Session = Depends(get_db)):
    obj = purchase_order_service.get_or_404(db, order_id)
    return purchase_order_service.update(db, obj, payload.model_dump(exclude_unset=True))
