"""capacity_flow: the one capacity-vs-flow metric (committed/free, in/out, coverage).

Pure read over existing services — no copilot, no mocking. Verifies the
composition and the durations that the over-order guard + order UI rely on.
"""
from __future__ import annotations

from datetime import date, timedelta

from app.models.catalog import Organization, Product, ProductSupplier
from app.models.flow import Asset, AssetStatus, Location, LocationType
from app.models.procurement import OrderItem, OrderStatus, PurchaseOrder
from app.services import planning

TODAY = date(2026, 6, 1)


def _save(db, obj):
    db.add(obj)
    db.flush()
    return obj


def _warehouse(db, code, cap, used=0):
    wh = _save(db, Location(code=code, name=code, location_type=LocationType.WAREHOUSE, capacity=cap))
    if used:
        filler = _save(db, Product(product_code=f"F-{code}", name="Filler"))
        for i in range(used):
            db.add(Asset(serial_number=f"S-{code}-{i}", product_id=filler.id,
                         status=AssetStatus.IN_STORAGE, current_location_id=wh.id))
        db.flush()
    return wh


def _inbound(db, wh, product, qty, *, eta_days):
    """An open PO line of `qty` heading to `wh`, ETA `eta_days` from TODAY."""
    sup = _save(db, Organization(code=f"SUP-{product.product_code}", name="Sup", is_supplier=True))
    ps = _save(db, ProductSupplier(product_id=product.id, supplier_id=sup.id,
                                   contract_price=100, standard_lead_time_days=10))
    po = _save(db, PurchaseOrder(order_number=f"PO-{product.product_code}", supplier_id=sup.id,
                                 destination_id=wh.id, status=OrderStatus.PLACED))
    _save(db, OrderItem(order_id=po.id, product_id=product.id, product_supplier_id=ps.id,
                        quantity=qty, unit_price=100,
                        estimated_delivery_date=TODAY + timedelta(days=eta_days)))


def _burn(db, product, n_deployed_in_window):
    """Deploy n assets in the recent burn window so daily_burn > 0 for `product`."""
    for i in range(n_deployed_in_window):
        db.add(Asset(serial_number=f"DEP-{product.product_code}-{i}", product_id=product.id,
                     status=AssetStatus.DEPLOYED,
                     deployed_date=TODAY - timedelta(days=10 + i)))
    db.flush()


def test_capacity_flow_committed_and_free(db_session):
    # Warehouse cap 200, 40 on hand, 30 inbound → committed 70, committed_pct 0.35.
    wh = _warehouse(db_session, "WH1", 200, used=40)
    prod = _save(db_session, Product(product_code="P1", name="P1"))
    _inbound(db_session, wh, prod, 30, eta_days=10)

    cf = planning.capacity_flow(db_session, today=TODAY)
    assert cf["capacity"] == 200
    assert cf["on_hand"] == 40
    assert cf["inbound"] == 30
    assert cf["committed"] == 70
    assert cf["committed_pct"] == 0.35
    # free_to_order = free(160) − inbound(30) = 130.
    assert cf["free_to_order"] == 130


def test_capacity_flow_daily_in_from_eta(db_session):
    wh = _warehouse(db_session, "WH2", 500, used=0)
    prod = _save(db_session, Product(product_code="P2", name="P2"))
    _inbound(db_session, wh, prod, 20, eta_days=10)   # 20 units over 10 days = 2/day
    cf = planning.capacity_flow(db_session, today=TODAY)
    assert cf["daily_in"] == 2.0


def test_capacity_flow_coverage_and_depletion(db_session):
    # A product with BOTH warehouse stock and recent deployments (burn) — the
    # realistic case: stock on hand that is being consumed. inventory_plan only
    # tracks products with on-hand/on-order, so the burn product must hold stock.
    wh = _warehouse(db_session, "WH3", 200)
    prod = _save(db_session, Product(product_code="P3", name="P3"))
    # 30 of P3 sitting in the warehouse (on hand)…
    for i in range(30):
        db_session.add(Asset(serial_number=f"OH-P3-{i}", product_id=prod.id,
                             status=AssetStatus.IN_STORAGE, current_location_id=wh.id))
    db_session.flush()
    _burn(db_session, prod, 9)         # …and 9 deployed recently → daily_burn > 0
    cf = planning.capacity_flow(db_session, today=TODAY)
    assert cf["daily_out"] > 0
    assert cf["days_to_depletion"] is not None
    # depletion = on_hand / daily_out; cover in weeks = that / 7.
    assert cf["days_to_depletion"] == round(cf["on_hand"] / cf["daily_out"], 1)
    assert cf["weeks_of_cover"] == round(cf["on_hand"] / cf["daily_out"] / 7, 1)


def test_capacity_flow_net_flow_sign(db_session):
    # inbound fast (2/day) but ~no burn → net flow positive (filling).
    wh = _warehouse(db_session, "WH4", 500, used=0)
    prod = _save(db_session, Product(product_code="P4", name="P4"))
    _inbound(db_session, wh, prod, 20, eta_days=10)
    cf = planning.capacity_flow(db_session, today=TODAY)
    assert cf["net_flow_per_day"] > 0


def test_capacity_flow_no_warehouse_is_uncapped(db_session):
    # No warehouse defined → no storage limit (free_to_order None), never blocks.
    cf = planning.capacity_flow(db_session, today=TODAY)
    assert cf["free_to_order"] is None
    assert cf["capacity"] == 0


def test_capacity_flow_endpoint(client, db_session):
    _warehouse(db_session, "WH-API", 100, used=10)
    r = client.get("/api/v1/planning/capacity-flow")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["capacity"] == 100 and body["on_hand"] == 10
    assert "weeks_of_cover" in body and "days_to_depletion" in body
