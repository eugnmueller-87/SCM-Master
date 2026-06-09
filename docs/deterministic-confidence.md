# Deterministic confidence — how the auto-place score is computed (and audited)

The auto-place gate compares a buy's **confidence** to a calibrated bar. That
confidence used to be a number the LLM asserted about itself. It is now computed
**deterministically from the buy's evidence** by
[`app/agent/confidence.py`](../backend/app/agent/confidence.py) — same inputs
always yield the same score, and every score carries a factor-by-factor trail so
a reviewer can see *why* it scored what it did. The LLM is advisory only
(narration); it never sets the number the gate reads.

## The policy (decided 2026-06-09 — €200k pending management sign-off)

> Trust the deterministic evidence and auto-place when **confidence ≥ 0.90 AND
> order total < €200,000**; otherwise escalate to a human. The €200k spend ceiling
> is the real brake. Garbage / unavailable LLM advice does NOT block a clean,
> sub-ceiling, high-confidence buy — the evidence still governs.

Config ([`core/config.py`](../backend/app/core/config.py)):
`act_confidence_floor = 0.90`, `auto_place_confidence = 0.90`,
`auto_place_spend_cap = 200000`, `escalate_spend_threshold = 200000`.
**The €200k figure is provisional pending management alignment** — management may
raise it; it is a single env-overridable knob.

## How the score is built

Start at a base (`0.94`) and apply a bounded multiplier per risk lens. A factor
< 1.0 is a reason for *less* confidence; a small > 1.0 is corroboration. Result is
clamped to `[0, 0.99]` (never a falsely certain 1.0).

| Factor | When it pulls down / up |
|---|---|
| **sourcing_depth** | no active source → ×0.55 (blocker); sole source → ×0.96 (mild caution — most procurement is single-source); multi-source → ×1.0 |
| **source_completeness** | missing lead time or price → ×0.82 (one) / ×0.70 (both) — acting on an incomplete contract |
| **demand_basis** | lifecycle replacement ×1.04 / reorder ×1.02 (observed facts) vs forecast_shortfall ×0.97 (a projection) |
| **netting_stakes** | a small residual on top of lots already committed → ×1.02 (low-stakes top-up) |
| **storage_fit** | storage-capped → ×0.90 (only a partial fix; the rest defers) |

Worked examples (verified):
- clean lifecycle, sole-source, full data → **0.938** → act
- clean forecast, sole-source, full data → **0.875** → stages (forecast is less certain; a *borderline* buy that trust can promote)
- source missing lead time → **0.77** → stages
- no active source → **0.35** → escalate

## The audit trail (the "see why" requirement)

`score_line` returns the score AND a list of `Factor` rows (name, observed value,
multiplier, plain-language note). Two surfaces expose it:

1. **`as_dict()["headline"]`** — the single biggest mover in words, e.g. *"source
   contract is missing lead time — acting on incomplete data."* Used in the agent
   rationale.
2. **The PR line rationale suffix** — `_confidence_audit_suffix` appends the
   moving factors to every staged/placed line, e.g.
   `[confidence 0.65: sourcing_depth x0.96, source_completeness x0.82, demand_basis x0.97, storage_fit x0.9]`.
   So the audit shows exactly which factors produced the score that drove the
   auto-place / escalate decision.

This is the same breakdown a future ML calibrator (`services/calibration`) would
re-weight — the factor trail is the feature vector. See
[autonomy-and-learning.md](autonomy-and-learning.md).

## Relationship to the LLM

- The LLM may still narrate and offer an **advisory** decision/rationale; its
  self-reported confidence is recorded as `llm_confidence_advisory` for comparison
  but never gates.
- If the LLM is unavailable or returns garbage, the deterministic score stands and
  the decision follows it (bounded by the spend ceiling) — the LLM failing is not
  a safety brake; the cap and the evidence are.

## Tests

Confidence-driven behaviour is exercised across `tests/test_purchasing_run.py`,
`tests/test_requisitions.py`, `tests/test_requisition_netting.py`, and the
adversarial/correctness scenarios in `tests/agent_eval/`. Scenarios that need a
buy to *stay staged* build a genuinely lower-confidence case (e.g. a source
without a lead time) rather than mocking an LLM number — because the number is no
longer the lever.
