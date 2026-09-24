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

    # A KPI cannot enter the registry without its explanation; a test double carries one too.
    ex = kpis.KpiExplain(basis="measured", calculation="counted", reads="test", caveats="none", why="a probe", needs="nothing")
    probe = kpis.KpiDef("probe", "warehouse", "Probe", "count", "higher", "A counted read.", "test", counted, explain=ex)
    silent = kpis.KpiDef("silent", "warehouse", "Silent", "count", "higher", "A read with nothing to measure.", "test", unmeasurable, explain=ex)
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


# --- the explanation travels with the value ----------------------------------------
#
# The contract, not the sentences: every KPI explains itself in every field, the row the
# API serves carries exactly the registry's words, a KPI the data cannot measure says what
# would make it measurable, and the registry refuses a KPI that explains nothing.

EXPLAIN_FIELDS = ("basis", "calculation", "reads", "caveats", "why", "needs")


def test_every_kpi_explains_itself_in_every_field(db_session, client):
    for k in svc.KPIS:
        for f in EXPLAIN_FIELDS:
            assert str(getattr(k.explain, f)).strip(), f"{k.id}: {f} is empty"
        assert k.explain.basis in svc.BASES, f"{k.id}: basis {k.explain.basis!r} is not one of {svc.BASES}"
    # the served row carries the registry's words, unchanged, whether the KPI measured or not
    _stock(db_session)
    rows = {r["id"]: r for r in svc.compute_all(db_session, today=TODAY)}
    for k in svc.KPIS:
        for f in EXPLAIN_FIELDS:
            assert rows[k.id][f] == getattr(k.explain, f)
    api_rows = {r["id"]: r for r in client.get("/api/v1/kpis").json()}
    for k in svc.KPIS:
        for f in EXPLAIN_FIELDS:
            assert api_rows[k.id][f] == getattr(k.explain, f)


def test_a_not_measurable_kpi_says_what_would_make_it_measurable(db_session):
    rows = {r["id"]: r for r in svc.compute_all(db_session, today=TODAY)}
    silent = [r for r in rows.values() if r["current"] is None]
    assert silent, "an empty database leaves something unmeasurable"
    for r in silent:
        assert r["reason"], f"{r['id']}: no value and no reason"
        assert r["needs"].strip(), f"{r['id']}: no value and nothing said about what would give one"
        assert r["needs"] != r["reason"], f"{r['id']}: 'needs' only repeats the reason"
    # the four the demo cannot measure today: no bill of materials, no requisition decided
    for kid in ("negotiation_gap_eur", "products_above_target_pct", "auto_placed_pct", "requisition_cycle_hours"):
        assert rows[kid]["current"] is None and rows[kid]["needs"]


def test_registry_refuses_a_kpi_without_an_explanation():
    import pytest

    def bare(db, today):
        return 1.0, None

    with pytest.raises(ValueError):
        svc.KpiDef("bare", "warehouse", "Bare", "count", "higher", "no words", "test", bare)
    with pytest.raises(ValueError):
        svc.explained(basis="guess", calculation="x", reads="x", caveats="x", why="x", needs="x")
    with pytest.raises(ValueError):
        svc.explained(basis="measured", calculation="x", reads="x", caveats=" ", why="x", needs="x")


def test_requisition_decision_hours_count_only_a_person(db_session):
    """The gate writes decided_at in the same run that stages a requisition. Counted, those
    zero-hour rows would pull 'hours to a person deciding' toward nothing; the KPI's own
    definition says a person, so the compute has to say so too."""
    from datetime import datetime, timezone

    from app.models.requisition import PurchaseRequisition, RequisitionStatus

    sup = _save(db_session, Organization(code="SUP-R", name="Sup R", is_supplier=True))
    t0 = datetime(2026, 9, 20, 8, 0, tzinfo=timezone.utc)
    db_session.add(PurchaseRequisition(supplier_id=sup.id, status=RequisitionStatus.PLACED, auto_placed=True,
                                       date_created=t0, decided_at=t0, decided_by="agent"))
    db_session.add(PurchaseRequisition(supplier_id=sup.id, status=RequisitionStatus.PLACED, auto_placed=False,
                                       date_created=t0, decided_at=t0 + timedelta(hours=10), decided_by="buyer"))
    db_session.add(PurchaseRequisition(supplier_id=sup.id, status=RequisitionStatus.STAGED))
    db_session.flush()
    by = {r["id"]: r for r in svc.compute_all(db_session, today=TODAY, snapshot=False)}
    assert by["requisition_cycle_hours"]["current"] == 10.0     # the person's 10 h, not the median of (0, 10)
    assert by["auto_placed_pct"]["current"] == 50.0             # one of two decided; the staged one is not decided
