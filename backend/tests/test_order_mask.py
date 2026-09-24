"""The ordering mask: what is needed and why, what we already own that could serve it, what is
coming in, and the consequence of a quantity before anything is ordered.

A small fleet built with the ORM, every compartment populated, so each figure can be worked out
by hand: the tiers read off the state machine, the horizons of the return chain, the factors
that make the recommendation and their sum, the guard reused as it is, the intake compartment
now and on the delivery date, the price and its verdict, what a quantity covers, and what would
make room when there is none.
"""
from __future__ import annotations

import math
from datetime import date, timedelta
from decimal import Decimal

import pytest

from app.core.config import settings
from app.models.catalog import Organization, Product, ProductSupplier
from app.models.flow import DEPLOYABLE_STATUSES, Asset, AssetStatus, Location, LocationType
from app.models.procurement import OrderItem, OrderStatus, PurchaseOrder
from app.models.rental import ContractStatus, RentalContract
from app.services import lifecycle, order_mask, planning, warehouse
from app.services.exceptions import NotFoundError, ValidationError

TODAY = date(2026, 9, 24)
CAPACITY = {"ST-NEW": 100, "ST-RETURNS": 50, "ST-MDM": 50, "ST-WIPE": 50, "ST-REPAIR": 50, "ST-REFURB": 50, "ST-SECOND": 50, "ST-SELL": 50, "ST-SWAP": 50}
DPM = 30.4375


def _save(db, obj):
    db.add(obj)
    db.flush()
    return obj


def _unit(db, prod, serial, status, days, station=None, **kw):
    row = dict(serial_number=serial, product_id=prod.id, status=status, cycle_no=kw.pop("cycle_no", 1),
               received_date=TODAY - timedelta(days=400), current_location_id=(station.id if station else None),
               status_since=(TODAY - timedelta(days=days)) if days is not None else None)
    row.update(kw)
    return _save(db, Asset(**row))


def _fleet(db):
    """Phone 1 (Maker A): eight devices at customers past their useful life, three rented a month ago, one
    defect return; 2 new, 1 second-life, 2 sellable, 1 swap, and a return chain of 2 + 1 + 1 + 1. Laptop 1
    (Maker B): 5 new and 20 on order due in ten days. Every station has a capacity; 500 places in all."""
    maker_a = _save(db, Organization(code="MAKER-A", name="Maker A", is_supplier=True, is_manufacturer=True))
    maker_b = _save(db, Organization(code="MAKER-B", name="Maker B", is_supplier=True, is_manufacturer=True))
    sup = _save(db, Organization(code="SUP-T", name="Supplier T (role-only)", is_supplier=True))
    cust = _save(db, Organization(code="CUST-T", name="Customer T (role-only)", is_supplier=False))
    phone = _save(db, Product(product_code="PH-1", name="Phone 1", category="Smartphone"))
    laptop = _save(db, Product(product_code="LT-1", name="Laptop 1", category="Laptop"))
    _save(db, ProductSupplier(product_id=phone.id, supplier_id=sup.id, manufacturer_id=maker_a.id, contract_price=Decimal("500.00"),
                              standard_lead_time_days=21, min_order_quantity=50, preference_rank=1))
    _save(db, ProductSupplier(product_id=laptop.id, supplier_id=sup.id, manufacturer_id=maker_b.id, contract_price=Decimal("1000.00"),
                              standard_lead_time_days=14, min_order_quantity=10, preference_rank=1))
    st = {c.code: _save(db, Location(code=c.code, name=c.name, location_type=LocationType.WAREHOUSE, capacity=CAPACITY[c.code]))
          for c in warehouse.COMPARTMENTS}
    # at customers: eight past the useful life (end-of-life replacements), three rented a month ago (usage)
    for i in range(8):
        a = _unit(db, phone, f"E-{i}", AssetStatus.RENTED, 1100, customer_id=cust.id, deployed_date=TODAY - timedelta(days=1100))
        db.add(RentalContract(asset_id=a.id, product_id=phone.id, customer_id=cust.id, cycle_no=1, start_date=TODAY - timedelta(days=1100),
                              term_months=48, planned_end=TODAY + timedelta(days=300), status=ContractStatus.RUNNING))
    for i in range(3):
        a = _unit(db, phone, f"R-{i}", AssetStatus.RENTED, 30, customer_id=cust.id, deployed_date=TODAY - timedelta(days=30))
        db.add(RentalContract(asset_id=a.id, product_id=phone.id, customer_id=cust.id, cycle_no=1, start_date=TODAY - timedelta(days=30),
                              term_months=24, planned_end=TODAY + timedelta(days=700), status=ContractStatus.RUNNING))
    # one contract ended with a defect ten days ago: the swap buffer's measured defect rate
    gone = _unit(db, phone, "D-1", AssetStatus.SWAP_BUFFER, 10, st["ST-SWAP"], grade="A")
    db.add(RentalContract(asset_id=gone.id, product_id=phone.id, customer_id=cust.id, cycle_no=1, start_date=TODAY - timedelta(days=200),
                          term_months=24, planned_end=TODAY + timedelta(days=500), actual_end=TODAY - timedelta(days=10), end_reason="defect",
                          status=ContractStatus.ENDED))
    # the warehouse, Phone 1
    _unit(db, phone, "N-1", AssetStatus.IN_STORAGE, 5, st["ST-NEW"], cycle_no=0, grade="A")
    _unit(db, phone, "N-2", AssetStatus.RECEIVED, 1, st["ST-NEW"], cycle_no=0)
    _unit(db, phone, "S-1", AssetStatus.READY_SECOND, 20, st["ST-SECOND"], grade="A")
    _unit(db, phone, "SL-1", AssetStatus.SELLABLE, 70, st["ST-SELL"], cycle_no=2, grade="C")
    _unit(db, phone, "SL-2", AssetStatus.SELLABLE, 90, st["ST-SELL"], cycle_no=2, grade="D")
    _unit(db, phone, "RT-1", AssetStatus.RETURNED, 12, st["ST-RETURNS"])
    _unit(db, phone, "RT-2", AssetStatus.RETURNED, 3, st["ST-RETURNS"])
    _unit(db, phone, "M-1", AssetStatus.MDM_RELEASE, 30, st["ST-MDM"], cycle_no=2)
    _unit(db, phone, "RP-1", AssetStatus.REPAIR, 25, st["ST-REPAIR"], grade="C")
    _unit(db, phone, "F-1", AssetStatus.REFURB, 5, st["ST-REFURB"], grade="B")
    # two sold this year at 100 and 300: the forgone proceeds of a sellable unit
    _unit(db, phone, "SOLD-1", AssetStatus.SOLD, 40, cycle_no=2, grade="C", sold_date=TODAY - timedelta(days=40), sale_price=Decimal("100.00"))
    _unit(db, phone, "SOLD-2", AssetStatus.SOLD, 80, cycle_no=2, grade="B", sold_date=TODAY - timedelta(days=80), sale_price=Decimal("300.00"))
    # Laptop 1: five new, twenty on order into new stock
    for i in range(5):
        _unit(db, laptop, f"L-{i}", AssetStatus.IN_STORAGE, 9, st["ST-NEW"], cycle_no=0, grade="A")
    po = _save(db, PurchaseOrder(order_number="PO-OPEN", supplier_id=sup.id, status=OrderStatus.PLACED, destination_id=st["ST-NEW"].id,
                                 date_ordered=TODAY - timedelta(days=5)))
    _save(db, OrderItem(order_id=po.id, product_id=laptop.id, quantity=20, unit_price=Decimal("1000.00"),
                        estimated_delivery_date=TODAY + timedelta(days=10)))
    db.flush()
    return phone, laptop, st


def _tier(m, code):
    return next(t for t in m["owned"]["tiers"] if t["code"] == code)


def _chain(m, code):
    return next(c for c in m["owned"]["return_chain"]["compartments"] if c["code"] == code)


# ---------------------------------------------------------------------------
# the tiers are the state machine's, not a list


def test_the_rentable_tiers_and_the_return_chain_are_read_off_the_state_machine():
    rentable = [c.code for c in warehouse.COMPARTMENTS if lifecycle.can_transition(c.statuses[0], AssetStatus.RENTED)]
    assert list(order_mask._RENTABLE_CODES) == rentable == ["ST-NEW", "ST-SECOND", "ST-SELL", "ST-SWAP"]
    assert list(order_mask._COUNTED_CODES) == ["ST-NEW", "ST-SECOND"]
    assert set(order_mask._COUNTED_CODES) == {warehouse.STATION_OF_STATUS[s].code for s in DEPLOYABLE_STATUSES}
    assert list(order_mask._CHAIN_CODES) == ["ST-RETURNS", "ST-MDM", "ST-WIPE", "ST-REPAIR", "ST-REFURB"]
    # target dwells ahead before second-life stock: the shortest and the longest path the machine allows
    assert order_mask._horizons(AssetStatus.RETURNED) == (10 + 3 + 12, 10 + 21 + 3 + 20 + 12)
    assert order_mask._horizons(AssetStatus.MDM_RELEASE) == (21 + 3 + 12, 21 + 3 + 20 + 12)
    assert order_mask._horizons(AssetStatus.REPAIR) == (20 + 12, 20 + 12)
    assert order_mask._horizons(AssetStatus.REFURB) == (12, 12)


def test_scopes_list_models_manufacturers_and_classes(db_session):
    _fleet(db_session)
    s = order_mask.scopes(db_session)
    assert s["scenario"] == "daas"
    assert [(p["code"], p["family"], p["manufacturer"]) for p in s["products"]] == [("LT-1", "Laptop", "Maker B"), ("PH-1", "Smartphone", "Maker A")]
    assert s["manufacturers"] == ["Maker A", "Maker B"] and s["families"] == ["Laptop", "Smartphone"]


def test_a_scope_resolves_by_model_manufacturer_or_class(db_session):
    _fleet(db_session)
    assert [p["code"] for p in order_mask.mask(db_session, product_code="ph-1", today=TODAY)["scope"]["products"]] == ["PH-1"]
    assert [p["code"] for p in order_mask.mask(db_session, manufacturer="maker a", today=TODAY)["scope"]["products"]] == ["PH-1"]
    m = order_mask.mask(db_session, family="Laptop", today=TODAY)
    assert [p["code"] for p in m["scope"]["products"]] == ["LT-1"] and m["scope"]["label"] == "all laptops"
    assert order_mask.mask(db_session, manufacturer="Maker B", family="Laptop", today=TODAY)["scope"]["label"] == "Maker B laptops"
    assert len(order_mask.mask(db_session, today=TODAY)["scope"]["products"]) == 2
    with pytest.raises(NotFoundError):
        order_mask.mask(db_session, product_code="NOPE", today=TODAY)
    with pytest.raises(NotFoundError):
        order_mask.mask(db_session, manufacturer="Maker A", family="Laptop", today=TODAY)
    with pytest.raises(ValidationError):
        order_mask.mask(db_session, product_code="PH-1", quantity=-1, today=TODAY)


# ---------------------------------------------------------------------------
# what we already own, and what taking it costs


def test_owned_tiers_count_the_scope_and_say_what_taking_them_costs(db_session):
    phone, _laptop, _st = _fleet(db_session)
    m = order_mask.mask(db_session, product_code="PH-1", today=TODAY)
    o = m["owned"]
    assert [t["code"] for t in o["tiers"]] == ["ST-NEW", "ST-SECOND", "ST-SELL", "ST-SWAP"]
    assert o["rentable_total"] == 2 + 1 + 2 + 1 and o["counted_total"] == 3 and o["not_counted_total"] == 3
    new = _tier(m, "ST-NEW")
    assert new["units"] == 2 and new["counted"] and new["median_days"] == 1.0 and new["oldest_days"] == 5 and new["cost"]["kind"] == "none"
    assert new["cycles"] == [{"cycle": "0", "label": "New, never rented", "units": 2}]
    second = _tier(m, "ST-SECOND")
    assert second["units"] == 1 and second["counted"] and second["cost"]["kind"] == "rent_share" and second["cost"]["value"] == 0.7
    assert "placeholder" in second["cost"]["basis"] and "Head of Sales" in second["cost"]["basis"]
    sell = _tier(m, "ST-SELL")
    assert sell["units"] == 2 and not sell["counted"] and sell["cost"]["kind"] == "forgone_proceeds"
    assert sell["cost"]["value"] == 200.0 and sell["cost"]["sold"] == 2 and sell["cost"]["forgone_total"] == 400.0   # (100 + 300) / 2 per unit, two units
    assert sell["cost"]["measured"] is True and sell["cost"]["grade_ab_share"] == 0.0
    swap = _tier(m, "ST-SWAP")
    assert swap["units"] == 1 and not swap["counted"] and swap["cost"]["kind"] == "defect_cover"
    per_month = 1 / (90 / DPM)                                          # one defect return in 90 days
    assert swap["cost"]["defects_per_month"] == round(per_month, 2) and swap["cost"]["value"] == round(1 / per_month, 1) == 3.0
    assert swap["cost"]["rented"] == 11 and swap["cost"]["per_rented"] == round(1 / 11, 4)
    assert m["recommendation"]["not_counted"] == {"ST-SELL": 2, "ST-SWAP": 1}


def test_the_return_chain_comes_with_its_rule_and_its_horizon(db_session):
    _fleet(db_session)
    rc = order_mask.mask(db_session, product_code="PH-1", today=TODAY)["owned"]["return_chain"]
    assert rc["units"] == 5 and rc["reason"] is None
    assert [(c["code"], c["units"]) for c in rc["compartments"]] == [("ST-RETURNS", 2), ("ST-MDM", 1), ("ST-WIPE", 0), ("ST-REPAIR", 1), ("ST-REFURB", 1)]
    rt = _chain(rc and {"owned": {"return_chain": rc}}, "ST-RETURNS")
    # after a first rental 72 % go to a second rental and 19 % to repair first: 2 x 0.91 rounds to 2; a device after a second rental is sold
    assert rt["expected_second_life"] == 2 and rt["horizon_days_min"] == 25 and rt["horizon_days_max"] == 66 and rt["median_days"] == 3.0
    assert _chain({"owned": {"return_chain": rc}}, "ST-MDM")["expected_second_life"] == 0 and _chain({"owned": {"return_chain": rc}}, "ST-MDM")["expected_sale"] == 1
    assert _chain({"owned": {"return_chain": rc}}, "ST-REPAIR")["expected_second_life"] == 1      # already on its way: counted whole
    assert rc["expected_second_life"] == round(2 * 0.91 + 0 + 1 + 1) == 4 and rc["expected_sale"] == round(2 * 0.05 + 0.96) == 1
    assert "NEXT_STEP_SHARE" in rc["rule_basis"] and "placeholder" in rc["horizon_basis"]


# ---------------------------------------------------------------------------
# the recommendation: factors a person can add up


def test_the_factors_add_up_to_the_gap_and_the_moq_makes_the_recommendation(db_session):
    phone, _laptop, _st = _fleet(db_session)
    m = order_mask.mask(db_session, product_code="PH-1", today=TODAY)
    r, d = m["recommendation"], m["demand"]
    fc = next(x for x in planning.demand_forecast(db_session, today=TODAY) if x["product_id"] == phone.id)
    assert d["eol"] == 8 and d["usage"] == round(fc["projected_usage"], 1) and d["gross"] == math.ceil(fc["projected_demand"])
    assert d["lead_time_days"] == 21 and d["horizon_days"] == settings.demand_horizon_days
    by = {f["key"]: f for f in r["factors"]}
    assert [f["key"] for f in r["factors"]] == ["usage", "eol", "buffer", "new", "second_life", "inbound", "staged"]
    assert by["new"]["value"] == 2 and by["second_life"]["value"] == 1 and by["inbound"]["value"] == 0 and by["staged"]["value"] == 0
    assert by["buffer"]["value"] == r["buffer"] and all(f["basis"] for f in r["factors"])
    signed = sum((f["value"] if f["sign"] == "+" else -f["value"]) for f in r["factors"])
    assert r["gap"] == max(0, math.ceil(signed)) and r["gap"] == d["gross"] + r["buffer"] - 3
    assert r["gap"] > 0 and r["recommended"] == math.ceil(r["gap"] / 50) * 50 == 50, "rounded up to the minimum order quantity of 50"
    assert r["position_model_net"] == max(0, d["gross"] - 3 - r["buffer"]) and "inventory_position" in r["position_model_basis"]
    assert r["forecast_recommended"] == fc["recommended_order_qty"]
    # the guard was asked about the recommendation itself, and it fits: 500 places, 16 used, 20 inbound
    assert r["guard_for"] == "recommendation" and r["guard"] == planning.check_order_capacity(db_session, 50, today=TODAY)
    assert r["guard"]["verdict"] == "ok" and r["guard"]["free_to_order"] == 500 - 16 - 20 == 464
    assert r["orderable_now"] == 50 and r["deferred"] == 0
    assert m["what_if"] is None and m["inbound"]["units"] == 0 and "no open order line" in m["inbound"]["reason"]
    row = r["products"][0]
    assert row["code"] == "PH-1" and row["moq"] == 50 and row["unit_price"] == 500.0 and row["recommended"] == 50


def test_a_scope_with_inbound_and_no_gap(db_session):
    _fleet(db_session)
    m = order_mask.mask(db_session, product_code="LT-1", today=TODAY)
    r = m["recommendation"]
    assert r["new"] == 5 and r["inbound"] == 20 and r["gap"] == 0 and r["recommended"] == 0 and m["demand"]["gross"] == 0
    assert m["demand"]["reason"] and m["inbound"]["units"] == 20 and m["inbound"]["next_eta"] == TODAY + timedelta(days=10)
    assert [(ln["order_number"], ln["outstanding"], ln["late"], ln["days_to_eta"]) for ln in m["inbound"]["lines"]] == [("PO-OPEN", 20, False, 10)]


# ---------------------------------------------------------------------------
# the what-if


def test_what_if_reuses_the_guard_and_checks_the_intake_now_and_at_delivery(db_session):
    _fleet(db_session)
    m = order_mask.mask(db_session, product_code="PH-1", quantity=100, today=TODAY)
    w = m["what_if"]
    assert w["quantity"] == 100 and w["fits"] and w["guard"] == planning.check_order_capacity(db_session, 100, today=TODAY)
    assert m["recommendation"]["guard_for"] == "what_if" and m["recommendation"]["orderable_now"] == 50, "the recommendation is judged against free-to-order, not the what-if"
    i = w["intake"]
    # new stock: 7 on hand (2 phones, 5 laptops), 20 inbound, capacity 100: 27 committed now, 127 with the order
    assert i["capacity"] == 100 and i["on_hand"] == 7 and i["inbound"] == 20 and i["committed_now"] == 27 and i["committed_with"] == 127
    assert i["over_now"] == 0 and i["over_with"] == 27 and i["utilisation_with"] == 1.27
    # at delivery: three first rentals in the last three full months drain 1/30.4375 a day for 21 days, the 20 due land, then the 100
    assert i["lead_time_days"] == 21 and i["eta"] == TODAY + timedelta(days=21)
    outflow = (3 / 3) / DPM
    assert i["outflow_per_day"] == round(outflow, 1) and "measured" in i["outflow_basis"]
    assert i["stock_at_eta_without"] == int(round(7 - outflow * 21)) == 6 and i["inbound_due_by_eta"] == 20
    assert i["stock_at_eta_with"] == 126 and i["over_at_eta"] == 26
    assert w["plan"]["with_order_state"] == "breaks_at_delivery" and w["plan"]["with_order_month"] == "2026-10"
    assert w["warehouse"] == {"capacity": 500, "committed": 36, "committed_pct": 0.072, "committed_with": 136, "committed_pct_with": 0.272}
    c = w["cost"]
    assert c["total"] == 50_000.0 and c["landed"] == round(50_000 * (1 + settings.landed_cost_adder_pct), 2) and c["verdict"] == "under_cap"
    assert c["unpriced_units"] == 0 and "placeholder" in c["adder_basis"]
    cv = w["covers"]
    assert cv["recommended"] == 50 and cv["vs_gap"] == 50 and cv["verdict"] == "covers"
    rate = m["demand"]["rate_per_day"]
    assert rate > 0 and cv["days_of_demand"] == round(100 / rate, 1) and cv["cover_days_after"] == round((3 + 100) / rate, 1)
    assert w["room"] is None
    assert w["split"] == [{"product_id": w["split"][0]["product_id"], "code": "PH-1", "name": "Phone 1", "units": 100, "unit_price": 500.0, "cost": 50_000.0,
                           "moq": 50, "lead_time_days": 21, "moq_short": False, "eta": TODAY + timedelta(days=21)}]


def test_what_if_says_what_would_make_room_when_there_is_none(db_session):
    _fleet(db_session)
    w = order_mask.mask(db_session, product_code="PH-1", quantity=600, today=TODAY)["what_if"]
    assert not w["fits"] and w["guard"]["verdict"] == "clamp" and w["guard"]["allowed"] == 464
    r = w["room"]
    assert r["needed"] == 600 - 464 == 136
    levers = {lv["kind"]: lv["units"] for lv in r["levers"]}
    # sellable stock (2), the chain's units past their target dwell (RT-1 at 12 > 10, M-1 at 30 > 21, RP-1 at 25 > 20), the places to lease
    assert levers == {"sell": 2, "clear_chain": 3, "lease": 136} and "late_inbound" not in levers
    assert w["cost"]["verdict"] == "escalate" and w["cost"]["total"] == 300_000.0


def test_what_if_splits_a_quantity_over_a_scope_in_proportion_to_its_gaps(db_session):
    _fleet(db_session)
    w = order_mask.mask(db_session, quantity=10, today=TODAY)["what_if"]
    assert [(s["code"], s["units"], s["moq_short"]) for s in w["split"]] == [("LT-1", 0, False), ("PH-1", 10, True)], "only the phone has a gap"
    assert w["cost"]["total"] == 5_000.0 and w["intake"]["lead_time_days"] == 21
    assert order_mask._split(10, [3.0, 1.0]) == [8, 2] and order_mask._split(7, [0.0, 0.0]) == [4, 3] and order_mask._split(0, [1.0]) == [0]


def test_the_datacenter_scenario_answers_with_a_reason(client, db_session):
    wh = _save(db_session, Location(code="WH", name="Transit warehouse", location_type=LocationType.WAREHOUSE, capacity=10))
    prod = _save(db_session, Product(product_code="SRV-1", name="Server 1"))
    _save(db_session, Asset(serial_number="DC-1", product_id=prod.id, status=AssetStatus.IN_STORAGE, current_location_id=wh.id, received_date=TODAY))
    m = order_mask.mask(db_session, today=TODAY)
    assert m["scenario"] == "datacenter" and m["recommendation"] is None and m["reason"]
    assert client.get("/api/v1/order-mask").json()["scenario"] == "datacenter"


def test_api_order_mask(client, db_session):
    _fleet(db_session)
    s = client.get("/api/v1/order-mask/scopes")
    assert s.status_code == 200 and [p["code"] for p in s.json()["products"]] == ["LT-1", "PH-1"]
    r = client.get("/api/v1/order-mask?product_code=PH-1&quantity=100")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["scenario"] == "daas" and body["recommendation"]["recommended"] == 50 and body["what_if"]["quantity"] == 100
    assert body["what_if"]["intake"]["committed_with"] == 127 and body["timing_ms"]["total"] >= 0
    assert [f["key"] for f in body["recommendation"]["factors"]][:3] == ["usage", "eol", "buffer"]
    assert client.get("/api/v1/order-mask?product_code=NOPE").status_code == 404
    assert client.get("/api/v1/order-mask?quantity=-1").status_code == 422
    assert client.anon().get("/api/v1/order-mask").status_code in (401, 403)
