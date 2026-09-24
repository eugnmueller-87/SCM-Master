"""The movement log: which compartment a device left, which it entered, when, how long it stayed.

Every move here goes through the asset service (the simulation's own path), so the test asserts
what the service now writes on the event row: the day of the move on the fleet's calendar, the
dwell start it ended and the stay in days; then what the window read makes of it, per pair and
per compartment, next to the derived figures; a device's path; and that the calendar shift moves
the log's dates and leaves its day counts alone.
"""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest

from app.models.catalog import Organization, Product
from app.models.flow import Asset, AssetEvent, AssetEventType, AssetStatus, Location, LocationType
from app.models.procurement import OrderItem, OrderStatus, PurchaseOrder
from app.models.rental import ContractStatus, RentalContract
from app.services import cycle, movements, timeshift, warehouse
from app.services.asset import asset_service
from app.services.exceptions import NotFoundError

TODAY = date(2026, 9, 24)


def _save(db, obj):
    db.add(obj)
    db.flush()
    return obj


def _unit(db, prod, serial, status, days, station, **kw):
    row = dict(serial_number=serial, product_id=prod.id, status=status, cycle_no=kw.pop("cycle_no", 1),
               received_date=TODAY - timedelta(days=400), current_location_id=(station.id if station else None),
               status_since=(TODAY - timedelta(days=days)) if days is not None else None)
    row.update(kw)
    return _save(db, Asset(**row))


def _fleet(db):
    prod = _save(db, Product(product_code="PH-1", name="Phone 1", category="Smartphone"))
    cust = _save(db, Organization(code="CUST-T", name="Customer T (role-only)", is_supplier=False))
    sup = _save(db, Organization(code="SUP-T", name="Supplier T (role-only)", is_supplier=True))
    st = {c.code: _save(db, Location(code=c.code, name=c.name, location_type=LocationType.WAREHOUSE, capacity=50)) for c in warehouse.COMPARTMENTS}
    rented = _unit(db, prod, "R-1", AssetStatus.RENTED, 30, None, customer_id=cust.id, deployed_date=TODAY - timedelta(days=30))
    db.add(RentalContract(asset_id=rented.id, product_id=prod.id, customer_id=cust.id, cycle_no=1, start_date=TODAY - timedelta(days=30),
                          term_months=24, planned_end=TODAY + timedelta(days=700), status=ContractStatus.RUNNING))
    db.flush()
    return prod, cust, sup, st


def _moves(db, prod, cust, st):
    """Four devices moved today through the service, with stays of 10, 0, 5 and unknown days, one old move, one receipt, one pure move."""
    a = _unit(db, prod, "A-1", AssetStatus.RETURNED, 10, st["ST-RETURNS"])
    cycle.book_in(db, a.id, actor="test", today=TODAY)                       # returns intake -> MDM hold, after 10 days
    cycle.release(db, a.id, actor="test", today=TODAY)                       # MDM hold -> wipe and grading, after 0 days
    b = _unit(db, prod, "B-1", AssetStatus.IN_STORAGE, 5, st["ST-NEW"], cycle_no=0, grade="A")
    cycle.rent(db, b.id, customer_id=cust.id, term_months=24, actor="test", today=TODAY)   # new stock -> at customers, after 5 days
    u = _unit(db, prod, "U-1", AssetStatus.REFURB, None, st["ST-REFURB"], grade="B")
    cycle.refurb_done(db, u.id, cost=30.0, actor="test", today=TODAY)       # refurbishment -> second-life stock, stay unknown
    old = _unit(db, prod, "O-1", AssetStatus.RETURNED, 50, st["ST-RETURNS"])
    asset_service.transition(db, old.id, AssetStatus.MDM_RELEASE, location_id=st["ST-MDM"].id, actor="test",
                             effective_date=TODAY - timedelta(days=40))       # forty days ago: outside a 30-day window
    m = _unit(db, prod, "P-1", AssetStatus.SELLABLE, 3, st["ST-SELL"], cycle_no=2, grade="C")
    asset_service.move(db, m.id, st["ST-SWAP"].id, actor="test")             # a pure move: no compartment change
    db.flush()
    return a, b, u, old, m


def _pair(view, frm, to):
    return next(p for p in view["pairs"] if p["from_status"] == frm and p["to_status"] == to)


def _comp(view, code):
    return next(c for c in view["compartments"] if c["code"] == code)


def test_the_asset_service_stamps_the_day_the_stay_and_where_it_came_from(db_session):
    prod, cust, _sup, st = _fleet(db_session)
    a, b, u, old, m = _moves(db_session, prod, cust, st)
    ev = {e.asset_id: [x for x in db_session.query(AssetEvent).filter(AssetEvent.asset_id == e.asset_id).order_by(AssetEvent.date_created, AssetEvent.id)]
          for e in db_session.query(AssetEvent).all()}
    first, second = ev[a.id]
    assert (first.from_status, first.to_status, first.effective_date, first.from_since, first.dwell_days) == (
        AssetStatus.RETURNED, AssetStatus.MDM_RELEASE, TODAY, TODAY - timedelta(days=10), 10)
    assert first.from_location_id == st["ST-RETURNS"].id and first.to_location_id == st["ST-MDM"].id, "where it came from is logged with where it went"
    assert (second.from_status, second.to_status, second.dwell_days, second.from_since) == (AssetStatus.MDM_RELEASE, AssetStatus.WIPE_GRADING, 0, TODAY)
    (rent,) = ev[b.id]
    assert (rent.to_status, rent.dwell_days, rent.from_location_id, rent.to_location_id) == (AssetStatus.RENTED, 5, st["ST-NEW"].id, None)
    (unk,) = ev[u.id]
    assert unk.to_status == AssetStatus.READY_SECOND and unk.dwell_days is None and unk.from_since is None and unk.effective_date == TODAY
    (past,) = ev[old.id]
    assert past.effective_date == TODAY - timedelta(days=40) and past.dwell_days == 10, "a backdated move measures its stay to its own day"
    (moved,) = ev[m.id]
    assert moved.event_type == AssetEventType.MOVED and moved.to_status is None and moved.dwell_days is None and moved.effective_date == date.today()


def test_the_window_counts_pairs_and_measures_the_finished_stays(db_session):
    prod, cust, sup, st = _fleet(db_session)
    _moves(db_session, prod, cust, st)
    v = movements.window(db_session, days=30, today=TODAY)
    assert v["days"] == 30 and v["since"] == TODAY - timedelta(days=30) and v["reason"] is None
    assert v["covered_days"] == 1 and "does not fill" in v["coverage_basis"], "every move in the window happened today: the log covers one day of it"
    assert v["moves"] == 4 and v["devices"] == 3 and v["pairs_count"] == 4 and v["undated_events"] == 0
    assert v["first_move"] == TODAY - timedelta(days=40) and v["last_move"] == TODAY, "the pure move and the old move are logged, only the window is counted"
    p = _pair(v, "RETURNED", "MDM_RELEASE")
    assert (p["from_name"], p["to_name"], p["from_code"], p["to_code"]) == ("Returns intake", "MDM release hold", "ST-RETURNS", "ST-MDM")
    assert p["units"] == 1 and p["dated_units"] == 1 and p["median_days"] == 10.0 and p["mean_days"] == 10.0 and p["max_days"] == 10 and p["per_week"] == 7.0
    assert _pair(v, "MDM_RELEASE", "WIPE_GRADING")["median_days"] == 0.0
    rent = _pair(v, "IN_STORAGE", "RENTED")
    assert rent["to_name"] == "At customers" and rent["to_code"] is None and rent["median_days"] == 5.0
    unk = _pair(v, "REFURB", "READY_SECOND")
    assert unk["units"] == 1 and unk["dated_units"] == 0 and unk["unknown_units"] == 1 and unk["median_days"] is None and "no stay measured" in unk["dwell_reason"]
    # pairs come in chain order, from the compartment they leave
    assert [(p["from_code"], p["to_code"]) for p in v["pairs"]] == [("ST-NEW", None), ("ST-RETURNS", "ST-MDM"), ("ST-MDM", "ST-WIPE"), ("ST-REFURB", "ST-SECOND")]
    # per compartment: in, out, the measured flow, the measured stay, next to the derived figures
    ret = _comp(v, "ST-RETURNS")
    assert (ret["units_in"], ret["units_out"], ret["out_per_week"], ret["median_days"], ret["dated_out"]) == (0, 1, 7.0, 10.0, 1)
    mdm = _comp(v, "ST-MDM")
    assert (mdm["units_in"], mdm["units_out"], mdm["mean_days"]) == (1, 1, 0.0) and mdm["target_dwell_days"] == 21
    assert mdm["on_hand"] == 1 and mdm["derived_units_per_week"] is not None and "derived" in mdm["derived_basis"] and "measured" in mdm["flow_basis"]
    ref = _comp(v, "ST-REFURB")
    assert ref["units_out"] == 1 and ref["dated_out"] == 0 and ref["unknown_out"] == 1 and ref["median_days"] is None and "no dwell start" in ref["dwell_reason"]
    assert _comp(v, "ST-SELL")["units_out"] == 0 and "nothing left" in _comp(v, "ST-SELL")["dwell_reason"], "a pure move is not a compartment movement"
    # a wider window takes the old move in, and the log then covers 41 of its 60 days
    wide = movements.window(db_session, days=60, today=TODAY)
    assert wide["moves"] == 5 and wide["covered_days"] == 41 and _pair(wide, "RETURNED", "MDM_RELEASE")["per_week"] == round(2 / 41 * 7, 1)


def test_a_receipt_is_a_move_from_an_order_line_into_new_stock(db_session):
    prod, cust, sup, st = _fleet(db_session)
    po = _save(db_session, PurchaseOrder(order_number="PO-1", supplier_id=sup.id, status=OrderStatus.PLACED, destination_id=st["ST-NEW"].id))
    line = _save(db_session, OrderItem(order_id=po.id, product_id=prod.id, quantity=3, unit_price=Decimal("500.00")))
    asset_service.receive(db_session, po.id, location_id=st["ST-NEW"].id, lines=[{"order_item_id": line.id, "quantity": 3}],
                          receipt_date=TODAY - timedelta(days=2), actor="test")
    v = movements.window(db_session, days=30, today=TODAY)
    p = _pair(v, None, "RECEIVED")
    assert (p["from_name"], p["to_name"], p["units"], p["dated_units"]) == ("On order", "New stock", 3, 0) and "receipt" in p["dwell_reason"]
    assert _comp(v, "ST-NEW")["units_in"] == 3 and v["devices"] == 3


def test_the_window_says_so_when_nothing_moved_and_counts_undated_rows(db_session):
    prod, cust, sup, st = _fleet(db_session)
    v = movements.window(db_session, days=30, today=TODAY)
    assert v["moves"] == 0 and v["pairs"] == [] and v["reason"] == "no move logged yet" and v["first_move"] is None and "no movement history" in v["history_note"]
    assert all(c["units_in"] == 0 and c["units_out"] == 0 and c["median_days"] is None for c in v["compartments"])
    a = _unit(db_session, prod, "L-1", AssetStatus.RETURNED, 4, st["ST-RETURNS"])
    db_session.add(AssetEvent(asset_id=a.id, event_type=AssetEventType.STATUS_CHANGED, from_status=AssetStatus.RENTED, to_status=AssetStatus.RETURNED))
    db_session.flush()
    v = movements.window(db_session, days=30, today=TODAY)
    assert v["moves"] == 0 and v["undated_events"] == 1 and "before the movement log had one" in v["undated_reason"]


def test_a_serial_opens_to_its_path_with_the_days_in_each_station(db_session):
    prod, cust, sup, st = _fleet(db_session)
    a, b, u, old, m = _moves(db_session, prod, cust, st)
    p = movements.path(db_session, "A-1", today=TODAY)
    assert (p["product"], p["status"], p["station_name"], p["station_code"], p["location_code"]) == ("Phone 1", "WIPE_GRADING", "Wipe and grading", "ST-WIPE", "ST-WIPE")
    assert p["since"] == TODAY and p["days_so_far"] == 0 and p["moves_logged"] == 2 and p["history_reason"] is None
    assert [(s["from_name"], s["to_name"], s["effective_date"], s["dwell_days"], s["actor"]) for s in p["steps"]] == [
        ("Returns intake", "MDM release hold", TODAY, 10, "test"), ("MDM release hold", "Wipe and grading", TODAY, 0, "test")]
    assert p["warehouse_days_measured"] == 10 and p["customer_days_measured"] == 0 and p["unknown_stays"] == 0
    q = movements.path(db_session, "B-1", today=TODAY)
    assert q["station_name"] == "At customers" and q["station_code"] is None and q["steps"][0]["dwell_days"] == 5 and q["warehouse_days_measured"] == 5
    r = movements.path(db_session, "U-1", today=TODAY)
    assert r["unknown_stays"] == 1 and r["warehouse_days_measured"] == 0
    n = movements.path(db_session, "R-1", today=TODAY)
    assert n["moves_logged"] == 0 and "no move logged for this device" in n["history_reason"] and n["days_so_far"] == 30
    with pytest.raises(NotFoundError):
        movements.path(db_session, "NOPE", today=TODAY)


def test_the_calendar_shift_moves_the_log_dates_and_leaves_the_day_counts(db_session):
    prod, cust, sup, st = _fleet(db_session)
    a, *_ = _moves(db_session, prod, cust, st)
    timeshift.advance(db_session, 5, action="test")
    first = db_session.query(AssetEvent).filter(AssetEvent.asset_id == a.id).order_by(AssetEvent.date_created, AssetEvent.id).first()
    assert first.effective_date == TODAY - timedelta(days=5) and first.from_since == TODAY - timedelta(days=15) and first.dwell_days == 10
    # a move made after the shift measures the stay that the calendar lengthened
    x = db_session.query(Asset).filter(Asset.serial_number == "A-1").one()
    assert x.status_since == TODAY - timedelta(days=5)
    cycle.grade(db_session, x.id, grade="A", actor="test", today=TODAY)
    last = db_session.query(AssetEvent).filter(AssetEvent.asset_id == a.id).order_by(AssetEvent.date_created.desc(), AssetEvent.id.desc()).first()
    assert (last.to_status, last.dwell_days, last.effective_date) == (AssetStatus.REFURB, 5, TODAY)
    assert movements.path(db_session, "A-1", today=TODAY)["warehouse_days_measured"] == 10 + 0 + 5


def test_api_movements(client, db_session):
    prod, cust, sup, st = _fleet(db_session)
    _moves(db_session, prod, cust, st)
    r = client.get("/api/v1/movements?days=30")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["moves"] == 4 and body["devices"] == 3 and len(body["compartments"]) == 9
    assert next(p for p in body["pairs"] if p["from_status"] == "RETURNED")["median_days"] == 10.0
    assert client.get("/api/v1/movements?days=0").status_code == 422
    p = client.get("/api/v1/movements/serials/A-1")
    assert p.status_code == 200 and p.json()["moves_logged"] == 2 and p.json()["steps"][0]["dwell_days"] == 10
    assert client.get("/api/v1/movements/serials/NOPE").status_code == 404
    assert client.anon().get("/api/v1/movements").status_code in (401, 403)
    # the asset's own event log carries the new facts too
    aid = p.json()["asset_id"]
    ev = client.get(f"/api/v1/assets/{aid}/events").json()
    assert ev[0]["dwell_days"] == 10 and ev[0]["effective_date"] == TODAY.isoformat() and ev[0]["from_since"] == (TODAY - timedelta(days=10)).isoformat()
