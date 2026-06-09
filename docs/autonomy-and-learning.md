# Autonomy & learning — the auto-place gate, how it learns, and where ML plugs in

This is the architectural close-out for the autonomous purchasing engine. It
answers three questions that keep coming up in pitch conversations (e.g. with a
hosting/datacenter buyer like IONOS, who has a large, real hardware supply
chain):

1. **What decides to auto-place a purchase order vs. ask a human?** (the *gate*)
2. **Does the system learn?** (yes — and it already does, deterministically)
3. **Would a machine-learning layer make sense, and how would it scale?**

The short version: **the LLM advises, deterministic code decides, and the
decision threshold learns from human outcomes — today, with no ML and no
training data required.** Machine learning is a documented *future* layer that
tunes the same threshold once enough real outcomes exist; it never replaces the
auditable rule that executes the spend.

---

## 1. The gate — what it is and what it is not

The "gate" is the rule that sorts every proposed buy into **auto-place** (the
agent converts a staged requisition into a PO by itself) or **escalate** (it
stays staged for a human to approve). It lives in
[`purchasing.run_requisition_cycle`](../backend/app/agent/purchasing.py) and is a
pure, readable rule — no model, no training:

```
auto-place a supplier bundle  ⟺   bundle_confidence ≥ calibrated_bar
                              AND tier ∈ {act, propose}
                              AND bundle_total ≤ auto_place_spend_cap
otherwise → stays STAGED for a human (escalate)
```

Every term is something the system already computes deterministically:

| Term | Where it comes from | Meaning |
|---|---|---|
| `bundle_confidence` | weakest line in the bundle (`_tier_bundle`) | an auto-place is only as safe as its least-confident line |
| `tier` (`act`/`propose`/`escalate`) | `_classify` — hard gates on source, spend, confidence | escalate is the fail-safe default |
| `calibrated_bar` | `calibration.calibrate(db, product, supplier)` | the **learned** threshold (next section) |
| `auto_place_spend_cap` | `settings.auto_place_spend_cap` (€25k) | per-bundle blast-radius cap |
| `escalate_spend_threshold` | `settings.escalate_spend_threshold` (€50k) | hard ceiling: at/above this it *always* escalates |

**What it is:** a deterministic, line-by-line explainable rule. You can point at
the exact clause that fired for any decision. That is what makes it defensible
to a procurement auditor or a customer's CFO.

**What it is not:** it is **not** a confidence score the LLM asserts. The LLM's
self-reported confidence is never trusted for the spend decision — critical
numbers are forced onto computed truth by the grounding guard
([`app/agent/grounding.py`](../backend/app/agent/grounding.py)), and the *gate*
reads deterministic tiers and a learned bar, not a vibe number.

### Fail-closed by construction

The gate is **allow-listed, not block-listed**: a buy auto-places only when it
*matches* the safe rule (has a contracted source, clears the bar, under the
caps). Anything novel, unpriced, over-cap, or low-confidence falls through to a
human by default. The engine fails **toward the human**, never toward spending.

### The kill switch

Live placement is off unless explicitly enabled. The cockpit proxy forces
`dry_run` unless `ALLOW_LIVE_PLACE === "true"`
([`pbi-repo/deploy/server.js`](../pbi-repo/deploy/server.js)), and the backend
defaults every run to dry-run. Flipping the flag reverts the whole engine to
preview-only with no deploy.

---

## 2. The learning loop — already built, no ML

A fixed threshold is dumb: it ignores that humans rubber-stamp some
(product, supplier) buys every time while editing or rejecting others. So the
bar **moves per (product, supplier)** based on the human track record. This is a
real learning system — it just uses a transparent rule instead of a neural net.

```
agent stages a PR  ──▶  human approves / edits / drops / rejects
                              │
                              ▼
                   RequisitionFeedback row (the label)
                              │
                              ▼
        calibration.calibrate() recomputes the bar for that (product, supplier)
                              │
                              ▼
        next run: trusted pairs auto-place more; distrusted pairs wait for a human
```

- **Signal:** one [`RequisitionFeedback`](../backend/app/models/requisition.py)
  row per decided line — `approved` (+1, strongest trust), `edited` (−0.5),
  `dropped` / `rejected` (−1, strongest distrust).
- **Rule:** mean signal in `[-1, 1]` → moves the bar **down** (trust → auto-place
  more) or **up** (distrust → keep escalating), clamped to
  `calibration_max_delta` (±0.10) and never outside `[0.5, 0.99]`.
- **Cold-start guard:** below `calibration_min_samples` (3) rows of history, the
  bar is the global default — the system does **not** move it on noise.
- **Explainable:** `calibrate()` returns the counts, approval rate, trust score,
  and a plain-language `reason` ("Trusted: 5/6 approved unchanged — bar lowered
  85% → 78%"), so the UI can say *why* a buy auto-placed or waited.

This is the honest answer to "does it learn": **yes, and it's running.** It
learns from the one label a running business produces for free — what the
planner did with the proposal — and it needs zero training data to start.

---

## 3. Would an ML layer make sense? Yes — as a tuner, later.

The deterministic calibrator above answers "did the human accept the proposal?"
A machine-learning layer answers a **harder, slower** question the rule can't:
*did the decision turn out right?* — i.e. did the buy actually prevent the
stockout, was the quantity close to real consumption, did we over-order and tie
up capital?

### Why not now (the honest blocker is data, not architecture)

A model that predicts "this decision will turn out well" needs labeled
**outcomes**, and a running business produces them only with a lag:

- The [`DecisionLog`](../backend/app/models/decision.py) records what was decided
  and why (the feature vector), but most rows are dry-run and have no executed
  outcome.
- An outcome resolves *weeks later* — when the PO receives, or a stockout does or
  doesn't happen. You need that join (`DecisionLog ⨝ outcomes`) before any model
  is better than the rule. Train on <100 half-labeled rows and the model is
  worse than the deterministic bar, and confidently so.

So the model is genuinely a **wait-for-data** item. What you build *now* is the
**seam**, not the model.

### Where it plugs in (the seam already exists)

`calibration.calibrate(db, product_id, supplier_id) -> Calibration` is a pure
function returning `adjusted_floor`. An ML calibrator is a **drop-in alternative
implementation of that one function** — same signature, same return type. The
gate (`run_requisition_cycle`) does not change at all. The model:

- trains on `DecisionLog ⨝ outcomes` (a nightly batch job, see scaling below);
- **advises the threshold, never the spend** — it proposes where the bar is
  mis-tuned (over-confident → raise it; needlessly cautious → lower it);
- stays **explainable**: gradient-boosted trees (XGBoost / LightGBM) with SHAP
  per-decision attribution, so the audit story survives. The deterministic rule
  is always what *executes*; the model only tunes its threshold, and a human can
  approve threshold changes.

```
            ┌─ today ──────────────────────────────────────────┐
 feedback → calibration.calibrate() (rule) → adjusted_floor → gate
            └───────────────────────────────────────────────────┘
            ┌─ phase 3 (drop-in, same signature) ───────────────┐
 outcomes → calibration_ml.calibrate() (LightGBM) → adjusted_floor → gate
            └───────────────────────────────────────────────────┘
```

### Why **not** deep learning

Procurement decisions are **tabular and low-frequency** — thousands of rows, not
millions; a handful of decisions per cycle, not a stream. On data of that shape:

- gradient-boosted trees **beat** deep nets and stay inspectable (SHAP);
- a nightly retrain runs in **seconds on CPU** — no GPU, no model server, no
  real-time inference path;
- a neural confidence score is the *opposite* of the auditability the whole
  system is built around — you cannot put "the MLP said 0.87" in front of a
  procurement audit.

Claiming you need deep learning here would be a red flag to a technical buyer,
not a selling point. The defensible line: *"We don't bolt on deep learning for
buzz — procurement data doesn't warrant it. We log the evidence so the gate can
be calibrated against real outcomes with explainable trees, once those outcomes
exist."*

---

## 4. Rolling out in a live business (zero-spend-risk staircase)

You never flip a running business from "humans approve everything" to "machine
auto-spends" in one step — and you don't wait months either. The engine is built
to walk this staircase, and the early phases deliver value at **zero spend
risk**:

| Phase | What runs | Spend risk | What it builds |
|---|---|---|---|
| **0 — Shadow** | Gate computes "would auto-place / would escalate" and logs it next to what the human actually did. Humans still decide everything. | **None** (`dry_run`, `ALLOW_LIVE_PLACE` off) | the labeled history + proof the gate agrees with humans |
| **1 — Assist** | Gate's verdict + plain-language reason shown in the approval UI; humans click, but pre-sorted. | None — still 100% human-authorized | faster review + trust + more `RequisitionFeedback` |
| **2 — Narrow auto** | Auto-place **only** the safe corner: contracted source, under the per-bundle cap, clears the calibrated bar. Everything else escalates. | Bounded by `auto_place_spend_cap` + daily aggregate cap | real auto-place track record on the low-risk slice |
| **3 — Calibrate** | Now `DecisionLog ⨝ outcomes` has real labels → the ML tuner advises threshold moves (humans approve). Widen the auto corner only where outcomes prove the rule right. | Same caps; ML narrows risk, never expands scope on its own | continuous, audited improvement |

### Live-business guardrails (non-negotiable)

- **Per-decision + daily aggregate € caps** — worst-case auto-spend is a number
  the CFO pre-approved, not unbounded.
- **Allow-list, fail-to-human** — novelty and uncertainty escalate by default.
- **Kill switch** — `ALLOW_LIVE_PLACE=false` reverts to dry-run instantly.
- **Reversibility** — an auto-placed PO is a normal PO until received; it can be
  cancelled within the window before it's sent to the supplier.
- **Idempotency** — `_open_staged_pairs` guarantees one open PR per
  (supplier, product), so a re-run never double-spends. Load-bearing in live
  mode, not a nicety.

---

## 5. How it scales at an IONOS-sized supply chain

A demo has a handful of SKUs; a hosting company's procurement is tens of
thousands of part numbers across multiple datacenters and multiple suppliers per
part. What scales as-is, and what needs work before that pitch:

**Scales as-is**
- The *advise / decide* split caps LLM cost and keeps the spend decision in
  auditable code — exactly the enterprise-safe pattern.
- `DecisionLog` append-only audit is already the right shape for enterprise
  governance (immutable who/what/when/why, joins to the PO provenance chain).
- Stateless FastAPI + Postgres → standard horizontal scale.

**Needs work before enterprise volume**
1. **`inventory_position` recomputes the whole book per run** — O(SKUs ×
   suppliers). At enterprise scale, push the MRP decomposition into SQL / a
   materialized position table refreshed on inventory & PO events, computing only
   dirty SKUs. (This is the "build it in SQL?" instinct — now justified by volume,
   not by a single number mismatch.)
2. **LLM narration is the cost wall** — never call the LLM per line at 30k SKUs.
   The deterministic line is always present; the LLM narrates **only escalated
   lines**, which a good gate keeps to a small fraction. The gate's value is cost,
   not just safety: ~95% auto-place silently, ~5% get the expensive narration.
3. **Multi-DC / multi-tenant** — the position model needs a location dimension and
   the gate needs per-region authority limits.
4. **ML as a batch job, not a model server** — a nightly LightGBM retrain on the
   `DecisionLog ⨝ outcomes` join scoring a few thousand candidate decisions runs
   in seconds on CPU. Procurement is low-frequency and tabular; you are nowhere
   near needing deep-learning infrastructure, and saying you do would undercut a
   technical buyer's trust.

---

## TL;DR for the pitch

> Confidence is a **deterministic, auditable gate** from day one — it works on the
> customer's data immediately, no training. It already **learns**: the auto-place
> threshold moves per supplier from what planners actually do with proposals. As
> executed decisions accumulate, a lightweight calibration model (gradient-boosted
> trees, nightly CPU batch, SHAP-explainable) tunes the gate against real
> outcomes — plugging into a function seam that already exists. We deliberately do
> **not** use deep learning for the decision: procurement is tabular and
> low-frequency, the data doesn't warrant it, and a black box wouldn't pass a
> procurement audit. The rule always executes; the model only advises the bar.

### Code map

| Concern | File |
|---|---|
| The gate (auto-place vs. escalate) | [`backend/app/agent/purchasing.py`](../backend/app/agent/purchasing.py) — `run_requisition_cycle`, `_classify`, `_tier_bundle` |
| The learning loop (rule-based, today) | [`backend/app/services/calibration.py`](../backend/app/services/calibration.py) — `calibrate` (the ML seam) |
| The learning signal (labels) | [`backend/app/models/requisition.py`](../backend/app/models/requisition.py) — `RequisitionFeedback` |
| The audit trail (feature vector for ML) | [`backend/app/models/decision.py`](../backend/app/models/decision.py) — `DecisionLog` |
| Deterministic recovery policy | [`backend/app/services/recovery.py`](../backend/app/services/recovery.py) |
| Grounding guard (LLM can't assert critical numbers) | [`backend/app/agent/grounding.py`](../backend/app/agent/grounding.py) |
| Gate knobs | [`backend/app/core/config.py`](../backend/app/core/config.py) — `auto_place_confidence`, `auto_place_spend_cap`, `escalate_spend_threshold`, `calibration_min_samples`, `calibration_max_delta` |
| Kill switch / live gate (cockpit) | [`pbi-repo/deploy/server.js`](../pbi-repo/deploy/server.js) — `ALLOW_LIVE_PLACE` |
