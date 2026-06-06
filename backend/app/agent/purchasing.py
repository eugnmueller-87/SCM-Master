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
    """Demand-driven shortfall from the usage-based forecast.

    For each product the forecast projects demand (recency-weighted usage rate +
    end-of-life replacement) over the horizon. Where projected demand exceeds
    what's on hand, we raise a quantified ``forecast_shortfall`` need. We use
    ``projected_demand - on_hand`` as the GROSS need so the purchasing run's own
    inbound-netting (Step 2) subtracts open orders once — no double-counting with
    the forecast's own available figure.
    """
    import math

    out: dict[str, dict] = {}
    for row in planning.demand_forecast(db):
        gross = row["projected_demand"] - row["on_hand"]
        if gross <= 0:
            continue
        out[row["product_id"]] = {
            "type": "forecast_shortfall",
            "gross_need": int(math.ceil(gross)),
            "evidence": {
                "usage_rate_per_day": row["usage_rate_per_day"],
                "horizon_days": row["horizon_days"],
                "projected_demand": row["projected_demand"],
                "eol_replacement": row["eol_replacement"],
                "on_hand": row["on_hand"],
                "on_order": row["on_order"],
            },
        }
    return out


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
                          period_days: int = 7,
                          approve_suppliers: Optional[set[str]] = None) -> PurchasingRunResult:
    """Run the purchasing automation.

    Normal run:
      - dry_run=True  -> compute and tier bundles, place nothing (preview);
      - dry_run=False -> auto-place every ACT-tier supplier bundle.

    Confirm run (``approve_suppliers`` given) — the human approve->place path for
    the review screen: the run is recomputed from live data (so a stale or
    no-longer-justified approval can't place a wrong PO), and a supplier's PO is
    placed ONLY when that supplier is in ``approve_suppliers`` AND its recomputed
    tier is placeable (``act`` or ``propose`` — a human approving a proposal is
    valid). An ``escalate`` bundle is NEVER placed by approval (no source, over
    threshold, or blocked); it is returned unplaced for the UI to surface.
    """
    run_at = _now()
    confirming = approve_suppliers is not None
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

        # Step 6: side effects — ONE multi-line PO per supplier.
        #   Normal run:  auto-place ACT bundles on a live (non-dry) run.
        #   Confirm run: place a bundle iff this supplier was approved AND its
        #                recomputed tier is placeable (act/propose, never escalate).
        placed_po_id: Optional[str] = None
        if confirming:
            if supplier_id in approve_suppliers and tier in ("act", "propose"):
                placed_po_id = _place(db, supplier_id, lines, run_at)
        elif tier == "act" and not dry_run:
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
                      "placed_po_id": placed_po_id,
                      "mode": "confirm" if confirming else ("dry_run" if dry_run else "live")}})

    # Summary — count distinct POs for placed; committed spend = what was placed.
    placed_pos = {d.placed_po_id for d in decisions if d.placed_po_id}
    act_count = sum(1 for d in decisions if d.tier == "act")
    proposed = sum(1 for d in decisions if d.tier == "propose")
    escalated = sum(1 for d in decisions if d.tier == "escalate")
    total_committed = sum(d.total for d in decisions if d.placed_po_id)

    return PurchasingRunResult(
        # A confirm run places POs, so it is never a dry run.
        run_at=run_at, dry_run=(dry_run and not confirming), period_days=period_days,
        decisions=decisions,
        summary={
            "act": act_count, "placed": len(placed_pos), "proposed": proposed,
            "escalated": escalated,
            "total_committed": round(total_committed, 2),
        },
    )


def run_requisition_cycle(db: Session, *, period_days: int = 7,
                          actor: Optional[str] = None) -> dict:
    """Stage Purchase Requisitions from detected demand, auto-placing the ones
    that clear their *calibrated* confidence bar.

    The PR/PO distinction made concrete: this never edits a PO directly. For each
    supplier bundle it stages an editable PR (the cart). It then asks the
    calibration service for that supplier's learned auto-place bar per line
    (lowered for trusted product/supplier pairs, raised for ones humans keep
    editing). If the bundle's confidence clears the bar AND the bundle is
    placeable (act/propose, under spend caps), the PR is auto-approved into a PO
    in the same run (reversible: it's a normal PO until received). Otherwise the
    PR stays STAGED for a human.

    Returns a summary {staged, auto_placed, requisition_ids}. Idempotent only in
    the sense that it consumes current demand — running it twice may stage more.
    """
    from app.services import calibration as _calib
    from app.services.requisition import requisition_service as _req

    bundles, orphans = _compute_bundles(db, period_days)

    staged_ids: list[str] = []
    auto_ids: list[str] = []

    # Orphans (no contracted source) can't be staged as a PR — a PR is keyed to a
    # supplier and there is none. They are returned as escalations for the UI to
    # flag "needs a new supplier", never auto-placed.

    for supplier_id, lines in bundles.items():
        bundle_total = sum(line["line_total"] for line in lines)
        bundle_confidence = min((line["confidence"] for line in lines), default=0.0)
        worst = "act"
        for line in lines:
            worst = _worse(worst, line.get("agent_decision", "act"))
        tier = _classify(bundle_total=bundle_total, has_source=True,
                         agent_decision=worst, confidence=bundle_confidence)

        # Calibrated bar = the strictest (highest) across the bundle's lines, so a
        # single low-trust product can't drag a whole multi-line PR into auto-place.
        bar = max(
            (_calib.calibrate(db, line["product_id"], supplier_id).adjusted_floor
             for line in lines),
            default=settings.auto_place_confidence,
        )

        pr_lines = [{
            "product_id": line["product_id"],
            "product_supplier_id": line["product_supplier_id"],
            "qty": line["qty"], "unit_price": line["unit_price"],
            "trigger_type": line["trigger"]["type"],
            "line_confidence": line["confidence"],
            "rationale": (line.get("agent_rationale") or "")
            + (" [capped to fit warehouse storage]" if line.get("storage_capped") else ""),
        } for line in lines]

        pr = _req.stage(
            db, supplier_id=supplier_id, confidence=bundle_confidence,
            confidence_floor=bar, tier=tier,
            rationale=f"bundle_tier={tier}; {len(lines)} line(s); total={bundle_total:.2f}",
            order_by=None, lines=pr_lines)
        staged_ids.append(pr.id)

        # Auto-place only when confidence clears the calibrated bar AND the bundle
        # is placeable (act/propose) and under the auto spend cap.
        if (bundle_confidence >= bar and tier in ("act", "propose")
                and bundle_total <= settings.auto_place_spend_cap):
            _req.approve(db, pr.id, actor=actor or "agent", auto=True)
            auto_ids.append(pr.id)

    return {
        "staged": len(staged_ids),
        "auto_placed": len(auto_ids),
        "escalations_no_source": len(orphans),
        "requisition_ids": staged_ids,
        "auto_placed_ids": auto_ids,
    }


def _compute_bundles(db: Session, period_days: int) -> tuple[dict[str, list[dict]], list[dict]]:
    """Detect needs, net inbound, source, MOQ-round, and judge each line.

    Returns (bundles_by_supplier, orphan_bundles). Each bundle line carries qty,
    price, trigger, and the copilot's confidence/decision/rationale. Orphans are
    grouped under a synthetic key per product (no real supplier) for staging as
    escalate PRs. Shared by the requisition cycle; mirrors the run's Steps 1-4.
    """
    needs = _detect_needs(db, period_days)
    inbound = _inbound_by_product(db)

    net_needs: dict[str, dict] = {}
    for pid, info in needs.items():
        net = info["gross_need"] - inbound.get(pid, 0)
        if net > 0:
            net_needs[pid] = {**info, "net_need": net}

    # Storage cap: never order more than the warehouse can land. We draw down a
    # shared headroom budget as lines are built, so the whole run's buys fit.
    # None means no warehouse capacity is defined -> no cap applies.
    remaining_headroom = planning.storage_headroom(db)["storable_max"]

    bundles: dict[str, list[dict]] = defaultdict(list)
    orphans: list[dict] = []
    for pid, info in net_needs.items():
        ranked = sourcing.suggest_sources(db, pid)
        if not ranked:
            orphans.append({
                "supplier_id": None, "rationale": "No contracted source — new supplier needed.",
                "lines": [{"product_id": pid, "product_supplier_id": None,
                           "qty": info["net_need"], "unit_price": None,
                           "trigger_type": info["type"], "line_confidence": 0.0,
                           "rationale": "No source"}],
            })
            continue
        src = ranked[0]
        moq = src.get("min_order_quantity") or 1
        qty = max(info["net_need"], moq)
        if moq > 1:
            qty = math.ceil(qty / moq) * moq
        unit_price = float(src["contract_price"]) if src.get("contract_price") is not None else 0.0

        # Cap the order at remaining storage headroom — never buy more than fits.
        # remaining_headroom is None when no warehouse capacity is defined: then no
        # cap applies. We respect MOQ: round DOWN to a whole multiple that fits, and
        # skip the line entirely if not even one MOQ can be stored.
        storage_capped = False
        if remaining_headroom is not None and qty > remaining_headroom:
            fit = remaining_headroom
            if moq > 1:
                fit = (fit // moq) * moq
            if fit < moq:
                _log.info("purchasing: no storage headroom for product; skipping",
                          extra={"extra_fields": {"product_id": pid, "want": qty,
                                                  "headroom": remaining_headroom}})
                continue
            qty = fit
            storage_capped = True
        if remaining_headroom is not None:
            remaining_headroom -= qty

        line = {
            "product_id": pid, "product_supplier_id": src["product_supplier_id"],
            "qty": qty, "unit_price": unit_price, "line_total": qty * unit_price,
            "trigger": info, "storage_capped": storage_capped,
        }
        try:
            rec = copilot.recommend_sourcing(db, pid, qty)
            line["confidence"] = rec.confidence
            line["agent_decision"] = rec.decision
            line["agent_rationale"] = rec.rationale
        except copilot.AgentError as exc:
            line["confidence"] = 0.0
            line["agent_decision"] = "escalate"
            line["agent_rationale"] = f"copilot unavailable: {exc}"
        bundles[src["supplier_id"]].append(line)

    return bundles, orphans


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
