"""The device TCO: what a rented device costs over its life, per month of service, and what the second life gives back.

The layer maths to the cent, the per-month normalisation from the rental contracts, the
resale credit against what the sold devices were bought for, a class with no data saying
why, the seed's service events in proportion to the fleet it already has, and the
datacenter path left exactly as it was. The datacenter TCO tests stay untouched; this file
only asserts what the device view adds.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import date, timedelta
from decimal import Decimal

from app.models.catalog import Organization, Product
from app.models.flow import WAREHOUSE_STATUSES, Asset, AssetStatus, Location, LocationType
from app.models.procurement import OrderItem, PurchaseOrder
from app.models.rental import ContractStatus, RentalContract
from app.models.tco import DeploymentCost, DeploymentTask, LandedCost, LandedCostType, ServiceEvent, ServiceKind
from app.services import tco, tco_device
from app.services.tco_device import DAYS_PER_MONTH

TODAY = date(2026, 9, 23)
D = Decimal


def _save(db, obj):
    db.add(obj)
    db.flush()
    return obj


def _fleet(db):
    """Three smartphones, three laptops and one tablet with known prices, rentals, service
    events and dwell. One smartphone and one laptop have finished their lives."""
    cust = _save(db, Organization(code="CUST-T", name="Customer T (role-only)", is_supplier=False))
    supplier = _save(db, Organization(code="SUP-T", name="Supplier T (role-only)", is_supplier=True))
    ph = _save(db, Product(product_code="PH-1", name="Phone 1", category="Smartphone"))
    lt = _save(db, Product(product_code="LT-1", name="Laptop 1", category="Laptop"))
    tb = _save(db, Product(product_code="TB-1", name="Tablet 1", category="Tablet"))
    po = _save(db, PurchaseOrder(order_number="PO-T", supplier_id=supplier.id))
    ph_line = _save(db, OrderItem(order_id=po.id, product_id=ph.id, quantity=3, unit_price=D("500.00")))
    lt_line = _save(db, OrderItem(order_id=po.id, product_id=lt.id, quantity=3, unit_price=D("1000.00")))
    tb_line = _save(db, OrderItem(order_id=po.id, product_id=tb.id, quantity=1, unit_price=D("300.00")))
    _save(db, Location(code="ST-NEW", name="New stock", location_type=LocationType.WAREHOUSE, capacity=100))

    def unit(serial, prod, line, status, days, received, **kw):
        """A serial ``received`` days ago, in its current status for ``days``."""
        row = dict(serial_number=serial, product_id=prod.id, source_order_item_id=line.id, status=status,
                   received_date=TODAY - timedelta(days=received), status_since=TODAY - timedelta(days=days))
        row.update(kw)
        return _save(db, Asset(**row))

    def contract(asset, prod, cycle, start_days, end_days=None, reason=None, with_product=True, rent=None):
        """A rental that started ``start_days`` ago and ended ``end_days`` ago (running when None)."""
        start = TODAY - timedelta(days=start_days)
        end = None if end_days is None else TODAY - timedelta(days=end_days)
        return _save(db, RentalContract(asset_id=asset.id, product_id=(prod.id if with_product else None), customer_id=cust.id,
                                        cycle_no=cycle, start_date=start, term_months=24, planned_end=start + timedelta(days=730),
                                        actual_end=end, end_reason=reason, rent_eur_month=(None if rent is None else D(rent)),
                                        status=ContractStatus.ENDED if end else ContractStatus.RUNNING))

    # P-1, a finished life: received 810 days ago, bought for 500, rented 400 days at 25 a month and
    # then 300 days at 17.50, repaired (100) and refurbished (30) between the two rentals, sold for
    # 200 net 20 days ago. Owned 790 days, on rent 700, off rent 90 (10 + 20 + 60).
    p1 = unit("P-1", ph, ph_line, AssetStatus.SOLD, 20, 810, cycle_no=2, grade="C", sold_date=TODAY - timedelta(days=20),
              sale_price=D("200.00"), sale_channel="marketplace")
    contract(p1, ph, 1, 800, 400, rent="25.00")
    contract(p1, ph, 2, 380, 80, rent="17.50")
    _save(db, ServiceEvent(asset_id=p1.id, product_id=ph.id, kind=ServiceKind.REPAIR, cycle_no=2, event_date=TODAY - timedelta(days=395), cost=D("100.00")))
    _save(db, ServiceEvent(asset_id=p1.id, product_id=ph.id, kind=ServiceKind.REFURB, cycle_no=2, event_date=TODAY - timedelta(days=385), cost=D("30.00")))
    # P-2: received 110 days ago, rented for 100 days so far; its contract carries no model on purpose, the older path
    p2 = unit("P-2", ph, ph_line, AssetStatus.RENTED, 100, 110, cycle_no=1, customer_id=cust.id, deployed_date=TODAY - timedelta(days=100))
    contract(p2, ph, 1, 100, with_product=False, rent="25.00")
    # P-3: new stock, ten days on the shelf
    unit("P-3", ph, ph_line, AssetStatus.IN_STORAGE, 10, 10, cycle_no=0, grade="A")
    # L-1: received 205 days ago, rented for 200 days at 45
    l1 = unit("L-1", lt, lt_line, AssetStatus.RENTED, 200, 205, cycle_no=1, customer_id=cust.id, deployed_date=TODAY - timedelta(days=200))
    contract(l1, lt, 1, 200, rent="45.00")
    # L-2, a finished life without a sale: received 520 days ago, rented 400 days at 45, recycled 100 days ago
    l2 = unit("L-2", lt, lt_line, AssetStatus.RECYCLED, 100, 520, cycle_no=1, grade="D", sold_date=TODAY - timedelta(days=100))
    contract(l2, lt, 1, 500, 100, rent="45.00")
    # L-3: received 110 days ago, came back with a defect after 60 days, five days in sellable
    # stock; its contract carries no rent
    l3 = unit("L-3", lt, lt_line, AssetStatus.SELLABLE, 5, 110, cycle_no=1, grade="C")
    contract(l3, lt, 1, 100, 40, "defect")
    # T-1: bought, never rented, three days on the shelf
    unit("T-1", tb, tb_line, AssetStatus.IN_STORAGE, 3, 3, cycle_no=0, grade="A")
    db.flush()


R = tco_device.RATES


def _rate(rid, family=None):
    return R[rid].for_family(family)


def _m(x):
    """Money as the service rounds it: half up, to the cent."""
    return float(D(str(x)).quantize(D("0.01"), rounding="ROUND_HALF_UP"))


def _classes(cohort):
    return {g["key"]: g for g in cohort["classes"]}


def _layers(g):
    return {lay["id"]: lay for lay in g["layers"]}


def test_layer_maths_to_the_cent_over_the_whole_fleet(db_session):
    _fleet(db_session)
    T = tco_device.overview(db_session, today=TODAY)
    assert T["scenario"] == "daas" and T["reason"] is None
    assert [lay["id"] for lay in T["layers"]] == ["acquisition", "inbound", "enrolment", "outbound", "software", "support",
                                                  "returns", "service", "warehouse", "financing", "eol"]
    for r in T["rates"]:
        assert r["owner"], "every rate has an owner"
        assert r["placeholder"] == (r["source"] is None), "a rate is either sourced or a placeholder, never both"

    ph = _classes(T["cohorts"]["fleet"])["Smartphone"]
    assert ph["devices"] == 3 and ph["rented"] == 1 and ph["on_hand"] == 1 and ph["sold"] == 1 and ph["priced"] == 3
    assert ph["contracts"] == 3 and ph["contracts_cycle2"] == 1, "the contract without a model is found through its device"
    months = 800 / DAYS_PER_MONTH     # 400 + 300 days of finished rentals, plus 100 days running
    assert ph["device_months"] == round(months, 1) == 26.3
    L = _layers(ph)
    assert L["acquisition"]["total"] == 1500.0 and L["acquisition"]["per_device"] == 500.0
    assert L["acquisition"]["per_month"] == round(1500 / months, 2)
    assert L["inbound"]["total"] == _m(_rate("inbound", "Smartphone") * 3)           # three devices delivered
    assert L["enrolment"]["total"] == _m(_rate("enrolment", "Smartphone") * 3)       # three rental starts
    assert L["outbound"]["total"] == _m(_rate("outbound", "Smartphone") * 3)         # three parcels to a user
    assert L["software"]["total"] == _m(_rate("software") * months)
    assert L["support"]["total"] == round(1.5 * months, 2) == 39.43   # no defect or swap among the smartphones
    # two rentals ended (both of P-1's), neither by a defect: two return trips, two intakes with their wipe
    assert ph["returns"] == 2
    assert L["returns"]["total"] == _m(2 * _rate("return_ship", "Smartphone")) + _m(2 * _rate("intake", "Smartphone"))
    assert L["service"]["total"] == 130.0 and ph["repairs"] == 1 and ph["refurbs"] == 1
    # owned 790 + 110 + 10 = 910 device-days, on rent 800: 110 days off rent
    assert ph["device_days_owned"] == 910 and ph["device_days_off_rent"] == 110
    assert L["warehouse"]["total"] == _m(110 * _rate("warehouse_day"))
    # 500 EUR in each device for every day it is owned
    assert L["financing"]["total"] == _m(500 * 910 * _rate("capital") / 365)
    assert L["eol"]["total"] == -200.0                                # the resale is a credit
    costs = [L[k]["total"] for k in L if k != "eol"]
    assert ph["gross"] == round(sum(costs), 2) and ph["credit"] == 200.0 and ph["net"] == round(ph["gross"] - 200, 2)
    assert ph["per_device"]["net"] == round(ph["net"] / 3, 2)
    assert ph["per_month"]["net"] == round(ph["net"] / months, 2)
    assert ph["unmeasured"] == []
    # rent: 25 a month for 400 + 100 days, 17.50 for 300 days
    rev = round(17750 / DAYS_PER_MONTH, 2)
    rent = ph["rent"]
    assert rent["revenue"] == rev and rent["contracts_with_rent"] == 3 and rent["reason"] is None and rent["note"] is None
    assert rent["per_month"] == round(rev / months, 2)
    assert rent["margin"] == round(rev - ph["net"], 2) and rent["margin_per_month"] == round(rent["margin"] / months, 2)
    for lay in ph["layers"]:
        for c in lay["components"]:
            assert c["basis"] in ("measured", "quantity measured, rate placeholder", "quantity measured, rate from a public source"), \
                "a parameter is never presented as a measurement"
            if c["rate_id"]:
                assert (c["basis"] == "quantity measured, rate placeholder") == R[c["rate_id"]].placeholder
    assert _layers(ph)["acquisition"]["components"][0]["basis"] == "measured"
    assert _layers(ph)["software"]["components"][0]["basis"] == "quantity measured, rate from a public source"
    assert _layers(ph)["support"]["components"][0]["basis"] == "quantity measured, rate placeholder"
    # splitting the parcel out of enrolment did not move the old combined placeholder
    for fam, old in (("Smartphone", 15.0), ("Tablet", 15.0), ("Laptop", 25.0)):
        assert round(_rate("enrolment", fam) + _rate("outbound", fam), 2) == old
    for rid in ("outbound", "software", "return_ship", "intake"):
        assert R[rid].source and "http" in R[rid].source and not R[rid].placeholder, rid

    lt = _classes(T["cohorts"]["fleet"])["Laptop"]
    assert lt["swap_events"] == 1 and lt["returns"] == 2
    assert _layers(lt)["support"]["components"][1]["total"] == 35.0   # one contract ended by a defect
    # the defect return travels on the swap rate: one return trip, but two intakes
    ret = {c["id"]: c for c in _layers(lt)["returns"]["components"]}
    assert ret["return_ship"]["quantity"] == 1 and ret["intake"]["quantity"] == 2
    assert ret["intake"]["total"] == _m(2 * _rate("intake", "Laptop"))
    assert "swap" in ret["return_ship"]["note"]
    assert _layers(lt)["service"]["total"] == 0.0                     # no service event for a laptop: a measured zero
    assert _layers(lt)["eol"]["total"] == 6.0                         # one recycled laptop, nothing sold
    assert _layers(lt)["eol"]["components"][0]["total"] is None and "sold" in _layers(lt)["eol"]["components"][0]["reason"]
    # owned 205 + 420 + 110 = 735, on rent 200 + 400 + 60 = 660
    assert lt["device_days_owned"] == 735 and lt["device_days_off_rent"] == 75
    # L-3's contract carries no rent: the revenue is the other two, and the margin says what it leaves out
    assert lt["rent"]["contracts_with_rent"] == 2 and lt["rent"]["revenue"] == round(27000 / DAYS_PER_MONTH, 2)
    assert "carry no rent" in lt["rent"]["note"]


def test_finished_lives_are_the_whole_life_number(db_session):
    _fleet(db_session)
    T = tco_device.overview(db_session, today=TODAY)
    F = T["cohorts"]["finished"]
    ph = _classes(F)["Smartphone"]
    months = 700 / DAYS_PER_MONTH
    assert ph["devices"] == 1 and ph["sold"] == 1 and ph["contracts"] == 2 and ph["device_months"] == 23.0
    assert ph["months_per_device"] == 23.0 and ph["second_life_share_of_months"] == round(300 / 700, 4)
    L = _layers(ph)
    assert L["acquisition"]["total"] == 500.0 and L["service"]["total"] == 130.0
    assert L["enrolment"]["total"] == _m(2 * _rate("enrolment", "Smartphone"))
    # a finished life's days off rent come from its own dates: received 810 days ago, sold 20
    # days ago, 700 of the 790 days on rent
    assert ph["device_days_owned"] == 790 and ph["device_days_off_rent"] == 90
    assert L["warehouse"]["total"] == _m(90 * _rate("warehouse_day")) and L["warehouse"]["reason"] is None
    assert L["financing"]["total"] == _m(500 * 790 * _rate("capital") / 365)
    assert ph["unmeasured"] == [], "every layer of a finished life is measured now"
    gross = round(500 + _m(_rate("inbound", "Smartphone")) + _m(2 * _rate("enrolment", "Smartphone"))
                  + _m(2 * _rate("outbound", "Smartphone")) + _m(_rate("software") * months) + 34.50
                  + _m(2 * _rate("return_ship", "Smartphone")) + _m(2 * _rate("intake", "Smartphone")) + 130
                  + _m(90 * _rate("warehouse_day")) + _m(500 * 790 * _rate("capital") / 365), 2)
    assert ph["gross"] == gross and ph["credit"] == 200.0 and ph["net"] == round(gross - 200, 2)
    assert ph["per_month"]["gross"] == round(gross / months, 2)
    assert ph["per_month"]["net"] == round((gross - 200) / months, 2)
    # what its two rentals earned, and what is left after the whole-life cost
    rev = round(15250 / DAYS_PER_MONTH, 2)
    assert ph["rent"]["revenue"] == rev and ph["rent"]["margin"] == round(rev - ph["net"], 2)
    assert ph["rent"]["margin_share"] == round(ph["rent"]["margin"] / rev, 4)
    # the resale credit, against what those same devices were bought for
    assert ph["resale"] == {"sold": 1, "sold_priced": 1, "proceeds": 200.0, "acquisition_of_sold": 500.0,
                            "credit_share_of_acquisition": 0.4, "reason": None}

    lt = _classes(F)["Laptop"]
    assert lt["devices"] == 1 and lt["recycled"] == 1 and lt["sold"] == 0
    assert lt["resale"]["credit_share_of_acquisition"] is None and "sold" in lt["resale"]["reason"]
    lt_gross = round(1000 + _m(_rate("inbound", "Laptop")) + _m(_rate("enrolment", "Laptop")) + _m(_rate("outbound", "Laptop"))
                     + _m(_rate("software") * 400 / DAYS_PER_MONTH) + 19.71 + _m(_rate("return_ship", "Laptop")) + _m(_rate("intake", "Laptop"))
                     + _m(20 * _rate("warehouse_day")) + _m(1000 * 420 * _rate("capital") / 365) + 6, 2)
    assert _layers(lt)["eol"]["total"] == 6.0 and lt["gross"] == lt_gross and lt["net"] == lt_gross
    assert lt["rent"]["revenue"] == round(18000 / DAYS_PER_MONTH, 2)

    P = F["portfolio"]
    # the portfolio is computed from its own quantities (the rate times all 1,100 days), so it
    # can differ from the sum of the class figures by a cent of rounding per rounded component
    months_all = 1100 / DAYS_PER_MONTH
    assert P["devices"] == 2 and P["credit"] == 200.0 and P["device_months"] == round(months_all, 1)
    assert P["device_days_owned"] == 790 + 420 and P["returns"] == 3
    assert abs(P["gross"] - (gross + lt_gross)) <= 0.03
    assert P["rent"]["revenue"] == round(33250 / DAYS_PER_MONTH, 2)
    assert T["cohorts"]["fleet"]["portfolio"]["devices"] == 7, "the fleet to date carries the finished devices and the live ones"
    assert [m["product_code"] for m in F["models"]] == ["PH-1", "LT-1"], "models in class order; a model without a finished device has no row"


def test_a_class_with_no_data_says_why_instead_of_zero(db_session):
    _fleet(db_session)
    T = tco_device.overview(db_session, today=TODAY)
    assert [c["key"] for c in T["cohorts"]["fleet"]["classes"]] == ["Smartphone", "Tablet", "Laptop"]
    # no tablet has finished its life
    tab = _classes(T["cohorts"]["finished"])["Tablet"]
    assert tab["devices"] == 0 and tab["reason"] and "finished" in tab["reason"]
    assert tab["gross"] is None and tab["net"] is None and tab["per_month"]["net"] is None and tab["per_device"]["gross"] is None
    assert tab["per_month_reason"] == "no device in this group"
    for lay in tab["layers"]:
        assert lay["total"] is None and lay["reason"] == "no device in this group", lay["id"]
        assert all(c["total"] is None for c in lay["components"])
    assert tab["unmeasured"] == [lay["id"] for lay in T["layers"]]
    # the one tablet in the fleet was never rented: zero rental starts is a count, so the
    # per-rental layers are a measured zero, but there is no month to normalise by and the
    # per-month figures say so
    tab = _classes(T["cohorts"]["fleet"])["Tablet"]
    assert tab["devices"] == 1 and tab["contracts"] == 0 and tab["device_months"] == 0.0
    assert tab["per_month"]["net"] is None and "no month in service" in tab["per_month_reason"]
    L = _layers(tab)
    assert L["acquisition"]["total"] == 300.0 and L["inbound"]["total"] == 5.0
    assert L["enrolment"]["total"] == 0.0 and L["software"]["total"] == 0.0 and L["support"]["total"] == 0.0
    assert L["software"]["per_month"] is None and L["acquisition"]["per_month"] is None and L["acquisition"]["per_device"] == 300.0
    assert L["outbound"]["total"] == 0.0 and L["returns"]["total"] == 0.0
    # three days owned, none of them on rent: three days off rent, and three days of money in it
    assert L["warehouse"]["total"] == _m(3 * _rate("warehouse_day"))
    assert L["financing"]["total"] == _m(300 * 3 * _rate("capital") / 365)
    assert L["eol"]["components"][0]["total"] is None and L["eol"]["total"] == 0.0     # nothing sold, nothing recycled
    assert tab["unmeasured"] == []
    assert tab["gross"] == round(300 + 5 + L["warehouse"]["total"] + L["financing"]["total"], 2) and tab["net"] == tab["gross"]
    # never rented: no revenue and no margin, with the reason instead of a zero
    assert tab["rent"]["revenue"] is None and tab["rent"]["margin"] is None and tab["rent"]["reason"] == "no rental yet"


def test_datacenter_scenario_is_untouched(client, db_session):
    org = _save(db_session, Organization(code="S", name="Supplier", is_supplier=True))
    prod = _save(db_session, Product(product_code="SRV-1", name="Server 1", category="Servers"))
    po = _save(db_session, PurchaseOrder(order_number="PO-DC", supplier_id=org.id))
    oi = _save(db_session, OrderItem(order_id=po.id, product_id=prod.id, quantity=2, unit_price=D("3200.00")))
    a = _save(db_session, Asset(serial_number="DC-1", product_id=prod.id, status=AssetStatus.DEPLOYED, source_order_item_id=oi.id, received_date=TODAY))
    _save(db_session, Asset(serial_number="DC-2", product_id=prod.id, status=AssetStatus.IN_STORAGE, source_order_item_id=oi.id, received_date=TODAY))
    db_session.add_all([LandedCost(asset_id=a.id, cost_type=LandedCostType.FREIGHT, amount=D("200.00")),
                        LandedCost(asset_id=a.id, cost_type=LandedCostType.DUTY, amount=D("60.00")),
                        DeploymentCost(asset_id=a.id, task=DeploymentTask.RACKING, amount=D("140.00"))])
    db_session.flush()
    T = tco_device.overview(db_session, today=TODAY)
    assert T["scenario"] == "datacenter" and T["cohorts"] == {} and "datacenter" in T["reason"]
    # the per-asset rollups answer as they always did: every asset in the portfolio, the modelled one per class
    p = tco.portfolio_tco(db_session, D("100000"))
    assert p["assets"] == 2 and p["subtotals"]["acquisition"] == 6400.0 and p["subtotals"]["landed"] == 260.0
    assert p["subtotals"]["deployment"] == 140.0 and p["tco_total"] == 6800.0 and p["tscmc_pct"] == 400 / 100000
    assert tco.portfolio_tco(db_session, D("100000"), exclude_landed_types=["duty"])["subtotals"]["landed"] == 200.0
    rows = tco.tco_by_class(db_session)
    assert len(rows) == 1 and rows[0]["category"] == "Servers" and rows[0]["assets"] == 1
    assert rows[0]["acquisition"] == 3200.0 and rows[0]["landed"] == 260.0 and rows[0]["deployment"] == 140.0
    assert rows[0]["tco_total"] == 3600.0 and rows[0]["avg_tco"] == 3600.0
    assert tco.tco_by_class(db_session, exclude_landed_types=[LandedCostType.DUTY])[0]["landed"] == 200.0
    r = client.get("/api/v1/tco/devices")
    assert r.status_code == 200 and r.json()["scenario"] == "datacenter"


def test_datacenter_rollups_answer_at_once_over_a_fleet_without_layers(db_session):
    """The old rollups looped over every serial; over the device fleet they must stay a
    count and a sum, and say what is true: acquisition only, no modelled class."""
    _fleet(db_session)
    assert tco.tco_by_class(db_session) == []
    p = tco.portfolio_tco(db_session, D("1000000"))
    assert p["assets"] == 7 and p["subtotals"]["acquisition"] == 4800.0
    assert p["subtotals"]["opex"] == 0.0 and p["subtotals"]["recovery"] == 0.0 and p["tco_total"] == 4800.0


def test_api_device_tco(client, db_session):
    _fleet(db_session)
    r = client.get("/api/v1/tco/devices")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["scenario"] == "daas" and set(body["cohorts"]) == {"finished", "fleet"}
    assert [c["key"] for c in body["cohorts"]["fleet"]["classes"]] == ["Smartphone", "Tablet", "Laptop"]
    # a finished life does not move with the calendar: the API's today gives the fixture's numbers
    fin = tco_device.overview(db_session, today=TODAY)["cohorts"]["finished"]["classes"][0]
    assert body["cohorts"]["finished"]["classes"][0]["per_month"]["net"] == fin["per_month"]["net"]
    assert body["cohorts"]["finished"]["classes"][0]["rent"]["margin"] == fin["rent"]["margin"]
    assert body["cohorts"]["fleet"]["portfolio"]["devices"] == 7
    assert all("source" in r for r in body["rates"])
    assert client.anon().get("/api/v1/tco/devices").status_code in (401, 403)


def test_one_device_is_its_model_at_the_size_of_one(db_session):
    """A serial runs through the same figures as the fleet: where a group holds one device,
    the device and the group are the same number, and a model is the sum of its serials."""
    _fleet(db_session)
    T = tco_device.overview(db_session, today=TODAY)
    p1 = tco_device.device(db_session, "P-1", today=TODAY)
    fin_ph = _classes(T["cohorts"]["finished"])["Smartphone"]          # P-1 is the only finished smartphone
    assert p1["kind"] == "device" and p1["serial_number"] == "P-1" and p1["finished"] is True and p1["status"] == "SOLD"
    for k in ("gross", "credit", "net", "device_months", "returns", "device_days_owned", "device_days_off_rent", "swap_events"):
        assert p1[k] == fin_ph[k], k
    assert p1["per_month"] == fin_ph["per_month"] and p1["rent"] == fin_ph["rent"]
    assert {lay["id"]: lay["total"] for lay in p1["layers"]} == {lay["id"]: lay["total"] for lay in fin_ph["layers"]}

    # its life, in order: bought, rented, repaired, refurbished, rented again, sold
    life = p1["life"]
    assert [ev["kind"] for ev in life] == ["received", "rental", "repair", "refurb", "rental", "sold"]
    assert life[0]["amount"] == 500.0 and life[-1]["amount"] == 200.0 and life[-1]["channel"] == "marketplace"
    rentals = [ev for ev in life if ev["kind"] == "rental"]
    assert [ev["days"] for ev in rentals] == [400, 300] and [ev["rent_eur_month"] for ev in rentals] == [25.0, 17.5]
    assert rentals[0]["amount"] == round(25 * 400 / DAYS_PER_MONTH, 2)

    # the tablet is the only one of its class: never rented, so the device says why it earned nothing
    t1 = tco_device.device(db_session, "T-1", today=TODAY)
    tab = _classes(T["cohorts"]["fleet"])["Tablet"]
    assert t1["gross"] == tab["gross"] and t1["rent"]["reason"] == "no rental yet" and t1["life"][0]["kind"] == "received"

    # the smartphone model is the sum of its three serials, to the cent per rounded component
    fleet_ph = _classes(T["cohorts"]["fleet"])["Smartphone"]
    serials = [tco_device.device(db_session, s, today=TODAY) for s in ("P-1", "P-2", "P-3")]
    assert sum(s["devices"] for s in serials) == fleet_ph["devices"]
    assert sum(s["returns"] for s in serials) == fleet_ph["returns"]
    assert sum(s["device_days_owned"] for s in serials) == fleet_ph["device_days_owned"]
    assert abs(sum(s["gross"] for s in serials) - fleet_ph["gross"]) <= 0.05
    assert abs(sum(s["rent"]["revenue"] for s in serials if s["rent"]["revenue"]) - fleet_ph["rent"]["revenue"]) <= 0.02

    # a running rental ends today; the asset id finds the device as well as the serial does
    p2 = tco_device.device(db_session, "P-2", today=TODAY)
    assert p2["finished"] is False and p2["life"][1]["end"] is None and p2["life"][1]["days"] == 100
    assert tco_device.device(db_session, p2["id"], today=TODAY)["serial_number"] == "P-2"


def test_api_one_device_and_its_model_serials(client, db_session):
    _fleet(db_session)
    r = client.get("/api/v1/tco/devices/serial/P-1")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["serial_number"] == "P-1" and len(body["life"]) == 6 and body["rent"]["revenue"] > 0
    assert [lay["id"] for lay in body["layers"]][-1] == "eol"
    assert client.get("/api/v1/tco/devices/serial/NO-SUCH-SERIAL").status_code == 404

    pid = body["product_id"]
    r = client.get(f"/api/v1/tco/devices/models/{pid}/serials")
    assert r.status_code == 200, r.text
    s = r.json()
    assert s["label"] == "Phone 1" and s["family"] == "Smartphone"
    assert [(x["group"], x["serial_number"]) for x in s["serials"]] == [("finished", "P-1"), ("rented", "P-2"), ("on_hand", "P-3")]
    assert client.get("/api/v1/tco/devices/models/no-such-model/serials").status_code == 404
    assert client.anon().get("/api/v1/tco/devices/serial/P-1").status_code in (401, 403)


def test_seed_writes_the_events_a_device_state_proves_and_moves_no_total(db_session, monkeypatch):
    """One refurbishment before a second rental, a repair before it when the grade is C,
    nothing for a device still on the bench; every contract carries its device's model;
    300 rented and 100 on hand stay exactly that."""
    from app import seed_daas
    from app.seed_reset import dataset_is_stale

    monkeypatch.setattr(seed_daas, "SessionLocal", lambda: db_session)
    monkeypatch.setattr(seed_daas, "N_RENTED", 300)
    monkeypatch.setattr(seed_daas, "N_WAREHOUSE", 100)
    monkeypatch.setattr(seed_daas, "N_SOLD_LAST_YEAR", 30)
    monkeypatch.setattr(seed_daas, "N_RECYCLED_LAST_YEAR", 2)
    seed_daas.seed_daas()

    assets = db_session.query(Asset).all()
    assert sum(1 for a in assets if a.status == AssetStatus.RENTED) == 300
    assert sum(1 for a in assets if a.status in WAREHOUSE_STATUSES) == 100
    by_asset = defaultdict(Counter)
    for e in db_session.query(ServiceEvent).all():
        by_asset[e.asset_id][e.kind] += 1
        assert float(e.cost) > 0 and e.currency == "EUR" and e.cycle_no == 2
    prod_of = {a.id: a.product_id for a in assets}
    for a in assets:
        kinds = by_asset.get(a.id, Counter())
        refurbished = a.cycle_no >= 2 or a.status in (AssetStatus.READY_SECOND, AssetStatus.SWAP_BUFFER)
        if a.status in (AssetStatus.REPAIR, AssetStatus.REFURB):
            assert not kinds, "on the bench: not invoiced yet"
        elif refurbished:
            assert kinds[ServiceKind.REFURB] == 1 and kinds[ServiceKind.REPAIR] == (1 if a.grade == "C" else 0), a.serial_number
        else:
            assert not kinds, a.serial_number
    for e in db_session.query(ServiceEvent).all():
        assert e.product_id == prod_of[e.asset_id]
    assert all(c.product_id == prod_of[c.asset_id] for c in db_session.query(RentalContract).all())
    assert dataset_is_stale(db_session) is None

    T = tco_device.overview(db_session, today=date.today())
    fleet = T["cohorts"]["fleet"]
    assert [c["key"] for c in fleet["classes"]] == ["Smartphone", "Tablet", "Laptop"] and all(c["devices"] > 0 for c in fleet["classes"])
    assert fleet["portfolio"]["unmeasured"] == [] and fleet["portfolio"]["repairs"] > 0
    assert fleet["portfolio"]["refurbs"] > fleet["portfolio"]["repairs"]
    assert fleet["portfolio"]["devices"] == len(assets)
    fin = T["cohorts"]["finished"]
    assert fin["portfolio"]["devices"] == 32 and fin["portfolio"]["unmeasured"] == []
    # every seeded contract carries a rent, and every seeded device a receipt date
    assert fin["portfolio"]["rent"]["revenue"] > 0 and fin["portfolio"]["rent"]["note"] is None
    assert fleet["portfolio"]["rent"]["contracts_with_rent"] == fleet["portfolio"]["contracts"]
    assert fleet["portfolio"]["device_days_owned"] > 0 and fleet["portfolio"]["device_days_off_rent"] >= 0
    assert 0 < fin["portfolio"]["resale"]["credit_share_of_acquisition"] < 1

    # a fleet from before the events counts as stale, so the boot rebuilds it instead of showing an empty cost tab
    db_session.query(ServiceEvent).delete()
    db_session.flush()
    assert "service event" in (dataset_is_stale(db_session) or "")
