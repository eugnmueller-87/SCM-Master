# References — the methodology behind the engine

The authoritative sources for what SCM-Master's decision engine does, organized
by the part of the system each one grounds. These are here so the design is
*citable* (in a pitch, an audit, or a code review) rather than asserted — and so
the next person can check that what the code does matches the accepted method.

Two kinds of source are listed, kept separate on purpose:

- **Methodology** — the books/papers that define the correct method. Cite these.
- **Code / pattern** — libraries and examples we borrow *patterns* from when we
  build the ML calibration layer. We do **not** vendor anyone's notebook code;
  we adopt mature libraries (scikit-learn / LightGBM / SHAP) at the seam.

---

## 1. Forecasting & demand classification

Grounds: `services/forecasting.py`, `services/accuracy.py` (WMAPE/bias/MASE,
Syntetos–Boylan demand classes), `_forecast_shortfall` in `agent/purchasing.py`.

**Methodology**

- **Hyndman & Athanasopoulos — *Forecasting: Principles and Practice* (3rd ed.)**
  — the canonical, free, continuously-updated forecasting text. The spine for
  error metrics (why WMAPE/MASE over MAPE), seasonality, and method selection.
  <https://otexts.com/fpp3/>
  (Error-measures chapter is the direct justification for demoting MAPE on
  intermittent SKUs — our Forecast-tab rework.)

- **Syntetos & Boylan — intermittent-demand classification (ADI / CV²) and the
  SBA forecast.** The basis for splitting demand into smooth / erratic /
  intermittent / lumpy, and for why Croston is biased and SBA corrects it. This
  is exactly the classification in `accuracy.classify_demand`.
  - Empirical study (open PDF): <http://www.msc-les.org/proceedings/mas/2012/MAS2012_367.pdf>
  - Gardner & others, intermittent demand forecasting (open PDF):
    <https://www.bauer.uh.edu/egardner/3301H%20Operations%20Management/ESG%20Publications/2015%20Intermittent%20demand%20forecasting.pdf>
  - Spare-parts SKU optimization (recent, MDPI, open): <https://www.mdpi.com/2076-3417/15/22/12030>

  *Use:* confirms the ADI/CV² thresholds and that intermittent/lumpy SKUs need a
  different accuracy lens (MASE) — the optional Phase-3 forecast split.

## 2. Inventory control & safety stock

Grounds: `forecasting.safety_stock` (service-level `z(SL)·σ`), the survival-floor
and buffer-rebuild split in `services/recovery.py`, reorder logic.

**Methodology**

- **Silver, Pyke & Peterson — *Inventory Management and Production Planning and
  Scheduling* (3rd ed., Wiley, 1998).** The standard reference for safety stock,
  service levels, the protection interval (T+L), reorder points, and MRP. This is
  the textbook our safety-stock and recovery math should match.
  <https://www.scirp.org/reference/referencespapers?referenceid=1568920>

  *Use:* the "safety stock over the protection interval" definition is the check
  for `recover_line`'s buffer-rebuild component; the order-up-to-level logic backs
  the inventory-position decomposition.

## 3. Confidence calibration (the current build + the ML seam)

Grounds: `agent/confidence.py` (deterministic `score_line` — the named factors),
and the future `calibration_ml` that re-weights those factors from outcomes
(`services/calibration.py` is the seam).

**Methodology**

- **scikit-learn — Probability Calibration (user guide).** The authoritative,
  practical reference: reliability diagrams, isotonic vs. Platt scaling, when each
  applies, and `CalibratedClassifierCV`. This defines *how we prove* a learned
  confidence is honest before trusting it.
  <https://scikit-learn.org/stable/modules/calibration.html>

  *Use:* the shadow-mode promotion gate in `docs/autonomy-and-learning.md` —
  "ML calibration ≥ rule on held-out outcomes (Brier / reliability)" — is measured
  with exactly these tools. Brier score = the single number that says the score is
  trustworthy; reliability diagram = the picture a human reads before flipping the
  mode flag.

**Code / pattern** (adopt as dependencies when building Layer 1, do not vendor)

- **LightGBM** — gradient-boosted trees, the right model for tabular,
  low-frequency procurement data (not deep learning). Train on
  `DecisionLog ⨝ outcomes`. Calibration discussion (Platt/isotonic on LGBM):
  <https://github.com/microsoft/LightGBM/issues/1562>
- **SHAP** — per-decision factor attribution. Output maps 1:1 onto our existing
  `confidence.Factor` audit rows, so the model stays as explainable as the rule.
  LightGBM worked example: <https://shap.readthedocs.io/en/latest/example_notebooks/tabular_examples/tree_based_models/Census%20income%20classification%20with%20LightGBM.html>

## 4. Supply-chain libraries surveyed (reference only — NOT salvaged)

Surveyed when asked to find code to reuse. Verdict: **nothing wholesale-salvageable** —
the ML ones are portfolio notebooks (static CSV → one-off fit), the wrong shape
for a live transactional system, and the LSTM ones are the deep-learning-on-
tabular anti-pattern our own design argues against. Listed so the survey isn't
repeated.

- **KevinFasusi/supplychainpy** — the only *actual library* (exp. smoothing,
  Holt's, EOQ, ABC/XYZ). Excel/VBA-workflow oriented; overlaps what we built.
  Worth reading for API shape, not depending on.
  <https://github.com/KevinFasusi/supplychainpy>
- josericodata/SupplyChainDataModelling — LSTM demand forecast (the anti-pattern):
  <https://github.com/josericodata/SupplyChainDataModelling>
- ankitrajsh/Supply-Chain-Optimization, A1fred00 — Kaggle-style notebooks:
  <https://github.com/ankitrajsh/Supply-Chain-Optimization>

**Not relevant — name collision:** `guyeisenkot/supplygoat` is a *software
supply-chain security* training tool (Terraform/K8s misconfig lab), nothing to do
with procurement/inventory. Archived 2022.

---

## How these map to the system

| System part | Method source | File |
|---|---|---|
| Forecast error metrics (WMAPE/bias/MASE) | Hyndman & Athanasopoulos §error-measures | `services/accuracy.py` |
| Demand classification (smooth/erratic/intermittent/lumpy) | Syntetos & Boylan (ADI/CV²) | `services/accuracy.py` |
| Safety stock / service level | Silver, Pyke & Peterson | `services/forecasting.py` |
| Survival floor + buffer rebuild | Silver, Pyke & Peterson (protection interval) | `services/recovery.py` |
| Deterministic confidence factors | (our design — domain rules) | `agent/confidence.py` |
| Confidence calibration / honesty test | scikit-learn calibration guide; Brier score | `services/calibration.py` (+ future `calibration_ml.py`) |
| Learned factor weights + attribution | LightGBM + SHAP | future `calibration_ml.py` |

See also [autonomy-and-learning.md](autonomy-and-learning.md) for how the
deterministic gate, the rule-based calibration, and the ML seam fit together.
