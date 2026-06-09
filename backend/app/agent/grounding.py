"""Grounding guard: force LLM narration onto deterministic ground truth.

The house rule is "the LLM advises, deterministic code decides" — so any
DECISION-CRITICAL number a model emits (a quantity, a shortfall, a cost, a date
a planner acts on) must equal the value computed by code. This module is the one
backstop both AI-narration features share:

  - app.agent.copilot.reason_demand — grounds the model's recommended_qty /
    computed_shortfall against the deterministic demand forecast (this closes a
    pre-existing hole where the model's qty was rendered verbatim);
  - the demand-recovery narration — grounds against the RecoveryRecommendation.

Strategy (matches the design): prefer TEMPLATING (callers fill critical slots
from the computed object so the model never types the number), with this as the
strict backstop — on ANY divergence beyond tolerance in a critical field, the
computed value WINS (the model value is overwritten) and the mismatch is logged
so the override rate is visible. We ground only the named critical fields, not
every number, so legitimate qualitative figures ("ETA 22d") never false-trip.
"""
from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger("app.agent.grounding")


def ground(feature: str, item_id: str, model_obj: dict, computed: dict,
           critical: dict[str, float]) -> tuple[dict, list[dict]]:
    """Force `model_obj`'s critical fields onto `computed`'s values.

    Args:
      feature:  short tag for telemetry (e.g. "demand_reason", "recovery").
      item_id:  the line/product id, for telemetry.
      model_obj: the parsed model output (mutated copy returned).
      computed:  the deterministic ground truth (same keys as `critical`).
      critical:  {field_name: abs_tolerance}. A field diverging beyond its
                 tolerance is overwritten with the computed value.

    Returns (grounded_obj, mismatches). `mismatches` is a list of
    {feature, item_id, field, model_value, computed_value} — empty when clean.
    """
    out = dict(model_obj)
    mismatches: list[dict] = []
    for field, tol in critical.items():
        if field not in computed:
            continue
        truth = computed[field]
        mv = out.get(field)
        if not _within(mv, truth, tol):
            mismatches.append({
                "feature": feature, "item_id": item_id, "field": field,
                "model_value": mv, "computed_value": truth,
            })
            out[field] = truth          # computed value WINS — never the model's
    if mismatches:
        # Telemetry: a rising override rate is a signal, not just a caught error.
        for m in mismatches:
            log.warning("grounding override", extra={"grounding": m})
    return out, mismatches


def _within(model_value: Any, truth: Any, tol: float) -> bool:
    """True if the model value matches truth within tolerance (numeric), else ==."""
    try:
        return abs(float(model_value) - float(truth)) <= tol
    except (TypeError, ValueError):
        return model_value == truth
