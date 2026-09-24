"""The DaaS scenario: rental states on the lifecycle, the fleet summary, the return calendar, the seed.

The datacenter tests stay untouched; this file only asserts what the second
scenario adds. The seed runs at a tiny scale against the test database.
"""
from __future__ import annotations

from datetime import date, timedelta

import pytest

from app.models.catalog import Organization, Product
from app.models.flow import WAREHOUSE_STATUSES, Asset, AssetStatus, Location, LocationType
from app.models.rental import ContractStatus, RentalContract
from app.services import fleet, lifecycle, warehouse
from app.services.exceptions import ValidationError

TODAY = date(2026, 9, 22)


def _save(db, obj):
    db.add(obj)
    db.flush()
    return obj


def _fleet(db):
    wh = _save(db, Location(code="WH", name="WH", location_type=LocationType.WAREHOUSE, capacity=500))
    cust = _save(db, Organization(code="CUST-T", name="Customer T (role-only)", is_supplier=False))
    prod = _save(db, Product(product_code="PH-1", name="Phone 1", category="Smartphone"))
    rented = []
    for i in range(6):
        a = _save(db, Asset(serial_number=f"R-{i}", product_id=prod.id, status=AssetStatus.RENTED, cycle_no=(2 if i < 2 else 1),
                            customer_id=cust.id, received_date=TODAY - timedelta(days=400), deployed_date=TODAY - timedelta(days=300),
                            status_since=TODAY - timedelta(days=300)))
        db.add(RentalContract(asset_id=a.id, customer_id=cust.id, cycle_no=a.cycle_no, start_date=TODAY - timedelta(days=300), term_months=12,
                              planned_end=TODAY + timedelta(days=10 + 40 * i), status=ContractStatus.RUNNING))
        rented.append(a)
    for i, st in enumerate((AssetStatus.MDM_RELEASE, AssetStatus.MDM_RELEASE, AssetStatus.SELLABLE, AssetStatus.SWAP_BUFFER)):
        _save(db, Asset(serial_number=f"W-{i}", product_id=prod.id, status=st, cycle_no=1, current_location_id=wh.id,
                        received_date=TODAY - timedelta(days=500), status_since=TODAY - timedelta(days=(30 if i == 0 else 5))))
    _save(db, Asset(serial_number="S-0", product_id=prod.id, status=AssetStatus.SOLD, cycle_no=2, sold_date=TODAY - timedelta(days=20),
                    sale_price=200.0, sale_channel="marketplace", received_date=TODAY - timedelta(days=900)))
    db.flush()
    return prod


def test_lifecycle_allows_the_cycle_and_refuses_shortcuts():
    assert lifecycle.can_transition(AssetStatus.IN_STORAGE, AssetStatus.RENTED)
    assert lifecycle.can_transition(AssetStatus.RENTED, AssetStatus.RETURNED)
    assert lifecycle.can_transition(AssetStatus.RETURNED, AssetStatus.MDM_RELEASE)
    assert lifecycle.can_transition(AssetStatus.WIPE_GRADING, AssetStatus.REPAIR)
    assert lifecycle.can_transition(AssetStatus.REFURB, AssetStatus.READY_SECOND)
    assert lifecycle.can_transition(AssetStatus.READY_SECOND, AssetStatus.RENTED)
    assert not lifecycle.can_transition(AssetStatus.REFURB, AssetStatus.RENTED)      # second-life stock is its own compartment, never skipped
    assert lifecycle.can_transition(AssetStatus.SELLABLE, AssetStatus.SOLD)
    assert not lifecycle.can_transition(AssetStatus.RENTED, AssetStatus.SOLD)          # a rented device is not sold from the customer
    assert not lifecycle.can_transition(AssetStatus.RETURNED, AssetStatus.RENTED)      # no re-rental without wipe and grading
    with pytest.raises(ValidationError):
        lifecycle.assert_transition(AssetStatus.SOLD, AssetStatus.RENTED)
    # the datacenter flow is unchanged
    assert lifecycle.can_transition(AssetStatus.RECEIVED, AssetStatus.DEPLOYED)
    assert not lifecycle.can_transition(AssetStatus.DISPOSED, AssetStatus.RECEIVED)


def test_scenario_is_detected_not_configured(db_session):
    assert fleet.scenario(db_session) == "datacenter"
    _fleet(db_session)
    assert fleet.scenario(db_session) == "daas"


def test_summary_counts_and_stations(db_session):
    _fleet(db_session)
    s = fleet.summary(db_session, today=TODAY)
    assert s["rented"] == 6 and s["rented_cycle2"] == 2
    assert s["warehouse"] == 4
    assert s["returns_due_30d"] == 1 and s["returns_due_90d"] == 3
    assert s["sold_12m"] == 1 and s["sold_12m_eur"] == 200.0
    mdm = next(x for x in s["stations"] if x["status"] == "MDM_RELEASE")
    assert mdm["count"] == 2 and mdm["over_sla"] == 1
    assert set(s["by_status"]) >= {"RENTED", "MDM_RELEASE", "SELLABLE", "SWAP_BUFFER", "SOLD"}


def test_return_calendar_covers_every_running_contract(db_session):
    _fleet(db_session)
    cal = fleet.return_calendar(db_session, today=TODAY, months=12)
    assert sum(m["total"] for m in cal) == 6
    assert sum(m["from_cycle2"] for m in cal) == 2
    first = next(m for m in cal if m["total"])
    assert first["second_rental"] + first["repair"] + first["sale"] + first["recycling"] >= first["total"] - 1   # rounded shares
    up = fleet.upcoming_returns(db_session, today=TODAY, days=30)
    assert len(up) == 1 and up[0]["cycle_no"] in (1, 2) and up[0]["customer"].endswith("(role-only)")


def test_api_fleet_endpoints(client, db_session):
    _fleet(db_session)
    assert client.get("/api/v1/fleet/summary").json()["scenario"] == "daas"
    assert len(client.get("/api/v1/fleet/returns/calendar?months=6").json()) == 6
    assert client.get("/api/v1/fleet/returns/upcoming?days=365").status_code == 200


def test_warehouse_statuses_cover_the_stations():
    for st in (AssetStatus.RETURNED, AssetStatus.MDM_RELEASE, AssetStatus.WIPE_GRADING, AssetStatus.REPAIR, AssetStatus.REFURB,
               AssetStatus.READY_SECOND, AssetStatus.SELLABLE, AssetStatus.SWAP_BUFFER, AssetStatus.IN_STORAGE, AssetStatus.RECEIVED):
        assert st in WAREHOUSE_STATUSES
    assert AssetStatus.RENTED not in WAREHOUSE_STATUSES and AssetStatus.SOLD not in WAREHOUSE_STATUSES


def test_daas_seed_small_scale(db_session, monkeypatch):
    """The generator at 1/1000 scale: counts match the instruction ratio, every serial has provenance, no company name."""
    import base64

    from app import seed_daas

    monkeypatch.setattr(seed_daas, "SessionLocal", lambda: db_session)
    monkeypatch.setattr(db_session, "close", lambda: None)
    monkeypatch.setattr(seed_daas, "N_RENTED", 300)
    monkeypatch.setattr(seed_daas, "N_WAREHOUSE", 100)
    monkeypatch.setattr(seed_daas, "N_SOLD_LAST_YEAR", 30)
    monkeypatch.setattr(seed_daas, "N_RECYCLED_LAST_YEAR", 2)
    seed_daas.seed_daas()
    assets = db_session.query(Asset).all()
    assert sum(1 for a in assets if a.status == AssetStatus.RENTED) == 300
    assert sum(1 for a in assets if a.status in WAREHOUSE_STATUSES) == 100
    second = [a for a in assets if a.status == AssetStatus.READY_SECOND]
    assert len(second) == round(100 * seed_daas.WAREHOUSE_MIX[AssetStatus.READY_SECOND]), "the second-life compartment is seeded, out of the 100"
    assert all(a.grade in ("A", "B") and a.cycle_no == 1 and a.current_location_id for a in second)
    assert {loc.code for loc in db_session.query(Location).all()} >= {c.code for c in warehouse.COMPARTMENTS}
    assert all(a.source_order_item_id for a in assets), "every serial traces to an order line"
    assert all(a.received_date and a.received_date >= date(2020, 1, 1) for a in assets)
    contracts = db_session.query(RentalContract).all()
    assert sum(1 for c in contracts if c.status == ContractStatus.RUNNING) == 300
    for c in contracts:
        assert c.planned_end > c.start_date
        if c.actual_end:
            assert c.actual_end >= c.start_date
    names = " ".join(o.name for o in db_session.query(Organization).all()).lower()
    for denied in ("RXZlcnBob25l", "R3JvdmVy"):
        assert base64.b64decode(denied).decode().lower() not in names
    assert fleet.scenario(db_session) == "daas"


def test_a_purchase_before_the_catalogue_lands_on_the_whole_first_generation():
    """Never on one model: the old fallback put 72,485 of the 76,492 smartphones bought in 2023 on one Fairphone 5
    (24.09.2026). The catalogue check refuses an opening too thin to spread a fleet over, before anything is written."""
    import random
    from collections import Counter

    from app import seed_daas

    gen = seed_daas.check_catalogue()
    assert set(gen) >= set(seed_daas.FAMILY_MIX) and all(len(gen[f]) >= seed_daas.FIRST_GENERATION_MIN for f in seed_daas.FAMILY_MIX)
    launch = {row[0]: row[4] for row in seed_daas.CATALOGUE}
    by_family = {f: [row[0] for row in seed_daas.CATALOGUE if row[2] == f] for f in seed_daas.FAMILY_MIX}
    rng = random.Random(7)
    for family in seed_daas.FAMILY_MIX:
        early = min(launch[c] for c in by_family[family]) - timedelta(days=400)
        picks = Counter(seed_daas._choose_product(rng, family, early, launch, by_family, gen) for _ in range(600))
        assert set(picks) == set(gen[family]) and max(picks.values()) < 600 * 0.6, family
        late = date(2025, 9, 1)
        picks = Counter(seed_daas._choose_product(rng, family, late, launch, by_family, gen) for _ in range(600))
        assert all(launch[c] <= late - timedelta(days=14) for c in picks), "once models are on sale, only those on sale are bought"
    thin = [("A-1", "One", "Smartphone", "Apple", date(2021, 1, 1), 100, "u"), ("A-2", "Two", "Smartphone", "Apple", date(2023, 1, 1), 100, "u"),
            ("T-1", "Tab", "Tablet", "Apple", date(2021, 1, 1), 100, "u"), ("T-2", "Tab 2", "Tablet", "Apple", date(2021, 6, 1), 100, "u"),
            ("L-1", "Lap", "Laptop", "Apple", date(2021, 1, 1), 100, "u"), ("L-2", "Lap 2", "Laptop", "Apple", date(2021, 6, 1), 100, "u")]
    with pytest.raises(ValueError, match="Smartphone"):
        seed_daas.check_catalogue(thin)
    with pytest.raises(ValueError, match="Laptop"):
        seed_daas.check_catalogue([row for row in thin if row[2] != "Laptop"])


def test_the_residual_curve_starts_below_one_and_never_rises():
    """The laptop line, run back to a new device, said 149 per cent of the launch price (24.09.2026)."""
    from app import seed_daas

    for family, (_a, _b, n, youngest, oldest) in seed_daas.RESIDUAL_CURVE.items():
        shares = [seed_daas._residual_share(family, m, "B") for m in range(0, 100)]
        assert shares[0] < 1.0 and all(x >= y for x, y in zip(shares, shares[1:])), family
        assert seed_daas._residual_share(family, 0, "A") < 1.0 and n >= 6 and youngest < oldest
    # below the youngest anchor the line holds; on the anchors it is the fit itself (the fit's own q_48 for laptops)
    assert seed_daas._residual_share("Laptop", 6, "B") == seed_daas._residual_share("Laptop", 30.2, "B")
    assert seed_daas._residual_share("Laptop", 48, "B") == pytest.approx(0.4947, abs=0.001)


def test_growth_is_read_from_the_contracts(db_session):
    """A scaling fleet has to be able to say how much it grew, and against what."""
    _fleet(db_session)
    g = fleet.growth(db_session, today=TODAY, months=24)
    assert len(g["by_month"]) == 24                      # exactly 24 calendar months, the current one last
    assert g["by_month"][-1]["month"] == "2026-09"
    assert all(set(m) == {"month", "first_rentals"} for m in g["by_month"])
    assert g["rented_now"] == 6                          # six running contracts
    # every first rental started 300 days ago, so a year ago none of them was running yet
    assert g["rented_12m_ago"] == 0
    assert g["growth_12m_pct"] is None, "no base to compare against must not become a fake percentage"
    assert g["added_12m"] == 6
    assert fleet.summary(db_session, today=TODAY)["growth"]["rented_now"] == 6


def test_growth_counts_a_year_ago_as_what_was_running_then(db_session):
    """The honest comparison includes the devices that have come back since."""
    prod = _fleet(db_session)
    cust = db_session.query(Organization).first()
    a = _save(db_session, Asset(serial_number="OLD-1", product_id=prod.id, status=AssetStatus.SELLABLE, cycle_no=1,
                                received_date=TODAY - timedelta(days=900)))
    db_session.add(RentalContract(asset_id=a.id, customer_id=cust.id, cycle_no=1,
                                  start_date=TODAY - timedelta(days=800), term_months=24,
                                  planned_end=TODAY - timedelta(days=100), actual_end=TODAY - timedelta(days=120),
                                  status=ContractStatus.ENDED))
    db_session.flush()
    g = fleet.growth(db_session, today=TODAY)
    assert g["rented_12m_ago"] == 1, "a contract that ran a year ago and has since ended still counts for then"
    assert g["rented_now"] == 6
    assert g["growth_12m_pct"] == 500.0
