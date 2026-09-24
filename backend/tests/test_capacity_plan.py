"""The capacity plan in the DaaS scenario: Little's law inverted.

A fleet of 100 devices on 20-month contracts, a quarter on their second rental, and
compartments whose dwells have round means, so every figure can be worked out by hand:
the return flow at a milestone, the share per compartment from the next-step rule, the
required stock at today's dwell, the two levers, the month it breaks, a compartment
without dwell saying why, the datacenter scenario untouched, and the owned targets'
write path. The rounding in the assertions is the service's own (whole devices, one
decimal of days).
"""
from __future__ import annotations

from datetime import date, timedelta

import pytest

from app.models.auth import Role
from app.models.catalog import Organization, Product
from app.models.flow import Asset, AssetStatus, Location, LocationType
from app.models.kpi import FleetMilestone
from app.models.rental import ContractStatus, RentalContract
from app.services import capacity_plan, fleet, warehouse
from app.services.exceptions import NotFoundError, ValidationError

TODAY = date(2026, 10, 1)
M1, M2 = date(2027, 2, 1), date(2027, 10, 1)     # 123 days out, then 242 more
DPM = capacity_plan.DAYS_PER_MONTH
# ST-WIPE has no station on purpose; ST-REFURB has a station and no unit; ST-MDM is over capacity today.
CAPACITY = {"ST-NEW": 100, "ST-RETURNS": 7, "ST-MDM": 1, "ST-REPAIR": 20, "ST-REFURB": 20, "ST-SECOND": 60, "ST-SELL": 100, "ST-SWAP": 12}
DWELL = {"ST-NEW": (20, 40), "ST-RETURNS": (10, 20), "ST-MDM": (15, 15), "ST-WIPE": (3,), "ST-REPAIR": (30,),
         "ST-SECOND": (30, 60), "ST-SELL": (60, 120), "ST-SWAP": (1, 2, 3, 4)}


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


def _fleet(db, milestones=True):
    """100 devices at customers on 20-month contracts (25 on a second rental, 6 first rentals started
    last month), every compartment populated with dwells whose means are round numbers."""
    prod = _save(db, Product(product_code="PH-1", name="Phone 1", category="Smartphone"))
    cust = _save(db, Organization(code="CUST-T", name="Customer T (role-only)", is_supplier=False))
    for c in warehouse.COMPARTMENTS:
        if c.code in CAPACITY:
            _save(db, Location(code=c.code, name=c.name, location_type=LocationType.WAREHOUSE, capacity=CAPACITY[c.code]))
    for i in range(100):
        cycle = 2 if i < 25 else 1
        start = TODAY - timedelta(days=20) if 25 <= i < 31 else TODAY - timedelta(days=300)
        a = _unit(db, prod, f"R-{i}", AssetStatus.RENTED, (TODAY - start).days, cycle_no=cycle, customer_id=cust.id, deployed_date=start)
        db.add(RentalContract(asset_id=a.id, product_id=prod.id, customer_id=cust.id, cycle_no=cycle, start_date=start, term_months=20,
                              planned_end=start + timedelta(days=round(20 * DPM)), status=ContractStatus.RUNNING))
    for c in warehouse.COMPARTMENTS:
        for j, d in enumerate(DWELL.get(c.code, ())):
            _unit(db, prod, f"{c.code}-{j}", c.statuses[0], d, cycle_no=1, grade="A")
    db.flush()
    if milestones:
        capacity_plan.set_milestone(db, M1, target_fleet=400, owner="Business owner", note=None, actor="test", today=TODAY)
        capacity_plan.set_milestone(db, M2, target_fleet=1000, owner="Business owner", note=None, actor="test", today=TODAY)
    return prod


def _rows(ms):
    return {r["code"]: r for r in ms["rows"]}


def _comps(P):
    return {c["code"]: c for c in P["compartments"]}


def test_the_flow_maths_by_hand(db_session):
    """100 devices on 20-month terms, a quarter second rentals: 400 devices return 20 a month."""
    _fleet(db_session)
    P = capacity_plan.plan(db_session, today=TODAY)
    assert P["scenario"] == "daas" and P["fleet_now"] == 100 and P["reason"] is None
    m = P["model"]
    assert m["term_months"] == 20.0 and m["term_contracts"] == 100 and m["term_by_cycle"] == {"1": 20.0, "2": 20.0}
    assert m["cycle2_share"] == 0.25 and m["returns_per_month_now"] == 5
    # the next-step rule over the mix: 75 % of returns follow cycle 1, 25 % cycle 2
    s1, s2 = fleet.NEXT_STEP_SHARE[1], fleet.NEXT_STEP_SHARE[2]
    exit_share = 0.75 * (s1["sale"] + s1["recycling"]) + 0.25 * (s2["sale"] + s2["recycling"])
    assert m["exit_share"] == round(exit_share, 4) == 0.3175
    assert m["first_rentals_per_month_measured"] == 2 and m["first_rentals_months"] == 3      # six started last month
    assert m["exit_share_measured_12m"] is None and "no contract ended" in m["exit_share_measured_reason"]
    assert m["next_step_share"] == {"1": s1, "2": s2}, "the rule is reused, never restated"

    ms1 = P["milestones"][0]
    months = 123 / DPM
    assert ms1["date"] == M1 and ms1["target_fleet"] == 400 and ms1["owner"] == "Business owner" and ms1["placeholder"] is False
    assert ms1["months_from_today"] == round(months, 2)
    assert ms1["growth_per_month"] == round(300 / months) == 74
    assert ms1["returns_per_month"] == 20                                   # 400 / 20
    assert ms1["exits_per_month"] == round(20 * exit_share) == 6           # sold or recycled, to be replaced
    assert ms1["placements_per_month"] == round(300 / months + 20 * exit_share) == 81
    assert ms1["second_rentals_per_month"] == round(20 * 0.75 * (s1["second_rental"] + s1["repair"])) == 14
    r = _rows(ms1)
    assert r["ST-RETURNS"]["throughput_per_month"] == r["ST-MDM"]["throughput_per_month"] == r["ST-WIPE"]["throughput_per_month"] == 20
    assert r["ST-REPAIR"]["throughput_per_month"] == round(20 * 0.75 * s1["repair"]) == 3
    assert r["ST-REFURB"]["throughput_per_month"] == r["ST-SECOND"]["throughput_per_month"] == 14
    assert r["ST-SELL"]["throughput_per_month"] == round(20 * (0.75 * s1["sale"] + 0.25 * s2["sale"])) == 6
    assert r["ST-NEW"]["throughput_per_month"] == 81
    assert r["ST-SWAP"]["throughput_per_month"] is None and "reserve" in r["ST-SWAP"]["throughput_reason"]

    ms2 = P["milestones"][1]
    assert ms2["returns_per_month"] == 50 and ms2["growth_per_month"] == round(600 / (242 / DPM)) == 75
    assert P["path"][0] == {"date": TODAY, "month": "2026-10", "fleet": 100, "growth_per_month": 74, "toward": M1}
    assert [p["date"] for p in P["path"][:5]] == [TODAY, date(2026, 10, 31), date(2026, 11, 30), date(2026, 12, 31), date(2027, 1, 31)]
    assert P["path"][5]["date"] == M1 and P["path"][5]["fleet"] == 400 and P["path"][-1]["date"] == M2 and P["path"][-1]["fleet"] == 1000
    assert P["path"][3]["fleet"] == round(100 + 300 * 91 / 123) == 322


def test_the_inversion_gives_both_levers(db_session):
    """Returns intake: 20 a month at a mean stay of 15 days needs 9.86 places in a 7-place station."""
    _fleet(db_session)
    P = capacity_plan.plan(db_session, today=TODAY)
    r = _rows(P["milestones"][0])
    intake = r["ST-RETURNS"]
    assert intake["dwell_days"] == 15.0 and intake["required_stock"] == round(20 * 15 / DPM) == 10 and intake["capacity"] == 7
    assert intake["fits"] is False and intake["gap"] == -3
    assert intake["required_dwell_days"] == round(7 / 20 * DPM, 1) == 10.7     # lever 1: capacity / throughput
    assert intake["extra_places"] == 3                                        # lever 2: at today's dwell
    second = r["ST-SECOND"]
    assert second["dwell_days"] == 45.0 and second["required_stock"] == 20 and second["fits"] is True and second["extra_places"] == 0
    assert second["required_dwell_days"] == round(60 / (20 * 0.75 * 0.91) * DPM, 1)
    new = r["ST-NEW"]
    assert new["required_stock"] == round((300 / (123 / DPM) + 20 * 0.3175) * 30 / DPM) == 79 and new["fits"] is True
    swap = r["ST-SWAP"]
    assert P["model"]["swap_ratio"] == 0.04 and swap["required_stock"] == 16 and swap["extra_places"] == 4
    assert swap["required_dwell_days"] is None and "reserve" in swap["required_dwell_reason"]
    assert P["milestones"][0]["extra_places_total"] == 3 + 9 + 4 and P["milestones"][0]["compartments_short"] == 3


def test_the_breach_month_is_read_off_the_path(db_session):
    _fleet(db_session)
    P = capacity_plan.plan(db_session, today=TODAY)
    by = _comps(P)
    # returns intake: 7 places hold a flow of 7 / (15 / 30.4375) = 14.2 a month, a fleet of 284; the path crosses it on 31.12.2026
    intake = by["ST-RETURNS"]
    assert intake["breach_state"] == "later" and intake["breach_month"] == "2026-12" and intake["breach_date"] == date(2026, 12, 31)
    assert intake["breach_fleet"] == 322 and not intake["over_capacity_today"] and "322" in intake["breach_reason"]
    assert intake["required_now"] == round(5 * 15 / DPM) == 2 and intake["on_hand"] == 2
    # the swap buffer scales with the fleet: 12 places at 4 % is a fleet of 300, crossed the same month
    assert by["ST-SWAP"]["breach_month"] == "2026-12"
    # the MDM hold is over capacity today (two units in a one-place station) and the model already exceeds it
    mdm = by["ST-MDM"]
    assert mdm["over_capacity_today"] and mdm["breach_now"] and mdm["breach_state"] == "over_today" and mdm["breach_month"] == "2026-10"
    assert P["first_breach"]["code"] == "ST-MDM" and P["first_breach"]["month"] == "2026-10" and P["first_breach"]["over_capacity_today"]
    # everything else fits through the last milestone
    for code in ("ST-NEW", "ST-REPAIR", "ST-SECOND", "ST-SELL"):
        assert by[code]["breach_state"] == "fits" and by[code]["breach_month"] is None and M2.isoformat() in by[code]["breach_reason"], code


def test_no_data_says_why_instead_of_zero(db_session):
    _fleet(db_session)
    P = capacity_plan.plan(db_session, today=TODAY)
    r = _rows(P["milestones"][0])
    by = _comps(P)
    # refurbishment: a station, a flow, and not one unit: no dwell, so no required stock, but the dwell lever still answers
    refurb = r["ST-REFURB"]
    assert refurb["throughput_per_month"] == 14 and refurb["required_stock"] is None and refurb["extra_places"] is None
    assert refurb["reason"] == "no unit in this compartment" and refurb["fits"] is None
    assert refurb["required_dwell_days"] == round(20 / (20 * 0.75 * 0.91) * DPM, 1)
    assert by["ST-REFURB"]["breach_state"] == "unknown" and by["ST-REFURB"]["breach_month"] is None and by["ST-REFURB"]["required_now"] is None
    # wipe and grading: a flow and a dwell, but no station: nothing to compare against
    wipe = r["ST-WIPE"]
    assert wipe["required_stock"] == 2 and wipe["capacity"] is None and wipe["gap"] is None and wipe["fits"] is None
    assert wipe["required_dwell_days"] is None and "ST-WIPE" in wipe["required_dwell_reason"]
    assert by["ST-WIPE"]["breach_state"] == "no_capacity" and "ST-WIPE" in by["ST-WIPE"]["breach_reason"]
    assert by["ST-WIPE"]["capacity_placeholder"] is False and by["ST-NEW"]["capacity_placeholder"] is True
    assert by["ST-NEW"]["capacity_owner"] == capacity_plan.CAPACITY_OWNER and by["ST-NEW"]["capacity_set_by"] is None


def test_a_fleet_without_running_contracts_has_no_flow(db_session):
    prod = _save(db_session, Product(product_code="PH-2", name="Phone 2"))
    _unit(db_session, prod, "R-9", AssetStatus.RENTED, 10, cycle_no=1)
    _unit(db_session, prod, "S-9", AssetStatus.READY_SECOND, 10)
    P = capacity_plan.plan(db_session, today=TODAY)
    assert P["scenario"] == "daas" and P["fleet_now"] == 1
    assert P["model"]["term_months"] is None and "no running rental contract" in P["reason"]
    row = _rows(P["milestones"][0])["ST-SECOND"]
    assert row["throughput_per_month"] is None and row["required_stock"] is None and "no running rental contract" in row["reason"]
    assert all(c["breach_state"] in ("unknown", "no_capacity") for c in P["compartments"])
    assert P["first_breach"] is None


def test_the_owners_milestones_are_seeded_once_and_owned(db_session):
    _fleet(db_session, milestones=False)
    assert db_session.query(FleetMilestone).count() == 0
    P = capacity_plan.plan(db_session, today=TODAY)
    ms = P["milestones"]
    assert [(m["date"], m["target_fleet"]) for m in ms] == [(date(2026, 12, 31), 500_000), (date(2027, 12, 31), 1_000_000)]
    assert all(m["owner"] == capacity_plan.OWNER and m["placeholder"] is False and m["updated_by"] == "seed" for m in ms)
    assert db_session.query(FleetMilestone).count() == 2
    capacity_plan.plan(db_session, today=TODAY)
    assert db_session.query(FleetMilestone).count() == 2, "seeded once, never again"
    # a planner's what-if keeps the owner, records who asked
    capacity_plan.set_milestone(db_session, date(2026, 12, 31), target_fleet=800_000, owner=None, note=None, actor="planner", today=TODAY)
    row = db_session.query(FleetMilestone).filter_by(milestone_date=date(2026, 12, 31)).one()
    assert row.target_fleet == 800_000 and row.owner == capacity_plan.OWNER and row.updated_by == "planner"
    with pytest.raises(ValidationError):
        capacity_plan.set_milestone(db_session, TODAY, target_fleet=1, owner=None, note=None, actor="planner", today=TODAY)
    with pytest.raises(NotFoundError):
        capacity_plan.set_capacity(db_session, "ST-NOWHERE", capacity=1, actor="x")


def test_datacenter_scenario_is_untouched(client, db_session):
    wh = _save(db_session, Location(code="WH", name="Transit warehouse", location_type=LocationType.WAREHOUSE, capacity=10))
    prod = _save(db_session, Product(product_code="SRV-1", name="Server 1"))
    _save(db_session, Asset(serial_number="DC-1", product_id=prod.id, status=AssetStatus.IN_STORAGE, current_location_id=wh.id, received_date=TODAY))
    _save(db_session, Asset(serial_number="DC-2", product_id=prod.id, status=AssetStatus.DEPLOYED, received_date=TODAY, deployed_date=TODAY))
    P = capacity_plan.plan(db_session, today=TODAY)
    assert P["scenario"] == "datacenter" and P["milestones"] == [] and P["compartments"] == [] and P["reason"]
    assert P["fleet_now"] is None and P["model"] is None and P["first_breach"] is None
    assert db_session.query(FleetMilestone).count() == 0, "no milestone is seeded into the datacenter operation"
    r = client.get("/api/v1/capacity-plan")
    assert r.status_code == 200 and r.json()["scenario"] == "datacenter"


def test_api_read_and_the_owned_write_paths(client, db_session):
    _fleet(db_session)
    far = date(2030, 12, 31)
    r = client.get("/api/v1/capacity-plan")
    assert r.status_code == 200
    body = r.json()
    assert body["scenario"] == "daas" and len(body["compartments"]) == 9 and body["model"]["term_months"] == 20.0
    # the owner's target, changed by a planner: the owner stays, the plan comes back recomputed
    r = client.put(f"/api/v1/capacity-plan/milestones/{far.isoformat()}", json={"target_fleet": 800})
    assert r.status_code == 200
    ms = next(m for m in r.json()["milestones"] if m["date"] == far.isoformat())
    assert ms["target_fleet"] == 800 and ms["owner"] is None and ms["updated_by"] == "admin@example.com" and ms["returns_per_month"] == 40
    r = client.put(f"/api/v1/capacity-plan/milestones/{far.isoformat()}", json={"target_fleet": 900, "owner": "Business owner"})
    assert next(m for m in r.json()["milestones"] if m["date"] == far.isoformat())["owner"] == "Business owner"
    assert client.put("/api/v1/capacity-plan/milestones/2020-01-01", json={"target_fleet": 1}).status_code == 422
    assert client.put(f"/api/v1/capacity-plan/milestones/{far.isoformat()}", json={"target_fleet": -1}).status_code == 422
    whse, proc = client.as_role(Role.WAREHOUSE), client.as_role(Role.PROCUREMENT)
    assert whse.put(f"/api/v1/capacity-plan/milestones/{far.isoformat()}", json={"target_fleet": 5}).status_code == 403
    # the capacity: what room we assume, changed by the warehouse, and it stops being a placeholder
    r = whse.put("/api/v1/capacity-plan/compartments/ST-RETURNS/capacity", json={"capacity": 40})
    assert r.status_code == 200
    intake = next(c for c in r.json()["compartments"] if c["code"] == "ST-RETURNS")
    assert intake["capacity"] == 40 and intake["capacity_placeholder"] is False and intake["capacity_set_by"] == "warehouse@example.com"
    assert intake["capacity_set_on"] and intake["breach_state"] == "fits"
    r = whse.put("/api/v1/capacity-plan/compartments/ST-WIPE/capacity", json={"capacity": 5})
    assert next(c for c in r.json()["compartments"] if c["code"] == "ST-WIPE")["capacity"] == 5, "a station that did not exist is created"
    assert proc.put("/api/v1/capacity-plan/compartments/ST-RETURNS/capacity", json={"capacity": 1}).status_code == 403
    assert client.put("/api/v1/capacity-plan/compartments/ST-NOWHERE/capacity", json={"capacity": 1}).status_code == 404
    assert client.anon().get("/api/v1/capacity-plan").status_code in (401, 403)
