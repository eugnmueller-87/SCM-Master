"""KPIs tab: live values from the operational tables, owned targets, measured trend.

No fake zeros: a KPI without data says why. A seeded target is a placeholder until a
person sets it. Reading writes one snapshot per KPI per day.
"""
from __future__ import annotations

from datetime import date, timedelta

from app.models.catalog import Organization, Product
from app.models.flow import Asset, AssetStatus, Location, LocationType
from app.models.kpi import KpiSnapshot, KpiTarget
from app.models.procurement import OrderItem, OrderStatus, PurchaseOrder
from app.services import kpis
from app.services import kpis as svc

TODAY = date(2026, 9, 22)


def _save(db, obj):
    db.add(obj)
    db.flush()
    return obj


def _stock(db, *, n_old=3, n_new=2, price=1000.0):
    wh = _save(db, Location(code="WH-K", name="WH-K", location_type=LocationType.WAREHOUSE, capacity=100))
    sup = _save(db, Organization(code="SUP-K", name="Sup K", is_supplier=True))
    prod = _save(db, Product(product_code="K-1", name="Server K"))
    po = _save(db, PurchaseOrder(order_number="PO-K", supplier_id=sup.id, status=OrderStatus.RECEIVED))
    oi = _save(db, OrderItem(order_id=po.id, product_id=prod.id, quantity=n_old + n_new, unit_price=price))
    for i in range(n_old):
        db.add(Asset(serial_number=f"OLD-{i}", product_id=prod.id, status=AssetStatus.IN_STORAGE, current_location_id=wh.id,
                     source_order_item_id=oi.id, received_date=TODAY - timedelta(days=200)))
    for i in range(n_new):
        db.add(Asset(serial_number=f"NEW-{i}", product_id=prod.id, status=AssetStatus.IN_STORAGE, current_location_id=wh.id,
                     source_order_item_id=oi.id, received_date=TODAY - timedelta(days=5)))
    db.flush()
    return prod


def test_empty_system_says_why_not_zero(db_session):
    rows = svc.compute_all(db_session, today=TODAY)
    by = {r["id"]: r for r in rows}
    assert by["stock_value_eur"]["current"] is None
    assert by["stock_value_eur"]["reason"]
    assert by["stock_value_eur"]["status"] == "not_measurable"
    assert all(r["placeholder"] for r in rows)


def test_warehouse_kpis_from_assets(db_session):
    _stock(db_session)
    by = {r["id"]: r for r in svc.compute_all(db_session, today=TODAY)}
    assert by["stock_value_eur"]["current"] == 5000.0
    assert by["carrying_cost_eur_per_day"]["current"] == round(5000.0 * svc.CARRYING_COST_RATE_PA / 365.0, 2)
    assert by["aging_stock_pct"]["current"] == 60.0            # 3 of 5 older than 90 days
    assert by["median_days_in_stock"]["current"] == 200.0
    assert by["dead_stock_value_eur"]["current"] == 5000.0     # nothing of K-1 deployed in 180 days
    assert by["capacity_committed_pct"]["current"] == 5.0      # 5 of 100
    assert by["stock_turns"]["current"] is None                # no deployments in the last 90 days, says so
    assert by["stock_turns"]["reason"]


def test_seeded_targets_follow_the_direction_and_stay_placeholders(db_session):
    _stock(db_session)
    by = {r["id"]: r for r in svc.compute_all(db_session, today=TODAY)}
    aging = by["aging_stock_pct"]                 # lower is better: targets go down
    assert aging["target_y1"] < aging["current"] and aging["target_y3"] < aging["target_y1"]
    assert aging["placeholder"] is True
    turns = by["stock_turns"]                     # not measurable: no target seeded
    assert turns["target_y1"] is None


def test_set_target_is_owned_and_status_follows(db_session):
    _stock(db_session)
    svc.compute_all(db_session, today=TODAY)
    svc.set_target(db_session, "aging_stock_pct", y1=70.0, y2=40.0, y3=20.0, owner="Head of Procurement", note="agreed", actor="tester")
    by = {r["id"]: r for r in svc.compute_all(db_session, today=TODAY, snapshot=False)}
    a = by["aging_stock_pct"]
    assert a["placeholder"] is False and a["owner"] == "Head of Procurement" and a["updated_by"] == "tester"
    assert a["status"] == "met"                   # 60 % is already under the 70 % year-one target
    svc.set_target(db_session, "aging_stock_pct", y1=10.0, y2=5.0, y3=0.0, owner="Head of Procurement", note=None, actor="tester")
    by = {r["id"]: r for r in svc.compute_all(db_session, today=TODAY, snapshot=False)}
    assert by["aging_stock_pct"]["status"] == "open"          # measured once: nothing to judge yet
    assert by["aging_stock_pct"]["gap_to_y1"] == -50.0          # 10 target minus 60 today


def test_snapshot_once_per_day(db_session):
    _stock(db_session)
    svc.compute_all(db_session, today=TODAY)
    svc.compute_all(db_session, today=TODAY)
    n = db_session.query(KpiSnapshot).filter(KpiSnapshot.kpi_id == "stock_value_eur").count()
    assert n == 1
    svc.compute_all(db_session, today=TODAY + timedelta(days=1))
    assert db_session.query(KpiSnapshot).filter(KpiSnapshot.kpi_id == "stock_value_eur").count() == 2
    assert db_session.query(KpiTarget).count() == len(svc.KPIS)


def test_api_read_and_guarded_write(client, db_session):
    _stock(db_session)
    r = client.get("/api/v1/kpis")
    assert r.status_code == 200
    rows = r.json()
    assert {x["id"] for x in rows} == {k.id for k in svc.KPIS}
    r = client.put("/api/v1/kpis/aging_stock_pct/target", json={"target_y1": 50, "target_y2": 30, "target_y3": 10, "owner": "Head of Procurement"})
    assert r.status_code == 200 and r.json()["placeholder"] is False
    r = client.put("/api/v1/kpis/nope/target", json={"target_y1": 1})
    assert r.status_code == 404
    r = client.get("/api/v1/kpis/aging_stock_pct/history")
    assert r.status_code == 200 and len(r.json()) >= 1


def test_a_kpi_is_measured_once_a_day_and_the_reason_travels_with_it(db_session, monkeypatch):
    """The tab reuses the day's measurement; only Measure again takes a new one.

    Thirty-two reads over a 400,000-device fleet are not a page-load job, and repeating
    them would not change a number: every KPI is defined over a day. What must not get
    lost in the reuse is WHY a KPI has no value.
    """
    calls = {"n": 0}

    def counted(db, today):
        calls["n"] += 1
        return 12.0, None

    def unmeasurable(db, today):
        return None, "no shipment has been tracked yet"

    probe = kpis.KpiDef("probe", "warehouse", "Probe", "count", "higher", "A counted read.", "test", counted)
    silent = kpis.KpiDef("silent", "warehouse", "Silent", "count", "higher", "A read with nothing to measure.", "test", unmeasurable)
    monkeypatch.setattr(kpis, "KPIS", [probe, silent])
    monkeypatch.setattr(kpis, "KPI_BY_ID", {k.id: k for k in (probe, silent)})
    today = date(2026, 9, 22)

    kpis.compute_all(db_session, today=today)
    assert calls["n"] == 1
    kpis.compute_all(db_session, today=today)
    assert calls["n"] == 1, "the second read serves the day's measurement"
    kpis.compute_all(db_session, today=today, refresh=True)
    assert calls["n"] == 2, "refresh measures again"
    kpis.compute_all(db_session, today=today + timedelta(days=1))
    assert calls["n"] == 3, "a new day is a new measurement"

    rows = {r["id"]: r for r in kpis.compute_all(db_session, today=today)}
    for row in rows.values():
        assert row["current"] is not None or row["reason"], "a KPI without a value always says why"
