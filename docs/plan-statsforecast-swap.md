# Plan — replace the statistical forecast engine with `statsforecast`

**Decision (locked):**
- Replace the statistical estimator engine with **Nixtla `statsforecast`**
  (Croston / CrostonSBA / TSB / ADIDA / IMAPA + probabilistic intervals).
- **Reserve `mlforecast`** for the future advisory layer — NOT in scope now.
- **Keep the gate and calibration EXACTLY as they are** — untouched.

## Hard constraints

1. **The gate, calibration, audit, recovery, and Syntetos–Boylan ROUTING are
   untouched.** We swap only the per-series rate estimator.
2. **`planning.py` does not change.** It calls
   `forecasting.daily_rate(method, deploy_dates, today, *, window_days,
   halflife_days, tsb_alpha, tsb_beta) -> (rate, method_used)`
   ([planning.py:906](../backend/app/services/planning.py#L906)) — that signature
   is preserved so the swap is invisible to every caller.
3. **Prove-before-trust:** statsforecast runs behind a config flag, default OFF
   (built-in engine), until a backtest on real data shows it matches/beats the
   incumbent. Instant rollback via the flag (same discipline as
   `ALLOW_LIVE_PLACE`).
4. **Isolation:** all statsforecast imports live in ONE adapter module. Nothing
   else imports the library, so it can be swapped or removed without touching the
   rest of the system.

## Integration contract (what must stay stable)

| Consumer | Call | Must keep returning |
|---|---|---|
| `planning.demand_forecast` ([:906](../backend/app/services/planning.py#L906)) | `daily_rate(...)` | `(rate_per_day: float, method_used: str)` |
| `planning` safety stock ([:588](../backend/app/services/planning.py#L588)) | `daily_series` + `safety_stock` | unchanged (Phase 4 adds an interval-based path behind the flag) |
| `accuracy` backtest | `forecast_method` tag | unchanged |

## Phases

### Phase 1 — adapter module (no behavior change)
- Add `statsforecast` (pinned) to `backend/requirements.txt`.
- New `backend/app/services/forecasting_sf.py` — the ONLY file importing
  statsforecast. Exposes:
  - `sf_daily_rate(series, *, model) -> (rate, method_used)` — bucket the existing
    daily series into the SF dataframe, fit the model, return the point rate +
    `method_used` tag (e.g. `"sf_croston_sba"`).
  - `sf_rate_with_interval(series, *, model, level) -> (rate, lo, hi)` — the
    probabilistic output (used in Phase 4).
- Map our Syntetos–Boylan route → SF model: intermittent/lumpy → `CrostonSBA`
  (or `TSB`), smooth/erratic → keep run-rate (SF adds nothing there).
- Nothing calls it yet. Zero runtime impact.

### Phase 2 — backtest / comparison (the de-risk gate)
- A test + small script: over real demand history, compare
  **built-in TSB vs. statsforecast** per SKU — agreement, divergence, and a
  hold-out backtest (which forecasts actual demand better). Output a short report.
- **Stop here for review.** No default flips until the evidence is read.

### Phase 3 — wire behind a flag (default built-in)
- `settings.forecast_engine: "builtin" | "statsforecast"` (default `"builtin"`).
- `daily_rate` dispatches to `forecasting_sf` when the flag is on AND the routed
  method is intermittent; otherwise the incumbent path runs unchanged.
- `method_used` reflects the real engine so the backtest/accuracy tags stay honest.

### Phase 4 — probabilistic safety stock (the value-add)
- Behind the same flag, feed SF's prediction interval into safety stock as an
  alternative to the DLT-σ heuristic. Built-in σ path stays the default fallback.

### Phase 5 — attribution + docs
- `backend/THIRD_PARTY_LICENSES.md` (statsforecast = Apache-2.0; the one OSS
  obligation).
- Update `references.md` with the swap rationale + the Phase-2 evidence.
- Note `mlforecast` reserved for the advisory layer (not adopted).

## Out of scope (explicit)
- `mlforecast`, the ML calibrator, the gate, calibration, recovery, the audit
  wiring, and any cockpit change. This plan is forecasting-engine only.

## Test-first
Each phase lands with tests green. Phase 1–2 add no risk to running behavior;
Phase 3 changes nothing until the flag is flipped.
