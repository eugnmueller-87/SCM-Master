"""Capacity diagnosis + storage-headroom tests.

The contract: over-capacity is a PLACEMENT problem (rebalance / hold inbound /
add capacity), never a reason to buy; and an order is capped by how much we can
actually store. Copilot is not involved here (pure reads), so no mocking needed.
"""
from __future__ import annotations

from datetime import date

from app.models.catalog import Organization, Product, ProductSupplier
from app.models.flow import Asset, AssetStatus, Location, LocationType
from app.models.procurement import OrderItem, OrderStatus, PurchaseOrder
from app.services import planning


def _save(db, obj):
    db.add(obj)
    db.flush()
    return obj


def _wh(db, code, cap):
    return _save(db, Location(code=code, name=code, location_type=LocationType.WAREHOUSE, capacity=cap))


def _rack(db, code, cap):
    return _save(db, Location(code=code, name=code, location_type=LocationType.RACK, capacity=cap))


def _fill(db, loc, n, *, product_id=None):
    pid = product_id
    if pid is None:
        pid = _save(db, Product(product_code=f"FILL-{loc.code}", name="Filler")).id
    for i in range(n):
        db.add(Asset(serial_number=f"S-{loc.code}-{i}", product_id=pid,
                     status=AssetStatus.IN_STORAGE, current_location_id=loc.id))
    db.flush()


def test_over_capacity_with_room_recommends_rebalance(db_session):
    a = _rack(db_session, "R-A", 5)
    _rack(db_session, "R-B", 50)        # same type, lots of room
    _fill(db_session, a, 7)             # 7/5 -> over
    row = next(d for d in planning.capacity_diagnosis(db_session) if d["code"] == "R-A")
    assert row["over_capacity"] is True
    assert row["recommended_action"] == "rebalance"
    assert row["room_elsewhere"] >= 2
    assert "do not order more" in row["summary"].lower()


def test_over_capacity_no_room_is_infrastructure_problem(db_session):
    a = _rack(db_session, "R-ONLY", 4)   # the only rack -> nowhere to move
    _fill(db_session, a, 6)
    row = next(d for d in planning.capacity_diagnosis(db_session) if d["code"] == "R-ONLY")
    assert row["recommended_action"] == "add_capacity"
    assert row["room_elsewhere"] == 0


def test_inbound_pressure_recommends_hold(db_session):
    # A full warehouse with an open PO heading to it -> hold the inbound.
    org = _save(db_session, Organization(name="S", code="S", is_supplier=True))
    prod = _save(db_session, Product(product_code="P", name="P"))
    wh = _wh(db_session, "WH-FULL", 5)
    _fill(db_session, wh, 5, product_id=prod.id)   # exactly full (100%)
    po = _save(db_session, PurchaseOrder(order_number="PO-IN", supplier_id=org.id,
                                         destination_id=wh.id, status=OrderStatus.PLACED))
    _save(db_session, OrderItem(order_id=po.id, product_id=prod.id, quantity=10))
    row = next(d for d in planning.capacity_diagnosis(db_session) if d["code"] == "WH-FULL")
    assert row["recommended_action"] == "hold_inbound"
    assert row["inbound_units"] == 10
    assert "PO-IN" in row["inbound_pos"]


def test_diagnosis_traces_cause_to_source_po(db_session):
    org = _save(db_session, Organization(name="S", code="S", is_supplier=True))
    prod = _save(db_session, Product(product_code="P", name="Widget"))
    _save(db_session, ProductSupplier(product_id=prod.id, supplier_id=org.id, preference_rank=1))
    po = _save(db_session, PurchaseOrder(order_number="PO-CAUSE", supplier_id=org.id,
                                         status=OrderStatus.RECEIVED))
    oi = _save(db_session, OrderItem(order_id=po.id, product_id=prod.id, quantity=6))
    rack = _rack(db_session, "R-CAUSE", 4)
    for i in range(6):
        db_session.add(Asset(serial_number=f"SC-{i}", product_id=prod.id,
                             status=AssetStatus.IN_STORAGE, current_location_id=rack.id,
                             source_order_item_id=oi.id))
    db_session.flush()
    row = next(d for d in planning.capacity_diagnosis(db_session) if d["code"] == "R-CAUSE")
    assert row["by_source_po"][0]["order_number"] == "PO-CAUSE"
    assert row["by_source_po"][0]["units"] == 6
    assert row["by_product"][0]["name"] == "Widget"


def test_healthy_location_not_flagged(db_session):
    a = _rack(db_session, "R-OK", 100)
    _fill(db_session, a, 10)            # 10% -> below threshold
    assert all(d["code"] != "R-OK" for d in planning.capacity_diagnosis(db_session))


# --- storage headroom ------------------------------------------------------

def test_storage_headroom_nets_inbound(db_session):
    org = _save(db_session, Organization(name="S", code="S", is_supplier=True))
    prod = _save(db_session, Product(product_code="P", name="P"))
    wh = _wh(db_session, "WH", 100)
    _fill(db_session, wh, 20, product_id=prod.id)   # 20 used -> 80 free
    po = _save(db_session, PurchaseOrder(order_number="PO", supplier_id=org.id,
                                         destination_id=wh.id, status=OrderStatus.PLACED))
    _save(db_session, OrderItem(order_id=po.id, product_id=prod.id, quantity=30))  # 30 inbound
    h = planning.storage_headroom(db_session)
    assert h["storable_max"] == 50          # 80 free - 30 inbound
    assert h["committed_inbound"] == 30


def test_storage_headroom_none_when_no_warehouse_capacity(db_session):
    # No warehouse zones at all -> no defined limit (None, not 0).
    _rack(db_session, "R", 50)              # a rack, not a warehouse
    assert planning.storage_headroom(db_session)["storable_max"] is None


def test_order_capped_at_storage_headroom(db_session, monkeypatch):
    """A buy is reduced to fit the warehouse, never exceeding storable space."""
    from app.agent import copilot, purchasing
    from app.agent.schemas import SourcingRecommendation

    def fake(db, pid, q=None):
        return SourcingRecommendation(
            product_id=pid, recommended_source_id="x", recommended_qty=q or 1,
            rationale="m", signals={}, assumptions=[], uncertainties=[],
            confidence=0.5, decision="recommend")
    monkeypatch.setattr(copilot, "recommend_sourcing", fake)

    org = _save(db_session, Organization(name="S", code="S", is_supplier=True))
    prod = _save(db_session, Product(product_code="P", name="P"))
    _save(db_session, ProductSupplier(product_id=prod.id, supplier_id=org.id,
                                      preference_rank=1, min_order_quantity=1, contract_price=10))
    wh = _wh(db_session, "WH", 100)
    _fill(db_session, wh, 95, product_id=prod.id)   # only 5 storable
    for i in range(40):                              # big lifecycle-replacement demand
        db_session.add(Asset(serial_number=f"D-{i}", product_id=prod.id,
                             status=AssetStatus.DECOMMISSIONED, decommissioned_date=date.today()))
    db_session.commit()

    bundles, _ = purchasing._compute_bundles(db_session, 7)
    line = bundles[org.id][0]
    assert line["qty"] <= 5, "order must be capped to the 5 storable units"
    assert line["storage_capped"] is True
