"""The rented fleet by customer, the month grid of what lands at the warehouse, and the data stream.

One small fleet with every case the reads must keep apart: an overdue device (counted once,
never again in the current month), a contract ending inside 90 days and one after, one after
the 12-month grid, a rented device without a customer, a customer without devices, a
manufacturer that is not a supplier (not a customer), open, late, undated, later and closed
order lines with a partial receipt, and the defect, repair and write-off rows the model holds.
"""
from __future__ import annotations

import json
import os
import stat
from datetime import date, timedelta

import pytest

from app.models.catalog import Organization, Product
from app.models.flow import Asset, AssetStatus, Location, LocationType, Receipt, ReceiptItem
from app.models.procurement import OrderItem, OrderStatus, PurchaseOrder
from app.models.rental import ContractStatus, RentalContract
from app.models.tco import ServiceEvent, ServiceKind
from app.services import fleet, rented_fleet
from app.services.exceptions import NotFoundError

TODAY = date(2026, 9, 22)
FIRST_BRIEF_KEYS = {"stream", "source_system", "owner_role", "stage", "as_of", "generated_at", "definition", "totals", "rows"}
ROW_KEYS = {"customer_code", "customer", "since", "devices_at_customer", "contracts_active", "contracts_ending_90d",
            "returns_overdue", "by_category", "returns_by_month"}
# what in the stream is not a figure: the envelope, the two containers (their keys are checked one by one), the
# explanations, and a row's name and code
STREAM_META = FIRST_BRIEF_KEYS | {"basis", "omissions"}
ROW_IDENTITY = {"customer_code", "customer"}


def _save(db, obj):
    db.add(obj)
    db.flush()
    return obj


def _world(db, today: date):
    wh = _save(db, Location(code="WH-T", name="WH", location_type=LocationType.WAREHOUSE, capacity=500))
    a = _save(db, Organization(code="CUST-A", name="Customer A (role-only)", is_supplier=False))
    b = _save(db, Organization(code="CUST-B", name="Customer B (role-only)", is_supplier=False))
    _save(db, Organization(code="CUST-C", name="Customer C (role-only)", is_supplier=False))
    _save(db, Organization(code="MFR-T", name="Manufacturer T", is_supplier=False, is_manufacturer=True))
    sup = _save(db, Organization(code="SUP-T", name="Supplier T (role-only)", is_supplier=True))
    phone = _save(db, Product(product_code="PH-1", name="Phone 1", category="Smartphone"))
    laptop = _save(db, Product(product_code="LT-1", name="Laptop 1", category="Laptop"))
    n = [0]

    def rented(prod, cust, start, planned_end):
        n[0] += 1
        asset = _save(db, Asset(serial_number=f"R-{n[0]}", product_id=prod.id, status=AssetStatus.RENTED, cycle_no=1,
                                customer_id=(cust.id if cust else None), deployed_date=start, status_since=start))
        if cust:
            _save(db, RentalContract(asset_id=asset.id, product_id=prod.id, customer_id=cust.id, cycle_no=1, start_date=start,
                                     term_months=12, planned_end=planned_end, status=ContractStatus.RUNNING))
        return asset

    rented(phone, a, today - timedelta(days=355), today + timedelta(days=10))     # ending in 90 days, next month
    rented(phone, a, today - timedelta(days=370), today - timedelta(days=5))      # overdue: still out after the end
    rented(phone, a, today - timedelta(days=165), today + timedelta(days=200))    # inside the grid, after 90 days
    rented(laptop, a, today - timedelta(days=330), today + timedelta(days=400))   # after the 12-month grid
    rented(phone, b, today - timedelta(days=100), today + timedelta(days=50))     # ending in 90 days
    rented(phone, None, today - timedelta(days=20), None)                         # rented, no customer, no contract

    # history: CUST-A's first contract, ended for a defect; that device is in repair now
    rep = _save(db, Asset(serial_number="X-REP", product_id=phone.id, status=AssetStatus.REPAIR, cycle_no=1,
                          current_location_id=wh.id, status_since=today - timedelta(days=3)))
    _save(db, RentalContract(asset_id=rep.id, product_id=phone.id, customer_id=a.id, cycle_no=1, start_date=today - timedelta(days=900),
                             term_months=36, planned_end=today + timedelta(days=200), actual_end=today - timedelta(days=30),
                             end_reason="defect", status=ContractStatus.ENDED))
    _save(db, Asset(serial_number="X-RCY", product_id=laptop.id, status=AssetStatus.RECYCLED, cycle_no=2,
                    sold_date=today - timedelta(days=40)))
    _save(db, ServiceEvent(asset_id=rep.id, product_id=phone.id, kind=ServiceKind.REPAIR, cycle_no=2,
                           event_date=today - timedelta(days=60), cost=80))

    # the inbound side
    def po(number, status):
        return _save(db, PurchaseOrder(order_number=number, status=status, supplier_id=sup.id, destination_id=wh.id,
                                       date_ordered=today - timedelta(days=20)))

    def line(order, prod, qty, eta):
        return _save(db, OrderItem(order_id=order.id, product_id=prod.id, quantity=qty, estimated_delivery_date=eta))

    open1 = po("PO-1", OrderStatus.PLACED)
    partly = line(open1, phone, 100, today + timedelta(days=15))     # 20 received, 80 outstanding
    line(open1, laptop, 50, today - timedelta(days=2))               # late
    line(open1, phone, 30, None)                                     # no delivery date
    line(po("PO-2", OrderStatus.PENDING), phone, 40, today + timedelta(days=500))   # after the grid
    line(po("PO-DONE", OrderStatus.RECEIVED), phone, 999, today + timedelta(days=20))  # closed: not inbound
    rec = _save(db, Receipt(purchase_order_id=open1.id, received_at_id=wh.id, receipt_date=today))
    _save(db, ReceiptItem(receipt_id=rec.id, order_item_id=partly.id, quantity_received=20))
    db.flush()


def _months_after(n: int) -> str:
    y, m = divmod(TODAY.month - 1 + n, 12)
    return f"{TODAY.year + y:04d}-{m + 1:02d}"


# ---------------------------------------------------------------------------
# per customer


def test_customers_counts_sorting_and_definition(db_session):
    _world(db_session, TODAY)
    C = rented_fleet.customers(db_session, TODAY)
    codes = [r["customer_code"] for r in C["rows"]]
    assert codes == ["CUST-A", "CUST-B", "CUST-C"], "most devices first; suppliers and manufacturers are not customers"
    A, B, Cc = C["rows"]
    assert A["devices_at_customer"] == 4 and A["contracts_active"] == 4
    assert A["contracts_ending_90d"] == 1, "the overdue contract and the ones beyond 90 days are not 'ending in 90 days'"
    assert A["returns_overdue"] == 1
    assert A["by_category"] == {"Smartphone": 3, "Laptop": 1}
    assert A["since"] == TODAY - timedelta(days=900), "the first contract, ended ones included"
    assert B["devices_at_customer"] == 1 and B["contracts_ending_90d"] == 1 and B["since"] == TODAY - timedelta(days=100)
    assert Cc["devices_at_customer"] == 0 and Cc["since"] is None and Cc["by_category"] == {}
    t = C["totals"]
    assert t["customers"] == 3 and t["customers_holding_devices"] == 2
    assert t["devices_at_customer"] == 5 and t["rented_total"] == 6 and t["devices_without_customer"] == 1
    assert t["contracts_active"] == 5 and t["contracts_ending_90d"] == 2 and t["returns_overdue"] == 1
    assert "RENTED" in C["definition"]


def test_returns_by_month_per_customer_counts_the_overdue_once(db_session):
    _world(db_session, TODAY)
    A = rented_fleet.customers(db_session, TODAY)["rows"][0]
    months = A["returns_by_month"]
    assert list(months) == rented_fleet.month_keys(TODAY) and len(months) == 12
    assert months[_months_after(0)] == 0, "the overdue device is not due again this month"
    assert months[_months_after(1)] == 1 and months[_months_after(7)] == 1
    assert sum(months.values()) == 2, "the laptop ends after the grid"


def test_customer_detail_by_model_and_contracts_ending(db_session):
    _world(db_session, TODAY)
    d = rented_fleet.customer_detail(db_session, "CUST-A", TODAY)
    assert [(m["product_code"], m["devices"]) for m in d["by_model"]] == [("PH-1", 3), ("LT-1", 1)]
    assert len(d["contracts_ending"]) == 1 and d["contracts_ending"][0]["days_left"] == 10
    sched = {m["month"]: m for m in d["returns_schedule"]}
    assert sched[_months_after(0)]["devices"] == 0 and sched[_months_after(1)]["by_category"] == {"Smartphone": 1}
    assert sched[_months_after(2)]["devices"] == 0, "CUST-B's contract ends in that month; it is not CUST-A's"
    for code in ("NOPE", "SUP-T", "MFR-T"):
        with pytest.raises(NotFoundError):
            rented_fleet.customer_detail(db_session, code, TODAY)


def test_customer_detail_lists_the_soonest_contracts_up_to_the_limit(db_session):
    _world(db_session, TODAY)
    a = db_session.query(Organization).filter_by(code="CUST-A").one()
    phone = db_session.query(Product).filter_by(product_code="PH-1").one()
    later = _save(db_session, Asset(serial_number="R-LATER", product_id=phone.id, status=AssetStatus.RENTED, cycle_no=1,
                                    customer_id=a.id, deployed_date=TODAY - timedelta(days=340), status_since=TODAY - timedelta(days=340)))
    _save(db_session, RentalContract(asset_id=later.id, product_id=phone.id, customer_id=a.id, cycle_no=1,
                                     start_date=TODAY - timedelta(days=340), term_months=12, planned_end=TODAY + timedelta(days=20),
                                     status=ContractStatus.RUNNING))
    db_session.flush()
    d = rented_fleet.customer_detail(db_session, "CUST-A", TODAY, limit=1)
    assert d["contracts_ending_90d"] == 2, "the count is the full one"
    assert d["contracts_ending_shown"] == 1 and d["contracts_ending_limit"] == 1
    assert d["contracts_ending"][0]["days_left"] == 10, "soonest first"


def test_return_calendar_narrows_to_one_customer(db_session):
    """The customer filter fleet.return_calendar gained: each customer's calendar holds its own contracts only."""
    _world(db_session, TODAY)
    ids = {o.code: o.id for o in db_session.query(Organization)}

    def cal(**kw):
        return {m["month"]: m["total"] for m in fleet.return_calendar(db_session, today=TODAY, months=12, **kw)}

    whole, a, b = cal(), cal(customer_id=ids["CUST-A"]), cal(customer_id=ids["CUST-B"])
    assert whole[_months_after(2)] == 1 and b[_months_after(2)] == 1 and a[_months_after(2)] == 0
    assert a[_months_after(0)] == 1 and b[_months_after(0)] == 0, "the overdue device is CUST-A's"
    assert sum(a.values()) == 3 and sum(b.values()) == 1 and sum(whole.values()) == 4
    assert sum(cal(customer_id=ids["CUST-C"]).values()) == 0, "a customer without contracts has an empty calendar"


def test_boundaries_of_overdue_the_90_days_and_the_grid(db_session):
    """A contract ending today is due, not overdue; day 90 is inside the window, day 91 is not; the last day of the
    grid's last month is in the grid, the first day after it is after it."""
    cust = _save(db_session, Organization(code="CUST-Z", name="Customer Z (role-only)", is_supplier=False))
    phone = _save(db_session, Product(product_code="PH-Z", name="Phone Z", category="Smartphone"))
    after_grid = date(2027, 9, 1)               # the first day after twelve months from September 2026
    ends = [TODAY, TODAY + timedelta(days=90), TODAY + timedelta(days=91), after_grid - timedelta(days=1), after_grid]
    start = TODAY - timedelta(days=30)
    for k, end in enumerate(ends):
        asset = _save(db_session, Asset(serial_number=f"B-{k}", product_id=phone.id, status=AssetStatus.RENTED, cycle_no=1,
                                        customer_id=cust.id, deployed_date=start, status_since=start))
        _save(db_session, RentalContract(asset_id=asset.id, product_id=phone.id, customer_id=cust.id, cycle_no=1, start_date=start,
                                         term_months=12, planned_end=end, status=ContractStatus.RUNNING))
    db_session.flush()
    row = rented_fleet.customers(db_session, TODAY)["rows"][0]
    assert row["returns_overdue"] == 0, "ending today is not overdue yet"
    assert row["contracts_ending_90d"] == 2, "today and day 90 are inside the window, day 91 is not"
    R = rented_fleet.returns(db_session, TODAY)
    by = {m["month"]: m["devices"] for m in R["months"]}
    assert R["overdue"]["devices"] == 0
    assert by[_months_after(0)] == 1, "ending today: the current month"
    assert by[_months_after(3)] == 2 and by[_months_after(11)] == 1, "the last day of the grid is in the grid"
    assert R["after_window"]["devices"] == 1, "the first day after the grid is after it"
    assert row["returns_by_month"][_months_after(11)] == 1 and sum(row["returns_by_month"].values()) == 4


# ---------------------------------------------------------------------------
# the month grid


def test_returns_partition_the_running_contracts(db_session):
    _world(db_session, TODAY)
    R = rented_fleet.returns(db_session, TODAY)
    by = {m["month"]: m["devices"] for m in R["months"]}
    assert R["overdue"] == {"devices": 1, "by_category": {"Smartphone": 1}}
    assert by[_months_after(0)] == 0 and by[_months_after(1)] == 1 and by[_months_after(2)] == 1 and by[_months_after(7)] == 1
    assert R["after_window"]["devices"] == 1
    assert R["overdue"]["devices"] + sum(by.values()) + R["after_window"]["devices"] == 5, "every running contract exactly once"


def test_return_calendar_unchanged_without_the_new_arguments(db_session):
    """The Returns tab's calendar still counts from the first of the month: the overdue device sits in this month."""
    _world(db_session, TODAY)
    cal = fleet.return_calendar(db_session, today=TODAY, months=12)
    assert cal[0]["month"] == _months_after(0) and cal[0]["total"] == 1
    assert sum(m["total"] for m in cal) == 4


def test_inbound_by_month_with_late_undated_and_later_apart(db_session):
    _world(db_session, TODAY)
    I = rented_fleet.inbound(db_session, TODAY)  # noqa: E741
    by = {m["month"]: m for m in I["months"]}
    assert by[_months_after(1)]["units"] == 80 and by[_months_after(1)]["by_category"] == {"Smartphone": 80}, "ordered minus received"
    assert I["late"] == {"units": 50, "by_category": {"Laptop": 50}}
    assert I["no_eta"]["units"] == 30 and I["after_window"]["units"] == 40
    assert I["total"] == 200 and I["lines"] == 4, "the received order is not inbound"
    assert sum(m["units"] for m in I["months"]) + 50 + 30 + 40 == I["total"]


def test_defects_by_month_from_what_the_model_records(db_session):
    _world(db_session, TODAY)
    D = rented_fleet.defects(db_session, TODAY)
    assert [m["month"] for m in D["months"]] == rented_fleet.past_month_keys(TODAY)
    by = {m["month"]: m for m in D["months"]}
    assert by[_months_after(-1)]["reported"] == 1 and by[_months_after(-1)]["written_off"] == 1
    assert by[_months_after(0)]["in_repair"] == 1 and D["in_repair_now"] == 1
    assert by[_months_after(-2)]["repairs_invoiced"] == 1
    assert any("replacements_consumed" in o for o in D["omissions"]), "what the model cannot tell is named"
    assert any(o.startswith("defects[].reported") and "property of the simulated data, not of the business" in o
               for o in D["omissions"]), "the seeded defect history is named as the seed's, not the business's"
    assert any(o.startswith("defects[].repairs_invoiced") and "at least eight days before" in o
               and "property of the simulated data, not of the business" in o for o in D["omissions"]), \
        "the seed's missing newest repair invoices are named as the seed's"
    assert "property of the simulated data" in D["basis"] and "Repairs invoiced: in the seeded data" in D["basis"]
    assert "The last month is the current one, up to as_of" in D["basis"]


def test_defects_count_only_defects_repairs_and_write_offs(db_session):
    """A counter-example beside every filter of defects(): planned and early ends, a refurbishment invoice, a sold
    device and a defect before the window do not count; a swap does; devices in repair reconcile with their months."""
    _world(db_session, TODAY)
    a = db_session.query(Organization).filter_by(code="CUST-A").one()
    phone = db_session.query(Product).filter_by(product_code="PH-1").one()
    last_month = TODAY - timedelta(days=30)                  # the month of the world's own defect end
    long_ago = TODAY - timedelta(days=400)                   # before the 12-month window

    def ended(serial, reason, on):
        asset = _save(db_session, Asset(serial_number=serial, product_id=phone.id, status=AssetStatus.READY_SECOND,
                                        cycle_no=1, status_since=on))
        _save(db_session, RentalContract(asset_id=asset.id, product_id=phone.id, customer_id=a.id, cycle_no=1,
                                         start_date=on - timedelta(days=300), term_months=12, planned_end=on, actual_end=on,
                                         end_reason=reason, status=ContractStatus.ENDED))
        return asset

    ended("N-PLAN", "planned", last_month)
    ended("N-EARLY", "early", last_month)
    swap = ended("N-SWAP", "swap", last_month)
    ended("N-OLD", "defect", long_ago)
    _save(db_session, ServiceEvent(asset_id=swap.id, product_id=phone.id, kind=ServiceKind.REFURB, cycle_no=1,
                                   event_date=last_month, cost=40))
    _save(db_session, Asset(serial_number="N-SOLD", product_id=phone.id, status=AssetStatus.SOLD, cycle_no=2,
                            sold_date=last_month, sale_price=100))
    _save(db_session, Asset(serial_number="N-REP-OLD", product_id=phone.id, status=AssetStatus.REPAIR, cycle_no=1, status_since=long_ago))
    _save(db_session, Asset(serial_number="N-REP-UNDATED", product_id=phone.id, status=AssetStatus.REPAIR, cycle_no=1, status_since=None))
    db_session.flush()

    D = rented_fleet.defects(db_session, TODAY)
    by = {m["month"]: m for m in D["months"]}
    prev = by[_months_after(-1)]
    assert prev["reported"] == 2, "the world's defect and the swap; planned and early ends are no defects"
    assert sum(m["reported"] for m in D["months"]) == 2, "the defect before the window is not in it"
    assert prev["repairs_invoiced"] == 0 and by[_months_after(-2)]["repairs_invoiced"] == 1, "a refurbishment is no repair"
    assert prev["written_off"] == 1, "a sold device is not written off; the recycled one is"
    assert D["in_repair_now"] == 3, "every device in repair, whatever sent it there"
    assert D["in_repair_before_window"] == 1 and D["in_repair_undated"] == 1
    assert sum(m["in_repair"] for m in D["months"]) + D["in_repair_before_window"] + D["in_repair_undated"] == D["in_repair_now"]


def test_overview_reconciles(db_session):
    _world(db_session, TODAY)
    ov = rented_fleet.overview(db_session, TODAY)
    assert ov["returns_overdue"] == 1 and ov["returns_overdue_by_category"] == {"Smartphone": 1}
    assert ov["returns_after_window"] == 1
    assert ov["inbound_open_total"] == 200 and ov["inbound_late"]["units"] == 50
    g = {r["month"]: r for r in ov["grid"]}
    assert g[_months_after(1)]["landing"] == g[_months_after(1)]["returns"] + g[_months_after(1)]["inbound"] == 81
    landing = ov["basis"]["landing"]
    assert "not stock that covers demand" in landing and "ordering mask" in landing and "capacity plan" in landing, \
        "landing says it is no cover, and which model counts the returns"
    assert "order less" not in landing and "recommends" not in landing, "a fact, not advice on how much to order"
    assert not {"rented_total", "customers", "customers_holding_devices", "contracts_active", "contracts_ending_90d",
                "devices_without_customer"} & set(ov), "the headline counts are the customers table's totals, counted once"


def test_one_key_carries_one_type(db_session):
    """returns_overdue and returns_after_window are device counts wherever they appear."""
    _world(db_session, TODAY)
    ov = rented_fleet.overview(db_session, TODAY)
    C = rented_fleet.customers(db_session, TODAY)
    d = rented_fleet.customer_detail(db_session, "CUST-A", TODAY)
    s = rented_fleet.stream(db_session, TODAY)
    for v in (ov["returns_overdue"], C["totals"]["returns_overdue"], C["rows"][0]["returns_overdue"], d["returns_overdue"],
              s["totals"]["returns_overdue"], s["rows"][0]["returns_overdue"], s["returns_overdue_total"],
              ov["returns_after_window"], s["returns_after_window"]):
        assert type(v) is int
    assert C["totals"]["contracts_ending_90d"] == sum(r["contracts_ending_90d"] for r in C["rows"]), \
        "the headline the page shows is the column's sum"


def test_the_tiles_count_the_customers_the_grid_every_running_contract(db_session):
    """An organisation flagged as a supplier can hold a running contract (written outside the app, or its flags changed
    after it rented). The customers table and the tiles above it leave it out; the month grid, the Returns tab
    (fleet.summary) and the stream's whole-fleet figures count it. The page shows the overdue difference on the tile."""
    _world(db_session, TODAY)
    sup = db_session.query(Organization).filter_by(code="SUP-T").one()
    phone = db_session.query(Product).filter_by(product_code="PH-1").one()
    start = TODAY - timedelta(days=300)
    for k, end in enumerate((TODAY - timedelta(days=7), TODAY + timedelta(days=30))):   # one overdue, one ending in 90 days
        asset = _save(db_session, Asset(serial_number=f"S-{k}", product_id=phone.id, status=AssetStatus.RENTED, cycle_no=1,
                                        customer_id=sup.id, deployed_date=start, status_since=start))
        _save(db_session, RentalContract(asset_id=asset.id, product_id=phone.id, customer_id=sup.id, cycle_no=1,
                                         start_date=start, term_months=12, planned_end=end, status=ContractStatus.RUNNING))
    db_session.flush()

    t = rented_fleet.customers(db_session, TODAY)["totals"]
    assert (t["returns_overdue"], t["contracts_ending_90d"], t["contracts_active"]) == (1, 2, 5), "the tiles: listed customers only"
    assert (t["rented_total"], t["devices_at_customer"], t["devices_without_customer"]) == (8, 5, 3), \
        "rented out now is every RENTED device; the difference is shown beside it"
    ov = rented_fleet.overview(db_session, TODAY)
    assert ov["returns_overdue"] == 2, "the grid's overdue row: every running contract"
    assert ov["returns_overdue"] - t["returns_overdue"] == 1, "the difference the overdue tile shows"
    F = fleet.summary(db_session, today=TODAY)
    assert (F["returns_overdue"], F["returns_due_90d"]) == (2, 3), "the Returns tab counts every running contract too"
    s = rented_fleet.stream(db_session, TODAY)
    assert (s["totals"]["returns_overdue"], s["returns_overdue_total"]) == (1, 2)
    assert s["rented_total"] == 8 and s["totals"]["devices_at_customer"] == 5


# ---------------------------------------------------------------------------
# the data stream


def test_stream_shape_keeps_the_first_brief_and_adds_the_grid(db_session):
    _world(db_session, TODAY)
    s = rented_fleet.stream(db_session, TODAY)
    json.dumps(s)                                   # plain JSON types only
    assert FIRST_BRIEF_KEYS <= set(s)
    assert (s["stream"], s["source_system"], s["owner_role"], s["stage"], s["as_of"]) == \
        ("devices_by_customer", "SCM-Master", "Fleet Operations", 1, TODAY.isoformat())
    assert s["totals"] == {"customers": 3, "devices_at_customer": 5, "contracts_ending_90d": 2, "returns_overdue": 1}
    assert all(set(r) == ROW_KEYS for r in s["rows"])
    assert s["rows"][0]["since"] == (TODAY - timedelta(days=900)).isoformat() and s["rows"][2]["since"] is None
    assert s["rented_total"] == 6 and s["returns_overdue_total"] == 1
    assert len(s["returns_schedule"]) == 12 and set(s["returns_schedule"][0]) == {"month", "devices", "by_category"}
    assert len(s["inbound_open_pos"]) == 12 and set(s["inbound_open_pos"][0]) == {"month", "units", "by_category"}
    assert set(s["defects"][0]) >= {"month", "reported", "in_repair", "written_off"}
    assert "defects_in_repair_now" not in s, "devices in repair are not a defect count"
    assert (s["in_repair_now"], s["in_repair_before_window"], s["in_repair_undated"]) == (1, 0, 0)
    assert sum(m["in_repair"] for m in s["defects"]) + s["in_repair_before_window"] + s["in_repair_undated"] == s["in_repair_now"]
    assert s["omissions"], "omitted figures are listed"
    assert any("property of the simulated data" in o for o in s["omissions"])
    figures = (set(s) - STREAM_META) | set(s["totals"]) | (ROW_KEYS - ROW_IDENTITY)
    assert "customers" in figures and "rented_total" in figures and "in_repair_undated" in figures
    assert figures <= set(s["basis"]), f"every figure says what it counts; no basis for {sorted(figures - set(s['basis']))}"


def test_export_cli_writes_the_file(db_session, monkeypatch, tmp_path):
    from app import export_stream

    _world(db_session, date.today())
    monkeypatch.setattr(export_stream, "SessionLocal", lambda: db_session)
    monkeypatch.setattr(db_session, "close", lambda: None)
    out = tmp_path / "exports" / "devices_by_customer.json"
    out.parent.mkdir()
    out.write_text("an older sample", encoding="utf-8")
    assert export_stream.main(["--out", str(out)]) == 0
    raw = out.read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf"), "no byte-order mark"
    data = json.loads(raw.decode("utf-8"))
    assert data["stream"] == "devices_by_customer" and data["totals"]["devices_at_customer"] == 5
    assert [p.name for p in out.parent.iterdir()] == [out.name], "the temporary file was moved over the target, none left"


def test_export_refuses_a_database_without_a_rented_fleet_or_a_customer(db_session, monkeypatch, tmp_path):
    """Nothing is written over the sample when there is no stream to give."""
    from app import export_stream

    monkeypatch.setattr(export_stream, "SessionLocal", lambda: db_session)
    monkeypatch.setattr(db_session, "close", lambda: None)
    out = tmp_path / "devices_by_customer.json"
    out.write_text("the committed sample", encoding="utf-8")

    # an empty database is the datacenter scenario: no rented fleet
    assert export_stream.main(["--out", str(out)]) == export_stream.EXIT_REFUSED == 3
    with pytest.raises(export_stream.ExportRefused, match="no rented fleet"):
        export_stream.export(str(out))
    assert out.read_text(encoding="utf-8") == "the committed sample"

    # a rented device, but every organisation is a supplier or a manufacturer: no customer, no rows
    phone = _save(db_session, Product(product_code="PH-9", name="Phone 9", category="Smartphone"))
    _save(db_session, Organization(code="SUP-9", name="Supplier 9 (role-only)", is_supplier=True))
    _save(db_session, Organization(code="MFR-9", name="Manufacturer 9", is_supplier=False, is_manufacturer=True))
    _save(db_session, Asset(serial_number="R-9", product_id=phone.id, status=AssetStatus.RENTED, cycle_no=1))
    db_session.flush()
    assert export_stream.main(["--out", str(out)]) == 3
    with pytest.raises(export_stream.ExportRefused, match="no customer"):
        export_stream.export(str(out))
    assert out.read_text(encoding="utf-8") == "the committed sample"
    assert [p.name for p in tmp_path.iterdir()] == [out.name], "no temporary file left behind"

    # bad arguments keep argparse's own code, so a caller can tell them from a refusal
    with pytest.raises(SystemExit) as bad:
        export_stream.main(["--months", "twelve"])
    assert bad.value.code == 2


def _export_world(db_session, monkeypatch):
    from app import export_stream

    _world(db_session, date.today())
    monkeypatch.setattr(export_stream, "SessionLocal", lambda: db_session)
    monkeypatch.setattr(db_session, "close", lambda: None)
    return export_stream


def test_export_writes_through_a_symlink(db_session, monkeypatch, tmp_path):
    """--out naming a symlink replaces the file it points to; the link stays a link."""
    export_stream = _export_world(db_session, monkeypatch)
    real = tmp_path / "real" / "devices_by_customer.json"
    real.parent.mkdir()
    real.write_text("an older sample", encoding="utf-8")
    link = tmp_path / "link.json"
    try:
        link.symlink_to(real)
    except (OSError, NotImplementedError):
        pytest.skip("this system does not let the test create a symlink")
    assert export_stream.main(["--out", str(link)]) == 0
    assert link.is_symlink(), "the link was not replaced by a file"
    assert json.loads(real.read_text(encoding="utf-8"))["stream"] == "devices_by_customer"
    assert sorted(p.name for p in real.parent.iterdir()) == [real.name], "no temporary file left beside the target"


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits; Windows keeps only a read-only flag")
def test_export_keeps_the_mode_of_the_file_it_replaces(db_session, monkeypatch, tmp_path):
    export_stream = _export_world(db_session, monkeypatch)
    kept, first = tmp_path / "kept.json", tmp_path / "first.json"
    kept.write_text("private", encoding="utf-8")
    kept.chmod(0o600)
    assert export_stream.main(["--out", str(kept)]) == 0 and export_stream.main(["--out", str(first)]) == 0
    assert stat.S_IMODE(kept.stat().st_mode) == 0o600, "a replaced file keeps its permission bits"
    assert stat.S_IMODE(first.stat().st_mode) == export_stream.NEW_FILE_MODE == 0o644, "a first export is readable to hand on"


# ---------------------------------------------------------------------------
# the API


def test_api_customers_detail_and_inflow(client, db_session):
    _world(db_session, date.today())
    r = client.get("/api/v1/fleet/customers")
    assert r.status_code == 200
    body = r.json()
    assert [x["customer_code"] for x in body["rows"]] == ["CUST-A", "CUST-B", "CUST-C"]
    assert body["totals"]["devices_at_customer"] == 5 and body["totals"]["returns_overdue"] == 1
    d = client.get("/api/v1/fleet/customers/CUST-A").json()
    assert d["devices_at_customer"] == 4 and len(d["by_model"]) == 2 and len(d["contracts_ending"]) == 1
    assert client.get("/api/v1/fleet/customers/NOPE").status_code == 404
    assert body["totals"]["rented_total"] == 6 and body["totals"]["contracts_ending_90d"] == 2
    o = client.get("/api/v1/fleet/inflow").json()
    assert o["returns_overdue"] == 1 and len(o["grid"]) == 12 and o["inbound_open_total"] == 200
    assert o["defects"]["in_repair_now"] == 1


def test_api_needs_a_login(client):
    anon = client.anon()
    for path in ("/api/v1/fleet/customers", "/api/v1/fleet/customers/CUST-A", "/api/v1/fleet/inflow"):
        assert anon.get(path).status_code == 401
