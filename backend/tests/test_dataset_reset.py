"""The boot replaces a database that holds the wrong dataset.

This is the bug of 22.09.2026 in test form: the console had been rebuilt for the
device-as-a-service fleet, the code was deployed, and the screens still showed the
datacenter operation, because both seeders bail out on a populated catalog and nothing
removed what was there. What follows pins the three properties that fix has to have:
the dataset is read from the data, a mismatch is replaced, a match is left alone — and
logins survive either way.
"""
from __future__ import annotations

from datetime import date, timedelta

import pytest

from app.core.db import Base
from app.models.auth import Role, User
from app.models.catalog import Organization, Product
from app.models.flow import Asset, AssetStatus
from app.models.rental import ContractStatus, RentalContract
from app.seed_reset import current_dataset, reset_operational_data, wanted_dataset
from app.services.auth import ensure_user

TODAY = date(2026, 9, 22)


def _datacenter(db):
    prod = Product(product_code="SRV-1", name="Server", category="Compute")
    db.add(prod)
    db.flush()
    for i in range(3):
        db.add(Asset(serial_number=f"DC-{i}", product_id=prod.id, status=AssetStatus.DEPLOYED,
                     received_date=TODAY - timedelta(days=100), deployed_date=TODAY - timedelta(days=90)))
    db.flush()
    return prod


def _daas(db):
    prod = Product(product_code="PH-1", name="Phone", category="Smartphone")
    cust = Organization(code="CUST-X", name="Customer X (role-only)", is_supplier=False)
    db.add_all([prod, cust])
    db.flush()
    a = Asset(serial_number="DAAS-1", product_id=prod.id, status=AssetStatus.RENTED, cycle_no=1,
              customer_id=cust.id, received_date=TODAY - timedelta(days=200), deployed_date=TODAY - timedelta(days=190))
    db.add(a)
    db.flush()
    db.add(RentalContract(asset_id=a.id, customer_id=cust.id, cycle_no=1, start_date=TODAY - timedelta(days=190),
                          term_months=24, planned_end=TODAY + timedelta(days=500), status=ContractStatus.RUNNING))
    db.flush()
    return prod


def test_dataset_is_read_from_the_data(db_session):
    assert current_dataset(db_session) is None          # empty database
    _datacenter(db_session)
    assert current_dataset(db_session) == "datacenter"
    _daas(db_session)
    assert current_dataset(db_session) == "daas"         # one rented device makes it a fleet


def test_wanted_dataset_defaults_to_the_fleet(monkeypatch):
    monkeypatch.delenv("SCM_SCENARIO", raising=False)
    assert wanted_dataset() == "daas"
    monkeypatch.setenv("SCM_SCENARIO", "DataCenter")
    assert wanted_dataset() == "datacenter"


def test_reset_empties_the_operation_but_keeps_the_logins(db_session):
    ensure_user(db_session, email="admin@example.com", full_name="Admin", password="admin", role=Role.ADMIN)  # nosec B106
    _datacenter(db_session)
    db_session.flush()
    removed = reset_operational_data(db_session)
    assert removed["asset"] == 3 and removed["product"] == 1
    assert current_dataset(db_session) is None
    assert db_session.query(Product).count() == 0
    assert db_session.query(User).count() == 1, "a reset must never lock anyone out of the demo"


def test_reset_covers_every_table_except_the_ones_it_promises_to_keep():
    """No operational table may quietly escape the reset — that is how stale data survives."""
    from app.seed_reset import KEEP_TABLES

    names = {t.name for t in Base.metadata.sorted_tables}
    assert KEEP_TABLES <= names | {"alembic_version"}
    assert "app_user" in KEEP_TABLES
    for table in ("asset", "rental_contract", "purchase_order", "order_item", "product", "kpi_snapshot"):
        assert table in names and table not in KEEP_TABLES


def test_reset_refuses_in_production(db_session, monkeypatch):
    from app.core import config
    from app.core.safety import ProductionSafetyError

    monkeypatch.setattr(config, "is_production", lambda: True)
    monkeypatch.setattr("app.core.safety.is_production", lambda: True)
    _datacenter(db_session)
    with pytest.raises(ProductionSafetyError):
        reset_operational_data(db_session)
    assert db_session.query(Asset).count() == 3, "production data is forge-locked"


def test_a_fleet_from_before_a_new_compartment_counts_as_stale(db_session):
    """Right kind of dataset, wrong generation: the boot has to notice and rebuild.

    A demo whose data predates a compartment shows that compartment correct and empty,
    which reads as a broken feature rather than as old data.
    """
    from app.models.flow import Location, LocationType
    from app.seed_reset import dataset_is_stale
    from app.services import warehouse

    _daas(db_session)
    reason = dataset_is_stale(db_session)
    assert reason and "ST-" in reason, "a fleet with no compartments at all is stale"

    for c in warehouse.COMPARTMENTS:
        db_session.add(Location(code=c.code, name=c.name, location_type=LocationType.WAREHOUSE, capacity=100))
    db_session.flush()
    assert dataset_is_stale(db_session) is None, "every compartment present means the data is current"

    gone = db_session.query(Location).filter(Location.code == "ST-SECOND").one()
    db_session.delete(gone)
    db_session.flush()
    assert "ST-SECOND" in (dataset_is_stale(db_session) or ""), "a missing compartment names itself"
