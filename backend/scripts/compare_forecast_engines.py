"""Phase-2 de-risk gate: built-in TSB vs. statsforecast, on real demand history.

Run BEFORE making statsforecast load-bearing. It answers the only question that
matters for owning this swap at a new company: *does the library agree with the
method we already trust, and does it forecast actual demand at least as well?*

Two comparisons, per product, intermittent SKUs only (smooth/erratic stay on the
run-rate and are out of scope here):

  1. AGREEMENT  — built-in tsb_daily_rate vs. statsforecast TSB & CrostonSBA on
                  the full series. How far apart are the point rates?
  2. BACKTEST   — hold out the last H days, forecast from the head, compare each
                  engine's predicted rate to the actual realised rate over the
                  hold-out. Lower error wins.

Reads real data from the local DB (the demo seed is representative). Prints a
table and a verdict; writes nothing. Usage:

    python -m scripts.compare_forecast_engines [--horizon 14] [--limit 0]
"""
from __future__ import annotations

import argparse
import statistics
import warnings

warnings.filterwarnings("ignore")

from sqlalchemy import select

from app.core.db import SessionLocal
from app.core.config import settings
from app.models.flow import Asset
from app.services import forecasting, forecasting_sf
from datetime import date


def _deploys_by_product(db) -> dict[str, list[date]]:
    """All deployment dates per product — same source planning.demand_forecast uses."""
    out: dict[str, list[date]] = {}
    for a in db.scalars(select(Asset).where(Asset.deployed_date.is_not(None))).all():
        out.setdefault(a.product_id, []).append(a.deployed_date)
    return out


def _backtest_actual_rate(series: list[int], horizon: int) -> float:
    """Realised demand/day over the last `horizon` buckets of the series."""
    tail = series[-horizon:]
    return sum(tail) / max(1, len(tail))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--horizon", type=int, default=14, help="hold-out length (days)")
    ap.add_argument("--limit", type=int, default=0, help="max products (0 = all)")
    args = ap.parse_args()

    today = date.today()
    window = settings.demand_window_days

    db = SessionLocal()
    try:
        deploys = _deploys_by_product(db)
    finally:
        db.close()

    rows = []
    for pid, dates in deploys.items():
        series = forecasting.daily_series(dates, today, window)
        cls = forecasting.classify_demand(series)
        if cls.recommended != "tsb":
            continue  # only the intermittent SKUs are in scope for this swap
        if sum(series) <= 0:
            continue

        # 1. Agreement on the full series.
        builtin = forecasting.tsb_daily_rate(
            series, alpha=settings.forecast_tsb_alpha, beta=settings.forecast_tsb_beta)
        sf_tsb, _ = forecasting_sf.sf_daily_rate(
            series, model="tsb",
            tsb_alpha=settings.forecast_tsb_alpha, tsb_beta=settings.forecast_tsb_beta)
        sf_sba, _ = forecasting_sf.sf_daily_rate(series, model="croston_sba")

        # 2. Backtest: fit on head, compare to realised tail rate.
        head = series[: -args.horizon] if len(series) > args.horizon + 3 else series
        actual = _backtest_actual_rate(series, args.horizon)
        bt_builtin = forecasting.tsb_daily_rate(
            head, alpha=settings.forecast_tsb_alpha, beta=settings.forecast_tsb_beta)
        bt_sf_sba, _ = forecasting_sf.sf_daily_rate(head, model="croston_sba")

        rows.append({
            "pid": pid[:8], "pattern": cls.pattern, "adi": cls.adi, "cv2": cls.cv2,
            "builtin": builtin, "sf_tsb": sf_tsb, "sf_sba": sf_sba,
            "actual": actual,
            "err_builtin": abs(bt_builtin - actual),
            "err_sf_sba": abs(bt_sf_sba - actual),
        })
        if args.limit and len(rows) >= args.limit:
            break

    if not rows:
        print("No intermittent SKUs found in the local DB. Seed the demo first.")
        return

    print(f"\nIntermittent SKUs compared: {len(rows)}  (horizon={args.horizon}d, window={window}d)\n")
    print(f"{'sku':<10}{'pattern':<13}{'builtin':>9}{'sf_tsb':>9}{'sf_sba':>9}"
          f"{'actual':>9}{'err_blt':>10}{'err_sba':>10}")
    for r in rows:
        print(f"{r['pid']:<10}{r['pattern']:<13}{r['builtin']:>9.3f}{r['sf_tsb']:>9.3f}"
              f"{r['sf_sba']:>9.3f}{r['actual']:>9.3f}{r['err_builtin']:>11.3f}{r['err_sf_sba']:>10.3f}")

    # Aggregate verdict.
    mae_builtin = statistics.mean(r["err_builtin"] for r in rows)
    mae_sf = statistics.mean(r["err_sf_sba"] for r in rows)
    agree = statistics.mean(abs(r["builtin"] - r["sf_tsb"]) for r in rows)
    print("\n--- VERDICT ---")
    print(f"Mean abs(builtin TSB - sf TSB) (agreement) : {agree:.4f} units/day")
    print(f"Backtest MAE  built-in TSB              : {mae_builtin:.4f}")
    print(f"Backtest MAE  statsforecast CrostonSBA  : {mae_sf:.4f}")
    if mae_sf < mae_builtin * 0.97:
        print("=> statsforecast forecasts the hold-out better. Switching is justified.")
    elif mae_builtin < mae_sf * 0.97:
        print("=> built-in TSB forecasts the hold-out better. Keep built-in; swap not justified on accuracy alone.")
    else:
        print("=> Near-tie. Decide on other grounds (probabilistic intervals, ownership/maintenance).")


if __name__ == "__main__":
    main()
