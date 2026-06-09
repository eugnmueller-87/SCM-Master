"""Demand-recovery policy — what to do when a line will stock out BEFORE its
inbound PO lands ("0 cover, +40 lands in 22 days").

This is a *deterministic recovery policy*: code decides which lever to pull AND
sizes it; an LLM may only narrate over the result (see app.agent.grounding). The
output is a structured recommendation a planner can audit — every number exposes
the inputs behind it.

Design (kept honest, not invented):
  1. Survival floor      — ceil(burn × gap_days): the non-negotiable "don't hit
     zero before inbound" quantity. Always computed, reported as its own field.
  2. Buffer rebuild      — service-level safety stock over the bridge window
     (reuses forecasting.safety_stock). A DISTINCT component, never merged into
     survival: the planner sees "survive: X / rebuild buffer: Y".
  3. Lever selection     — enumerate the feasible recovery options and score each
     by landed cost, then recommend the CHEAPEST that lands before the dry-out:
       a) Expedite the existing inbound's source (compressed lead, premium price)
       b) Bridge-buy from the next-ranked active alternate source (landed adder)
     Reuses the real cost primitive (unit × qty + a % adder) — no new cost path.
  4. Graceful degradation — when expedite lead/cost or an alternate price is
     missing, fall back to bridge-to-survive and surface expedite as an UNPRICED
     alternative; never silently drop a lever. Every assumed input is recorded.

Pure: no DB, no LLM, no I/O. All inputs are passed in, so it unit-tests in
isolation. inventory_plan() calls recover_line() with live data.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Optional

from app.services import forecasting


@dataclass(frozen=True)
class Source:
    """A resolved product source (from ProductSupplier), only the levers we pull."""
    supplier_name: Optional[str]
    lead_time_days: Optional[int]
    unit_price: Optional[float]
    moq: int = 1


@dataclass(frozen=True)
class RecoveryConfig:
    service_level: float          # buffer-rebuild service level
    expedite_lead_compression: float
    expedite_premium_pct: float
    landed_cost_adder_pct: float


def _round_to_moq(qty: int, moq: int) -> int:
    moq = max(1, moq or 1)
    return int(math.ceil(qty / moq) * moq) if qty > 0 else 0


def recover_line(
    *,
    on_hand: int,
    daily_burn: float,
    next_eta: Optional[date],
    on_order: int,
    today: date,
    primary: Optional[Source],
    alternate: Optional[Source],
    variability_series: list[int],
    cfg: RecoveryConfig,
) -> Optional[dict]:
    """Recovery recommendation for one line, or None if it is NOT at risk.

    "At risk" = it has a burn, an inbound is on the way, and current cover runs
    out strictly before that inbound lands (the gap we must bridge). A line with
    no burn, no inbound, or cover that already reaches the ETA is not at risk.
    """
    # Not actionable without a burn (can't run dry) — and no inbound means this is
    # a plain reorder, handled by the reorder-point logic, not a "bridge" case.
    if daily_burn <= 0 or on_order <= 0 or next_eta is None:
        return None

    cover_days = on_hand / daily_burn
    eta_days = (next_eta - today).days
    # If current stock already covers the wait, there's nothing to bridge.
    if cover_days >= eta_days:
        return None

    dry_out_date = today + timedelta(days=int(cover_days))
    gap_days = max(0, eta_days - int(cover_days))

    # 1. Survival floor — exactly enough to not hit zero before inbound lands.
    survival_qty = int(math.ceil(daily_burn * gap_days))

    # 2. Buffer rebuild — service-level safety over the bridge window (distinct).
    buffer_rebuild_qty = forecasting.safety_stock(
        variability_series, max(1, gap_days), service_level=cfg.service_level)

    assumptions: list[str] = []
    options: list[dict] = []

    # 3a. Expedite the existing inbound's source: compress lead, premium price.
    if primary is not None:
        std_lead = primary.lead_time_days
        if std_lead and std_lead > 0:
            exp_lead = max(1, int(round(std_lead * cfg.expedite_lead_compression)))
            assumptions.append(
                f"expedite lead ≈ {exp_lead}d ({int(cfg.expedite_lead_compression*100)}% of "
                f"{std_lead}d standard — no expedite SLA on file)")
        else:
            exp_lead = None
            assumptions.append("expedite lead unknown (no standard lead on primary source)")
        land = today + timedelta(days=exp_lead) if exp_lead is not None else None
        qty = _round_to_moq(max(survival_qty, 1), primary.moq)
        if primary.unit_price is not None:
            unit = primary.unit_price * (1 + cfg.expedite_premium_pct)
            assumptions.append(f"expedite premium +{int(cfg.expedite_premium_pct*100)}% on unit price")
            landed = round(unit * qty, 2)
            unpriced = False
        else:
            unit = landed = None
            unpriced = True
            assumptions.append("expedite cost unpriced (no contract price on primary source)")
        options.append({
            "lever": "expedite", "source": primary.supplier_name, "qty": qty,
            "unit_cost": round(unit, 2) if unit is not None else None,
            "landed_cost": landed, "land_date": land,
            "feasible": (land is not None and land <= dry_out_date),
            "unpriced": unpriced,
        })

    # 3b. Bridge-buy from the next-ranked active alternate source.
    if alternate is not None:
        alt_lead = alternate.lead_time_days
        land = today + timedelta(days=alt_lead) if alt_lead and alt_lead > 0 else None
        qty = _round_to_moq(max(survival_qty, 1), alternate.moq)
        if alternate.unit_price is not None:
            unit = alternate.unit_price * (1 + cfg.landed_cost_adder_pct)
            assumptions.append(f"bridge landed adder +{int(cfg.landed_cost_adder_pct*100)}% (duties/freight)")
            landed = round(unit * qty, 2)
            unpriced = False
        else:
            unit = landed = None
            unpriced = True
            assumptions.append("bridge cost unpriced (no contract price on alternate source)")
        options.append({
            "lever": "bridge_buy", "source": alternate.supplier_name, "qty": qty,
            "unit_cost": round(unit, 2) if unit is not None else None,
            "landed_cost": landed, "land_date": land,
            "feasible": (land is not None and land <= dry_out_date),
            "unpriced": unpriced,
        })

    # 5. Recommend the cheapest FEASIBLE priced option; prefer expedite on a tie.
    priced_feasible = [o for o in options if o["feasible"] and not o["unpriced"]]
    recommended = None
    if priced_feasible:
        recommended = min(
            priced_feasible,
            key=lambda o: (o["landed_cost"], 0 if o["lever"] == "expedite" else 1))
    else:
        # 4. Graceful degradation: nothing priced+feasible. Bridge to survive on the
        #    cheapest priced option regardless of landing; else the first option as
        #    an explicit unpriced fallback. Expedite stays surfaced in options[].
        priced = [o for o in options if not o["unpriced"]]
        if priced:
            recommended = min(priced, key=lambda o: o["landed_cost"])
            assumptions.append("no option lands before dry-out — recommending cheapest available; expedite to close the gap")
        elif options:
            recommended = options[0]
            assumptions.append("recovery options unpriced — sizing the survival buy; price the source to compare levers")

    summary = _summary(
        survival_qty, buffer_rebuild_qty, gap_days, dry_out_date, next_eta, recommended)

    return {
        "at_risk": True,
        "dry_out_date": dry_out_date,
        "inbound_land_date": next_eta,
        "gap_days": gap_days,
        "survival_qty": survival_qty,
        "buffer_rebuild_qty": buffer_rebuild_qty,
        "recommended": recommended,
        "options": options,
        "assumptions": assumptions,
        "summary": summary,
    }


def _summary(survival_qty, buffer_rebuild, gap_days, dry_out, inbound, rec) -> str:
    """Deterministic, decision-complete one-liner — readable with AI off."""
    base = (f"Runs dry {dry_out.isoformat()} — {gap_days}d before inbound lands "
            f"{inbound.isoformat()}. Bridge {survival_qty} unit(s) to survive"
            f"{f' (+{buffer_rebuild} to rebuild buffer)' if buffer_rebuild else ''}.")
    if not rec:
        return base + " No recovery source on file — add one to act."
    lever = "Expedite" if rec["lever"] == "expedite" else "Bridge-buy"
    src = rec["source"] or "source"
    cost = f" · landed ≈ €{rec['landed_cost']:,.0f}" if rec.get("landed_cost") is not None else " · unpriced"
    land = f", lands {rec['land_date'].isoformat()}" if rec.get("land_date") else ""
    return f"{base} → {lever} {rec['qty']} via {src}{land}{cost}."
