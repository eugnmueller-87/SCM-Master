"""Unit tests for the pure forecasting estimators + the demand classifier.

These are DB-free: they exercise the algorithms directly on known series with
hand-verified expected outputs, so a regression in TSB or the Syntetos–Boylan
routing is caught deterministically.
"""
from __future__ import annotations

from datetime import date, timedelta

import pytest

from app.services.forecasting import (
    classify_abc,
    classify_demand,
    daily_rate,
    daily_series,
    safety_stock,
    tsb_daily_rate,
    weighted_daily_rate,
    z_score,
)

# --- TSB on a KNOWN intermittent series -------------------------------------

def test_tsb_intermittent_known_value():
    # Demand only every 4th period, size 4 → true long-run rate = 1.0/period.
    series = [0, 0, 0, 4] * 5
    rate = tsb_daily_rate(series, alpha=0.1, beta=0.1)
    # Hand-verified against the implementation; in the ballpark of the 1.0 truth.
    assert rate == pytest.approx(1.1433, abs=1e-3)


def test_tsb_reduces_to_rate_on_steady_series():
    # Steady demand of 2 every period → TSB must return ~2 (it must NOT mangle
    # smooth series; this is why routing matters, not blanket-applying TSB).
    assert tsb_daily_rate([2] * 20) == pytest.approx(2.0, abs=1e-6)


def test_tsb_all_zero_is_zero():
    assert tsb_daily_rate([0] * 10) == 0.0


def test_tsb_decays_when_demand_dries_up():
    # Demand early, then a long zero tail → TSB's per-period probability update
    # pulls the forecast DOWN (the key advantage over classic Croston).
    early = [5, 5, 5, 5] + [0] * 16
    late = [0] * 16 + [5, 5, 5, 5]
    assert tsb_daily_rate(early) < tsb_daily_rate(late)


# --- Syntetos–Boylan classification -----------------------------------------

def test_classify_smooth_routes_to_run_rate():
    c = classify_demand([2] * 20)
    assert c.pattern == "smooth"
    assert c.adi == 1.0
    assert c.recommended == "run_rate"


def test_classify_intermittent_routes_to_tsb():
    c = classify_demand([0, 0, 0, 4] * 5)  # ADI 4.0
    assert c.pattern == "intermittent"
    assert c.adi == pytest.approx(4.0)
    assert c.recommended == "tsb"


def test_classify_lumpy_routes_to_tsb():
    # Intermittent AND variable sizes → lumpy → TSB.
    c = classify_demand([0, 0, 1, 0, 0, 9, 0, 0, 2, 0, 0, 8])
    assert c.adi >= 1.32 and c.cv2 >= 0.49
    assert c.pattern == "lumpy"
    assert c.recommended == "tsb"


def test_classify_empty_series_is_smooth_runrate():
    c = classify_demand([0] * 10)
    assert c.recommended == "run_rate"


# --- daily_series bucketing -------------------------------------------------

def test_daily_series_buckets_by_age():
    today = date(2026, 6, 1)
    # two deploys today, one 3 days ago, one outside the 5-day window.
    dates = [today, today, today - timedelta(days=3), today - timedelta(days=10)]
    series = daily_series(dates, today, window_days=5)
    assert len(series) == 6              # window+1 days
    assert series[-1] == 2               # today (newest) bucket
    assert series[-4] == 1               # 3 days ago
    assert sum(series) == 3              # the 10-day-old one is excluded


# --- dispatcher routing -----------------------------------------------------

def _dates_from_series(series, today):
    """Reconstruct deploy-date list (oldest-first series) for the dispatcher."""
    out = []
    n = len(series)
    for i, count in enumerate(series):
        age = (n - 1) - i
        out += [today - timedelta(days=age)] * count
    return out


def test_dispatcher_auto_routes_per_pattern():
    today = date(2026, 6, 1)
    steady = _dates_from_series([2] * 20, today)
    lumpy = _dates_from_series([0, 0, 0, 4] * 5, today)

    _, m_steady = daily_rate("auto", steady, today, window_days=19, halflife_days=30)
    _, m_lumpy = daily_rate("auto", lumpy, today, window_days=19, halflife_days=30)
    assert m_steady == "run_rate"
    assert m_lumpy == "tsb"


def test_dispatcher_explicit_methods():
    today = date(2026, 6, 1)
    dates = _dates_from_series([0, 0, 0, 4] * 5, today)
    _, m1 = daily_rate("run_rate", dates, today, window_days=19, halflife_days=30)
    _, m2 = daily_rate("tsb", dates, today, window_days=19, halflife_days=30)
    assert m1 == "run_rate" and m2 == "tsb"


def test_dispatcher_unknown_method_raises():
    with pytest.raises(ValueError, match="unknown forecast method"):
        daily_rate("magic", [], date(2026, 6, 1), window_days=10, halflife_days=30)


# --- run_rate wrapper parity ------------------------------------------------

def test_run_rate_matches_weighted_daily_rate():
    today = date(2026, 6, 1)
    dates = [today - timedelta(days=d) for d in (0, 1, 2, 30, 60)]
    direct = weighted_daily_rate(dates, today, window_days=90, halflife_days=30)
    via, method = daily_rate("run_rate", dates, today, window_days=90, halflife_days=30)
    assert via == pytest.approx(direct)
    assert method == "run_rate"


# --- service-level safety stock ---------------------------------------------

def test_z_score_known_quantiles():
    assert z_score(0.50) == pytest.approx(0.0, abs=1e-6)
    assert z_score(0.95) == pytest.approx(1.645, abs=1e-3)
    assert z_score(0.975) == pytest.approx(1.960, abs=1e-3)


def test_safety_stock_rises_with_service_level():
    # Same variable series, higher service level -> larger buffer.
    series = [0, 4, 0, 0, 6, 0, 2, 0, 0, 8]
    s90 = safety_stock(series, lead_time_days=20, service_level=0.90)
    s95 = safety_stock(series, lead_time_days=20, service_level=0.95)
    s99 = safety_stock(series, lead_time_days=20, service_level=0.99)
    assert s90 < s95 < s99


def test_safety_stock_rises_with_demand_variability():
    # Same mean (~2/day), but lumpy demand has higher σ -> bigger buffer than steady.
    steady = [2] * 20
    lumpy = [0, 0, 0, 0, 0, 0, 0, 0, 0, 40]  # same total (40) but all in one spike
    s_steady = safety_stock(steady, lead_time_days=20, service_level=0.95)
    s_lumpy = safety_stock(lumpy, lead_time_days=20, service_level=0.95)
    assert s_lumpy > s_steady


def test_safety_stock_zero_when_no_variability():
    # Perfectly constant demand -> σ=0 -> no buffer needed.
    assert safety_stock([3] * 30, lead_time_days=20, service_level=0.95) == 0


def test_safety_stock_zero_without_lead_time():
    assert safety_stock([0, 5, 0, 3, 0, 7], lead_time_days=0, service_level=0.95) == 0


# --- ABC classification -----------------------------------------------------

def test_classify_abc_known_pareto():
    # Total value = 100. Ranked: X=70, Y=20, Z=7, W=3.
    #   X: prev cum 0%   -> A
    #   Y: prev cum 70%  (<80%) -> A   (the item crossing 80% still counts A)
    #   Z: prev cum 90%  (<95%) -> B
    #   W: prev cum 97%  (≥95%) -> C
    abc = classify_abc({"X": 70, "Y": 20, "Z": 7, "W": 3},
                       a_threshold=0.80, b_threshold=0.95)
    assert abc == {"X": "A", "Y": "A", "Z": "B", "W": "C"}


def test_classify_abc_zero_value_is_c():
    abc = classify_abc({"big": 100, "dead": 0}, a_threshold=0.80, b_threshold=0.95)
    assert abc["big"] == "A"
    assert abc["dead"] == "C"


def test_classify_abc_empty_and_all_zero():
    assert classify_abc({}) == {}
    assert classify_abc({"a": 0, "b": 0}) == {"a": "C", "b": "C"}
