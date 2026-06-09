# Research report — prior art we can replicate, adapt, or cite

**Status:** private working report
**Question asked:** find ~10 repos/sources where people have *already solved* our
use case — ML / AI-driven autonomous procurement: confidence-scored auto-ordering,
demand forecasting for intermittent SKUs, inventory agents, LLM supply-chain
control towers — so we can replicate, reuse, or position commercially.

**Headline finding.** Nobody open-source has shipped *our exact product*
(a production FastAPI/SQLAlchemy procurement engine with a deterministic,
audited, outcome-calibrated auto-place gate). What exists splits into three
buckets: **(A) production-grade libraries we should adopt as dependencies**,
**(B) research / reference architectures that validate our design and give us
patterns** (but are notebooks or simulators, not deployable), and **(C) a
just-published academic framework that is almost identical to our architecture —
strong validation and a citable blueprint.** That gap *is* the commercial
opening: the ideas are public, a hardened, auditable, sellable implementation is
not.

Companion docs: [references.md](references.md) (methodology spine),
[autonomy-and-learning.md](autonomy-and-learning.md) (our gate + learning + ML
seam).

---

## Scorecard — 12 sources, judged

| # | Source | Category | License · maintained | Verdict for us |
|---|---|---|---|---|
| 1 | **Nixtla `statsforecast`** | Lib | Apache-2.0 · active (v2.0.3, 4.8k★) | **ADOPT** — Croston/SBA/TSB/ADIDA/IMAPA: forecasts the intermittent SKUs we mis-handle today |
| 2 | **Amazon `GluonTS`** | Lib | Apache-2.0 · active | **ADOPT (later)** — probabilistic forecasts; Amazon uses it for *its own* supply-chain demand |
| 3 | **Unit8 `darts`** | Lib | Apache-2.0 · active (8k★) | **EVALUATE** — friendly unified API, wraps statsforecast; good if we want one forecasting facade |
| 4 | **Salesforce `Merlion`** | Lib | BSD-3 · active | **REFERENCE** — its *live-deployment re-training simulation* is the pattern for our shadow-mode eval |
| 5 | **scikit-learn** calibration | Lib | BSD-3 · active | **ADOPT** — `CalibratedClassifierCV` + Brier: the honesty test for a learned confidence |
| 6 | **LightGBM + SHAP** | Lib | MIT · active | **ADOPT** — Layer-1 model + per-decision attribution onto our `Factor` rows |
| 7 | **`ikatsov/tensor-house`** | Ref apps | Apache-2.0 · active (1.4k★) | **REPLICATE PATTERNS** — enterprise SCM notebooks incl. an **LLM control center**; closest to our Control Tower |
| 8 | **`zefang-liu/InvAgent`** | Research | Apache-2.0 · 2024 | **INSIGHT** — LLM multi-agent inventory (AutoGen, IPPO/MAPPO); simulator code, not deployable |
| 9 | **`hubbs5/or-gym`** | Sim env | MIT · active (445★) | **VALIDATE** — canonical OR/RL inventory benchmark; train/test an ordering policy offline |
| 10 | **`kishorkukreja/SupplyChainv0_gym`** | Sim env | (unstated) · evolving | **VALIDATE** — Gym envs (reorder w/ & w/o backlog) built on OR-Gym |
| 11 | **AAIPS — arXiv 2511.23366 (Nov 2025)** | Paper | n/a | **CITE / BLUEPRINT** — agentic replenishment architecture nearly identical to ours |
| 12 | **`KevinFasusi/supplychainpy`** | Lib | BSD-3 · **stale (~2016)** | **REFERENCE** — EOQ/safety-stock/ABC-XYZ API shape only |

Dead end (name collision): `guyeisenkot/supplygoat` — *software-supply-chain
security* lab, archived 2022. Nothing to do with procurement.
Mislabeled: `Harshitha-katturajan/Procurement-ai-agent-` — titled "procurement
agent" but is actually an IndiaMART scraper + RAG demo (single commit, 0★). Skip.

---

## A. Libraries to adopt (production-grade, behind our own service interfaces)

### 1. Nixtla `statsforecast` — the highest-value salvage
<https://github.com/Nixtla/statsforecast> · Apache-2.0 · active.
Provides **CrostonClassic / CrostonOptimized / CrostonSBA / TSB / ADIDA / IMAPA** —
the exact intermittent-demand models matching the Syntetos–Boylan classes we
already compute but don't yet forecast with. Our current recency-weighted usage
rate is the biased method on intermittent SKUs — the root cause of the 135% MAPE.
sklearn-style `.fit()/.predict()`, pandas, probabilistic intervals, optional
(not mandatory) Spark/Dask. **Use:** route only intermittent/lumpy SKUs to
CrostonSBA/TSB; keep the current method for smooth/erratic. Turns the deferred
"Phase-3 demand-class split" into a small wiring job. Isolate behind
`services/forecasting.py`; pin the version (numba first-import cost).

### 2. Amazon `GluonTS`
<https://aws.amazon.com/blogs/opensource/gluon-time-series-open-source-time-series-modeling-toolkit/> ·
Apache-2.0. Probabilistic time-series toolkit Amazon uses for *its own* product
and labor demand. Heavier (deep models) than statsforecast — adopt later only if
we need richer probabilistic forecasts than Croston/SBA give.

### 3. Unit8 `darts`
<https://github.com/unit8co/darts> · Apache-2.0 · 8k★. Unified, friendly
forecasting API that *wraps* statsforecast/pmdarima and adds reconciliation.
**Evaluate** as a single facade if we end up juggling several model families;
otherwise statsforecast direct is leaner.

### 4. Salesforce `Merlion`
<https://github.com/salesforce/Merlion> · BSD-3. Notable for one thing we need:
an **evaluation framework that simulates live deployment and re-training in
production**. That is precisely the shadow-mode / promotion-gate machinery in
`autonomy-and-learning.md`. **Reference** its backtest-as-if-live design.

### 5–6. scikit-learn calibration + LightGBM + SHAP — the ML seam
- scikit-learn `CalibratedClassifierCV` (sigmoid/isotonic) + `brier_score_loss`:
  <https://scikit-learn.org/stable/modules/calibration.html> — proves a learned
  confidence is honest before we trust it (the promotion gate's metric).
- LightGBM (MIT) — learns confidence-factor weights from `DecisionLog ⨝ outcomes`.
- SHAP (MIT) — per-decision attribution, same shape as `confidence.Factor`.
  <https://shap.readthedocs.io/en/latest/example_notebooks/tabular_examples/tree_based_models/Census%20income%20classification%20with%20LightGBM.html>
Plug point: a future `services/calibration_ml.py` implementing the existing
`calibrate()` signature, advisory-only behind a `calibration_mode` flag.

## B. Reference architectures & simulators (patterns/validation, not deployable)

### 7. `ikatsov/tensor-house` — closest to our product shape
<https://github.com/ikatsov/tensor-house> · Apache-2.0 · 1.4k★. Curated
enterprise AI/ML reference apps: single- & multi-echelon inventory optimization,
a supply-chain simulator, demand forecasting, pricing, and — most relevant — an
**LLM-powered control center** where a model dynamically writes Python that calls
multiple APIs to answer operational questions. That is a more advanced cousin of
our cockpit Control Tower. **Replicate patterns** (the LLM-control-center design,
the inventory-optimization notebooks) — they're prototypes (🧪/🚀), not prod code,
so we lift ideas, not files.

### 8. `zefang-liu/InvAgent`
<https://github.com/zefang-liu/InvAgent> · Apache-2.0. LLM multi-agent inventory
management (AutoGen orchestration; IPPO/MAPPO baselines; OpenAI API), built around
a simulator (`src/env.py`). **Insight only:** the multi-agent decomposition
(monitor / forecast / decide) maps onto our pipeline, but it's research code with
no DB/API layer. Borrow the agent-role split, not the code.

### 9–10. RL inventory simulators — `or-gym` & `SupplyChainv0_gym`
- `hubbs5/or-gym` <https://github.com/hubbs5/or-gym> · MIT · 445★ — the de-facto
  OR/RL benchmark (`InvManagement`, `Newsvendor`, `NetworkManagement`).
- `kishorkukreja/SupplyChainv0_gym` <https://github.com/kishorkukreja/SupplyChainv0_gym> —
  Gym envs (reorder with/without backlog) built on OR-Gym + Ray (PPO/DQN).
**Validate, don't ship:** use these offline to stress-test an ordering/safety-stock
policy against stochastic demand & lead times before trusting it live. Good for a
"our policy beats a naive baseline by X%" pitch slide; not part of the product.

## C. The blueprint paper — strong validation

### 11. AAIPS — "Agentic AI Framework for Smart Inventory Replenishment"
arXiv 2511.23366, Nov 2025: <https://arxiv.org/abs/2511.23366>. Describes a
modular agentic architecture: **Inventory Monitoring Agent → Demand Forecasting
Agent → Reorder Decision Agent → Execution module** that places POs, talks to
WMS/ERP, and **feeds fulfillment KPIs back into the forecasting/optimization
loop**. This is essentially our system (triggers → forecast → gate → place →
DecisionLog/outcome feedback) described independently and academically. **Use it
two ways:** (a) cite it to show our design matches the published state of the art;
(b) mine its module boundaries for anything we're missing (e.g. an explicit
supplier-selection agent step).

---

## What this means commercially (the "sell it" angle)

- The **algorithms and architectures are public** (statsforecast, OR-Gym, tensor-house,
  AAIPS). The moat is **not** the math — it's what none of them have:
  a **hardened, auditable, production** system with a *deterministic, explainable,
  outcome-calibrated auto-place gate* and an **append-only decision audit** a CFO
  can defend. That is the defensible, sellable layer.
- **Replicate (legally):** all adopt-list libs are permissive (Apache-2.0/BSD/MIT)
  — safe to depend on in a commercial product. Keep them behind our service
  interfaces so we can swap implementations and so our IP (the gate, the
  calibration policy, the audit) stays ours.
- **Don't** copy notebook/simulator code into the product — wrong shape and it
  dilutes the "production-grade" claim that *is* the value.

## Recommended next actions (priority order)

1. **`statsforecast` for intermittent SKUs** — highest value, lowest risk; fixes
   the MAPE root cause with a textbook method. Behind `forecasting`.
2. **Finish the audit wiring** — persist `confidence.score_line` factors onto
   `DecisionLog` + outcome-capture hook. Prerequisite for *any* learning.
3. **Offline policy validation on OR-Gym** — a credibility slide; no product risk.
4. **Layer-1 calibrator** (only once outcomes exist) — LightGBM + sklearn
   calibration + SHAP, behind `calibrate()`, advisory-only, human-promoted.
5. **Borrow tensor-house's LLM-control-center pattern** for the cockpit, and cite
   AAIPS as design validation in the pitch deck.
