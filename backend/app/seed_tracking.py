"""Seed the logistics control-tower schema (ports scm_tracking_seed.sql).

Run (from backend/, after ``alembic upgrade head``):
    .venv\\Scripts\\python -m app.seed_tracking

Idempotent: bails out if shipments already exist.
"""
from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import select

from app.core.db import SessionLocal
from app.models.tracking import Shipment, ShipmentEvent, TrkPurchaseOrder, TrkSupplier

SUPPLIERS = [
    ("S001", "Shenzhen Optics Co.", "CN", 2, "NET45"),
    ("S002", "Pan-Asia Distribution", "TW", 1, "NET60"),
    ("S003", "Bosch Rexroth", "DE", 1, "NET30"),
    ("S004", "Murata Mfg.", "JP", 1, "NET30"),
    ("S005", "Würth Group", "DE", 1, "NET14"),
    ("S006", "Berliner Verpackung", "DE", 3, "NET30"),
]

POS = [
    ("PO-10288", "S001", "2026-04-12", "2026-04-30", 84200.00, "open"),
    ("PO-10310", "S002", "2026-04-25", "2026-05-19", 210000.00, "open"),
    ("PO-10293", "S003", "2026-04-28", "2026-05-05", 31500.00, "open"),
    ("PO-10301", "S004", "2026-04-26", "2026-05-04", 18900.00, "open"),
    ("PO-10275", "S005", "2026-04-28", "2026-05-03", 6420.00, "closed"),
    ("PO-10312", "S006", "2026-05-03", "2026-05-12", 2150.00, "open"),
]

SHIPMENTS = [
    # shipment_id, po_id, mode, carrier, current_status, progress_idx, current_location,
    # lat, lng, last_event_at, eta_original, eta_current, exception_flag, exception_reason
    ("SHP-0288", "PO-10288", "ocean", "Maersk", "customs", 3, "Hamburg customs, DE",
     53.55, 9.99, "2026-05-02T09:20", "2026-04-30", "2026-05-04", True, "Held — HS code documentation query"),
    ("SHP-0310", "PO-10310", "ocean", "Evergreen", "departed_origin", 1, "Kaohsiung Port, TW",
     22.6163, 120.2818, "2026-05-13T11:05", "2026-05-19", "2026-05-21", False, None),
    ("SHP-0293", "PO-10293", "road", "DB Schenker", "in_transit", 2, "Frankfurt hub, DE",
     50.1109, 8.6821, "2026-05-03T14:40", "2026-05-05", "2026-05-05", False, None),
    ("SHP-0301", "PO-10301", "air", "Lufthansa Cargo", "out_for_delivery", 4, "Munich, DE",
     48.1374, 11.5755, "2026-05-04T07:55", "2026-05-04", "2026-05-04", False, None),
    ("SHP-0275", "PO-10275", "road", "Würth Logistik", "delivered", 5, "Berlin DC, DE",
     52.52, 13.405, "2026-05-03T10:12", "2026-05-03", "2026-05-03", False, None),
    ("SHP-0312", "PO-10312", "road", "local", "placed", 0, "Berlin, DE",
     52.52, 13.405, "2026-05-03T16:00", "2026-05-12", "2026-05-12", False, None),
]

EVENTS = {
    "SHP-0288": [
        (1, "placed", "Shenzhen, CN", "2026-04-12T10:00", "Order confirmed by supplier"),
        (2, "packed", "Shenzhen, CN", "2026-04-18T17:30", "Packed, awaiting vessel"),
        (3, "departed_origin", "Yantian Port, CN", "2026-04-22T08:15", "Loaded on MV Hanjin (ETD)"),
        (4, "arrived_hub", "Port of Hamburg, DE", "2026-04-28T06:40", "Container discharged"),
        (5, "customs", "Hamburg customs, DE", "2026-05-02T09:20", "Held — HS code documentation query"),
    ],
    "SHP-0310": [
        (1, "placed", "Hsinchu, TW", "2026-04-25T09:00", "Order confirmed"),
        (2, "packed", "Hsinchu, TW", "2026-05-09T15:20", "Packed & sealed"),
        (3, "departed_origin", "Kaohsiung Port, TW", "2026-05-13T11:05", "At port — vessel congestion, ETD slipping"),
    ],
    "SHP-0293": [
        (1, "placed", "Lohr am Main, DE", "2026-04-28T11:00", "Order confirmed"),
        (2, "packed", "Lohr am Main, DE", "2026-04-30T13:10", "Picked & packed"),
        (3, "departed_origin", "Würzburg, DE", "2026-05-02T08:30", "Departed origin"),
        (4, "in_transit", "Frankfurt hub, DE", "2026-05-03T14:40", "In transit — line haul"),
    ],
    "SHP-0301": [
        (1, "placed", "Kyoto, JP", "2026-04-26T10:30", "Order confirmed"),
        (2, "departed_origin", "Kansai Airport, JP", "2026-04-29T22:15", "Air freight departed"),
        (3, "arrived_hub", "Frankfurt FRA, DE", "2026-05-01T05:50", "Customs cleared at FRA"),
        (4, "out_for_delivery", "Munich, DE", "2026-05-04T07:55", "Out for delivery"),
    ],
    "SHP-0275": [
        (1, "placed", "Künzelsau, DE", "2026-04-28T09:45", "Order confirmed"),
        (2, "departed_origin", "Künzelsau, DE", "2026-04-30T16:00", "Dispatched"),
        (3, "delivered", "Berlin DC, DE", "2026-05-03T10:12", "Delivered — signed M. Krause"),
    ],
    "SHP-0312": [
        (1, "placed", "Berlin, DE", "2026-05-03T16:00", "PO issued & confirmed"),
    ],
}


def _d(s):
    return date.fromisoformat(s) if s else None


def _ts(s):
    return datetime.fromisoformat(s) if s else None


def seed_tracking() -> None:
    db = SessionLocal()
    try:
        if db.scalar(select(Shipment).limit(1)):
            print("Tracking already seeded — skipping.")
            return
        for sid, name, country, tier, terms in SUPPLIERS:
            db.add(TrkSupplier(supplier_id=sid, name=name, country=country, tier=tier, payment_terms=terms))
        for po_id, sid, od, ed, val, st in POS:
            db.add(TrkPurchaseOrder(po_id=po_id, supplier_id=sid, order_date=_d(od),
                                    expected_delivery=_d(ed), total_value=val, status=st))
        for (sid, po_id, mode, carrier, cs, pidx, loc, lat, lng, lev, eo, ec, exc, reason) in SHIPMENTS:
            db.add(Shipment(shipment_id=sid, po_id=po_id, mode=mode, carrier=carrier,
                            current_status=cs, progress_idx=pidx, current_location=loc,
                            current_lat=lat, current_lng=lng, last_event_at=_ts(lev),
                            eta_original=_d(eo), eta_current=_d(ec),
                            exception_flag=exc, exception_reason=reason))
            for seq, status, locname, ets, notes in EVENTS[sid]:
                db.add(ShipmentEvent(shipment_id=sid, seq=seq, status=status,
                                     location_name=locname, event_ts=_ts(ets), notes=notes))
        db.commit()
        print(f"Tracking seed complete: {len(SUPPLIERS)} suppliers, {len(POS)} POs, {len(SHIPMENTS)} shipments.")
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


if __name__ == "__main__":
    seed_tracking()
