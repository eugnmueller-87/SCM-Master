"""Weekly purchasing automation — the decision brain.

Collects justified demand over a period, nets it against what's already inbound,
resolves the contracted source, bundles per supplier, asks the copilot to judge
each bundle, classifies into act / propose / escalate, and (only for act, only
when not a dry run) places the PO via the existing purchase-order service.

HARD RULE enforced throughout: a PO is NEVER created or proposed unless it is
backed by a detected, quantified demand trigger. No speculative or round-number
buys — if no trigger fires for a product, it is not bought.

This is a pure SCM-Master service (no n8n, no scheduler here). It reuses the
existing read services and the agent copilot; it does not modify them, and it
performs no schema changes. There is no Contract entity in the model, so the
"contracted source" is the preferred ProductSupplier (contract price, MOQ, lead
time, preference_rank), resolved via the sourcing service.
"""
from __future__ import annotations

import logging
import math
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agent import copilot
from app.agent.schemas import (
    DemandTrigger,
    PurchasingDecision,
    PurchasingRunResult,
)
from app.core.config import settings
from app.models.flow import Asset, AssetStatus
from app.services import planning, sourcing
from app.services.procurement import purchase_order_service

_log = logging.getLogger("app.agent.purchasing")

_ON_HAND = (AssetStatus.RECEIVED, AssetStatus.IN_STORAGE)
_GONE = (AssetStatus.DECOMMISSIONED, AssetStatus.DISPOSED)


# --- Step 1: detect justified need per product ----------------------------

def _now() -> datetime:
    return datetime.now(timezone.utc)


def _lifecycle_replacements(db: Session, since: date) -> dict[str, dict]:
    """Products with units decommissioned/disposed within the period.

    Detected from Asset.decommissioned_date (stamped on the DECOMMISSIONED
    transition) — real data, not a guess. gross need = count * REPLACE_RATIO.
    """
    rows = db.scalars(
        select(Asset).where(
            Asset.status.in_(_GONE),
            Asset.decommissioned_date.is_not(None),
            Asset.decommissioned_date >= since,
        )
    ).all()
    by_product: dict[str, int] = defaultdict(int)
    for a in rows:
        by_product[a.product_id] += 1
    out: dict[str, dict] = {}
    for pid, n in by_product.items():
        need = math.ceil(n * settings.replace_ratio)
        if need > 0:
            out[pid] = {
                "type": "lifecycle_replacement",
                "gross_need": need,
                "evidence": {
                    "decommissioned_in_period": n,
                    "replace_ratio": settings.replace_ratio,
                    "since": since.isoformat(),
                },
            }
    return out


def _reorder_floor_needs(db: Session) -> dict[str, dict]:
    """Products where on-hand + inbound is below the reorder floor.

    Uses asset counts (on-hand) + inbound pipeline (already coming). When the
    floor is 0 (the default) this trigger never fires — by design, so we don't
    invent demand.
    """
    floor = settings.default_reorder_floor
    if floor <= 0:
        return {}

    on_hand: dict[str, int] = defaultdict(int)
    for a in db.scalars(select(Asset).where(Asset.status.in_(_ON_HAND))).all():
        on_hand[a.product_id] += 1

    inbound = _inbound_by_product(db)

    products = set(on_hand) | set(inbound)
    out: dict[str, dict] = {}
    for pid in products:
        have = on_hand.get(pid, 0) + inbound.get(pid, 0)
        if have < floor:
            out[pid] = {
                "type": "reorder_floor",
                "gross_need": floor - have,
                "evidence": {"on_hand": on_hand.get(pid, 0),
                             "inbound": inbound.get(pid, 0), "floor": floor},
            }
    return out


def _forecast_shortfall(db: Session) -> dict[str, dict]:
    """Capacity-driven shortfall: locations projected over capacity.

    A conservative, data-only reading — we only raise a shortfall where the
    planning capacity view already flags ``over_capacity``. Without a per-product
    deployment target in the model we do not fabricate product-level demand here;
    this trigger surfaces the capacity risk for escalation rather than a blind buy.
    Returns an empty dict when nothing is over capacity.
    """
    caps = planning.location_capacity(db)
    over = [c for c in caps if c.get("over_capacity")]
    if not over:
        return {}
    # No product mapping for a location-level breach in the current model, so we
    # do not attach product buys to it; it is reported via the run log/escalation.
    _log.info("forecast_shortfall: over-capacity locations detected",
              extra={"extra_fields": {"over_capacity_locations": [c["code"] for c in over]}})
    return {}


def _inbound_by_product(db: Session) -> dict[str, int]:
    """Outstanding inbound units per product (already on order, not yet received)."""
    out: dict[str, int] = defaultdict(int)
    for row in planning.inbound_pipeline(db):
        out[row["product_id"]] += int(row.get("outstanding", 0))
    return dict(out)


def _detect_needs(db: Session, period_days: int) -> dict[str, dict]:
    """Merge all triggers; first trigger to claim a product wins its justification.

    Priority: lifecycle replacement > reorder floor > forecast shortfall.
    """
    since = date.today() - timedelta(days=period_days)
    needs: dict[str, dict] = {}
    for source in (_lifecycle_replacements(db, since),
                   _reorder_floor_needs(db),
                   _forecast_shortfall(db)):
        for pid, info in source.items():
            needs.setdefault(pid, info)  # don't overwrite a higher-priority trigger
    return needs


# --- Steps 2-6: net, source, bundle, judge, classify, place ---------------

def run_weekly_purchasing(db: Session, *, dry_run: bool = True,
                          period_days: int = 7) -> PurchasingRunResult:
    run_at = _now()
    needs = _detect_needs(db, period_days)
    inbound = _inbound_by_product(db)

    # Step 2: net every gross need against what's already inbound.
    net_needs: dict[str, dict] = {}
    for pid, info in needs.items():
        gross = info["gross_need"]
        already = inbound.get(pid, 0)
        net = gross - already
        if net <= 0:
            _log.info("purchasing: need fully covered by inbound; skipping",
                      extra={"extra_fields": {"product_id": pid, "gross": gross,
                                              "inbound": already}})
            continue
        net_needs[pid] = {**info, "net_need": net, "already_inbound": already}

    # Step 3: resolve contracted source + MOQ rounding + price.
    # Bundle key = supplier_id; products with no source become escalate decisions.
    bundles: dict[str, list[dict]] = defaultdict(list)
    orphan_decisions: list[PurchasingDecision] = []

    for pid, info in net_needs.items():
        ranked = sourcing.suggest_sources(db, pid)
        if not ranked:
            trigger = DemandTrigger(type=info["type"], evidence=info["evidence"])
            orphan_decisions.append(PurchasingDecision(
                product_id=pid, supplier_id=None, qty=info["net_need"],
                unit_price=None, total=0.0, trigger=trigger, tier="escalate",
                confidence=0.0,
                rationale="No contracted source exists for this product — new supplier needed.",
            ))
            _log.info("purchasing: no contracted source -> escalate",
                      extra={"extra_fields": {"product_id": pid, "net_need": info["net_need"]}})
            continue

        src = ranked[0]  # preferred source (already ranked by preference_rank)
        moq = src.get("min_order_quantity") or 1
        qty = max(info["net_need"], moq)
        if moq > 1:
            qty = math.ceil(qty / moq) * moq
        unit_price = float(src["contract_price"]) if src.get("contract_price") is not None else 0.0
        bundles[src["supplier_id"]].append({
            "product_id": pid,
            "product_supplier_id": src["product_supplier_id"],
            "qty": qty,
            "unit_price": unit_price,
            "line_total": qty * unit_price,
            "lead_time_days": src.get("standard_lead_time_days"),
            "trigger": info,
        })

    # Steps 4-6: ONE PO per supplier (required for invoice matching), so the
    # whole supplier bundle is judged and tiered together — a single PO maps to a
    # single tier and a single placed_po_id. The copilot still judges each line
    # (it is product-specific); the bundle's tier is the conservative aggregate.
    decisions: list[PurchasingDecision] = list(orphan_decisions)

    for supplier_id, lines in bundles.items():
        bundle_total = sum(line["line_total"] for line in lines)

        # Step 4: judge each line; collect confidences and per-line agent calls.
        line_confidences: list[float] = []
        worst_decision = "act"  # degrades toward escalate as lines disagree
        for line in lines:
            try:
                rec = copilot.recommend_sourcing(db, line["product_id"], line["qty"])
                line["confidence"] = rec.confidence
                line["agent_decision"] = rec.decision
                line["agent_rationale"] = rec.rationale
            except copilot.AgentError as exc:
                line["confidence"] = 0.0
                line["agent_decision"] = "escalate"
                line["agent_rationale"] = f"copilot unavailable: {exc}"
            line_confidences.append(line["confidence"])
            worst_decision = _worse(worst_decision, line["agent_decision"])

        # Bundle confidence = weakest link (an auto-place is only as safe as its
        # least-confident line). Tier the bundle as a whole.
        bundle_confidence = min(line_confidences) if line_confidences else 0.0
        tier = _classify(
            bundle_total=bundle_total,
            has_source=True,
            agent_decision=worst_decision,
            confidence=bundle_confidence,
        )

        # Step 6: side effects — ONE multi-line PO per supplier for act + live run.
        placed_po_id: Optional[str] = None
        if tier == "act" and not dry_run:
            placed_po_id = _place(db, supplier_id, lines, run_at)

        # Emit one decision per line (preserving per-line trigger/evidence), all
        # sharing the bundle's tier, confidence, and placed_po_id.
        for line in lines:
            trigger = DemandTrigger(type=line["trigger"]["type"],
                                    evidence=line["trigger"]["evidence"])
            justification = (
                f"[{trigger.type}] {trigger.evidence} | net_need={line['qty']} | "
                f"bundle_tier={tier} | {line['agent_rationale']}"
            )
            decisions.append(PurchasingDecision(
                product_id=line["product_id"], supplier_id=supplier_id,
                qty=line["qty"], unit_price=line["unit_price"],
                total=line["line_total"], trigger=trigger, tier=tier,
                confidence=bundle_confidence, rationale=justification,
                placed_po_id=placed_po_id,
            ))

        _log.info("purchasing bundle decision",
                  extra={"extra_fields": {
                      "supplier_id": supplier_id, "lines": len(lines),
                      "bundle_total": bundle_total, "tier": tier,
                      "bundle_confidence": bundle_confidence,
                      "placed_po_id": placed_po_id, "dry_run": dry_run}})

    # Summary — count distinct POs/bundles for placed, lines for the rest.
    placed_pos = {d.placed_po_id for d in decisions if d.placed_po_id}
    act_count = sum(1 for d in decisions if d.tier == "act")
    proposed = sum(1 for d in decisions if d.tier == "propose")
    escalated = sum(1 for d in decisions if d.tier == "escalate")
    total_committed = sum(d.total for d in decisions if d.tier == "act")

    return PurchasingRunResult(
        run_at=run_at, dry_run=dry_run, period_days=period_days,
        decisions=decisions,
        summary={
            "act": act_count, "placed": len(placed_pos), "proposed": proposed,
            "escalated": escalated,
            "total_committed": round(total_committed, 2),
        },
    )


def _worse(a: str, b: str) -> str:
    """Return the more conservative of two agent decisions (escalate > recommend > act)."""
    order = {"act": 0, "recommend": 1, "escalate": 2}
    return a if order[a] >= order[b] else b


def _classify(*, bundle_total: float, has_source: bool,
              agent_decision: str, confidence: float) -> str:
    """Explicit tier gates (Step 5)."""
    # ESCALATE: hard blockers first.
    if not has_source:
        return "escalate"
    if bundle_total >= settings.escalate_spend_threshold:
        return "escalate"
    if confidence < settings.act_confidence_floor:
        return "escalate" if agent_decision == "escalate" else "propose"
    # ACT requires ALL: agent says act, confidence floor met, under the auto cap.
    if (agent_decision == "act"
            and confidence >= settings.act_confidence_floor
            and bundle_total <= settings.auto_place_spend_cap):
        return "act"
    if agent_decision == "escalate":
        return "escalate"
    return "propose"


def _place(db: Session, supplier_id: str, lines: list[dict], run_at: datetime) -> str:
    """Place ONE multi-line PO for the supplier via the existing PO service.

    A single PO per supplier is required for invoice matching (the supplier's
    invoice reconciles against one order), so every line in the bundle goes onto
    the same purchase order.
    """
    order_number = f"AUTO-{run_at.strftime('%Y%m%d%H%M%S')}-{supplier_id[:8]}"
    order = purchase_order_service.create(db, {
        "order_number": order_number,
        "supplier_id": supplier_id,
        "date_ordered": run_at.date(),
        "items": [{
            "product_id": line["product_id"],
            "product_supplier_id": line["product_supplier_id"],
            "quantity": line["qty"],
            "unit_price": line["unit_price"],
        } for line in lines],
    })
    return order.id
