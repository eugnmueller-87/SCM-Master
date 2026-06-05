"""Tests for the flat CSV export endpoints (Power BI / Tableau)."""
from __future__ import annotations

import csv
import io
from datetime import date, timedelta

from app.models.auth import Role
from app.models.catalog import Organization, Product, ProductSupplier
from app.models.flow import Asset, AssetStatus, Location, LocationType

B = "/api/v1"


def _seed_history(db, *, per_month=8, months=18, end=date(2026, 6, 1)):
    org = Organization(name="Acme", code="ACME", is_supplier=True)
    product = Product(product_code="WID-1", name="Widget", category="Widgets")
    db.add(org)
    db.add(product)
    db.flush()
    db.add(ProductSupplier(product_id=product.id, supplier_id=org.id,
                           standard_lead_time_days=14, min_order_quantity=1,
                           preference_rank=1, active=True))
    rack = Location(code="R1", name="Rack", location_type=LocationType.RACK, capacity=10000)
    db.add(rack)
    db.flush()
    for m in range(months):
        total = (end.year * 12 + end.month - 1) - (months - m)
        m_start = date(total // 12, total % 12 + 1, 1)
        for j in range(per_month):
            db.add(Asset(serial_number=f"SN-{m}-{j}", product_id=product.id,
                         status=AssetStatus.DEPLOYED, current_location_id=rack.id,
                         deployed_date=m_start + timedelta(days=1 + j % 25)))
    db.flush()


def _parse_csv(text: str) -> list[dict]:
    return list(csv.DictReader(io.StringIO(text)))


def test_demand_history_csv(client, db_session):
    _seed_history(db_session, per_month=8, months=6)
    r = client.get(f"{B}/analytics/exports/demand-history.csv")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
    assert "attachment" in r.headers["content-disposition"]
    rows = _parse_csv(r.text)
    assert len(rows) == 6
    assert {"month", "month_start", "product_code", "units_deployed"} <= set(rows[0].keys())
    assert all(row["units_deployed"] == "8" for row in rows)


def test_forecast_accuracy_csv(client, db_session):
    _seed_history(db_session, per_month=10, months=18)
    r = client.get(f"{B}/analytics/exports/forecast-accuracy.csv")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
    rows = _parse_csv(r.text)
    assert rows, "expected backtest rows"
    assert {"as_of_date", "predicted_demand", "actual_demand", "abs_error", "ape"} <= set(rows[0].keys())


def test_exports_have_header_even_when_empty(client):
    # No data seeded -> still a valid CSV with a header row, zero data rows.
    r = client.get(f"{B}/analytics/exports/forecast-accuracy.csv")
    assert r.status_code == 200
    rows = _parse_csv(r.text)
    assert rows == []
    assert "predicted_demand" in r.text.splitlines()[0]


def test_exports_require_auth(client):
    anon = client.anon()
    r = anon.get(f"{B}/analytics/exports/demand-history.csv")
    assert r.status_code == 401


def test_viewer_can_read_exports(client, db_session):
    # Exports are reads — any authenticated user, including VIEWER, may pull.
    _seed_history(db_session, per_month=5, months=6)
    viewer = client.as_role(Role.VIEWER)
    r = viewer.get(f"{B}/analytics/exports/demand-history.csv")
    assert r.status_code == 200
