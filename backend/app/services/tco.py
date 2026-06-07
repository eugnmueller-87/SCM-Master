"""Total Cost of Ownership (TCO) computation — pure-ish service over the layers.

Per-asset waterfall:

    tco_total = acquisition + Σlanded + Σdeployment + Σopex + Σeol − recovery

Anchoring (Phase 0, confirmed):
  - acquisition = ACTUAL PAID, read from the provenance chain
    (asset → source_order_item → OrderItem.unit_price). Should-cost is NOT the
    base; it stays read-only and is exposed only as a derived variance.
  - landed / deployment are multi-row → summed per asset. An optional
    ``exclude_landed_types`` filter drops landed components at query time (e.g.
    DUTY for a tariff scenario) — generalised from "exclude duty".
  - currency: amounts are assumed already EUR; the service FAILS LOUD on any
    non-EUR row rather than silently mixing (CurrencyMixError).

Portfolio rollup exposes per-layer subtotals plus TWO correctly-named ratios:
    total_cost_pct = ΣTCO / baseline                  (includes hardware)
    tscmc_pct      = Σ(TCO − acquisition) / baseline   (SCOR/APQC: excludes the
                     COGS-analog acquisition — the cost of operating the chain)
The per-layer subtotals make a stricter SCOR TSCMC (also stripping run-time
OpEx) a one-liner later, with no rework.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Iterable, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.costing import BOM, ShouldCostRun
from app.models.flow import Asset
from app.models.procurement import OrderItem
from app.models.tco import (
    DeploymentCost,
    EolCost,
    LandedCost,
    LandedCostType,
    OpexLedger,
    RecoveryValue,
)
from app.services.exceptions import NotFoundError, ValidationError

_ZERO = Decimal("0.00")


class CurrencyMixError(ValidationError):
    """Raised when a TCO sum would mix currencies — fail loud, never silently mix."""


def _d(x) -> Decimal:
    return Decimal(str(x)) if x is not None else _ZERO


def _assert_eur(rows: Iterable, label: str) -> None:
    for r in rows:
        cur = getattr(r, "currency", "EUR")
        if cur and cur != "EUR":
            raise CurrencyMixError(
                f"{label}: row {getattr(r, 'id', '?')!r} is {cur}, not EUR — "
                "TCO sums are EUR-only (no FX conversion)."
            )


# ---- acquisition (actual paid, from provenance) ---------------------------

def _acquisition(db: Session, asset: Asset) -> Decimal:
    if asset.source_order_item_id is None:
        return _ZERO
    oi = db.get(OrderItem, asset.source_order_item_id)
    return _d(oi.unit_price) if oi is not None else _ZERO


def _should_cost_target(db: Session, asset: Asset) -> Optional[Decimal]:
    """The should-cost target_price for this asset's product, if a BOM/run exists.

    Read-only: prefer the latest persisted ShouldCostRun; else None. Used only
    for the derived variance, never as the TCO base.
    """
    run = db.scalar(
        select(ShouldCostRun)
        .where(ShouldCostRun.product_id == asset.product_id)
        .order_by(ShouldCostRun.date_created.desc())
    )
    if run is not None:
        return _d(run.target_price)
    # Fall back to whether the product even has a BOM (target computable later);
    # we don't compute it here to keep should-cost read-only and avoid coupling.
    if db.scalar(select(BOM).where(BOM.product_id == asset.product_id)) is None:
        return None
    return None


# ---- per-asset waterfall --------------------------------------------------

def asset_tco(db: Session, asset_id: str,
              exclude_landed_types: Optional[Iterable[str]] = None) -> dict:
    """Full TCO waterfall for one asset.

    ``exclude_landed_types`` drops landed components by type (e.g. {"DUTY"}) —
    the tariff-scenario filter. Default: include all.
    """
    asset = db.get(Asset, asset_id)
    if asset is None:
        raise NotFoundError(f"Asset {asset_id!r} not found")

    excluded = {str(t).upper() for t in (exclude_landed_types or [])}

    acquisition = _acquisition(db, asset)

    landed_rows = db.scalars(select(LandedCost).where(LandedCost.asset_id == asset_id)).all()
    deploy_rows = db.scalars(select(DeploymentCost).where(DeploymentCost.asset_id == asset_id)).all()
    opex_rows = db.scalars(select(OpexLedger).where(OpexLedger.asset_id == asset_id)).all()
    eol_row = db.scalar(select(EolCost).where(EolCost.asset_id == asset_id))
    rec_row = db.scalar(select(RecoveryValue).where(RecoveryValue.asset_id == asset_id))

    _assert_eur(landed_rows, "landed_cost")
    _assert_eur(deploy_rows, "deployment_cost")
    _assert_eur(opex_rows, "opex_ledger")
    if eol_row is not None:
        _assert_eur([eol_row], "eol_cost")
    if rec_row is not None:
        _assert_eur([rec_row], "recovery_value")

    landed = sum((_d(r.amount) for r in landed_rows
                  if r.cost_type.value not in excluded), _ZERO)
    deployment = sum((_d(r.amount) for r in deploy_rows), _ZERO)
    opex = sum((_d(r.power_kwh) * _d(r.pue) * _d(r.energy_rate)
                + _d(r.cooling) + _d(r.maintenance) + _d(r.license)
                for r in opex_rows), _ZERO)
    eol = _ZERO if eol_row is None else (
        _d(eol_row.decommission) + _d(eol_row.data_destruction)
        + _d(eol_row.weee) + _d(eol_row.itad_fee))
    recovery = _ZERO if rec_row is None else _d(rec_row.residual_value)

    tco_total = acquisition + landed + deployment + opex + eol - recovery

    # Derived should-cost → actual variance (cost-avoidance signal).
    sc_target = _should_cost_target(db, asset)
    variance = None if sc_target is None else {
        "should_cost_target": float(sc_target),
        "actual_acquisition": float(acquisition),
        # positive = paid MORE than should-cost (overpay); negative = below target
        "variance_abs": float(acquisition - sc_target),
        "variance_pct": (float((acquisition - sc_target) / sc_target)
                         if sc_target != 0 else None),
    }

    return {
        "asset_id": asset.id,
        "serial_number": asset.serial_number,
        "product_id": asset.product_id,
        "waterfall": {
            "acquisition": float(acquisition),
            "landed": float(landed),
            "deployment": float(deployment),
            "opex": float(opex),
            "eol": float(eol),
            "recovery": float(-recovery),  # shown as a negative step
        },
        "tco_total": float(tco_total),
        "should_cost_variance": variance,
        "excluded_landed_types": sorted(excluded),
    }


# ---- portfolio rollup -----------------------------------------------------

def portfolio_tco(db: Session, baseline: Decimal,
                  exclude_landed_types: Optional[Iterable[str]] = None) -> dict:
    """Portfolio-wide per-layer subtotals + the two labelled ratios.

    ``baseline`` is a passed-in revenue/cost figure (no stored revenue model);
    the ratios are expressed against it. Raises if baseline <= 0.
    """
    if baseline is None or Decimal(str(baseline)) <= 0:
        raise ValidationError("portfolio baseline must be a positive number")
    baseline = Decimal(str(baseline))

    sub = {"acquisition": _ZERO, "landed": _ZERO, "deployment": _ZERO,
           "opex": _ZERO, "eol": _ZERO, "recovery": _ZERO}
    tco_total = _ZERO
    n = 0
    for asset in db.scalars(select(Asset)).all():
        r = asset_tco(db, asset.id, exclude_landed_types=exclude_landed_types)
        w = r["waterfall"]
        sub["acquisition"] += _d(w["acquisition"])
        sub["landed"] += _d(w["landed"])
        sub["deployment"] += _d(w["deployment"])
        sub["opex"] += _d(w["opex"])
        sub["eol"] += _d(w["eol"])
        sub["recovery"] += _d(w["recovery"])  # already negative
        tco_total += _d(r["tco_total"])
        n += 1

    # TSCMC excludes acquisition (the COGS analog) by SCOR/APQC definition.
    tscmc_numerator = tco_total - sub["acquisition"]

    return {
        "assets": n,
        "baseline": float(baseline),
        "subtotals": {k: float(v) for k, v in sub.items()},
        "tco_total": float(tco_total),
        "total_cost_pct": float(tco_total / baseline),
        "tscmc_pct": float(tscmc_numerator / baseline),
        "excluded_landed_types": sorted({str(t).upper() for t in (exclude_landed_types or [])}),
    }


# Re-export for callers that pass enum members to the filter.
__all__ = ["asset_tco", "portfolio_tco", "CurrencyMixError", "LandedCostType"]
