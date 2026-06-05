"""Tests for the forecast-accuracy backtest and the demand-history series.

Builds a small, fully-controlled dated dataset directly against the test session
(faster and more deterministic than running the full 18-month seed), then asserts
the backtest scores predicted vs actual sensibly.
"""
from __future__ import annotations

from datetime import date, timedelta

from app.models.catalog import Organization, Product, ProductSupplier
from app.models.flow import Asset, AssetStatus, Location, LocationType
from app.services import accuracy


def _seed_steady_history(db, *, product_code="WID-1", per_month=10, months=18,
                         end=date(2026, 6, 1)):
    """Deploy `per_month` units/month for `months` months ending at `end`."""
    org = Organization(name="Acme", code="ACME", is_supplier=True)
    db.add(org)
    product = Product(product_code=product_code, name="Widget", category="Widgets")
    db.add(product)
    db.flush()
    db.add(ProductSupplier(product_id=product.id, supplier_id=org.id,
                           standard_lead_time_days=14, min_order_quantity=1,
                           preference_rank=1, active=True))
    rack = Location(code="R1", name="Rack", location_type=LocationType.RACK, capacity=10000)
    db.add(rack)
    db.flush()

    for m in range(months):
        # month start, oldest first
        total = (end.year * 12 + end.month - 1) - (months - m)
        m_start = date(total // 12, total % 12 + 1, 1)
        for j in range(per_month):
            deploy = m_start + timedelta(days=1 + j % 25)
            db.add(Asset(
                serial_number=f"SN-{product_code}-{m}-{j}",
                product_id=product.id, status=AssetStatus.DEPLOYED,
                current_location_id=rack.id, deployed_date=deploy,
            ))
    db.flush()
    return product


def test_backtest_scores_steady_demand_accurately(db_session):
    _seed_steady_history(db_session, per_month=10, months=18)
    rows = accuracy.backtest(db_session)

    assert rows, "expected backtest rows from 18 months of history"
    # Every row is a real comparison with the standard horizon.
    assert all(r["horizon_days"] == 90 for r in rows)
    assert all(r["actual_demand"] >= 0 for r in rows)

    # Steady ~10/month ≈ 30 over a 90-day horizon; the forecast should be close.
    summary = accuracy.accuracy_summary(db_session)
    assert summary["rows"] == len(rows)
    assert summary["mape"] is not None
    assert summary["mape"] < 0.25  # within 25% on steady demand


def test_backtest_empty_without_history(db_session):
    assert accuracy.backtest(db_session) == []
    s = accuracy.accuracy_summary(db_session)
    assert s == {"rows": 0, "mape": None, "bias": None, "by_product": []}


def test_monthly_demand_history_buckets_by_month(db_session):
    _seed_steady_history(db_session, per_month=7, months=6)
    hist = accuracy.monthly_demand_history(db_session)
    assert len(hist) == 6                      # one row per month (single product)
    assert all(r["units_deployed"] == 7 for r in hist)
    assert all(r["month_start"].endswith("-01") for r in hist)
    # Months are sorted ascending.
    assert hist == sorted(hist, key=lambda r: r["month"])


def test_ape_is_null_when_actual_zero(db_session):
    # A product with history but a zero-actual horizon should not divide by zero.
    _seed_steady_history(db_session, per_month=5, months=18)
    rows = accuracy.backtest(db_session)
    # If any row had zero actuals, its ape must be None (not 0/0 -> error).
    for r in rows:
        if r["actual_demand"] == 0:
            assert r["ape"] is None


def test_history_seed_spreads_dates(db_session):
    """The real history seed produces multi-month dated deployments.

    Runs the seed's date helper to confirm 18 distinct month starts, without
    standing up the whole catalog (that path is covered by the smoke run)."""
    from app.seed_history import HISTORY_MONTHS, _month_start
    starts = {_month_start(mi) for mi in range(HISTORY_MONTHS, 0, -1)}
    assert len(starts) == HISTORY_MONTHS
    assert all(s.day == 1 for s in starts)
