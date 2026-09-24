"""The simulation tab: the day-to-day of a device fleet, fired at the running system.

A small fleet built with the ORM, every compartment populated, an open order line due
today, and running contracts with planned ends around today, so each action's claim can
be checked by hand: what moved and between which compartments, what was refused and why,
the contracts and invoices the moves wrote, the price the residual curve gives a sale,
the day that conserves the fleet, the hold that builds a queue, and the two gates:
production refuses, and the read-only guest cannot fire.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal

import pytest

from app import seed_daas as rules
from app import seed_reset
from app.core.safety import ProductionSafetyError
from app.models.auth import Role
from app.models.catalog import Organization, Product
from app.models.flow import Asset, AssetEvent, AssetStatus, Location, LocationType
from app.models.kpi import FleetMilestone, KpiSnapshot
from app.models.procurement import OrderItem, OrderStatus, PurchaseOrder
from app.models.rental import ContractStatus, RentalContract
from app.models.simulation import WorldClock
from app.models.tco import ServiceEvent, ServiceKind
from app.services import capacity_plan, kpis, lifecycle, timeshift, warehouse
from app.services import simulation as sim
from app.services.exceptions import NotFoundError, ValidationError

TODAY = date(2026, 9, 24)
# ST-MDM: 8 units fit today, the model breaks it in October; a five-day hold at two returns a day crosses it.
CAPACITY = {"ST-NEW": 60, "ST-RETURNS": 40, "ST-MDM": 17, "ST-WIPE": 20, "ST-REPAIR": 20, "ST-REFURB": 20, "ST-SECOND": 30, "ST-SELL": 40, "ST-SWAP": 10}
MDM_DWELL = (30, 40, 18, 19, 20, 2, 1, 1)     # two past the 21-day target, three that cross it within five days
PRICE = Decimal("700.00")


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


def _fleet(db):
    """Sixty devices at customers on two-month contracts (ten overdue, ten on a second rental), fifty sold in the
    last 90 days, one order line due today, every compartment populated, and one owned milestone a year out."""
    prod = _save(db, Product(product_code="APL-IP16-128", name="iPhone 16 / 128 GB", category="Smartphone"))
    sup = _save(db, Organization(code="SUP-A", name="IT reseller A (role-only)", is_supplier=True))
    custs = [_save(db, Organization(code=f"CUST-{i:03d}", name=f"Customer {i:03d} (role-only)", is_supplier=False)) for i in (1, 2, 3)]
    stations = {}
    for c in warehouse.COMPARTMENTS:
        stations[c.code] = _save(db, Location(code=c.code, name=c.name, location_type=LocationType.WAREHOUSE, capacity=CAPACITY[c.code]))
    po = _save(db, PurchaseOrder(order_number="PO-OPEN", supplier_id=sup.id, status=OrderStatus.PLACED, destination_id=stations["ST-NEW"].id))
    due = _save(db, OrderItem(order_id=po.id, product_id=prod.id, quantity=50, unit_price=PRICE, estimated_delivery_date=TODAY - timedelta(days=1)))
    _save(db, OrderItem(order_id=po.id, product_id=prod.id, quantity=20, unit_price=PRICE, estimated_delivery_date=TODAY + timedelta(days=10)))
    old = _save(db, PurchaseOrder(order_number="PO-OLD", supplier_id=sup.id, status=OrderStatus.RECEIVED))
    line = _save(db, OrderItem(order_id=old.id, product_id=prod.id, quantity=200, unit_price=PRICE))
    # at customers: ten overdue, the rest due in three weeks; ten on a second rental
    for i in range(60):
        cycle = 2 if i >= 50 else 1
        start = TODAY - timedelta(days=70 if i < 10 else 40)
        a = _unit(db, prod, f"R-{i}", AssetStatus.RENTED, (TODAY - start).days, cycle_no=cycle, customer_id=custs[i % 3].id,
                  deployed_date=start, source_order_item_id=line.id)
        db.add(RentalContract(asset_id=a.id, product_id=prod.id, customer_id=custs[i % 3].id, cycle_no=cycle, start_date=start,
                              term_months=2, planned_end=start + timedelta(days=61), status=ContractStatus.RUNNING))
    for i in range(50):
        _unit(db, prod, f"SOLD-{i}", AssetStatus.SOLD, 1 + i, cycle_no=2, grade="B", sold_date=TODAY - timedelta(days=1 + i), sale_price=300)
    # the warehouse
    for i in range(10):
        _unit(db, prod, f"N-{i}", AssetStatus.IN_STORAGE, 10 - i, cycle_no=0, grade="A", current_location_id=stations["ST-NEW"].id,
              source_order_item_id=line.id)
    for i in range(5):
        _unit(db, prod, f"RT-{i}", AssetStatus.RETURNED, 5 - i, current_location_id=stations["ST-RETURNS"].id)
    for i, d in enumerate(MDM_DWELL):
        _unit(db, prod, f"M-{i}", AssetStatus.MDM_RELEASE, d, current_location_id=stations["ST-MDM"].id)
    for i in range(4):
        _unit(db, prod, f"W-{i}", AssetStatus.WIPE_GRADING, 4 - i, cycle_no=(2 if i == 3 else 1), current_location_id=stations["ST-WIPE"].id)
    for i in range(3):
        _unit(db, prod, f"RP-{i}", AssetStatus.REPAIR, 12 - i, grade="C", current_location_id=stations["ST-REPAIR"].id)
    for i in range(3):
        _unit(db, prod, f"F-{i}", AssetStatus.REFURB, 6 - i, grade="B", current_location_id=stations["ST-REFURB"].id)
    for i in range(6):
        _unit(db, prod, f"S-{i}", AssetStatus.READY_SECOND, 30 - i, grade=("A" if i % 2 else "B"), current_location_id=stations["ST-SECOND"].id,
              source_order_item_id=line.id)
    for i, g in enumerate("ABCDBA"):
        _unit(db, prod, f"SL-{i}", AssetStatus.SELLABLE, 105 - i, cycle_no=2, grade=g, current_location_id=stations["ST-SELL"].id,
              source_order_item_id=line.id)
    for i in range(2):
        _unit(db, prod, f"SW-{i}", AssetStatus.SWAP_BUFFER, 3 + i, grade="A", current_location_id=stations["ST-SWAP"].id)
    db.flush()
    capacity_plan.set_milestone(db, TODAY + timedelta(days=365), target_fleet=120, owner="Business owner", note=None, actor="test", today=TODAY)
    return prod, due, stations


def _counts(db) -> dict:
    return {s: n for s, n in db.query(Asset.status, Asset.status).group_by(Asset.status).with_entities(Asset.status, __import__("sqlalchemy").func.count()).all()}


def _moves(ans) -> dict:
    return {(m["from_status"], m["to_status"]): m["units"] for m in ans["moved"]}


def _comp(ans, code) -> dict:
    return next(c for c in ans["compartments"] if c["code"] == code)


def _kpi(ans, kid) -> dict:
    return next(k for k in ans["kpis"] if k["id"] == kid)


# ---------------------------------------------------------------------------
# the catalogue and the rules it reuses


def test_the_actions_are_the_business_and_the_defaults_are_measured(db_session):
    _fleet(db_session)
    cat = sim.catalogue(db_session, today=TODAY)
    assert cat["scenario"] == "daas" and cat["reason"] is None
    assert [a["id"] for a in cat["actions"]] == ["delivery", "first_rentals", "returns", "intake", "mdm_release", "grading", "repair_done",
                                                  "refurb_done", "second_rentals", "sales", "recycling", "day", "hold", "move"]
    r = cat["rates"]
    assert r["returns_per_day"] == 2                     # 60 contracts end within 30 days
    assert r["first_rentals_per_day"] == 1               # 50 first rentals over the last three full months
    assert r["sales_per_day"] == 1 and r["second_rentals_per_day"] == 0
    # the chain: every return passes intake, the hold and grading; repairs at 2 x 5/6 x 0.19 round to none, refurbishments at 2 x 0.76 to two
    assert r["chain"] == {"ST-RETURNS": 2, "ST-MDM": 2, "ST-WIPE": 2, "ST-REPAIR": 0, "ST-REFURB": 2}
    assert r["deliveries_due"] == 50 and "measured" in r["returns_basis"] and "derived" in r["chain_basis"]
    by = {a["id"]: a for a in cat["actions"]}
    assert by["returns"]["params"][0]["default"] == 2 and by["delivery"]["params"][0]["default"] == 50
    assert by["hold"]["params"][0]["default"] == "ST-MDM" and by["hold"]["params"][1]["default"] == 5
    assert cat["choices"]["stations"][0]["code"] == "ST-NEW" and "ST-SWAP" not in [s["code"] for s in cat["choices"]["stations"]]
    assert cat["choices"]["products"] == [{"code": "APL-IP16-128", "name": "iPhone 16 / 128 GB"}]
    assert cat["max_units"] == sim.MAX_UNITS and "time pass" in cat["calendar_note"]
    assert cat["world"] == {"days_advanced": 0, "advanced_at": None, "last_action": None, "last_days": None}
    assert set(cat["kpis"]) == set(sim.SIM_KPIS) and "forecast_mape_pct" not in cat["kpis"] and "mdm_release_over_sla_pct" in cat["kpis"]


def test_the_mix_is_applied_in_proportion_and_deterministically():
    assert sim._spread(4, rules.GRADE_MIX) == ["B", "A", "C", "B"]
    hundred = sim._spread(100, rules.GRADE_MIX)
    assert {g: hundred.count(g) for g in "ABCD"} == {"A": 35, "B": 40, "C": 20, "D": 5}
    assert sim._spread(100, rules.GRADE_MIX) == hundred, "two runs pick the same"
    assert sim._spread(0, rules.GRADE_MIX) == []


# ---------------------------------------------------------------------------
# each action moves exactly what it claims


def test_a_delivery_is_received_against_the_open_line(db_session):
    prod, due, stations = _fleet(db_session)
    ans = sim.fire(db_session, "delivery", {"units": 10}, actor="test", today=TODAY)
    assert _moves(ans) == {("ORDERED", "RECEIVED"): 10} and ans["moved_total"] == 10 and ans["refused"] == []
    new = db_session.query(Asset).filter(Asset.status == AssetStatus.RECEIVED).all()
    assert len(new) == 10 and all(a.source_order_item_id == due.id and a.current_location_id == stations["ST-NEW"].id for a in new)
    assert all(a.status_since == TODAY for a in new), "the dwell clock starts at receipt"
    assert db_session.get(PurchaseOrder, due.order_id).status == OrderStatus.PARTIALLY_RECEIVED
    assert _comp(ans, "ST-NEW")["on_hand_before"] == 10 and _comp(ans, "ST-NEW")["on_hand_after"] == 20
    # more than is on order: the rest is refused, and says how much there was
    ans = sim.fire(db_session, "delivery", {"units": 100}, actor="test", today=TODAY)
    assert _moves(ans) == {("ORDERED", "RECEIVED"): 60}
    assert ans["refused"] == [{"what": "delivery", "reason": "only 60 on order", "units": 40}]
    assert db_session.get(PurchaseOrder, due.order_id).status == OrderStatus.RECEIVED
    with pytest.raises(NotFoundError):
        sim.fire(db_session, "delivery", {"units": 1, "product_code": "NOPE"}, actor="test", today=TODAY)


def test_first_rentals_start_from_the_oldest_new_stock_with_a_contract(db_session):
    prod, _due, _st = _fleet(db_session)
    ans = sim.fire(db_session, "first_rentals", {"units": 3}, actor="test", today=TODAY)
    assert _moves(ans) == {("IN_STORAGE", "RENTED"): 3} and ans["refused"] == []
    rented = db_session.query(Asset).filter(Asset.serial_number.in_(["N-0", "N-1", "N-2"])).all()   # the three that waited longest
    assert all(a.status == AssetStatus.RENTED and a.cycle_no == 1 and a.customer_id and a.current_location_id is None for a in rented)
    assert all(a.deployed_date == TODAY and a.status_since == TODAY for a in rented)
    contracts = {c.asset_id: c for c in db_session.query(RentalContract).filter(RentalContract.start_date == TODAY, RentalContract.cycle_no == 1)}
    assert len(contracts) == 3
    terms = sorted(contracts[a.id].term_months for a in rented)
    assert terms == [24, 24, 36], "terms from the seed's term mix, in proportion"
    for a in rented:
        c = contracts[a.id]
        assert c.status == ContractStatus.RUNNING and c.customer_id == a.customer_id and c.product_id == prod.id
        assert c.planned_end == TODAY + timedelta(days=round(c.term_months * sim.DAYS_PER_MONTH))
        assert float(c.rent_eur_month) == round(700 * rules.RENT_SHARE_PER_MONTH["Smartphone"] * rules.TERM_RATE_FACTOR[c.term_months], 2)
    assert len({a.customer_id for a in rented}) == 3, "spread over the customer accounts"
    assert _kpi(ans, "stock_turns")["after"] is not None


def test_returns_end_the_contracts_nearest_their_end_overdue_first(db_session):
    _prod, _due, stations = _fleet(db_session)
    ans = sim.fire(db_session, "returns", {"units": 3}, actor="test", today=TODAY)
    assert _moves(ans) == {("RENTED", "RETURNED"): 3}
    ended = db_session.query(RentalContract).filter(RentalContract.status == ContractStatus.ENDED).all()
    assert len(ended) == 3 and all(c.actual_end == TODAY and c.end_reason == "planned" and c.planned_end < TODAY for c in ended)
    back = [db_session.get(Asset, c.asset_id) for c in ended]
    assert all(a.status == AssetStatus.RETURNED and a.customer_id is None and a.current_location_id == stations["ST-RETURNS"].id for a in back)
    assert all(a.status_since == TODAY for a in back), "the dwell clock restarts in returns intake"
    ev = db_session.query(AssetEvent).filter(AssetEvent.asset_id == back[0].id, AssetEvent.to_status == AssetStatus.RETURNED).one()
    assert ev.actor == "test" and "ended" in ev.note
    assert _kpi(ans, "returns_overdue")["before"] == 10.0 and _kpi(ans, "returns_overdue")["after"] == 7.0
    assert _comp(ans, "ST-RETURNS")["delta"] == 3


def test_the_chain_intake_release_grading_repair_refurbishment(db_session):
    _fleet(db_session)
    assert _moves(sim.fire(db_session, "intake", {"units": 2}, actor="test", today=TODAY)) == {("RETURNED", "MDM_RELEASE"): 2}
    assert _moves(sim.fire(db_session, "mdm_release", {"units": 2}, actor="test", today=TODAY)) == {("MDM_RELEASE", "WIPE_GRADING"): 2}
    released = db_session.query(Asset).filter(Asset.serial_number.in_(["M-0", "M-1"])).all()
    assert all(a.status == AssetStatus.WIPE_GRADING for a in released), "the longest-waiting units leave the hold first"
    # grading: four units W-0..W-3 waited 4, 3, 2, 1 days; the mix gives B, A, C, B; W-3 is on its second rental
    ans = sim.fire(db_session, "grading", {"units": 4}, actor="test", today=TODAY)
    assert _moves(ans) == {("WIPE_GRADING", "REFURB"): 2, ("WIPE_GRADING", "REPAIR"): 1, ("WIPE_GRADING", "SELLABLE"): 1}
    by = {a.serial_number: a for a in db_session.query(Asset).filter(Asset.serial_number.in_(["W-0", "W-1", "W-2", "W-3"]))}
    assert (by["W-0"].grade, by["W-0"].status) == ("B", AssetStatus.REFURB)
    assert (by["W-1"].grade, by["W-1"].status) == ("A", AssetStatus.REFURB)
    assert (by["W-2"].grade, by["W-2"].status) == ("C", AssetStatus.REPAIR)
    assert (by["W-3"].grade, by["W-3"].status) == ("B", AssetStatus.SELLABLE), "after a second rental everything is cleared for sale"
    # repair: an invoice per device at the midpoint of the seed's range, then refurbishment
    ans = sim.fire(db_session, "repair_done", {"units": 2}, actor="test", today=TODAY)
    assert _moves(ans) == {("REPAIR", "REFURB"): 2}
    inv = db_session.query(ServiceEvent).filter(ServiceEvent.kind == ServiceKind.REPAIR).all()
    assert len(inv) == 2 and all(float(e.cost) == sum(rules.REPAIR_COST["Smartphone"]) / 2 and e.cycle_no == 2 and e.event_date == TODAY for e in inv)
    assert any("placeholder, Head of Service Operations" in n for n in ans["notes"])
    # refurbishment: the invoice, then the second-life stock
    ans = sim.fire(db_session, "refurb_done", {"units": 2}, actor="test", today=TODAY)
    assert _moves(ans) == {("REFURB", "READY_SECOND"): 2}
    inv = db_session.query(ServiceEvent).filter(ServiceEvent.kind == ServiceKind.REFURB).all()
    assert len(inv) == 2 and all(float(e.cost) == sum(rules.REFURB_COST["Smartphone"]) / 2 for e in inv)
    assert db_session.query(Asset).filter(Asset.serial_number == "F-0").one().status == AssetStatus.READY_SECOND


def test_second_rentals_sales_and_recycling(db_session):
    prod, _due, _st = _fleet(db_session)
    ans = sim.fire(db_session, "second_rentals", {"units": 2}, actor="test", today=TODAY)
    assert _moves(ans) == {("READY_SECOND", "RENTED"): 2}
    c = db_session.query(RentalContract).filter(RentalContract.cycle_no == 2, RentalContract.start_date == TODAY).all()
    assert len(c) == 2 and {x.term_months for x in c} <= set(rules.TERM_MIX_CYCLE2)
    assert all(float(x.rent_eur_month) == round(700 * rules.RENT_SHARE_PER_MONTH["Smartphone"] * rules.TERM_RATE_FACTOR[x.term_months] * rules.RENT2_SHARE, 2) for x in c)
    assert all(db_session.get(Asset, x.asset_id).cycle_no == 2 for x in c)
    # sales: the oldest sellable unit first, priced by the residual curve net of the channel fee
    ans = sim.fire(db_session, "sales", {"units": 1}, actor="test", today=TODAY)
    assert _moves(ans) == {("SELLABLE", "SOLD"): 1}
    sold = db_session.query(Asset).filter(Asset.serial_number == "SL-0").one()
    age = 400 / sim.DAYS_PER_MONTH
    expected = round(949 / (1 + rules.VAT) * rules._residual_share("Smartphone", age, "A") * (1 - rules.CHANNEL_FEE["marketplace"]), 2)
    assert sold.status == AssetStatus.SOLD and sold.sold_date == TODAY and sold.sale_channel == "marketplace" and float(sold.sale_price) == expected
    assert ans["proceeds_eur"] == expected and any("residual curve" in n for n in ans["notes"])
    # six sellable over fifty sales in 90 days before, five over fifty-one after: the reach shortens
    assert _kpi(ans, "sellable_reach_months")["before"] == round(6 / (50 / 3), 1) == 0.4
    assert _kpi(ans, "sellable_reach_months")["after"] == round(5 / (51 / 3), 1) == 0.3
    # recycling: the terminal exit, dated like a sale
    ans = sim.fire(db_session, "recycling", {"units": 1}, actor="test", today=TODAY)
    assert _moves(ans) == {("SELLABLE", "RECYCLED"): 1}
    rec = db_session.query(Asset).filter(Asset.serial_number == "SL-1").one()
    assert rec.status == AssetStatus.RECYCLED and rec.sold_date == TODAY and rec.decommissioned_date == TODAY and rec.sale_price is None


def test_a_sale_without_a_price_says_why_instead_of_inventing_one(db_session):
    _fleet(db_session)
    prod2 = _save(db_session, Product(product_code="NO-CAT", name="Unknown model", category="Tablet"))
    _unit(db_session, prod2, "X-1", AssetStatus.SELLABLE, 500, cycle_no=2, grade="B", received_date=None)
    ans = sim.fire(db_session, "sales", {"units": 1}, actor="test", today=TODAY)
    assert _moves(ans) == {("SELLABLE", "SOLD"): 1}
    x = db_session.query(Asset).filter(Asset.serial_number == "X-1").one()
    assert x.status == AssetStatus.SOLD and x.sale_price is None
    assert any("sold without a price" in n and "no launch price" in n for n in ans["notes"])


# ---------------------------------------------------------------------------
# refusals: the state machine, the stock, the cap


def test_a_forbidden_move_is_refused_with_the_machines_reason_and_nothing_is_touched(db_session):
    _fleet(db_session)
    before = db_session.query(AssetEvent).count()
    ans = sim.fire(db_session, "move", {"from_status": "RENTED", "to_status": "IN_STORAGE", "units": 5}, actor="test", today=TODAY)
    assert ans["moved"] == [] and ans["moved_total"] == 0
    assert ans["refused"] == [{"what": "move", "units": 5, "reason": "Illegal transition RENTED -> IN_STORAGE (allowed from RENTED: RETURNED)"}]
    assert db_session.query(AssetEvent).count() == before, "refused before a row was touched"
    assert all(c["delta"] == 0 for c in ans["compartments"]) and all(k["delta"] in (None, 0) for k in ans["kpis"])
    with pytest.raises(ValidationError):
        lifecycle.assert_transition(AssetStatus.RENTED, AssetStatus.IN_STORAGE)
    # a step that carries a fact is refused too: a free move cannot write a contract
    ans = sim.fire(db_session, "move", {"from_status": "READY_SECOND", "to_status": "RENTED", "units": 1}, actor="test", today=TODAY)
    assert ans["refused"][0]["units"] == 1 and "second rentals" in ans["refused"][0]["reason"]
    # a legal warehouse move goes through
    ans = sim.fire(db_session, "move", {"from_status": "READY_SECOND", "to_status": "SWAP_BUFFER", "units": 2}, actor="test", today=TODAY)
    assert _moves(ans) == {("READY_SECOND", "SWAP_BUFFER"): 2}
    with pytest.raises(ValidationError):
        sim.fire(db_session, "move", {"from_status": "NOWHERE", "to_status": "RENTED", "units": 1}, actor="test", today=TODAY)


def test_more_than_the_stock_is_partially_applied_and_the_rest_refused_with_the_count(db_session):
    _fleet(db_session)
    ans = sim.fire(db_session, "mdm_release", {"units": 50}, actor="test", today=TODAY)
    assert _moves(ans) == {("MDM_RELEASE", "WIPE_GRADING"): 8}
    assert ans["refused"] == [{"what": "MDM release", "reason": "only 8 in the MDM release hold", "units": 42}]
    assert _comp(ans, "ST-MDM")["on_hand_after"] == 0 and _comp(ans, "ST-WIPE")["on_hand_after"] == 12
    assert _kpi(ans, "mdm_release_over_sla_pct")["after"] is None and "nothing waiting" in _kpi(ans, "mdm_release_over_sla_pct")["after_reason"]


def test_a_batch_is_capped_and_the_datacenter_scenario_refuses(db_session):
    _fleet(db_session)
    with pytest.raises(ValidationError, match="never the fleet"):
        sim.fire(db_session, "sales", {"units": sim.MAX_UNITS + 1}, actor="test", today=TODAY)
    with pytest.raises(ValidationError):
        sim.fire(db_session, "hold", {"station": "ST-MDM", "days": sim.MAX_DAYS + 1, "factor": 0.0}, actor="test", today=TODAY)
    with pytest.raises(NotFoundError):
        sim.fire(db_session, "nope", {}, actor="test", today=TODAY)
    ans = sim.fire(db_session, "hold", {"station": "ST-SWAP", "days": 1, "factor": 0.0}, actor="test", today=TODAY)
    assert ans["moved"] == [] and "reserve" in ans["refused"][0]["reason"]


def test_the_datacenter_scenario_has_nothing_to_simulate(db_session):
    prod = _save(db_session, Product(product_code="SRV-1", name="Server 1"))
    _save(db_session, Asset(serial_number="DC-1", product_id=prod.id, status=AssetStatus.DEPLOYED, received_date=TODAY, deployed_date=TODAY))
    cat = sim.catalogue(db_session, today=TODAY)
    assert cat["scenario"] == "datacenter" and cat["rates"] is None and cat["reason"]
    with pytest.raises(ValidationError, match="datacenter"):
        sim.fire(db_session, "day", {}, actor="test", today=TODAY)


# ---------------------------------------------------------------------------
# a day of normal operation conserves the fleet


def test_a_day_of_normal_operation_conserves_the_fleet(db_session):
    from sqlalchemy import func

    _fleet(db_session)

    def counts():
        return {s: int(n) for s, n in db_session.query(Asset.status, func.count()).group_by(Asset.status).all()}

    before, total_before = counts(), db_session.query(Asset).count()
    r20 = db_session.query(Asset.id).filter(Asset.serial_number == "R-20").scalar()
    planned_end_before = db_session.query(RentalContract.planned_end).filter(RentalContract.asset_id == r20).scalar()
    ans = sim.fire(db_session, "day", {}, actor="test", today=TODAY)
    after, total_after = counts(), db_session.query(Asset).count()
    m = _moves(ans)
    received = m.get(("ORDERED", "RECEIVED"), 0)
    assert received == 50, "the line due today arrives in full"
    assert total_after == total_before + received, "nothing appears or disappears except what was delivered"
    for status in set(before) | set(after):
        inflow = sum(n for (f, t), n in m.items() if t == status.value)
        outflow = sum(n for (f, t), n in m.items() if f == status.value)
        assert after.get(status, 0) - before.get(status, 0) == inflow - outflow, status
    # the rotation at the measured rates: two returns, one first rental, the chain at two a day, two refurbishments, one sale
    assert m[("RENTED", "RETURNED")] == 2 and m[("IN_STORAGE", "RENTED")] == 1
    assert m[("RETURNED", "MDM_RELEASE")] == 2 and m[("MDM_RELEASE", "WIPE_GRADING")] == 2
    assert sum(n for (f, t), n in m.items() if f == "WIPE_GRADING") == 2
    assert m[("REFURB", "READY_SECOND")] == 2 and m[("SELLABLE", "SOLD")] == 1
    assert ans["rates"]["returns_per_day"] == 2 and ans["refused"] == []
    assert db_session.query(RentalContract).filter(RentalContract.status == ContractStatus.ENDED).count() == 2
    assert db_session.query(RentalContract).filter(RentalContract.start_date == TODAY).count() == 1
    assert ans["timing_ms"]["action"] >= 0 and ans["timing_ms"]["kpis"] >= 0
    assert all(k["after_measured_at"] is not None for k in ans["kpis"]), "every KPI was measured again now"
    # one day passed: every date moved back by one, the clock says so, and the trend gained a day
    assert ans["world"]["advanced_days"] == 1 and ans["world"]["days_advanced"] == 1 and ans["world"]["last_action"] == "day"
    assert db_session.query(RentalContract.planned_end).filter(RentalContract.asset_id == r20).scalar() == planned_end_before - timedelta(days=1)
    assert [h["as_of"] for h in kpis.history(db_session, "returns_overdue")][-2:] == [TODAY - timedelta(days=1), TODAY]
    assert any("The world moved 1 day" in n for n in ans["notes"])
    # every KPI is in the answer; the event re-measured what an event can move and kept the rest, saying which world-day
    # they were measured on: the day before, now one simulated day ago, and the backtest was not paid for again
    assert len(ans["kpis"]) == len(kpis.KPIS) and set(ans["kpis_measured"]) == set(sim.SIM_KPIS)
    kept = {k["id"]: k for k in ans["kpis_kept"]}
    assert set(kept) == {k.id for k in kpis.KPIS} - set(sim.SIM_KPIS) and "forecast_mape_pct" in kept
    assert kept["forecast_mape_pct"]["measured_on"] == TODAY - timedelta(days=1) and kept["forecast_mape_pct"]["stale_days"] == 1
    fm = _kpi(ans, "forecast_mape_pct")
    assert fm["after_measured_at"] == fm["before_measured_at"] and fm["after_stale_days"] == 1 and fm["delta"] in (None, 0)
    assert _kpi(ans, "returns_overdue")["after_stale_days"] == 0 and "backtest" in ans["kpis_kept_reason"]


# ---------------------------------------------------------------------------
# the bottleneck: the acceptance test


def test_a_hold_on_the_mdm_station_builds_the_queue_and_the_plan_breaks_today(db_session):
    """Hold the MDM station for five days: five days pass, the queue grows by five days of arrivals staggered over
    those days, the stock already waiting ages past the 21-day service level, the MDM-over-SLA KPI rises, the
    compartment crosses its capacity, and the capacity plan's breach comes forward to today."""
    _fleet(db_session)
    ans = sim.fire(db_session, "hold", {"station": "ST-MDM", "days": 5, "factor": 0.0}, actor="test", today=TODAY)
    m = _moves(ans)
    assert m[("RETURNED", "MDM_RELEASE")] == 10 and ("MDM_RELEASE", "WIPE_GRADING") not in m, "arrivals kept coming, nothing left"
    assert (ans["world"]["advanced_days"], ans["world"]["days_advanced"], ans["world"]["last_action"], ans["world"]["last_days"]) == (5, 5, "hold", 5)
    mdm = _comp(ans, "ST-MDM")
    assert (mdm["on_hand_before"], mdm["on_hand_after"]) == (8, 18) and mdm["capacity"] == 17
    assert not mdm["over_capacity_before"] and mdm["over_capacity_after"]
    assert (mdm["verdict_before"], mdm["verdict_after"]) == ("healthy", "over_capacity")
    assert (mdm["breach_before"], mdm["breach_month_before"]) == ("later", "2026-10"), "the model broke it next month"
    assert mdm["breach_after"] == "over_today"
    fb0, fb1 = ans["plan"]["first_breach_before"], ans["plan"]["first_breach_after"]
    assert fb0["code"] == "ST-MDM" and fb0["date"] == date(2026, 10, 31) and fb1["code"] == "ST-MDM" and fb1["date"] == TODAY, "the breach came forward"
    # the waiting stock aged: 30, 40, 18, 19, 20, 2, 1, 1 days became 35, 45, 23, 24, 25, 7, 6, 6, so five are past the
    # 21-day target instead of two, and the ten arrivals (0 to 4 days old) join them: 5 of 18 against 2 of 8 before
    assert (mdm["past_target_before"], mdm["past_target_after"]) == (2, 5), "the past-target count grew"
    k = _kpi(ans, "mdm_release_over_sla_pct")
    assert (k["before"], k["after"]) == (25.0, 27.8) and k["better"] is False, "the KPI rose, the way an operator expects"
    h = ans["hold"]
    assert h["outflow_per_day"] == 2 and h["arrived"] == 10 and h["held_back"] == 10 and h["held_back_past_target"] == 5
    assert h["crossed_target"] == 3 and (h["past_target_before"], h["past_target_after"]) == (2, 5)
    assert any("5 days passed and the station did not drain" in n and "3 of the units already waiting crossed the target" in n for n in ans["notes"])
    assert any("The world moved 5 days" in n and "stands 5 days later" in n for n in ans["notes"])
    # the ten units booked in (the five older seed returns first, then five of the new ones) are staggered over the
    # five days, two a day, not all stamped on one day
    since = sorted(a.status_since for a in db_session.query(Asset).filter(Asset.status == AssetStatus.MDM_RELEASE, ~Asset.serial_number.like("M-%")))
    assert since == sorted([TODAY - timedelta(days=d) for d in (4, 4, 3, 3, 2, 2, 1, 1, 0, 0)])
    # downstream starves: wipe and grading got nothing from the hold and kept clearing
    assert _comp(ans, "ST-WIPE")["on_hand_after"] == 0
    # every date moved with the world: the owner's milestone, the contracts
    assert db_session.query(FleetMilestone).one().milestone_date == TODAY + timedelta(days=360)
    assert db_session.query(RentalContract).filter(RentalContract.status == ContractStatus.RUNNING).first().planned_end == TODAY + timedelta(days=16)
    # the warehouse, the plan and the KPI read agree with the answer
    W = warehouse.compartments(db_session, today=TODAY)
    assert next(c for c in W["compartments"] if c["code"] == "ST-MDM")["on_hand"] == 18
    assert capacity_plan.plan(db_session, today=TODAY)["first_breach"]["date"] == TODAY
    assert {r["id"]: r for r in kpis.compute_all(db_session, today=TODAY)}["mdm_release_over_sla_pct"]["current"] == 27.8


def test_a_throttled_station_keeps_half_its_flow(db_session):
    _fleet(db_session)
    ans = sim.fire(db_session, "hold", {"station": "ST-MDM", "days": 2, "factor": 0.5}, actor="test", today=TODAY)
    m = _moves(ans)
    assert m[("RETURNED", "MDM_RELEASE")] == 4 and m[("MDM_RELEASE", "WIPE_GRADING")] == 2
    assert ans["hold"]["held_back"] == 2 and _comp(ans, "ST-MDM")["delta"] == 2 and ans["world"]["days_advanced"] == 2


# ---------------------------------------------------------------------------
# time passes: the world moves back by N days


def test_advance_moves_every_date_together_and_leaves_the_audit_trail(db_session):
    """Every Date column of every table moves back by N, one UPDATE per table; the wall-clock stamps stay; a table
    whose date sits under a unique key (the snapshots, the milestones) moves without tripping it."""
    _fleet(db_session)
    kpis.compute_all(db_session, today=TODAY - timedelta(days=1))     # two days of snapshots: a shift of one lands on the other
    kpis.compute_all(db_session, today=TODAY)
    capacity_plan.set_milestone(db_session, TODAY + timedelta(days=366), target_fleet=130, owner="Business owner", note=None, actor="test", today=TODAY)
    a = db_session.query(Asset).filter(Asset.serial_number == "M-0").one()
    line = db_session.query(OrderItem).filter(OrderItem.estimated_delivery_date.is_not(None)).order_by(OrderItem.estimated_delivery_date).first()
    created, snap_updated = a.date_created, db_session.query(KpiSnapshot).first().last_updated
    since, received, eta, line_id = a.status_since, a.received_date, line.estimated_delivery_date, line.id
    walked = {t.name: ([c.name for c in cols], keyed) for t, cols, keyed in timeshift.date_columns()}
    assert set(walked["asset"][0]) == {"received_date", "deployed_date", "warranty_end_date", "decommissioned_date", "status_since", "sold_date"}
    assert set(walked["rental_contract"][0]) == {"start_date", "planned_end", "actual_end"} and walked["service_event"][0] == ["event_date"]
    assert walked["kpi_snapshot"] == (["as_of"], True) and walked["fleet_milestone"] == (["milestone_date"], True)
    assert "date_created" not in walked["asset"][0] and "app_user" not in walked and "asset_event" not in walked, "wall-clock stamps stay"

    world = timeshift.advance(db_session, 1, action="test")
    assert world["days_advanced"] == 1 and world["last_action"] == "test" and world["last_days"] == 1 and world["advanced_at"] is not None
    a = db_session.query(Asset).filter(Asset.serial_number == "M-0").one()
    assert a.status_since == since - timedelta(days=1) and a.received_date == received - timedelta(days=1)
    assert a.date_created == created, "when the row was written did not move"
    assert db_session.get(OrderItem, line_id).estimated_delivery_date == eta - timedelta(days=1)
    days = sorted({s.as_of for s in db_session.query(KpiSnapshot).filter(KpiSnapshot.kpi_id == "returns_overdue")})
    assert days == [TODAY - timedelta(days=2), TODAY - timedelta(days=1)], "both snapshot days moved, no collision"
    assert db_session.query(KpiSnapshot).first().last_updated == snap_updated, "the real time of a measurement stays"
    assert sorted(m.milestone_date for m in db_session.query(FleetMilestone)) == [TODAY + timedelta(days=364), TODAY + timedelta(days=365)]
    timeshift.advance(db_session, 3, action="test")
    assert timeshift.state(db_session)["days_advanced"] == 4 and db_session.query(WorldClock).count() == 2, "one row per move"
    assert [d for _, d, _ in timeshift.moves(db_session)] == [1, 3]
    assert db_session.query(Asset).filter(Asset.serial_number == "M-0").one().status_since == since - timedelta(days=4)


def test_advance_refuses_what_it_must(db_session, monkeypatch):
    _fleet(db_session)
    with pytest.raises(ValueError):
        timeshift.advance(db_session, 0, action="test")
    with pytest.raises(ValueError):
        timeshift.advance(db_session, timeshift.MAX_DAYS + 1, action="test")
    monkeypatch.setattr("app.core.safety.is_production", lambda: True)
    since = db_session.query(Asset).filter(Asset.serial_number == "M-0").one().status_since
    with pytest.raises(ProductionSafetyError):
        timeshift.advance(db_session, 1, action="test")
    assert db_session.query(Asset).filter(Asset.serial_number == "M-0").one().status_since == since
    assert timeshift.state(db_session)["days_advanced"] == 0


def test_a_measurement_taken_before_the_world_moved_still_stands_and_says_how_old_it_is(db_session, monkeypatch):
    """The once-a-day rule under a moving calendar: a measurement taken before the world moved by N days is still the
    current one (nothing real happened since), described as measured N simulated days ago; a real day passing, or
    Measure again, takes a new one. No wall-clock comparison: the calendar's own log decides."""
    _fleet(db_session)
    calls = {"n": 0}
    probe_def = next(k for k in kpis.KPIS if k.id == "forecast_mape_pct")

    def counted(db, today, _f=probe_def.compute):
        calls["n"] += 1
        return _f(db, today)

    monkeypatch.setattr(kpis, "KPIS", [replace(k, compute=counted) if k is probe_def else k for k in kpis.KPIS])
    first = {r["id"]: r for r in kpis.compute_all(db_session, today=TODAY)}
    assert calls["n"] == 1 and first["forecast_mape_pct"]["stale_days"] == 0 and first["forecast_mape_pct"]["measured_on"] == TODAY
    timeshift.advance(db_session, 3, action="test")
    again = {r["id"]: r for r in kpis.compute_all(db_session, today=TODAY)}
    assert calls["n"] == 1, "the measurement from before the move still stands"
    row = again["forecast_mape_pct"]
    assert row["measured_on"] == TODAY - timedelta(days=3) and row["stale_days"] == 3 and row["measured_at"] == first["forecast_mape_pct"]["measured_at"]
    assert len(kpis._current_snapshots(db_session, TODAY)) == len(kpis.KPIS), "the boot would not measure again either"
    # a fleet event re-measures the fleet subset only and reports the kept ones as three simulated days old
    ans = sim.fire(db_session, "intake", {"units": 1}, actor="test", today=TODAY)
    assert calls["n"] == 1 and _kpi(ans, "forecast_mape_pct")["after_stale_days"] == 3 and _kpi(ans, "aging_stock_pct")["after_stale_days"] == 0
    # a real day passes: the gap outgrows what the calendar moved since the measurement, so it is measured anew
    later = {r["id"]: r for r in kpis.compute_all(db_session, today=TODAY + timedelta(days=1))}
    assert calls["n"] == 2 and later["forecast_mape_pct"]["stale_days"] == 0
    # Measure again measures now, whatever stands
    kpis.compute_all(db_session, today=TODAY + timedelta(days=1), refresh=True)
    assert calls["n"] == 3


def test_a_rebuild_puts_the_clock_back_to_zero(db_session):
    _fleet(db_session)
    timeshift.advance(db_session, 7, action="test")
    removed = seed_reset.reset_operational_data(db_session)
    assert removed["world_clock"] == 1 and timeshift.state(db_session)["days_advanced"] == 0


# ---------------------------------------------------------------------------
# the KPI measurement after an action


def test_the_refresh_measures_only_the_kpis_a_fleet_event_can_move(db_session, monkeypatch):
    _fleet(db_session)
    calls = {"in": 0, "out": 0}
    inside = next(k for k in kpis.KPIS if k.id == "aging_stock_pct")
    outside = next(k for k in kpis.KPIS if k.id == "forecast_mape_pct")

    def count(k, key):
        def wrapped(db, today, _f=k.compute):
            calls[key] += 1
            return _f(db, today)
        return replace(k, compute=wrapped)

    monkeypatch.setattr(kpis, "KPIS", [count(k, "in") if k is inside else count(k, "out") if k is outside else k for k in kpis.KPIS])
    kpis.compute_all(db_session, today=TODAY)                                 # the day's measurement
    assert calls == {"in": 1, "out": 1}
    rows = kpis.compute_all(db_session, today=TODAY, refresh=True, only={"aging_stock_pct"})
    assert calls == {"in": 2, "out": 1}, "the backtest is not paid for again"
    by = {r["id"]: r for r in rows}
    assert by["aging_stock_pct"]["measured_at"] is not None and by["forecast_mape_pct"]["measured_at"] is not None
    kpis.compute_all(db_session, today=TODAY, refresh=True)
    assert calls == {"in": 3, "out": 2}, "a plain refresh still measures everything"


# ---------------------------------------------------------------------------
# the gates: production refuses, the guest cannot fire, the API answers


def test_production_refuses_every_simulated_write(db_session, client, monkeypatch):
    _fleet(db_session)
    monkeypatch.setattr("app.core.safety.is_production", lambda: True)
    with pytest.raises(ProductionSafetyError):
        sim.fire(db_session, "sales", {"units": 1}, actor="test", today=TODAY)
    with pytest.raises(ProductionSafetyError):
        seed_reset.rebuild_in_background()
    assert db_session.query(Asset).filter(Asset.status == AssetStatus.SOLD).count() == 50, "nothing was sold"
    r = client.post("/api/v1/simulation/actions/sales", json={"units": 1})
    assert r.status_code == 403 and "SCM_ENV=prod" in r.json()["detail"]
    assert client.post("/api/v1/simulation/reset").status_code == 403
    assert client.get("/api/v1/simulation/actions").status_code == 200, "reading the catalogue is still allowed"


def test_the_role_gate_holds(client, db_session, monkeypatch):
    _fleet(db_session)
    monkeypatch.setattr(seed_reset, "rebuild_in_background", lambda: {"running": True, "started_at": None, "finished_at": None, "ok": None, "detail": None})
    anon, viewer, whse = client.anon(), client.as_role(Role.VIEWER), client.as_role(Role.WAREHOUSE)
    assert anon.post("/api/v1/simulation/actions/sales", json={"units": 1}).status_code == 401
    assert anon.get("/api/v1/simulation/actions").status_code == 401
    assert viewer.get("/api/v1/simulation/actions").status_code == 200, "the guest may look"
    assert viewer.post("/api/v1/simulation/actions/sales", json={"units": 1}).status_code == 403, "the guest cannot fire"
    assert viewer.post("/api/v1/simulation/reset").status_code == 403
    st = viewer.get("/api/v1/simulation/status").json()
    assert st["can_fire"] is False and st["can_reset"] is False and st["writes_allowed"] is True
    r = whse.post("/api/v1/simulation/actions/mdm_release", json={"units": 2})
    assert r.status_code == 200 and r.json()["moved_total"] == 2, "warehouse operations may fire"
    assert whse.post("/api/v1/simulation/reset").status_code == 403, "only an admin rebuilds"
    assert whse.get("/api/v1/simulation/status").json()["can_fire"] is True
    r = client.post("/api/v1/simulation/reset")
    assert r.status_code == 202 and r.json()["running"] is True
    assert client.get("/api/v1/simulation/status").json()["can_reset"] is True


def test_the_api_answers_with_what_changed(client, db_session):
    _fleet(db_session)
    cat = client.get("/api/v1/simulation/actions").json()
    assert cat["scenario"] == "daas" and len(cat["actions"]) == 14
    r = client.post("/api/v1/simulation/actions/intake", json={"units": 2})
    assert r.status_code == 200
    body = r.json()
    assert body["action"] == "intake" and body["moved"] == [{"from_status": "RETURNED", "to_status": "MDM_RELEASE", "from_name": "Returns intake",
                                                             "to_name": "MDM release hold", "units": 2}]
    assert body["actor"] == "simulation (admin@example.com)" and body["requested"] == {"units": 2}
    # every KPI is in the answer; the ones a fleet event can move were measured again, the rest kept the day's measurement
    assert {k["id"] for k in body["kpis"]} == {k.id for k in kpis.KPIS}
    by = {k["id"]: k for k in body["kpis"]}
    assert by["mdm_release_over_sla_pct"]["after_measured_at"] > by["mdm_release_over_sla_pct"]["before_measured_at"]
    assert by["spend_under_contract_pct"]["after_measured_at"] == by["spend_under_contract_pct"]["before_measured_at"]
    assert by["spend_under_contract_pct"]["delta"] in (None, 0) and body["world"]["advanced_days"] == 0
    assert [c["code"] for c in body["compartments"]] == [c.code for c in warehouse.COMPARTMENTS]
    assert set(body["timing_ms"]) == {"reads_before", "action", "reads_after", "kpis"}
    ev = db_session.query(AssetEvent).filter(AssetEvent.actor == "simulation (admin@example.com)").count()
    assert ev == 2, "every move is on the asset's event log"
    assert client.post("/api/v1/simulation/actions/sales", json={"units": 0}).status_code == 422
    assert client.post("/api/v1/simulation/actions/nope", json={}).status_code == 404
    fresh = client.get("/api/v1/kpis").json()
    assert all(row["measured_at"] for row in fresh), "the KPIs tab says when each figure was measured"
