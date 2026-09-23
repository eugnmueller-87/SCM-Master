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

**The rollups aggregate in the database.** They used to call the per-asset
waterfall once per asset, six queries each. Over the 431,200 serials of the device
fleet, none of which has a datacenter cost layer, that took 563 seconds and held
the only worker for the whole of it; every other request queued behind it. Now
each layer is one SUM, acquisition is a count per order line times the line's
price (an index scan, no probe per asset), and the currency guard is one existence
probe per table. The per-asset waterfall is unchanged. These layers are the
datacenter's; the device fleet's cost is ``tco_device.py``.
"""
from __future__ import annotations

from collections import defaultdict
from decimal import ROUND_HALF_UP, Decimal
from typing import Iterable, Optional

from sqlalchemy import func, select, union
from sqlalchemy.orm import Session

from app.models.catalog import Product
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
_CENT = Decimal("0.01")


class CurrencyMixError(ValidationError):
    """Raised when a TCO sum would mix currencies — fail loud, never silently mix."""


def _d(x) -> Decimal:
    return Decimal(str(x)) if x is not None else _ZERO


def _cents(x) -> Decimal:
    """A sum read back from the database, to the cent.

    SQLite does Numeric arithmetic in floating point, so a product of three rates can
    come back a hair off the exact figure; money is cents, and the per-layer subtotal is
    where the rounding belongs.
    """
    return Decimal(str(x if x is not None else 0)).quantize(_CENT, rounding=ROUND_HALF_UP)


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

    excluded = {t.value for t in _excluded_types(exclude_landed_types)}

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


# ---- rollups, computed in the database ------------------------------------

# The layer tables and the expression that is one asset's cost in each. NULL parts count
# as zero, as they did when the waterfall summed them row by row in Python.
_LAYER_TABLES = (
    (LandedCost, "landed_cost"), (DeploymentCost, "deployment_cost"), (OpexLedger, "opex_ledger"),
    (EolCost, "eol_cost"), (RecoveryValue, "recovery_value"),
)
_LAYER_EXPR = {
    "landed": (LandedCost, func.coalesce(LandedCost.amount, 0)),
    "deployment": (DeploymentCost, func.coalesce(DeploymentCost.amount, 0)),
    "opex": (OpexLedger, func.coalesce(OpexLedger.power_kwh, 0) * func.coalesce(OpexLedger.pue, 0)
             * func.coalesce(OpexLedger.energy_rate, 0) + func.coalesce(OpexLedger.cooling, 0)
             + func.coalesce(OpexLedger.maintenance, 0) + func.coalesce(OpexLedger.license, 0)),
    "eol": (EolCost, func.coalesce(EolCost.decommission, 0) + func.coalesce(EolCost.data_destruction, 0)
            + func.coalesce(EolCost.weee, 0) + func.coalesce(EolCost.itad_fee, 0)),
    "recovery": (RecoveryValue, func.coalesce(RecoveryValue.residual_value, 0)),
}


def _excluded_types(exclude_landed_types: Optional[Iterable]) -> list[LandedCostType]:
    """The landed types to drop, as enum members. Names are case-insensitive; a name that
    is not a landed type is ignored, as the per-row filter always did."""
    names = set()
    for t in (exclude_landed_types or []):
        names.add(t.value if isinstance(t, LandedCostType) else str(t).upper())
    return [LandedCostType[n] for n in sorted(names) if n in LandedCostType.__members__]


def _assert_eur_everywhere(db: Session) -> None:
    """The rollups' currency guard: one existence probe per layer table, not a check per row."""
    for model, label in _LAYER_TABLES:
        bad = db.execute(select(model.id, model.currency)
                         .where(model.currency.is_not(None), model.currency != "EUR").limit(1)).first()
        if bad is not None:
            raise CurrencyMixError(f"{label}: row {bad[0]!r} is {bad[1]}, not EUR: TCO sums are EUR-only (no FX conversion).")


def _landed_filter(stmt, excluded: list[LandedCostType]):
    return stmt.where(LandedCost.cost_type.notin_(excluded)) if excluded else stmt


def portfolio_tco(db: Session, baseline: Decimal,
                  exclude_landed_types: Optional[Iterable[str]] = None) -> dict:
    """Portfolio-wide per-layer subtotals + the two labelled ratios, over every asset.

    ``baseline`` is a passed-in revenue/cost figure (no stored revenue model);
    the ratios are expressed against it. Raises if baseline <= 0.
    """
    if baseline is None or Decimal(str(baseline)) <= 0:
        raise ValidationError("portfolio baseline must be a positive number")
    baseline = Decimal(str(baseline))
    _assert_eur_everywhere(db)
    excluded = _excluded_types(exclude_landed_types)

    # count(*), not count(asset.id): the id is a text primary key rather than SQLite's rowid, so
    # counting it visits the row behind every index entry, and a 431,200-entry index scan
    # becomes a table walk (1.2 s instead of 40 ms on the full fleet).
    n = int(db.scalar(select(func.count()).select_from(Asset)) or 0)
    # Acquisition: how many assets came from each order line, from the index, times that
    # line's price. The old way joined every asset to its line, a probe per serial.
    per_line = db.execute(select(Asset.source_order_item_id, func.count())
                          .where(Asset.source_order_item_id.is_not(None))
                          .group_by(Asset.source_order_item_id)).all()
    prices: dict[str, Decimal] = {}
    line_ids = [lid for lid, _ in per_line]
    for i in range(0, len(line_ids), 500):
        prices.update({lid: _d(p) for lid, p in db.execute(
            select(OrderItem.id, OrderItem.unit_price).where(OrderItem.id.in_(line_ids[i:i + 500]))).all()})
    sub = {"acquisition": sum((prices.get(lid, _ZERO) * int(cnt) for lid, cnt in per_line), _ZERO)}
    for name, (model, expr) in _LAYER_EXPR.items():
        stmt = select(func.coalesce(func.sum(expr), 0)).select_from(model)
        if name == "landed":
            stmt = _landed_filter(stmt, excluded)
        sub[name] = _cents(db.scalar(stmt))
    sub["recovery"] = -sub["recovery"]  # a credit: shown as the negative step it is
    tco_total = sum(sub.values(), _ZERO)

    # TSCMC excludes acquisition (the COGS analog) by SCOR/APQC definition.
    tscmc_numerator = tco_total - sub["acquisition"]

    return {
        "assets": n,
        "baseline": float(baseline),
        "subtotals": {k: float(v) for k, v in sub.items()},
        "tco_total": float(tco_total),
        "total_cost_pct": float(tco_total / baseline),
        "tscmc_pct": float(tscmc_numerator / baseline),
        "excluded_landed_types": sorted(t.value for t in excluded),
    }


def tco_by_class(db: Session,
                 exclude_landed_types: Optional[Iterable[str]] = None) -> list[dict]:
    """Per-product-category TCO breakdown — only assets that have TCO layers.

    For the analytics/cockpit view: groups the waterfall by the asset's product
    category (storage/compute/gpu/switch). Assets with no cost layers recorded
    (e.g. baseline demo assets) are excluded so the breakdown reflects modelled
    TCO, not acquisition-only rows. The modelled assets drive every query, so a
    fleet of 431,200 serials without a single layer answers at once, and empty.
    """
    _assert_eur_everywhere(db)
    excluded = _excluded_types(exclude_landed_types)
    modelled = union(select(LandedCost.asset_id), select(OpexLedger.asset_id)).subquery("modelled")
    key = func.coalesce(Product.category, "other")

    buckets: dict[str, dict] = defaultdict(lambda: {
        "assets": 0, "acquisition": _ZERO, "landed": _ZERO, "deployment": _ZERO,
        "opex": _ZERO, "eol": _ZERO, "recovery": _ZERO})
    rows = db.execute(
        select(key, func.count(Asset.id), func.coalesce(func.sum(OrderItem.unit_price), 0))
        .select_from(modelled).join(Asset, Asset.id == modelled.c.asset_id)
        .outerjoin(Product, Product.id == Asset.product_id)
        .outerjoin(OrderItem, OrderItem.id == Asset.source_order_item_id)
        .group_by(key)).all()
    for cat, n, acq in rows:
        buckets[cat]["assets"] = int(n)
        buckets[cat]["acquisition"] = _cents(acq)
    for name, (model, expr) in _LAYER_EXPR.items():
        stmt = (select(key, func.coalesce(func.sum(expr), 0)).select_from(model)
                .join(Asset, Asset.id == model.asset_id)
                .outerjoin(Product, Product.id == Asset.product_id)
                .where(Asset.id.in_(select(modelled.c.asset_id)))
                .group_by(key))
        if name == "landed":
            stmt = _landed_filter(stmt, excluded)
        for cat, total in db.execute(stmt).all():
            buckets[cat][name] = _cents(total)

    out = []
    for cat, b in buckets.items():
        tco_total = b["acquisition"] + b["landed"] + b["deployment"] + b["opex"] + b["eol"] - b["recovery"]
        out.append({
            "category": cat,
            "assets": b["assets"],
            "acquisition": float(b["acquisition"]),
            "landed": float(b["landed"]),
            "deployment": float(b["deployment"]),
            "opex": float(b["opex"]),
            "eol": float(b["eol"]),
            "recovery": float(-b["recovery"]),
            "tco_total": float(tco_total),
            "avg_tco": float(tco_total / b["assets"]) if b["assets"] else 0.0,
        })
    return sorted(out, key=lambda x: -x["tco_total"])


# Re-export for callers that pass enum members to the filter.
__all__ = ["asset_tco", "portfolio_tco", "tco_by_class", "CurrencyMixError", "LandedCostType"]
