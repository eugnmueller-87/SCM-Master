"""statsforecast engine: adapter sanity + flag dispatch.

These tests need statsforecast installed; they skip cleanly if it isn't, so the
suite stays green on a lean environment that hasn't opted into the engine.
"""
from __future__ import annotations

from datetime import date

import pytest

from app.services import forecasting

sf = pytest.importorskip("statsforecast", reason="statsforecast not installed")
from app.services import forecasting_sf  # noqa: E402  (after the skip guard)


INTERMITTENT = [0, 0, 2, 0, 0, 0, 1, 0, 0, 3, 0, 0, 0, 0, 1, 0, 0, 0, 2, 0, 0]


def test_sf_daily_rate_returns_sane_positive_rate():
    rate, used = forecasting_sf.sf_daily_rate(INTERMITTENT, model="croston_sba")
    assert used == "sf_croston_sba"
    # ~7 units over 21 days -> a fraction of a unit/day, never negative.
    assert 0.0 < rate < 2.0


def test_sf_all_zero_series_is_zero():
    rate, _ = forecasting_sf.sf_daily_rate([0, 0, 0, 0, 0], model="croston_sba")
    assert rate == 0.0


def test_sf_interval_brackets_the_point_rate():
    rate, lo, hi, used = forecasting_sf.sf_rate_with_interval(
        INTERMITTENT, model="croston_sba", level=90)
    assert used == "sf_croston_sba"
    assert lo is not None and hi is not None
    assert lo <= rate <= hi
    assert lo >= 0.0


def test_short_series_falls_back_to_no_interval():
    # Too short for the conformal windows -> point rate, None bounds (caller uses DLT-σ).
    rate, lo, hi, _ = forecasting_sf.sf_rate_with_interval([0, 2, 0], model="croston_sba")
    assert lo is None and hi is None
    assert rate >= 0.0


def test_unknown_model_raises():
    with pytest.raises(ValueError):
        forecasting_sf.sf_daily_rate(INTERMITTENT, model="not_a_model")


def test_daily_rate_engine_dispatch_routes_to_statsforecast():
    """With engine='statsforecast' and an intermittent series, daily_rate returns
    the sf_* method tag; with engine='builtin' it returns 'tsb'. Same routing."""
    today = date(2024, 6, 1)
    # Build deploy dates that yield an intermittent daily series in the window.
    deploys = [today, today, date(2024, 5, 20), date(2024, 4, 15), date(2024, 3, 10)]

    _, used_builtin = forecasting.daily_rate(
        "tsb", deploys, today, window_days=90, halflife_days=30, engine="builtin")
    assert used_builtin == "tsb"

    _, used_sf = forecasting.daily_rate(
        "tsb", deploys, today, window_days=90, halflife_days=30,
        engine="statsforecast", sf_model="croston_sba")
    assert used_sf == "sf_croston_sba"


def test_smooth_route_ignores_engine():
    """run_rate (smooth/erratic) never touches statsforecast, whatever the engine."""
    today = date(2024, 6, 1)
    deploys = [date(2024, 5, d) for d in range(1, 28)]  # demand most days = smooth
    _, used = forecasting.daily_rate(
        "run_rate", deploys, today, window_days=90, halflife_days=30,
        engine="statsforecast")
    assert used == "run_rate"


def test_sf_safety_stock_nonneg_or_none():
    # Intermittent series + a lead time -> a probabilistic buffer (>=0) or None
    # (interval collapsed / series too short). Never negative.
    s = forecasting_sf.sf_safety_stock(INTERMITTENT, 14, service_level=0.95)
    assert s is None or s >= 0


def test_sf_safety_stock_none_without_lead_time():
    assert forecasting_sf.sf_safety_stock(INTERMITTENT, 0, service_level=0.95) is None
