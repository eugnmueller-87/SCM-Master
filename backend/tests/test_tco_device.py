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

    def unit(serial, prod, line, status, days, **kw):
        row = dict(serial_number=serial, product_id=prod.id, source_order_item_id=line.id, status=status,
                   received_date=TODAY - timedelta(days=900), status_since=TODAY - timedelta(days=days))
        row.update(kw)
        return _save(db, Asset(**row))

    def contract(asset, prod, cycle, start_days, end_days=None, reason=None, with_product=True):
        """A rental that started ``start_days`` ago and ended ``end_days`` ago (running when None)."""
        start = TODAY - timedelta(days=start_days)
        end = None if end_days is None else TODAY - timedelta(days=end_days)
        return _save(db, RentalContract(asset_id=asset.id, product_id=(prod.id if with_product else None), customer_id=cust.id,
                                        cycle_no=cycle, start_date=start, term_months=24, planned_end=start + timedelta(days=730),
                                        actual_end=end, end_reason=reason,
                                        status=ContractStatus.ENDED if end else ContractStatus.RUNNING))

    # P-1, a finished life: bought for 500, rented 400 days and then 300 days, repaired (100) and
    # refurbished (30) between the two rentals, sold for 200 net
    p1 = unit("P-1", ph, ph_line, AssetStatus.SOLD, 20, cycle_no=2, grade="C", sold_date=TODAY - timedelta(days=20),
              sale_price=D("200.00"), sale_channel="marketplace")
    contract(p1, ph, 1, 800, 400)
    contract(p1, ph, 2, 380, 80)
    _save(db, ServiceEvent(asset_id=p1.id, product_id=ph.id, kind=ServiceKind.REPAIR, cycle_no=2, event_date=TODAY - timedelta(days=395), cost=D("100.00")))
    _save(db, ServiceEvent(asset_id=p1.id, product_id=ph.id, kind=ServiceKind.REFURB, cycle_no=2, event_date=TODAY - timedelta(days=385), cost=D("30.00")))
    # P-2: rented for 100 days so far; its contract carries no model on purpose, the older path
    p2 = unit("P-2", ph, ph_line, AssetStatus.RENTED, 100, cycle_no=1, customer_id=cust.id, deployed_date=TODAY - timedelta(days=100))
    contract(p2, ph, 1, 100, with_product=False)
    # P-3: new stock, ten days on the shelf
    unit("P-3", ph, ph_line, AssetStatus.IN_STORAGE, 10, cycle_no=0, grade="A")
    # L-1: rented for 200 days
    l1 = unit("L-1", lt, lt_line, AssetStatus.RENTED, 200, cycle_no=1, customer_id=cust.id, deployed_date=TODAY - timedelta(days=200))
    contract(l1, lt, 1, 200)
    # L-2, a finished life without a sale: rented 400 days, then recycled
    l2 = unit("L-2", lt, lt_line, AssetStatus.RECYCLED, 100, cycle_no=1, grade="D", sold_date=TODAY - timedelta(days=100))
    contract(l2, lt, 1, 500, 100)
    # L-3: came back with a defect after 60 days, five days in sellable stock
    l3 = unit("L-3", lt, lt_line, AssetStatus.SELLABLE, 5, cycle_no=1, grade="C")
    contract(l3, lt, 1, 100, 40, "defect")
    # T-1: bought, never rented, three days on the shelf
    unit("T-1", tb, tb_line, AssetStatus.IN_STORAGE, 3, cycle_no=0, grade="A")
    db.flush()


def _classes(cohort):
    return {g["key"]: g for g in cohort["classes"]}


def _layers(g):
    return {lay["id"]: lay for lay in g["layers"]}


def test_layer_maths_to_the_cent_over_the_whole_fleet(db_session):
    _fleet(db_session)
    T = tco_device.overview(db_session, today=TODAY)
    assert T["scenario"] == "daas" and T["reason"] is None
    assert [lay["id"] for lay in T["layers"]] == ["acquisition", "inbound", "enrolment", "software", "support", "service", "warehouse", "eol"]
    assert all(r["placeholder"] and r["owner"] for r in T["rates"]), "every rate is a placeholder with an owner"

    ph = _classes(T["cohorts"]["fleet"])["Smartphone"]
    assert ph["devices"] == 3 and ph["rented"] == 1 and ph["on_hand"] == 1 and ph["sold"] == 1 and ph["priced"] == 3
    assert ph["contracts"] == 3 and ph["contracts_cycle2"] == 1, "the contract without a model is found through its device"
    months = 800 / DAYS_PER_MONTH     # 400 + 300 days of finished rentals, plus 100 days running
    assert ph["device_months"] == round(months, 1) == 26.3
    L = _layers(ph)
    assert L["acquisition"]["total"] == 1500.0 and L["acquisition"]["per_device"] == 500.0
    assert L["acquisition"]["per_month"] == round(1500 / months, 2)
    assert L["inbound"]["total"] == 12.0                              # three devices at the smartphone rate of 4
    assert L["enrolment"]["total"] == 45.0                            # three rental starts at 15
    assert L["software"]["total"] == round(2.5 * months, 2) == 65.71
    assert L["support"]["total"] == round(1.5 * months, 2) == 39.43   # no defect or swap among the smartphones
    assert L["service"]["total"] == 130.0 and ph["repairs"] == 1 and ph["refurbs"] == 1
    assert L["warehouse"]["total"] == 1.5                             # 10 device-days at 0.04, plus 500 EUR over 10 days at 8 % a year
    assert L["eol"]["total"] == -200.0                                # the resale is a credit
    assert ph["gross"] == 1793.64 and ph["credit"] == 200.0 and ph["net"] == 1593.64
    assert ph["per_device"]["net"] == round(1593.64 / 3, 2)
    assert ph["per_month"]["net"] == round(1593.64 / months, 2)
    assert ph["unmeasured"] == []
    for lay in ph["layers"]:
        for c in lay["components"]:
            assert c["basis"] in ("measured", "quantity measured, rate placeholder"), "a parameter is never presented as a measurement"
    assert _layers(ph)["acquisition"]["components"][0]["basis"] == "measured"
    assert _layers(ph)["software"]["components"][0]["basis"] == "quantity measured, rate placeholder"

    lt = _classes(T["cohorts"]["fleet"])["Laptop"]
    assert lt["swap_events"] == 1
    assert _layers(lt)["support"]["components"][1]["total"] == 35.0   # one contract ended by a defect
    assert _layers(lt)["service"]["total"] == 0.0                     # no service event for a laptop: a measured zero
    assert _layers(lt)["eol"]["total"] == 6.0                         # one recycled laptop, nothing sold
    assert _layers(lt)["eol"]["components"][0]["total"] is None and "sold" in _layers(lt)["eol"]["components"][0]["reason"]


def test_finished_lives_are_the_whole_life_number(db_session):
    _fleet(db_session)
    T = tco_device.overview(db_session, today=TODAY)
    F = T["cohorts"]["finished"]
    ph = _classes(F)["Smartphone"]
    months = 700 / DAYS_PER_MONTH
    assert ph["devices"] == 1 and ph["sold"] == 1 and ph["contracts"] == 2 and ph["device_months"] == 23.0
    assert ph["months_per_device"] == 23.0 and ph["second_life_share_of_months"] == round(300 / 700, 4)
    L = _layers(ph)
    assert L["acquisition"]["total"] == 500.0 and L["enrolment"]["total"] == 30.0 and L["service"]["total"] == 130.0
    assert L["warehouse"]["total"] is None and "movement log" in L["warehouse"]["reason"], "a finished life has no logged warehouse days"
    assert ph["unmeasured"] == ["warehouse"]
    assert ph["gross"] == 755.99 and ph["credit"] == 200.0 and ph["net"] == 555.99
    assert ph["per_month"]["gross"] == round(755.99 / months, 2)
    assert ph["per_month"]["net"] == round(555.99 / months, 2) == 24.18
    # the resale credit, against what those same devices were bought for
    assert ph["resale"] == {"sold": 1, "sold_priced": 1, "proceeds": 200.0, "acquisition_of_sold": 500.0,
                            "credit_share_of_acquisition": 0.4, "reason": None}

    lt = _classes(F)["Laptop"]
    assert lt["devices"] == 1 and lt["recycled"] == 1 and lt["sold"] == 0
    assert lt["resale"]["credit_share_of_acquisition"] is None and "sold" in lt["resale"]["reason"]
    assert _layers(lt)["eol"]["total"] == 6.0 and lt["gross"] == 1092.56 and lt["net"] == 1092.56

    P = F["portfolio"]
    # the portfolio is computed from its own quantities (the rate times all 1,100 days), so it
    # can differ from the sum of the class figures by a cent of rounding: 1848.56, not 1848.55
    months_all = 1100 / DAYS_PER_MONTH
    assert P["devices"] == 2 and P["credit"] == 200.0 and P["device_months"] == round(months_all, 1)
    assert P["gross"] == round(1500 + 13 + 55 + round(2.5 * months_all, 2) + round(1.5 * months_all, 2) + 130 + 6, 2) == 1848.56
    assert abs(P["gross"] - (755.99 + 1092.56)) <= 0.01
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
    assert L["warehouse"]["total"] == round(3 * 0.04 + 300 * 3 * 0.08 / 365, 2)
    assert L["eol"]["components"][0]["total"] is None and L["eol"]["total"] == 0.0     # nothing sold, nothing recycled
    assert tab["unmeasured"] == []
    assert tab["gross"] == round(300 + 5 + L["warehouse"]["total"], 2) and tab["net"] == tab["gross"]


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
    assert body["cohorts"]["finished"]["classes"][0]["per_month"]["net"] == 24.18
    assert body["cohorts"]["fleet"]["portfolio"]["devices"] == 7
    assert client.anon().get("/api/v1/tco/devices").status_code in (401, 403)


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
    assert fin["portfolio"]["devices"] == 32 and fin["portfolio"]["unmeasured"] == ["warehouse"]
    assert 0 < fin["portfolio"]["resale"]["credit_share_of_acquisition"] < 1

    # a fleet from before the events counts as stale, so the boot rebuilds it instead of showing an empty cost tab
    db_session.query(ServiceEvent).delete()
    db_session.flush()
    assert "service event" in (dataset_is_stale(db_session) or "")
