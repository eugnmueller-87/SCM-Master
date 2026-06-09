"""Phase-2 scale benchmark: built-in TSB vs. statsforecast at N SKUs.

The 6-SKU DB comparison was an accuracy *tie* — but accuracy was never the scale
question. At 1000+ SKUs the questions that actually decide the swap are:

  1. SPEED      — our pure-Python per-series TSB loop vs. statsforecast vectorised
                  across ALL series in one call. This is "how will it scale".
  2. ACCURACY   — does the point-forecast tie hold at scale? (it should: same
                  estimator family — volume converges both, it doesn't create a gap)
  3. COVERAGE   — do statsforecast's conformal intervals actually contain realised
                  demand ~level% of the time once there's real history? (the value-add)

Synthetic, DB-free, deterministic (seeded series generated from index, no RNG that
breaks reproducibility). Realistic mix of demand patterns. Usage:

    python -m scripts.bench_forecast_scale --skus 1000 --days 180 --horizon 14
"""
from __future__ import annotations

import argparse
import statistics
import time
import warnings

warnings.filterwarnings("ignore")

from app.core.config import settings
from app.services import forecasting, forecasting_sf


def _synth_series(idx: int, days: int) -> list[int]:
    """Deterministic demand series for SKU #idx over `days`, no RNG.

    A reproducible pseudo-pattern: ~1/3 smooth (demand most days), ~1/3
    intermittent (demand every k days), ~1/3 lumpy (occasional batches). Uses a
    fixed integer hash of (idx, day) so it's identical across runs and across
    engines — fair comparison, and avoids the disallowed Math.random/Date.now.
    """
    bucket = idx % 3
    series = [0] * days
    for d in range(days):
        h = (idx * 2654435761 + d * 40503) & 0xFFFF  # cheap deterministic hash
        if bucket == 0:                       # smooth: most days have 1-3
            series[d] = 1 + (h % 3)
        elif bucket == 1:                     # intermittent: demand every ~5 days
            series[d] = (1 + h % 2) if d % 5 == (idx % 5) else 0
        else:                                 # lumpy: rare batches
            series[d] = (5 + h % 8) if d % 17 == (idx % 17) else 0
    return series


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--skus", type=int, default=1000)
    ap.add_argument("--days", type=int, default=180)
    ap.add_argument("--horizon", type=int, default=14)
    ap.add_argument("--level", type=int, default=90)
    args = ap.parse_args()

    a, b = settings.forecast_tsb_alpha, settings.forecast_tsb_beta
    series_list = [_synth_series(i, args.days) for i in range(args.skus)]
    heads = [s[: -args.horizon] for s in series_list]
    actuals = [sum(s[-args.horizon:]) / args.horizon for s in series_list]

    # 1. SPEED + ACCURACY — built-in (per-series Python loop).
    t0 = time.perf_counter()
    builtin_fc = [forecasting.tsb_daily_rate(h, alpha=a, beta=b) for h in heads]
    t_builtin = time.perf_counter() - t0

    # 1. SPEED + ACCURACY — statsforecast, ONE vectorised call over all series.
    t0 = time.perf_counter()
    sf_fc, sf_lo, sf_hi = _sf_batch(heads, level=args.level)
    t_sf = time.perf_counter() - t0

    mae_builtin = statistics.mean(abs(f - act) for f, act in zip(builtin_fc, actuals))
    mae_sf = statistics.mean(abs(f - act) for f, act in zip(sf_fc, actuals))

    # 3. COVERAGE — fraction of SKUs whose realised tail-rate fell within the
    #    conformal interval (ideal ~ level/100).
    covered = total = 0
    for lo, hi, act in zip(sf_lo, sf_hi, actuals):
        if lo is None or hi is None:
            continue
        total += 1
        if lo <= act <= hi:
            covered += 1
    coverage = (covered / total) if total else float("nan")

    print(f"\n=== Scale benchmark: {args.skus} SKUs x {args.days} days "
          f"(horizon={args.horizon}d, level={args.level}%) ===\n")
    print(f"{'engine':<22}{'wall_clock_s':>14}{'per_sku_ms':>12}{'backtest_MAE':>14}")
    print(f"{'built-in TSB (loop)':<22}{t_builtin:>14.3f}{t_builtin/args.skus*1000:>12.3f}{mae_builtin:>14.4f}")
    print(f"{'statsforecast (batch)':<22}{t_sf:>14.3f}{t_sf/args.skus*1000:>12.3f}{mae_sf:>14.4f}")
    print(f"\nspeed ratio (builtin / sf)      : {t_builtin / t_sf:.2f}x  "
          f"({'sf faster' if t_sf < t_builtin else 'builtin faster'})")
    print(f"interval coverage @ {args.level}% (ideal ~{args.level/100:.2f}) : "
          f"{coverage:.2f}  over {total} SKUs with intervals")


def _sf_batch(series_list: list[list[int]], *, level: int):
    """One vectorised statsforecast call over many series. Returns (rates, los, his)."""
    import pandas as pd
    from statsforecast import StatsForecast
    from statsforecast.models import CrostonSBA
    from statsforecast.utils import ConformalIntervals

    frames = []
    for i, s in enumerate(series_list):
        n = len(s)
        frames.append(pd.DataFrame({
            "unique_id": [f"sku{i}"] * n,
            "ds": pd.date_range("2000-01-01", periods=n, freq="D"),
            "y": [float(v) for v in s],
        }))
    df = pd.concat(frames, ignore_index=True)

    ci = ConformalIntervals(h=1, n_windows=2)
    sf = StatsForecast(models=[CrostonSBA(prediction_intervals=ci)], freq="D")
    fc = sf.forecast(df=df, h=1, level=[level]).set_index("unique_id")

    rates, los, his = [], [], []
    locol, hicol = f"CrostonSBA-lo-{level}", f"CrostonSBA-hi-{level}"
    for i in range(len(series_list)):
        row = fc.loc[f"sku{i}"]
        rates.append(max(0.0, float(row["CrostonSBA"])))
        los.append(max(0.0, float(row[locol])) if locol in fc.columns else None)
        his.append(max(0.0, float(row[hicol])) if hicol in fc.columns else None)
    return rates, los, his


if __name__ == "__main__":
    main()
