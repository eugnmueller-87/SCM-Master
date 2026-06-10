"""Deterministic confidence — how sure we are about ONE proposed purchase line,
computed from the evidence the planning model already produced (not asserted by
an LLM).

Why this exists
---------------
The auto-place gate (``purchasing.run_requisition_cycle``) compares a line's
confidence to a *learned* bar (``services/calibration``). Historically the
confidence on the LLM path was a number the model wrote about itself, and on the
deterministic path it was a flat constant — neither was grounded in the actual
risk of the buy. This module replaces that with a confidence DERIVED from
factors the system already computes, so the same evidence always yields the same
score, and every score can be explained.

Design (kept honest)
--------------------
Confidence starts at a base and each factor applies a bounded multiplier in
roughly [0.5, 1.05]. A factor < 1.0 is a reason for *less* confidence (a risk);
a factor slightly > 1.0 is a corroborating signal (a hard, observed trigger).
The factors are independent risk lenses — sourcing risk, data completeness,
demand basis, netting stakes, storage fit, price certainty — so multiplying them
compounds risk the way it actually compounds: two weak signals are worse than
one. The result is clamped to [0.0, 0.99] (never a certain 1.0 — there is always
residual real-world risk).

Auditable by construction: ``score_line`` returns the final score AND a list of
``Factor`` rows (name, observed value, multiplier, human note). That breakdown is
persisted on the DecisionLog so an auditor can read exactly why a line scored
what it did — and therefore why it auto-placed or escalated.

Pure: no DB, no LLM, no I/O. All inputs are passed in, so it unit-tests in
isolation. ``purchasing._compute_bundles`` builds the inputs from live data.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

# Base confidence before any factor. A clean buy — hard trigger, a contracted
# source with full data, the full need fits — SHOULD clear the act floor (0.90,
# the "trust everything above 90%" policy) on merit, because that is exactly the
# case automation is for. Genuine risks (missing source data, no source at all,
# storage-capped, forecast-only basis) each pull it down from here. Sole-source is
# a MILD caution, not a disqualifier: most real procurement is single-source, so
# it must not by itself sink an otherwise-clean buy below the floor.
_BASE = 0.94


@dataclass(frozen=True)
class Factor:
    """One risk lens and its effect on the score — the audit unit."""
    name: str              # stable key, e.g. "sourcing_depth"
    value: str             # the observed evidence, human-readable ("sole source")
    multiplier: float      # what it did to the running score (1.0 = neutral)
    note: str              # one line: why this value moves confidence this way

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "value": self.value,
            "multiplier": round(self.multiplier, 3),
            "note": self.note,
        }


@dataclass
class ConfidenceScore:
    """The final confidence and the factor trail that produced it."""
    score: float
    base: float
    factors: list[Factor] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "score": round(self.score, 3),
            "base": round(self.base, 3),
            "factors": [f.as_dict() for f in self.factors],
            # A one-line plain-language summary of the biggest mover, for the UI.
            "headline": self._headline(),
        }

    def _headline(self) -> str:
        if not self.factors:
            return f"Base confidence {self.base:.0%} — no modifying factors."
        # The factor furthest from neutral (in either direction) is the story.
        mover = min(self.factors, key=lambda f: f.multiplier)
        if mover.multiplier >= 0.99:
            up = max(self.factors, key=lambda f: f.multiplier)
            return f"{up.note}"
        return mover.note


# Hard, observed triggers are more trustworthy than a statistical projection:
# a decommission actually happened; a forecast might not. This corroborates.
_TRIGGER_WEIGHT = {
    "lifecycle_replacement": (1.04, "replacements are backed by units actually decommissioned"),
    "reorder_floor": (1.02, "below a configured reorder floor — an observed level, not a projection"),
    "forecast_shortfall": (0.97, "need is a forecast projection, not yet observed demand"),
}


def score_line(
    *,
    trigger_type: str,
    active_source_count: int,
    chosen_lead_time_days: Optional[int],
    chosen_unit_price: Optional[float],
    net_requirement: int,
    already_committed: int,
    storage_capped: bool,
) -> ConfidenceScore:
    """Confidence for one proposed purchase line, with its factor trail.

    Inputs are all things ``_compute_bundles`` already has in hand:
      - ``trigger_type``        why the buy is justified (hard fact vs. projection)
      - ``active_source_count`` how many active sources exist (sole-source risk)
      - ``chosen_lead_time_days`` / ``chosen_unit_price`` of the picked source
        (None means the contract is missing that field — we're guessing)
      - ``net_requirement``     the netted shortfall before this proposal
      - ``already_committed``   on_order + open staged for this product
      - ``storage_capped``      True if we could NOT order the full need (partial)
    """
    factors: list[Factor] = []
    score = _BASE

    # 1. Sourcing depth — no source is a hard blocker; sole source is a MILD
    #    caution (no fallback if the supplier slips), not a disqualifier;
    #    multi-source is the safest and gets a small corroboration.
    if active_source_count <= 0:
        m, note, val = 0.55, "no active contracted source — cannot safely auto-place", "no source"
    elif active_source_count == 1:
        m, note, val = 0.96, "sole source — no fallback if this supplier slips (normal, mild caution)", "sole source"
    else:
        m, note, val = 1.0, f"{active_source_count} active sources — a fallback exists", f"{active_source_count} sources"
    factors.append(Factor("sourcing_depth", val, m, note))
    score *= m

    # 2. Source data completeness — a missing lead time or price means we are
    #    acting on an incomplete contract; lower confidence until it's filled in.
    missing = []
    if chosen_lead_time_days is None or chosen_lead_time_days <= 0:
        missing.append("lead time")
    if chosen_unit_price is None or chosen_unit_price <= 0:
        missing.append("contract price")
    if missing:
        m = 0.82 if len(missing) == 1 else 0.70
        note = f"source contract is missing {', '.join(missing)} — acting on incomplete data"
        factors.append(Factor("source_completeness", ", ".join(missing) + " missing", m, note))
        score *= m
    else:
        factors.append(Factor("source_completeness", "complete", 1.0,
                              "source has both lead time and contract price"))

    # 3. Demand basis — an observed trigger corroborates; a projection tempers.
    m, why = _TRIGGER_WEIGHT.get(trigger_type, (1.0, "demand basis"))
    factors.append(Factor("demand_basis", trigger_type, m, why))
    score *= m

    # 4. Netting stakes — a small residual on top of a lot already committed is a
    #    low-stakes top-up (most of the need is already handled); a buy where
    #    nothing is committed yet is the whole bet, so it carries full weight.
    total_need = max(1, net_requirement + already_committed)
    fresh_share = net_requirement / total_need
    if already_committed > 0 and fresh_share <= 0.34:
        m = 1.02
        note = (f"top-up: {net_requirement} on top of {already_committed} already committed "
                f"({fresh_share:.0%} of need is fresh) — most of the need is already handled")
        factors.append(Factor("netting_stakes", f"{fresh_share:.0%} fresh", m, note))
        score *= m
    else:
        factors.append(Factor("netting_stakes", f"{fresh_share:.0%} fresh", 1.0,
                              "this proposal carries the bulk of the unmet need"))

    # 5. Storage fit — a storage-capped line only PARTIALLY solves the need; the
    #    deferred remainder will re-surface, so it's a less complete decision.
    if storage_capped:
        m = 0.90
        note = "capped to warehouse headroom — only a partial fix; the rest defers"
        factors.append(Factor("storage_fit", "capped", m, note))
        score *= m
    else:
        factors.append(Factor("storage_fit", "fits", 1.0, "the full need fits warehouse headroom"))

    # Clamp: never below 0, never a falsely certain 1.0.
    score = max(0.0, min(0.99, score))
    return ConfidenceScore(score=score, base=_BASE, factors=factors)
