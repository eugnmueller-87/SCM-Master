"""Logistics control-tower reads (design handoff §6.4)."""
from __future__ import annotations

from datetime import date

from app.models.tracking import Shipment, ShipmentEvent, TrkPurchaseOrder, TrkSupplier

B = "/api/v1"


def _seed_tracking(db_session):
    db_session.add(TrkSupplier(supplier_id="S1", name="Shenzhen Optics", country="CN", tier=2))
    db_session.add(TrkPurchaseOrder(po_id="PO-1", supplier_id="S1", total_value=84200,
                                    expected_delivery=date(2026, 4, 30)))
    db_session.add(Shipment(
        shipment_id="SHP-1", po_id="PO-1", mode="ocean", carrier="Maersk",
        current_status="customs", progress_idx=3, current_location="Hamburg, DE",
        eta_original=date(2026, 4, 30), eta_current=date(2026, 5, 4),
        exception_flag=True, exception_reason="HS code query"))
    # a clean on-time one
    db_session.add(TrkSupplier(supplier_id="S2", name="Würth", country="DE", tier=1))
    db_session.add(TrkPurchaseOrder(po_id="PO-2", supplier_id="S2", total_value=6420,
                                    expected_delivery=date(2026, 5, 3)))
    db_session.add(Shipment(
        shipment_id="SHP-2", po_id="PO-2", mode="road", carrier="Würth",
        current_status="delivered", progress_idx=5, current_location="Berlin, DE",
        eta_original=date(2026, 5, 3), eta_current=date(2026, 5, 3), exception_flag=False))
    for seq, st, loc in [(1, "placed", "Shenzhen, CN"), (2, "packed", "Shenzhen, CN"),
                         (3, "customs", "Hamburg, DE")]:
        db_session.add(ShipmentEvent(shipment_id="SHP-1", seq=seq, status=st, location_name=loc))
    db_session.commit()


def test_order_tracking_rollup_and_labels(client, db_session):
    _seed_tracking(db_session)
    rows = client.get(f"{B}/v_order_tracking").json()
    by_po = {r["po_id"]: r for r in rows}
    assert by_po["PO-1"]["status_label"] == "Delayed"   # exception flag
    assert by_po["PO-1"]["delay_days"] == 4              # eta_current - eta_original
    assert by_po["PO-2"]["status_label"] == "Delivered"
    assert by_po["PO-1"]["supplier"] == "Shenzhen Optics"


def test_shipment_events_ordered_and_postgrest_filter(client, db_session):
    _seed_tracking(db_session)
    ev = client.get(f"{B}/shipment_events?shipment_id=eq.SHP-1&order=seq").json()
    assert [e["seq"] for e in ev] == [1, 2, 3]
    assert [e["status"] for e in ev] == ["placed", "packed", "customs"]


def test_shipment_events_unknown_404(client, db_session):
    _seed_tracking(db_session)
    assert client.get(f"{B}/shipment_events?shipment_id=eq.NOPE").status_code == 404


def test_tracking_requires_auth(client, db_session):
    _seed_tracking(db_session)
    assert client.anon().get(f"{B}/v_order_tracking").status_code == 401
