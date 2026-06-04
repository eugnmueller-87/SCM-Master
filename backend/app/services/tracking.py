"""Control-tower reads over the logistics schema.

Two reads the Tracking screen needs:
  - ``order_tracking`` — the rolled-up "where is it now" view (one row per
    shipment, joined to its PO + supplier), with the derived status_label the
    SQL view computed (Delivered / Delayed / At risk / On time);
  - ``shipment_events`` — the ordered scan-by-scan trail for one shipment.

The derivations the seed SQL did in a VIEW are done here instead, so the schema
stays portable (SQLite dev / Postgres prod).
"""
from __future__ import annotations

from typing import Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.tracking import Shipment, ShipmentEvent, TrkPurchaseOrder, TrkSupplier
from app.services.exceptions import NotFoundError


def _status_label(sh: Shipment) -> str:
    delay = ((sh.eta_current - sh.eta_original).days
             if sh.eta_current and sh.eta_original else 0)
    if sh.current_status == "delivered":
        return "Delivered"
    if sh.exception_flag:
        return "Delayed"
    if delay > 0:
        return "At risk"
    return "On time"


def _delay_days(sh: Shipment) -> int:
    if sh.eta_current and sh.eta_original:
        return (sh.eta_current - sh.eta_original).days
    return 0


def order_tracking(db: Session) -> list[dict]:
    """One row per shipment — the control-tower 'current position' view."""
    rows = db.execute(
        select(Shipment, TrkPurchaseOrder, TrkSupplier)
        .join(TrkPurchaseOrder, Shipment.po_id == TrkPurchaseOrder.po_id)
        .join(TrkSupplier, TrkPurchaseOrder.supplier_id == TrkSupplier.supplier_id)
    ).all()
    out = []
    for sh, po, sup in rows:
        out.append({
            "po_id": po.po_id,
            "supplier": sup.name,
            "country": sup.country,
            "shipment_id": sh.shipment_id,
            "mode": sh.mode,
            "current_status": sh.current_status,
            "progress_idx": sh.progress_idx,
            "current_location": sh.current_location,
            "current_lat": sh.current_lat,
            "current_lng": sh.current_lng,
            "last_event_at": sh.last_event_at,
            "eta_original": sh.eta_original,
            "eta_current": sh.eta_current,
            "delay_days": _delay_days(sh),
            "exception_flag": sh.exception_flag,
            "total_value": float(po.total_value) if po.total_value is not None else None,
            "currency": po.currency,
            "status_label": _status_label(sh),
        })
    return sorted(out, key=lambda r: r["po_id"])


def shipment_events(db: Session, shipment_id: str) -> Sequence[ShipmentEvent]:
    """Ordered scan trail for one shipment (raises 404 if unknown)."""
    if db.get(Shipment, shipment_id) is None:
        raise NotFoundError(f"Shipment {shipment_id!r} not found")
    return db.scalars(
        select(ShipmentEvent)
        .where(ShipmentEvent.shipment_id == shipment_id)
        .order_by(ShipmentEvent.seq)
    ).all()
