"""The warehouse in the DaaS scenario, one compartment at a time.

The second-life compartment in the lifecycle, the split of the warehouse into
compartments, the dwell and rotation maths, the no-fake-zero rule, the offenders
read, and the datacenter scenario left exactly as it was. The datacenter tests stay
untouched; this file only asserts what the compartments add.
"""
from __future__ import annotations

from datetime import date, timedelta

import pytest

from app.models.catalog import Organization, Product
from app.models.flow import DEPLOYABLE_STATUSES, WAREHOUSE_STATUSES, Asset, AssetStatus, Location, LocationType
from app.models.rental import ContractStatus, RentalContract
from app.services import kpis, lifecycle, planning, warehouse
from app.services.asset import asset_service
from app.services.exceptions import NotFoundError, ValidationError

TODAY = date(2026, 9, 23)
# ST-WIPE has no station location on purpose: a compartment without a station has to say so.
CAPACITY = {"ST-NEW": 50, "ST-RETURNS": 2, "ST-MDM": 50, "ST-REPAIR": 50, "ST-REFURB": 50, "ST-SECOND": 10, "ST-SELL": 10, "ST-SWAP": 50}


def _save(db, obj):
    db.add(obj)
    db.flush()
    return obj


def _unit(db, prod, serial, status, days, **kw):
    row = dict(serial_number=serial, product_id=prod.id, status=status, cycle_no=kw.pop("cycle_no", 1),
               received_date=TODAY - timedelta(days=400),
               status_since=(TODAY - timedelta(days=days)) if days is not None else None)
    row.update(kw)
    return _save(db, Asset(**row))


def _warehouse(db):
    """A small fleet: one device rented (so the scenario is DaaS) and every compartment populated on purpose."""
    prod = _save(db, Product(product_code="PH-1", name="Phone 1", category="Smartphone"))
    cust = _save(db, Organization(code="CUST-T", name="Customer T (role-only)", is_supplier=False))
    for c in warehouse.COMPARTMENTS:
        if c.code in CAPACITY:
            _save(db, Location(code=c.code, name=c.name, location_type=LocationType.WAREHOUSE, capacity=CAPACITY[c.code]))
    rented = _unit(db, prod, "R-1", AssetStatus.RENTED, 30, cycle_no=2, customer_id=cust.id, deployed_date=TODAY - timedelta(days=30))
    db.add(RentalContract(asset_id=rented.id, customer_id=cust.id, cycle_no=2, start_date=TODAY - timedelta(days=30), term_months=24,
                          planned_end=TODAY + timedelta(days=700), status=ContractStatus.RUNNING))
    # new stock: two statuses in one compartment, and one unit without a dwell date
    _unit(db, prod, "N-1", AssetStatus.IN_STORAGE, 5, cycle_no=0, grade="A")
    _unit(db, prod, "N-2", AssetStatus.IN_STORAGE, 5, cycle_no=0, grade="A")
    _unit(db, prod, "N-3", AssetStatus.RECEIVED, 0, cycle_no=0)
    _unit(db, prod, "N-4", AssetStatus.IN_STORAGE, None, cycle_no=0)
    # returns intake: three units in a two-unit station
    for i, d in enumerate((1, 2, 3)):
        _unit(db, prod, f"RT-{i}", AssetStatus.RETURNED, d)
    # MDM hold: both far past the 21-day service level
    _unit(db, prod, "M-1", AssetStatus.MDM_RELEASE, 40)
    _unit(db, prod, "M-2", AssetStatus.MDM_RELEASE, 50)
    # wipe and grading: one unit, no station location
    _unit(db, prod, "W-1", AssetStatus.WIPE_GRADING, 1)
    # repair: empty on purpose
    # refurbishment: one unit on the bench
    _unit(db, prod, "F-1", AssetStatus.REFURB, 4, grade="B")
    # second-life stock: refurbished A and B after one rental, dwell chosen for the maths
    for i, (d, g) in enumerate(((10, "A"), (20, "B"), (40, "A"), (100, "B"))):
        _unit(db, prod, f"S-{i}", AssetStatus.READY_SECOND, d, grade=g, cycle_no=1)
    # sellable: everything past the 60-day target, the median not yet past 1.5 times it
    for i, d in enumerate((70, 80, 90, 200)):
        _unit(db, prod, f"SL-{i}", AssetStatus.SELLABLE, d, grade="C", cycle_no=2)
    # swap buffer: two fresh units
    _unit(db, prod, "SW-1", AssetStatus.SWAP_BUFFER, 3, grade="A")
    _unit(db, prod, "SW-2", AssetStatus.SWAP_BUFFER, 9, grade="A")
    db.flush()
    return prod


def _by_code(view):
    return {c["code"]: c for c in view["compartments"]}


def test_second_life_is_its_own_compartment_in_the_lifecycle():
    assert lifecycle.can_transition(AssetStatus.REFURB, AssetStatus.READY_SECOND)
    assert lifecycle.can_transition(AssetStatus.READY_SECOND, AssetStatus.RENTED)
    assert lifecycle.can_transition(AssetStatus.READY_SECOND, AssetStatus.SELLABLE)     # no second customer: cleared for sale
    assert lifecycle.can_transition(AssetStatus.READY_SECOND, AssetStatus.SWAP_BUFFER)
    assert not lifecycle.can_transition(AssetStatus.REFURB, AssetStatus.RENTED), "the compartment cannot be skipped"
    assert not lifecycle.can_transition(AssetStatus.WIPE_GRADING, AssetStatus.READY_SECOND), "no second life without refurbishment"
    assert not lifecycle.can_transition(AssetStatus.READY_SECOND, AssetStatus.RETURNED)
    with pytest.raises(ValidationError):
        lifecycle.assert_transition(AssetStatus.READY_SECOND, AssetStatus.SOLD)
    assert AssetStatus.READY_SECOND in WAREHOUSE_STATUSES
    assert AssetStatus.READY_SECOND in DEPLOYABLE_STATUSES
    assert AssetStatus.REFURB not in DEPLOYABLE_STATUSES, "a unit on the bench cannot go out next"
    # the datacenter flow is unchanged
    assert lifecycle.can_transition(AssetStatus.RECEIVED, AssetStatus.DEPLOYED)
    assert not lifecycle.can_transition(AssetStatus.DISPOSED, AssetStatus.RECEIVED)


def test_compartments_split_the_warehouse_in_chain_order(db_session):
    _warehouse(db_session)
    W = warehouse.compartments(db_session, today=TODAY)
    assert W["scenario"] == "daas"
    assert W["chain"] == [c["code"] for c in W["compartments"]] == [c.code for c in warehouse.COMPARTMENTS]
    assert [c["step"] for c in W["compartments"]] == list(range(1, 10))
    by = _by_code(W)
    assert by["ST-NEW"]["on_hand"] == 4 and by["ST-NEW"]["statuses"] == ["IN_STORAGE", "RECEIVED"]   # two statuses, one compartment
    assert by["ST-SECOND"]["on_hand"] == 4 and by["ST-SELL"]["on_hand"] == 4 and by["ST-REPAIR"]["on_hand"] == 0
    assert by["ST-SECOND"]["stage"] == "second life" and by["ST-NEW"]["stage"] == "first life"
    assert W["on_hand"] == sum(c["on_hand"] for c in W["compartments"]) == 21
    assert W["capacity"] == sum(CAPACITY.values()) and W["free"] == W["capacity"] - 21
    assert W["over_capacity"] == 1 and W["reason"] is None


def test_dwell_and_rotation_maths(db_session):
    """Second-life stock: four units at 10, 20, 40 and 100 days."""
    _warehouse(db_session)
    s = _by_code(warehouse.compartments(db_session, today=TODAY))["ST-SECOND"]
    assert s["median_days"] == 20.0 and s["p90_days"] == 100.0 and s["mean_days"] == 42.5 and s["oldest_days"] == 100
    assert s["target_dwell_days"] == 45 and s["target_placeholder"] is True and s["target_owner"]
    assert s["past_target_units"] == 1 and s["past_target_share"] == 0.25
    # Little's law: four units over a mean stay of 42.5 days is 0.7 a week, and 365 / 42.5 turns a year
    assert s["units_per_week"] == round(4 / 42.5 * 7, 1) == 0.7
    assert s["turns_per_year"] == round(365 / 42.5, 2)
    assert s["throughput_derived"] is True and "derived" in s["throughput_basis"] and "upper bound" in s["throughput_basis"]
    assert s["capacity"] == 10 and s["free"] == 6 and s["utilisation"] == 0.4 and not s["over_capacity"]


def test_verdicts_follow_the_target(db_session):
    _warehouse(db_session)
    by = _by_code(warehouse.compartments(db_session, today=TODAY))
    assert by["ST-RETURNS"]["verdict"] == "over_capacity" and by["ST-RETURNS"]["overflow"] == 1
    assert by["ST-MDM"]["verdict"] == "stalled"          # median 40 d against a target of 21 d
    assert by["ST-SELL"]["verdict"] == "slow_moving"     # all four past 60 d, median 80 d still under 1.5 x 60
    assert by["ST-SECOND"]["verdict"] == "healthy"       # one of four past target
    assert by["ST-SWAP"]["verdict"] == "healthy"
    assert by["ST-NEW"]["verdict"] == "healthy"
    assert all(c["verdict_reason"] for c in by.values())


def test_no_data_says_why_instead_of_zero(db_session):
    _warehouse(db_session)
    by = _by_code(warehouse.compartments(db_session, today=TODAY))
    empty = by["ST-REPAIR"]
    assert empty["on_hand"] == 0 and empty["verdict"] == "empty"
    for k in ("median_days", "p90_days", "mean_days", "oldest_days", "units_per_week", "turns_per_year", "past_target_share"):
        assert empty[k] is None, k
    assert empty["dwell_reason"] and empty["throughput_reason"]
    no_station = by["ST-WIPE"]
    assert no_station["on_hand"] == 1
    assert no_station["capacity"] is None and no_station["free"] is None and no_station["utilisation"] is None
    assert not no_station["over_capacity"] and "ST-WIPE" in no_station["capacity_reason"]
    assert no_station["median_days"] == 1.0                    # dwell is known even where capacity is not
    new = by["ST-NEW"]
    assert new["undated_units"] == 1 and new["on_hand"] == 4 and new["median_days"] == 5.0   # the undated unit counts, never as a dwell


def test_offenders_name_the_late_stock_and_the_oldest_units(db_session):
    _warehouse(db_session)
    o = warehouse.offenders(db_session, "ST-SELL", today=TODAY, limit=3)
    assert o["target_dwell_days"] == 60
    assert o["past_target_by_product"] == [{"name": "Phone 1", "family": "Smartphone", "units": 4}]
    assert [u["days"] for u in o["oldest"]] == [200, 90, 80]
    assert o["oldest"][0]["serial_number"] == "SL-3" and o["oldest"][0]["cycle_no"] == 2 and o["oldest"][0]["status"] == "SELLABLE"
    fresh = warehouse.offenders(db_session, "ST-SWAP", today=TODAY)
    assert fresh["past_target_by_product"] == [] and len(fresh["oldest"]) == 2
    with pytest.raises(NotFoundError):
        warehouse.offenders(db_session, "ST-NOWHERE", today=TODAY)


def test_transition_restarts_the_dwell_clock(db_session):
    """A device moved from the bench into second-life stock starts its dwell there at zero."""
    _warehouse(db_session)
    unit = db_session.query(Asset).filter_by(serial_number="F-1").one()
    assert unit.status_since == TODAY - timedelta(days=4)
    asset_service.transition(db_session, unit.id, AssetStatus.READY_SECOND, actor="test", effective_date=TODAY)
    assert unit.status == AssetStatus.READY_SECOND and unit.status_since == TODAY
    by = _by_code(warehouse.compartments(db_session, today=TODAY))
    assert by["ST-REFURB"]["on_hand"] == 0 and by["ST-SECOND"]["on_hand"] == 5 and by["ST-SECOND"]["oldest_days"] == 100


def test_datacenter_scenario_is_untouched(client, db_session):
    wh = _save(db_session, Location(code="WH", name="Transit warehouse", location_type=LocationType.WAREHOUSE, capacity=10))
    prod = _save(db_session, Product(product_code="SRV-1", name="Server 1"))
    _save(db_session, Asset(serial_number="DC-1", product_id=prod.id, status=AssetStatus.IN_STORAGE, current_location_id=wh.id, received_date=TODAY))
    _save(db_session, Asset(serial_number="DC-2", product_id=prod.id, status=AssetStatus.DEPLOYED, received_date=TODAY, deployed_date=TODAY))
    W = warehouse.compartments(db_session, today=TODAY)
    assert W["scenario"] == "datacenter" and W["compartments"] == [] and W["reason"]
    assert W["capacity"] is None and W["on_hand"] is None
    cap = planning.location_capacity(db_session)
    assert cap[0]["code"] == "WH" and cap[0]["used"] == 1 and cap[0]["capacity"] == 10 and not cap[0]["over_capacity"]
    r = client.get("/api/v1/warehouse/compartments")
    assert r.status_code == 200 and r.json()["scenario"] == "datacenter"


def test_api_warehouse_endpoints(client, db_session):
    _warehouse(db_session)
    r = client.get("/api/v1/warehouse/compartments")
    assert r.status_code == 200
    body = r.json()
    assert body["scenario"] == "daas" and len(body["compartments"]) == 9
    second = next(c for c in body["compartments"] if c["code"] == "ST-SECOND")
    assert second["on_hand"] == 4 and second["throughput_derived"] is True and second["target_placeholder"] is True
    o = client.get("/api/v1/warehouse/compartments/ST-SELL/offenders?limit=2").json()
    assert [u["serial_number"] for u in o["oldest"]] == ["SL-3", "SL-2"]
    assert client.get("/api/v1/warehouse/compartments/ST-NOWHERE/offenders").status_code == 404
    assert client.anon().get("/api/v1/warehouse/compartments").status_code in (401, 403)


def test_second_life_reach_kpi(db_session):
    _warehouse(db_session)
    by = {r["id"]: r for r in kpis.compute_all(db_session, today=TODAY)}
    # four refurbished units against one second rental started in the last 90 days: 4 / (1 / 3) = 12 months
    assert by["second_life_reach_months"]["current"] == 12.0 and by["second_life_reach_months"]["reason"] is None


def test_second_life_reach_kpi_says_why_without_second_rentals(db_session):
    prod = _save(db_session, Product(product_code="PH-2", name="Phone 2"))
    _unit(db_session, prod, "R-9", AssetStatus.RENTED, 10, cycle_no=1)
    _unit(db_session, prod, "S-9", AssetStatus.READY_SECOND, 10, grade="A")
    by = {r["id"]: r for r in kpis.compute_all(db_session, today=TODAY)}
    assert by["second_life_reach_months"]["current"] is None and "second rental" in by["second_life_reach_months"]["reason"]
