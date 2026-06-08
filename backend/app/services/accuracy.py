"""Forecast-accuracy backtest — score the demand agent against what really happened.

This is the payoff of the dated history (``seed_history``): because
:func:`planning.demand_forecast` takes an as-of ``today``, we can stand at a past
month-end, ask the agent what it forecast for the next horizon, then look at the
deployments that ACTUALLY happened in that window and measure the error. Run that
over every backtestable month and you get a per-product, per-period accuracy
series — exactly the fact table a Power BI dashboard charts.

Everything here is a pure read over existing data; no new tables. The output is a
flat list of rows (one per as-of date × product), ready to flatten to CSV.

Definitions, kept apples-to-apples:
  - ``predicted_demand`` : the forecast's ``projected_demand`` as of the as-of date
    (recency-weighted usage × horizon + EOL replacement).
  - ``actual_demand``    : units actually DEPLOYED in (as_of, as_of + horizon],
    by ``Asset.deployed_date``.
  - ``abs_error``        : |predicted − actual|.
  - ``ape``              : abs_error / actual  (absolute percentage error; null
    when actual is 0 to avoid divide-by-zero — those rows are excluded from MAPE).
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.catalog import Product
from app.models.flow import Asset

# How far apart the as-of snapshots are taken (days). ~monthly.
_STEP_DAYS = 30


def _as_of_dates(history_start: date, history_end: date, horizon: int) -> list[date]:
    """Month-spaced as-of dates that have BOTH enough trailing history to
    forecast from (>= the usage window) and a full horizon of future actuals to
    score against (<= history_end - horizon)."""
    window = settings.demand_window_days
    first = history_start + timedelta(days=window)
    last = history_end - timedelta(days=horizon)
    out: list[date] = []
    d = first
    step = timedelta(days=_STEP_DAYS)
    while d <= last:
        out.append(d)
        d = d + step
    return out


def _history_bounds(db: Session) -> Optional[tuple[date, date]]:
    lo = db.scalar(select(func.min(Asset.deployed_date)).where(Asset.deployed_date.is_not(None)))
    hi = db.scalar(select(func.max(Asset.deployed_date)).where(Asset.deployed_date.is_not(None)))
    if lo is None or hi is None:
        return None
    return lo, hi


def _actual_deploys(db: Session, product_id: str, start: date, end: date) -> int:
    """Units of a product deployed in (start, end] by deployed_date."""
    n = db.scalar(
        select(func.count(Asset.id)).where(
            Asset.product_id == product_id,
            Asset.deployed_date.is_not(None),
            Asset.deployed_date > start,
            Asset.deployed_date <= end,
        )
    )
    return int(n or 0)


def backtest(db: Session, *, method: Optional[str] = None) -> list[dict]:
    """Walk month-spaced as-of dates across the deployment history, run the
    forecast at each, and compare to actual deployments over the next horizon.

    ``method`` selects the forecast estimator (run_rate / tsb / auto), so the
    same harness can score competing methods head-to-head on identical data.
    Returns one row per (as_of_date × product) that had a forecast. Empty if
    there isn't enough history (need at least window + horizon of span)."""
    from app.services import planning  # local import avoids any cycle

    bounds = _history_bounds(db)
    if bounds is None:
        return []
    history_start, history_end = bounds
    horizon = settings.demand_horizon_days

    out: list[dict] = []
    for as_of in _as_of_dates(history_start, history_end, horizon):
        forecast = {f["product_id"]: f
                    for f in planning.demand_forecast(db, today=as_of, method=method)}
        window_end = as_of + timedelta(days=horizon)
        for pid, f in forecast.items():
            actual = _actual_deploys(db, pid, as_of, window_end)
            predicted = float(f["projected_demand"])
            abs_error = abs(predicted - actual)
            ape = (abs_error / actual) if actual > 0 else None
            out.append({
                "as_of_date": as_of,
                "horizon_days": horizon,
                "window_end": window_end,
                "product_id": pid,
                "product_code": f.get("product_code"),
                "name": f.get("name"),
                "category": f.get("category"),
                "usage_rate_per_day": f.get("usage_rate_per_day"),
                "forecast_method": f.get("forecast_method"),
                "predicted_demand": round(predicted, 1),
                "actual_demand": actual,
                "abs_error": round(abs_error, 1),
                "ape": round(ape, 4) if ape is not None else None,
            })
    return out


def monthly_demand_history(db: Session) -> list[dict]:
    """Flat monthly actual-deployment counts per product — the demand time series.

    One row per (year-month × product) with at least one deployment, driven by
    ``Asset.deployed_date``. This is the raw history the forecast learns from and
    the natural fact table for a 'demand over time' Power BI chart.
    """
    # Bucket in Python (dataset is small) so it's portable across SQLite/Postgres
    # rather than relying on a dialect-specific date-truncation function.
    deployed = db.execute(
        select(Asset.product_id, Asset.deployed_date)
        .where(Asset.deployed_date.is_not(None))
    ).all()
    counts: dict[tuple[str, str], int] = {}
    for pid, d in deployed:
        ym = f"{d.year:04d}-{d.month:02d}"
        counts[(ym, pid)] = counts.get((ym, pid), 0) + 1

    products = {p.id: p for p in db.scalars(select(Product)).all()}
    out: list[dict] = []
    for (ym, pid), n in counts.items():
        p = products.get(pid)
        out.append({
            "month": ym,                       # 'YYYY-MM'
            "month_start": f"{ym}-01",         # date-typed in Power BI
            "product_id": pid,
            "product_code": p.product_code if p else None,
            "name": p.name if p else None,
            "category": p.category if p else None,
            "units_deployed": n,
        })
    return sorted(out, key=lambda r: (r["month"], r["product_code"] or ""))


def accuracy_summary(db: Session, *, method: Optional[str] = None) -> dict:
    """Headline accuracy across the whole backtest: MAPE and bias.

    - ``mape``  : mean APE over rows where actual > 0 (lower = better).
    - ``bias``  : mean signed error (predicted − actual); >0 = over-forecasting.
    - per-product breakdown of the same.
    ``method`` selects the forecast estimator scored (defaults to the configured one).
    """
    rows = backtest(db, method=method)
    if not rows:
        return {"rows": 0, "mape": None, "bias": None, "by_product": []}

    def _mape(rs: list[dict]) -> Optional[float]:
        apes = [r["ape"] for r in rs if r["ape"] is not None]
        return round(sum(apes) / len(apes), 4) if apes else None

    def _bias(rs: list[dict]) -> float:
        signed = [r["predicted_demand"] - r["actual_demand"] for r in rs]
        return round(sum(signed) / len(signed), 2)

    by_product: dict[str, list[dict]] = {}
    for r in rows:
        by_product.setdefault(r["product_code"] or r["product_id"], []).append(r)

    return {
        "rows": len(rows),
        "as_of_count": len({r["as_of_date"] for r in rows}),
        "mape": _mape(rows),
        "bias": _bias(rows),
        "by_product": sorted(
            [
                {"product_code": code, "rows": len(rs), "mape": _mape(rs), "bias": _bias(rs)}
                for code, rs in by_product.items()
            ],
            key=lambda x: (x["mape"] is None, x["mape"] or 0),
        ),
    }
