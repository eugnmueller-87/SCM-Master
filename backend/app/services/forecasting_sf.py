"""statsforecast adapter — the ONLY module that imports Nixtla statsforecast.

Why isolated here
-----------------
We swap the per-series demand-rate estimator from our hand-rolled TSB to
statsforecast's battle-tested intermittent-demand models (Croston / CrostonSBA /
TSB / ADIDA / IMAPA), and gain probabilistic prediction intervals. Keeping every
statsforecast import in this one file means:
  - the rest of the system (planning, accuracy, the gate, calibration) never
    imports the library and is unaffected by it;
  - the engine can be turned off by config (forecast_engine="builtin") with the
    library still installed; and
  - if we ever drop or replace statsforecast, only this file changes.

Contract
--------
This module speaks the SAME currency as services/forecasting: it takes the daily
demand series that ``forecasting.daily_series`` already produces (oldest-first,
one bucket per day, zeros included) and returns a **rate per day** plus a
``method_used`` tag — so ``planning.daily_rate`` can dispatch to it without any
caller noticing. It NEVER decides routing: the Syntetos–Boylan classification
stays in ``services/forecasting`` and tells us which model to ask for here.

Pure-ish: the only state is the model fit; no DB, no app imports. statsforecast
is imported lazily inside the functions so importing this module is cheap and so
a missing/broken install surfaces only when the engine is actually selected.
"""
from __future__ import annotations

from typing import Optional

# Model keys we expose. The Syntetos–Boylan route in services/forecasting maps a
# SKU's class to one of these; "run_rate" (smooth/erratic) is handled by the
# incumbent estimator and never reaches this adapter.
SF_MODELS = ("croston_sba", "tsb", "croston_classic", "adida", "imapa")


def _build_model(model: str, *, tsb_alpha: float, tsb_beta: float,
                 prediction_intervals=None):
    """Instantiate one statsforecast model by our key. Imported lazily."""
    from statsforecast.models import (
        ADIDA,
        IMAPA,
        TSB,
        CrostonClassic,
        CrostonOptimized,  # noqa: F401  (kept available; CrostonSBA is our default)
        CrostonSBA,
    )

    kw = {}
    if prediction_intervals is not None:
        kw["prediction_intervals"] = prediction_intervals

    if model == "croston_sba":
        return CrostonSBA(**kw)
    if model == "croston_classic":
        return CrostonClassic(**kw)
    if model == "adida":
        return ADIDA(**kw)
    if model == "imapa":
        return IMAPA(**kw)
    if model == "tsb":
        # statsforecast TSB takes the two smoothing params explicitly. We map our
        # tsb_alpha -> demand-probability smoothing, tsb_beta -> demand-size
        # smoothing, matching our own tsb_daily_rate semantics.
        return TSB(alpha_d=tsb_beta, alpha_p=tsb_alpha, **kw)
    raise ValueError(f"unknown statsforecast model {model!r}; expected one of {SF_MODELS}")


def _frame(series: list[int]):
    """Wrap an oldest-first daily series into the long frame statsforecast wants.

    statsforecast is date-indexed; our series carries no real dates, only order,
    so we synthesise a contiguous daily index. Demand RATE is invariant to the
    absolute dates — only spacing and values matter — so a synthetic calendar is
    correct here. Uses a fixed epoch (no Date.now) for determinism.
    """
    import pandas as pd

    n = len(series)
    return pd.DataFrame({
        "unique_id": ["sku"] * n,
        "ds": pd.date_range("2000-01-01", periods=n, freq="D"),
        "y": [float(v) for v in series],
    })


# Map our model key -> the column name statsforecast emits (its class name).
_COL = {
    "croston_sba": "CrostonSBA",
    "croston_classic": "CrostonClassic",
    "adida": "ADIDA",
    "imapa": "IMAPA",
    "tsb": "TSB",
}


def sf_daily_rate(series: list[int], *, model: str = "croston_sba",
                  tsb_alpha: float = 0.1, tsb_beta: float = 0.1) -> tuple[float, str]:
    """Point demand-rate per day for one SKU, via statsforecast.

    Returns ``(rate_per_day, method_used)`` where method_used is e.g.
    ``"sf_croston_sba"`` so the backtest/accuracy tags record the real engine.
    Returns ``(0.0, ...)`` for an all-zero / too-short series (no demand to model).
    """
    if not series or sum(series) <= 0 or len(series) < 3:
        return 0.0, f"sf_{model}"

    from statsforecast import StatsForecast

    m = _build_model(model, tsb_alpha=tsb_alpha, tsb_beta=tsb_beta)
    sf = StatsForecast(models=[m], freq="D")
    fc = sf.forecast(df=_frame(series), h=1)
    rate = float(fc[_COL[model]].iloc[0])
    return max(0.0, rate), f"sf_{model}"


def sf_rate_with_interval(series: list[int], *, model: str = "croston_sba",
                          level: int = 90, n_windows: int = 2,
                          tsb_alpha: float = 0.1, tsb_beta: float = 0.1
                          ) -> tuple[float, Optional[float], Optional[float], str]:
    """Probabilistic demand-rate: ``(rate, lo, hi, method_used)`` at ``level``%.

    Uses conformal prediction intervals (distribution-free — appropriate for
    intermittent demand where a Gaussian interval is wrong). ``lo``/``hi`` are the
    per-day rate bounds; they are ``None`` when the series is too short for the
    requested number of conformal windows, in which case the caller falls back to
    the deterministic DLT-σ safety stock. This is the genuine value-add over the
    incumbent point estimator.
    """
    if not series or sum(series) <= 0 or len(series) < (n_windows + 2):
        rate, used = sf_daily_rate(series, model=model, tsb_alpha=tsb_alpha, tsb_beta=tsb_beta)
        return rate, None, None, used

    from statsforecast import StatsForecast
    from statsforecast.utils import ConformalIntervals

    ci = ConformalIntervals(h=1, n_windows=n_windows)
    m = _build_model(model, tsb_alpha=tsb_alpha, tsb_beta=tsb_beta, prediction_intervals=ci)
    sf = StatsForecast(models=[m], freq="D")
    fc = sf.forecast(df=_frame(series), h=1, level=[level])
    col = _COL[model]
    rate = max(0.0, float(fc[col].iloc[0]))
    lo = fc.get(f"{col}-lo-{level}")
    hi = fc.get(f"{col}-hi-{level}")
    lo_v = max(0.0, float(lo.iloc[0])) if lo is not None else None
    hi_v = max(0.0, float(hi.iloc[0])) if hi is not None else None
    return rate, lo_v, hi_v, f"sf_{model}"


def sf_safety_stock(series: list[int], lead_time_days: int, *,
                    service_level: float, model: str = "croston_sba") -> Optional[int]:
    """Probabilistic safety stock from the conformal prediction interval, or None.

    The buffer is the UPPER-TAIL demand over the lead time that the point forecast
    does not cover: ``(hi_rate - point_rate) * lead_time``, where ``hi_rate`` is the
    conformal upper bound at ``service_level``. This is a distribution-free buffer —
    the right shape for intermittent demand, where a Gaussian DLT-σ buffer
    mis-states the spiky upper tail. Returns None (caller falls back to DLT-σ) when:
      - there is no lead time or the series is too short for conformal windows, or
      - the interval collapses (hi <= point), i.e. no upper-tail risk to buffer.

    ``service_level`` maps to the conformal coverage level (e.g. 0.95 -> 95).
    """
    if lead_time_days <= 0:
        return None
    import math as _math

    level = int(round(max(0.5, min(0.99, service_level)) * 100))
    rate, _lo, hi, _used = sf_rate_with_interval(series, model=model, level=level)
    if hi is None or hi <= rate:
        return None
    return max(0, _math.ceil((hi - rate) * lead_time_days))
