"""Demand-rate estimators — pure, DB-free, unit-testable.

Two methods produce a forward **demand rate (units/day)** from a daily demand
series, plus a Syntetos–Boylan classifier that decides which one fits a SKU:

  - run_rate : recency-weighted deployments/day (the incumbent). Best for SMOOTH
               demand (every period has demand, low variability).
  - tsb      : Teunter–Syntetos–Babai. For INTERMITTENT demand (many zero
               periods): it tracks demand *probability* and *size* separately and
               updates the probability EVERY period (unlike classic Croston, which
               only updates on a demand occurrence and so over-forecasts dying
               SKUs). Reduces to a sensible rate on steady series too.

Neither knows about the EOL replacement term — that is added by the caller and is
method-independent, so it is unaffected by which estimator runs.

The forecast over a horizon is ``rate * horizon`` (+ EOL, added by the caller).
"""
from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from datetime import date

# --- run-rate: recency-weighted deployments/day -----------------------------


def weighted_daily_rate(deploy_dates: list[date], today: date, *,
                        window_days: int, halflife_days: int) -> float:
    """Exponentially recency-weighted deployments/day over the trailing window.

    Each in-window deployment contributes ``exp(-λ·age)`` (λ = ln2/halflife),
    normalised by the decay-integral over the window so the result is units/day.
    This is the incumbent estimator, lifted verbatim from planning._weighted_daily_rate
    so both the live forecast and the backtest share one definition.
    """
    halflife = max(1, halflife_days)
    decay = math.log(2) / halflife
    weighted_events = 0.0
    for d in deploy_dates:
        age = (today - d).days
        if 0 <= age <= window_days:
            weighted_events += math.exp(-decay * age)
    eff_days = (1 - math.exp(-decay * window_days)) / decay
    return weighted_events / eff_days if eff_days > 0 else 0.0


# --- daily-bucket helper ----------------------------------------------------


def daily_series(deploy_dates: list[date], today: date, window_days: int) -> list[int]:
    """Demand bucketed by day over [today-window, today], oldest first.

    Index 0 is the oldest day in the window; the last index is ``today``. A day
    with no deployment is a 0 — which is exactly the signal intermittent methods
    need (and the run-rate ignores).
    """
    series = [0] * (window_days + 1)
    for d in deploy_dates:
        age = (today - d).days
        if 0 <= age <= window_days:
            series[window_days - age] += 1
    return series


# --- TSB (Teunter–Syntetos–Babai) -------------------------------------------


def tsb_daily_rate(series: list[int], *, alpha: float = 0.1, beta: float = 0.1) -> float:
    """TSB forecast of demand/day from a per-day demand series (oldest first).

    Maintains two exponentially-smoothed states:
      p : probability a period has demand   (updated EVERY period)
      z : mean demand SIZE when it occurs   (updated only on demand periods)
    Forecast rate = p * z. Updating p every period (not only on occurrences) is
    what makes TSB decay the forecast for SKUs whose demand has dried up — the
    key fix over classic Croston.

    alpha smooths the probability, beta the size. Defaults (0.1) are the common
    robust choice. Initialised from the series' own demand periods so the first
    forecast isn't cold-started at zero.
    """
    demand_periods = [v for v in series if v > 0]
    if not demand_periods:
        return 0.0
    # Initialise p from the demand frequency, z from the mean demand size.
    p = len(demand_periods) / len(series)
    z = sum(demand_periods) / len(demand_periods)

    for v in series:
        occurred = 1.0 if v > 0 else 0.0
        p = p + alpha * (occurred - p)        # probability updates EVERY period
        if v > 0:
            z = z + beta * (v - z)            # size updates only on occurrences
    return max(0.0, p * z)


# --- Syntetos–Boylan demand classification ----------------------------------


@dataclass(frozen=True)
class DemandClass:
    pattern: str          # "smooth" | "intermittent" | "erratic" | "lumpy"
    adi: float            # average inter-demand interval (periods per demand period)
    cv2: float            # squared coefficient of variation of demand SIZES
    recommended: str      # "run_rate" | "tsb"


# Standard Syntetos–Boylan cutoffs.
_ADI_CUT = 1.32
_CV2_CUT = 0.49


def classify_demand(series: list[int]) -> DemandClass:
    """Classify a demand series and recommend an estimator.

    ADI  = periods / number-of-demand-periods (≥1; higher = more intermittent).
    CV²  = (stdev/mean)² of the NON-ZERO demand sizes (higher = lumpier sizes).

    Syntetos–Boylan quadrants:
      smooth        ADI<1.32, CV²<0.49  -> run_rate
      erratic       ADI<1.32, CV²≥0.49  -> run_rate (frequent; size noise only)
      intermittent  ADI≥1.32, CV²<0.49  -> tsb
      lumpy         ADI≥1.32, CV²≥0.49  -> tsb
    The routing rule: intermittent demand (ADI≥1.32) -> TSB; otherwise run_rate.
    """
    n = len(series)
    demand_periods = [v for v in series if v > 0]
    k = len(demand_periods)
    if k == 0:
        return DemandClass("smooth", adi=float("inf"), cv2=0.0, recommended="run_rate")

    adi = n / k
    mean = sum(demand_periods) / k
    if k > 1 and mean > 0:
        var = sum((v - mean) ** 2 for v in demand_periods) / k
        cv2 = var / (mean ** 2)
    else:
        cv2 = 0.0

    intermittent = adi >= _ADI_CUT
    lumpy = cv2 >= _CV2_CUT
    if intermittent and lumpy:
        pattern = "lumpy"
    elif intermittent:
        pattern = "intermittent"
    elif lumpy:
        pattern = "erratic"
    else:
        pattern = "smooth"
    recommended = "tsb" if intermittent else "run_rate"
    return DemandClass(pattern=pattern, adi=round(adi, 3), cv2=round(cv2, 3),
                       recommended=recommended)


# --- dispatcher -------------------------------------------------------------

VALID_METHODS = ("run_rate", "tsb", "auto")


def daily_rate(method: str, deploy_dates: list[date], today: date, *,
               window_days: int, halflife_days: int,
               tsb_alpha: float = 0.1, tsb_beta: float = 0.1) -> tuple[float, str]:
    """Return (rate_per_day, method_used) for a SKU under the chosen method.

    method:
      "run_rate" -> the recency-weighted rate;
      "tsb"      -> TSB over the daily series;
      "auto"     -> classify the series, then route (intermittent -> tsb,
                    otherwise run_rate). method_used reflects what actually ran.
    """
    if method == "run_rate":
        return weighted_daily_rate(deploy_dates, today, window_days=window_days,
                                   halflife_days=halflife_days), "run_rate"
    series = daily_series(deploy_dates, today, window_days)
    if method == "auto":
        chosen = classify_demand(series).recommended
    elif method == "tsb":
        chosen = "tsb"
    else:
        raise ValueError(f"unknown forecast method {method!r}; expected one of {VALID_METHODS}")

    if chosen == "tsb":
        return tsb_daily_rate(series, alpha=tsb_alpha, beta=tsb_beta), "tsb"
    return weighted_daily_rate(deploy_dates, today, window_days=window_days,
                               halflife_days=halflife_days), "run_rate"


# --- Service-level safety stock ---------------------------------------------


def z_score(service_level: float) -> float:
    """The standard-normal quantile z for a cycle service level (e.g. 0.95 -> 1.645).

    z is the number of standard deviations of lead-time demand to hold so that
    demand is covered with probability = service_level during replenishment.
    Clamped to a sane (0.5, 0.999) range; 0.5 -> z=0 (no buffer), higher -> more.
    """
    sl = min(0.999, max(0.5, service_level))
    return statistics.NormalDist().inv_cdf(sl)


def _lead_time_buckets(daily_demand_series: list[int], lead_time_days: int) -> list[int]:
    """Aggregate the daily series into NON-overlapping lead-time-length buckets.

    Demand-over-lead-time (DLT) is the quantity that matters for a stockout: you
    are exposed for one lead time between placing and receiving. Measuring σ on
    DLT buckets (rather than raw daily) is what captures BATCH-scale variability —
    a project-batched SKU has wildly different lead-time totals (one bucket holds a
    12-unit batch, the next holds 0), which raw daily counts smooth away. Buckets
    are taken from the most recent day backwards so partial history at the old end
    is dropped, not the recent end.
    """
    lt = max(1, lead_time_days)
    out: list[int] = []
    i = len(daily_demand_series)
    while i - lt >= 0:
        out.append(sum(daily_demand_series[i - lt:i]))
        i -= lt
    return out


def safety_stock(daily_demand_series: list[int], lead_time_days: int, *,
                 service_level: float) -> int:
    """Service-level safety stock = z(SL) × σ(demand over lead time), rounded up.

    σ is the std-dev of demand aggregated into lead-time-length buckets (DLT), the
    correct scale: the stockout risk is over one lead time, and bucketing captures
    batch/lumpy variability that a raw-daily σ×√lead would smooth away. We do NOT
    fabricate a lead-time-variability term — the data has a single
    ``standard_lead_time_days`` per source (no lead-time distribution), so that
    term is omitted rather than invented.

    A lumpy SKU (big project batches, many zero periods) has high DLT σ and earns
    a large buffer; steady demand earns a small one; ~constant demand earns ~0.
    Falls back to σ_daily×√lead when there isn't enough history for ≥2 DLT buckets,
    so short series still get a sensible (if smoother) estimate. Returns 0 when
    there is no variability or no lead time.
    """
    if lead_time_days <= 0 or len(daily_demand_series) < 2:
        return 0
    z = z_score(service_level)

    buckets = _lead_time_buckets(daily_demand_series, lead_time_days)
    if len(buckets) >= 2:
        sigma_ltd = statistics.pstdev(buckets)          # σ already at lead-time scale
    else:
        sigma_daily = statistics.pstdev(daily_demand_series)
        sigma_ltd = sigma_daily * math.sqrt(lead_time_days)   # fallback for short history
    if sigma_ltd <= 0:
        return 0
    return max(0, math.ceil(z * sigma_ltd))


# --- ABC classification (Pareto by value) -----------------------------------


def classify_abc(values: dict[str, float], *, a_threshold: float = 0.80,
                 b_threshold: float = 0.95) -> dict[str, str]:
    """Pareto ABC over a {item_id: annualised_value} map -> {item_id: "A"|"B"|"C"}.

    Items are ranked by value descending; an item falls in class A while the
    running cumulative share of total value is ≤ a_threshold (the vital few), B up
    to b_threshold, else C (the trivial many). The boundary item that crosses a
    threshold is included in the higher class. Zero/negative-value items are C.
    Empty input -> empty result.
    """
    ranked = sorted(values.items(), key=lambda kv: kv[1], reverse=True)
    total = sum(v for _, v in ranked if v > 0)
    out: dict[str, str] = {}
    if total <= 0:
        return {k: "C" for k in values}

    cum = 0.0
    for item_id, v in ranked:
        if v <= 0:
            out[item_id] = "C"
            continue
        # Use the cumulative share BEFORE this item to decide its class, so the
        # item that *crosses* a_threshold still counts as A (the vital few rule).
        prev_share = cum / total
        cum += v
        if prev_share < a_threshold:
            out[item_id] = "A"
        elif prev_share < b_threshold:
            out[item_id] = "B"
        else:
            out[item_id] = "C"
    return out
