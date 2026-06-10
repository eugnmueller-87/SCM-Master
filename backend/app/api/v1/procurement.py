"""Procurement routes: purchase orders (with nested lines).

Status is read-only here — an order is created PENDING and advanced via the
dedicated transitions added in Phase 3, never by a raw PATCH.
"""
from __future__ import annotations

from typing import List

from fastapi import APIRouter, Depends, status
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, get_db, require_role
from app.models.auth import Role
from app.schemas.procurement import (
    OrderItemRead,
    PurchaseOrderCreate,
    PurchaseOrderRead,
    PurchaseOrderUpdate,
)
from app.schemas.sourcing import OrderStatusRequest, ResourceLineRequest
from app.services.procurement import purchase_order_service

router = APIRouter(tags=["procurement"], dependencies=[Depends(get_current_user)])

# Procurement write operations require the PROCUREMENT role (ADMIN always passes).
_procurement = require_role(Role.PROCUREMENT)


@router.post("/purchase-orders", response_model=PurchaseOrderRead, status_code=status.HTTP_201_CREATED,
             dependencies=[Depends(_procurement)])
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


@router.post("/purchase-orders/{order_id}/status", response_model=PurchaseOrderRead,
             dependencies=[Depends(_procurement)])
def set_order_status(order_id: str, payload: OrderStatusRequest, db: Session = Depends(get_db)):
    """Drive the approval flow: PENDING -> APPROVED -> PLACED (or CANCELLED)."""
    return purchase_order_service.set_status(db, order_id, payload.target)


@router.post("/purchase-orders/{order_id}/items/{order_item_id}/resource",
             response_model=OrderItemRead, dependencies=[Depends(_procurement)])
def resource_order_line(order_id: str, order_item_id: str,
                        payload: ResourceLineRequest, db: Session = Depends(get_db)):
    """Supplier-swap: repoint a line to a different source of the same product."""
    return purchase_order_service.resource_line(
        db, order_id, order_item_id, payload.product_supplier_id)
