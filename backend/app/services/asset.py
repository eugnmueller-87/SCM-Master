"""Asset lifecycle service — the heart of the system.

Three responsibilities, all going through the lifecycle state machine and all
writing to the append-only AssetEvent log:

  1. Receiving — turn a PurchaseOrder (or some of its lines) into live, serial
     -tracked Assets. Each received unit is born in RECEIVED, linked to the
     OrderItem it came from (provenance), and placed at the receiving location.
     The order's status advances PENDING/PLACED -> PARTIALLY_RECEIVED -> RECEIVED
     based on cumulative received quantity vs ordered quantity.

  2. Transitions — store / deploy / send-to-maintenance / return / decommission
     / dispose. Every status change is validated against the state machine and
     logged.

  3. Moves — relocate an asset between locations, logged as a MOVED event.

Serial numbers are auto-generated here (SN-<short uuid>), since the receiving
flow mints assets in bulk.
"""
from __future__ import annotations

import uuid
from datetime import date
from typing import Optional, Sequence

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.flow import (
    Asset,
    AssetEvent,
    AssetEventType,
    AssetStatus,
    Location,
    Receipt,
    ReceiptItem,
)
from app.models.procurement import OrderItem, OrderStatus, PurchaseOrder
from app.services import lifecycle
from app.services.crud import CRUDService
from app.services.exceptions import NotFoundError, ValidationError


def _gen_serial() -> str:
    return f"SN-{uuid.uuid4().hex[:12].upper()}"


class AssetService(CRUDService[Asset]):
    def __init__(self):
        super().__init__(Asset)

    # --- queries ----------------------------------------------------------

    def list(self, db: Session, *, skip: int = 0, limit: int = 100,
             status: Optional[AssetStatus] = None,
             location_id: Optional[str] = None) -> Sequence[Asset]:
        stmt = select(Asset)
        if status is not None:
            stmt = stmt.where(Asset.status == status)
        if location_id is not None:
            stmt = stmt.where(Asset.current_location_id == location_id)
        stmt = stmt.offset(skip).limit(limit)
        return db.scalars(stmt).all()

    def events(self, db: Session, asset_id: str) -> Sequence[AssetEvent]:
        asset = self.get_or_404(db, asset_id)
        return asset.events

    # --- receiving --------------------------------------------------------

    def receive(self, db: Session, order_id: str, *, location_id: str,
                lines: list[dict], receipt_date: Optional[date] = None,
                actor: Optional[str] = None) -> Receipt:
        """Receive units against a PurchaseOrder.

        ``lines`` is a list of ``{"order_item_id": str, "quantity": int}``.
        Each unit becomes an Asset (auto serial) in RECEIVED at ``location_id``,
        linked to its OrderItem. Over-receipt (received-so-far + this batch >
        ordered) is rejected per line.
        """
        order = db.get(PurchaseOrder, order_id)
        if order is None:
            raise NotFoundError(f"PurchaseOrder {order_id!r} not found")
        if order.status in (OrderStatus.CANCELLED, OrderStatus.RECEIVED):
            raise ValidationError(f"Order {order.order_number} is {order.status.value}; cannot receive")

        location = db.get(Location, location_id)
        if location is None:
            raise NotFoundError(f"Location {location_id!r} not found")
        if not lines:
            raise ValidationError("Receipt must include at least one line")

        receipt = Receipt(
            purchase_order_id=order.id,
            received_at_id=location.id,
            receipt_date=receipt_date or date.today(),
        )
        db.add(receipt)
        db.flush()

        for line in lines:
            order_item = db.get(OrderItem, line["order_item_id"])
            if order_item is None:
                raise NotFoundError(f"OrderItem {line['order_item_id']!r} not found")
            if order_item.order_id != order.id:
                raise ValidationError("OrderItem does not belong to this order")

            qty = line["quantity"]
            if qty <= 0:
                raise ValidationError("Received quantity must be positive")

            already = self._received_qty(db, order_item.id)
            if already + qty > order_item.quantity:
                raise ValidationError(
                    f"Over-receipt on line {order_item.id}: "
                    f"{already}+{qty} exceeds ordered {order_item.quantity}"
                )

            db.add(ReceiptItem(
                receipt_id=receipt.id, order_item_id=order_item.id, quantity_received=qty,
            ))

            # Mint one Asset per unit, each born RECEIVED with provenance intact.
            for _ in range(qty):
                asset = Asset(
                    serial_number=_gen_serial(),
                    product_id=order_item.product_id,
                    status=AssetStatus.RECEIVED,
                    current_location_id=location.id,
                    source_order_item_id=order_item.id,
                    received_date=receipt.receipt_date,
                )
                db.add(asset)
                db.flush()
                self._log(db, asset, AssetEventType.RECEIVED,
                          to_status=AssetStatus.RECEIVED, to_location_id=location.id,
                          actor=actor, note=f"Received against {order.order_number}")

        db.flush()
        self._refresh_order_status(db, order)
        db.flush()
        db.refresh(receipt)
        return receipt

    def _received_qty(self, db: Session, order_item_id: str) -> int:
        total = db.scalar(
            select(func.coalesce(func.sum(ReceiptItem.quantity_received), 0))
            .where(ReceiptItem.order_item_id == order_item_id)
        )
        return int(total or 0)

    def _refresh_order_status(self, db: Session, order: PurchaseOrder) -> None:
        """Advance the order's status from cumulative received vs ordered qty."""
        items = db.scalars(
            select(OrderItem).where(OrderItem.order_id == order.id)
        ).all()
        if not items:
            return
        fully = all(self._received_qty(db, i.id) >= i.quantity for i in items)
        any_received = any(self._received_qty(db, i.id) > 0 for i in items)
        if fully:
            order.status = OrderStatus.RECEIVED
        elif any_received:
            order.status = OrderStatus.PARTIALLY_RECEIVED

    # --- transitions ------------------------------------------------------

    def transition(self, db: Session, asset_id: str, target: AssetStatus, *,
                   location_id: Optional[str] = None, actor: Optional[str] = None,
                   note: Optional[str] = None, effective_date: Optional[date] = None) -> Asset:
        """Move an asset to ``target`` status, validated by the state machine.

        Deploying may also set the destination location (a rack). The deployed_date
        / decommissioned_date stamps default to today, but ``effective_date`` lets a
        caller backdate them — used by the history seed to lay down dated usage the
        demand forecast can be backtested against. It never moves a stamp that is
        already set.
        """
        asset = self.get_or_404(db, asset_id)
        lifecycle.assert_transition(asset.status, target)

        from_status = asset.status
        from_location_id = asset.current_location_id

        if location_id is not None:
            if db.get(Location, location_id) is None:
                raise NotFoundError(f"Location {location_id!r} not found")
            asset.current_location_id = location_id

        when = effective_date or date.today()
        asset.status = target
        if target == AssetStatus.DEPLOYED and asset.deployed_date is None:
            asset.deployed_date = when
        if target == AssetStatus.DECOMMISSIONED and asset.decommissioned_date is None:
            asset.decommissioned_date = when

        db.flush()
        self._log(db, asset, AssetEventType.STATUS_CHANGED,
                  from_status=from_status, to_status=target,
                  from_location_id=from_location_id if location_id else None,
                  to_location_id=location_id, actor=actor, note=note)
        return asset

    def move(self, db: Session, asset_id: str, location_id: str, *,
             actor: Optional[str] = None, note: Optional[str] = None) -> Asset:
        """Relocate an asset without changing its status (a pure move)."""
        asset = self.get_or_404(db, asset_id)
        location = db.get(Location, location_id)
        if location is None:
            raise NotFoundError(f"Location {location_id!r} not found")
        if asset.current_location_id == location_id:
            raise ValidationError("Asset is already at that location")

        from_location_id = asset.current_location_id
        asset.current_location_id = location_id
        db.flush()
        self._log(db, asset, AssetEventType.MOVED,
                  from_location_id=from_location_id, to_location_id=location_id,
                  actor=actor, note=note)
        return asset

    # --- internal ---------------------------------------------------------

    def _log(self, db: Session, asset: Asset, event_type: AssetEventType, *,
             from_status: Optional[AssetStatus] = None,
             to_status: Optional[AssetStatus] = None,
             from_location_id: Optional[str] = None,
             to_location_id: Optional[str] = None,
             actor: Optional[str] = None, note: Optional[str] = None) -> None:
        db.add(AssetEvent(
            asset_id=asset.id, event_type=event_type,
            from_status=from_status, to_status=to_status,
            from_location_id=from_location_id, to_location_id=to_location_id,
            actor=actor, note=note,
        ))


asset_service = AssetService()
