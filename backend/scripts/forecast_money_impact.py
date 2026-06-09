"""Translate forecast accuracy into MONEY — over-order cost vs. stockout cost.

The question that decides the engine isn't "24% lower error" in the abstract — it
is "what does that 24% mean in euros at our order volume?" This model converts
each engine's backtest forecast error into the two costs forecast error actually
causes:

  - OVER-FORECAST  -> over-order  -> holding cost (cash tied up + space) on the
                      excess units, for the time they sit.
  - UNDER-FORECAST -> under-order -> stockout cost (lost availability / expedite)
                      on the short units.

We reuse the SAME synthetic demand + the SAME two engines as bench_forecast_scale,
so the money figures trace directly to the documented accuracy test. Every cost
assumption is an explicit CLI knob — change them to a customer's economics and the
breakdown re-prices. Deterministic, DB-free.

    python -m scripts.forecast_money_impact --skus 1000 --days 180 --horizon 14 \
        --unit-cost 800 --holding-rate 0.25 --stockout-mult 1.5
"""
from __future__ import annotations

import argparse
import warnings

warnings.filterwarnings("ignore")

from app.core.config import settings
from app.services import forecasting
from scripts.bench_forecast_scale import _synth_series, _sf_batch


def _money(forecasts, actuals, horizon, *, unit_cost, holding_rate,
           stockout_mult, days_held) -> dict:
    """Cost the forecast error per SKU and sum it.

    forecast is a daily RATE; project it over the horizon to an order qty and
    compare to realised demand over the horizon.
      over  = max(0, ordered - demand)   -> holding cost on excess
      short = max(0, demand - ordered)    -> stockout cost on shortfall
    holding cost = excess * unit_cost * holding_rate * (days_held/365)
    stockout cost = short * unit_cost * stockout_mult
    """
    over_cost = short_cost = over_u = short_u = 0.0
    for rate, act_rate in zip(forecasts, actuals):
        ordered = rate * horizon
        demand = act_rate * horizon
        over = max(0.0, ordered - demand)
        short = max(0.0, demand - ordered)
        over_u += over
        short_u += short
        over_cost += over * unit_cost * holding_rate * (days_held / 365.0)
        short_cost += short * unit_cost * stockout_mult
    return {
        "over_units": over_u, "short_units": short_u,
        "over_cost": over_cost, "short_cost": short_cost,
        "total": over_cost + short_cost,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--skus", type=int, default=1000)
    ap.add_argument("--days", type=int, default=180)
    ap.add_argument("--horizon", type=int, default=14)
    ap.add_argument("--unit-cost", type=float, default=800.0,
                    help="avg landed cost per unit (EUR)")
    ap.add_argument("--holding-rate", type=float, default=0.25,
                    help="annual holding cost as a fraction of unit cost (cash+space+risk)")
    ap.add_argument("--stockout-mult", type=float, default=1.5,
                    help="stockout cost per short unit as a multiple of unit cost")
    ap.add_argument("--days-held", type=int, default=60,
                    help="avg days excess inventory sits before it's consumed")
    args = ap.parse_args()

    a, b = settings.forecast_tsb_alpha, settings.forecast_tsb_beta
    series = [_synth_series(i, args.days) for i in range(args.skus)]
    heads = [s[: -args.horizon] for s in series]
    actuals = [sum(s[-args.horizon:]) / args.horizon for s in series]

    builtin = [forecasting.tsb_daily_rate(h, alpha=a, beta=b) for h in heads]
    sf, _lo, _hi = _sf_batch(heads, level=90)

    kw = dict(unit_cost=args.unit_cost, holding_rate=args.holding_rate,
              stockout_mult=args.stockout_mult, days_held=args.days_held)
    m_builtin = _money(builtin, actuals, args.horizon, **kw)
    m_sf = _money(sf, actuals, args.horizon, **kw)

    print(f"\n=== Forecast money impact: {args.skus} SKUs, one {args.horizon}-day cycle ===")
    print(f"Assumptions: unit_cost EUR{args.unit_cost:,.0f} | holding {args.holding_rate:.0%}/yr"
          f" | stockout x{args.stockout_mult} | excess sits {args.days_held}d\n")
    hdr = f"{'engine':<22}{'over_units':>11}{'short_units':>12}{'over_EUR':>12}{'stockout_EUR':>14}{'TOTAL_EUR':>13}"
    print(hdr)
    for name, m in (("built-in TSB", m_builtin), ("statsforecast SBA", m_sf)):
        print(f"{name:<22}{m['over_units']:>11.0f}{m['short_units']:>12.0f}"
              f"{m['over_cost']:>12,.0f}{m['short_cost']:>14,.0f}{m['total']:>13,.0f}")

    saving = m_builtin["total"] - m_sf["total"]
    pct = (saving / m_builtin["total"] * 100) if m_builtin["total"] else 0.0
    cycles = 365 / args.horizon
    print(f"\nPer-cycle saving (built-in -> statsforecast): EUR {saving:,.0f}  ({pct:.1f}%)")
    print(f"Annualised (x{cycles:.0f} cycles/yr): EUR {saving * cycles:,.0f}")
    print("\nNote: synthetic demand + illustrative cost knobs. Re-run with the "
          "customer's real unit cost / holding rate / stockout multiple to price it.")


if __name__ == "__main__":
    main()
