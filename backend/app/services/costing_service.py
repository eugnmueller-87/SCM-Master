"""Costing service — the DB adapter around the pure engine (services/costing.py).

This layer reads ORM rows (BOM, BOMLine, ComponentClass, Commodity prices,
ProductSupplier), resolves each line's as-of commodity multiplier, calls the
deterministic engine, and (optionally) persists a ShouldCostRun. It owns no cost
math — that all lives in the pure engine, so it stays trivially testable.

Services flush, never commit (the get_db dependency owns the transaction).
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.catalog import ProductSupplier
from app.models.costing import (
    BOM,
    Commodity,
    CommodityPrice,
    ComponentClass,
    CostingMethod,
    CostParams,
    ShouldCostRun,
)
from app.services import costing as engine
from app.services.costing import CostingError, LineInput, Params
from app.services.exceptions import NotFoundError, ValidationError


def _dec(x, default="0") -> Decimal:
    return Decimal(str(x if x is not None else default))


def _resolve_line(db: Session, line, as_of: date) -> LineInput:
    """Turn a BOMLine ORM row into an engine LineInput, looking up the as-of
    commodity multiplier for teardown lines."""
    cls: ComponentClass = line.component_class
    if cls.method is CostingMethod.reference_price:
        return LineInput(
            label=line.label, method=CostingMethod.reference_price, qty=line.qty,
            list_price=_dec(line.list_price) if line.list_price is not None else None,
            discount_pct=_dec(line.discount_pct) if line.discount_pct is not None else None,
        )

    # teardown — needs a commodity driver
    if cls.commodity_id is None:
        raise ValidationError(f"{line.label!r}: teardown class {cls.code!r} has no commodity driver")
    commodity = db.get(Commodity, cls.commodity_id)
    if commodity is None:
        raise ValidationError(f"{line.label!r}: commodity {cls.commodity_id!r} not found")
    prices = db.scalars(
        select(CommodityPrice).where(CommodityPrice.commodity_id == commodity.id)
    ).all()
    try:
        mult = engine.index_multiplier_as_of(
            [(p.price_date, _dec(p.value)) for p in prices],
            _dec(commodity.baseline_value, "1"), as_of,
        )
    except CostingError as e:
        raise ValidationError(str(e)) from e
    return LineInput(
        label=line.label, method=CostingMethod.teardown, qty=line.qty,
        base_material_cost=_dec(line.base_material_cost) if line.base_material_cost is not None else None,
        conversion_cost=_dec(line.conversion_cost), overhead_pct=_dec(line.overhead_pct),
        index_multiplier=mult,
    )


def _bom_for(db: Session, product_id: str) -> BOM:
    bom = db.scalar(select(BOM).where(BOM.product_id == product_id))
    if bom is None:
        raise NotFoundError(f"No BOM defined for product {product_id!r}")
    return bom


def get_bom(db: Session, product_id: str) -> BOM:
    return _bom_for(db, product_id)


def upsert_bom(db: Session, product_id: str, payload) -> BOM:
    """Replace a product's BOM (lines + params) from a BOMIn payload.

    Resolves each line's component_class_code → ComponentClass and validates the
    method-specific fields are present, so a bad BOM is rejected at write time.
    """
    from app.models.costing import BOMLine

    classes = {c.code: c for c in db.scalars(select(ComponentClass)).all()}

    # Build lines first so we can fail before mutating anything.
    new_lines = []
    for ln in payload.lines:
        cls = classes.get(ln.component_class_code)
        if cls is None:
            raise ValidationError(f"Unknown component_class_code {ln.component_class_code!r}")
        if cls.method is CostingMethod.teardown and ln.base_material_cost is None:
            raise ValidationError(f"{ln.label!r}: teardown class needs base_material_cost")
        if cls.method is CostingMethod.reference_price and (ln.list_price is None or ln.discount_pct is None):
            raise ValidationError(f"{ln.label!r}: reference_price class needs list_price + discount_pct")
        new_lines.append((cls, ln))

    bom = db.scalar(select(BOM).where(BOM.product_id == product_id))
    if bom is None:
        bom = BOM(product_id=product_id, notes=payload.notes)
        db.add(bom)
        db.flush()
    else:
        bom.notes = payload.notes
        for old in list(bom.lines):
            db.delete(old)
        if bom.params is not None:
            db.delete(bom.params)
        db.flush()

    for cls, ln in new_lines:
        db.add(BOMLine(
            bom_id=bom.id, component_class_id=cls.id, label=ln.label, qty=ln.qty,
            base_material_cost=ln.base_material_cost, conversion_cost=ln.conversion_cost,
            overhead_pct=ln.overhead_pct, list_price=ln.list_price, discount_pct=ln.discount_pct,
        ))
    if payload.params is not None:
        db.add(CostParams(
            bom_id=bom.id, integration_pct=payload.params.integration_pct,
            sga_pct=payload.params.sga_pct, target_margin_pct=payload.params.target_margin_pct,
        ))
    db.flush()
    db.refresh(bom)
    return bom


def create_commodity(db: Session, payload) -> Commodity:
    if db.scalar(select(Commodity).where(Commodity.code == payload.code)):
        raise ValidationError(f"Commodity {payload.code!r} already exists")
    c = Commodity(code=payload.code, name=payload.name, unit=payload.unit,
                  baseline_value=payload.baseline_value)
    db.add(c)
    db.flush()
    return c


def list_commodities(db: Session) -> list[Commodity]:
    return list(db.scalars(select(Commodity)).all())


def add_commodity_price(db: Session, commodity_id: str, payload) -> CommodityPrice:
    if db.get(Commodity, commodity_id) is None:
        raise NotFoundError(f"Commodity {commodity_id!r} not found")
    p = CommodityPrice(commodity_id=commodity_id, price_date=payload.price_date, value=payload.value)
    db.add(p)
    db.flush()
    return p


def _params(bom: BOM) -> Params:
    p: Optional[CostParams] = bom.params
    if p is None:
        return Params()
    return Params(
        integration_pct=_dec(p.integration_pct, "0.06"),
        sga_pct=_dec(p.sga_pct, "0.08"),
        target_margin_pct=_dec(p.target_margin_pct, "0.10"),
    )


def _lines(db: Session, bom: BOM, as_of: date) -> list[LineInput]:
    try:
        return [_resolve_line(db, ln, as_of) for ln in bom.lines]
    except CostingError as e:
        raise ValidationError(str(e)) from e


def compute_should_cost(db: Session, product_id: str, as_of: date,
                        *, persist: bool = False, quoted_price: Optional[Decimal] = None,
                        product_supplier_id: Optional[str] = None) -> dict:
    """Full breakdown + floor + target for a product's BOM, as of a date."""
    bom = _bom_for(db, product_id)
    params = _params(bom)
    try:
        result = engine.roll_up(_lines(db, bom, as_of), params)
    except CostingError as e:
        raise ValidationError(str(e)) from e

    out = result.as_dict()
    out["product_id"] = product_id
    out["as_of"] = as_of.isoformat()

    if persist:
        run = ShouldCostRun(
            product_id=product_id, as_of=as_of, product_supplier_id=product_supplier_id,
            should_cost_floor=result.should_cost_floor, target_price=result.target_price,
            quoted_price=quoted_price, breakdown=out,
        )
        db.add(run)
        db.flush()
        out["run_id"] = run.id
    return out


def _preferred_supplier(db: Session, product_id: str) -> Optional[ProductSupplier]:
    rows = db.scalars(
        select(ProductSupplier)
        .where(ProductSupplier.product_id == product_id, ProductSupplier.active.is_(True))
    ).all()
    if not rows:
        return None
    return sorted(rows, key=lambda r: r.preference_rank)[0]


def cost_gap(db: Session, product_id: str, as_of: date,
             annual_volume: int = 0) -> dict:
    """Should-cost vs the preferred ProductSupplier's contract price → gap."""
    bom = _bom_for(db, product_id)
    params = _params(bom)
    result = engine.roll_up(_lines(db, bom, as_of), params)

    supplier = _preferred_supplier(db, product_id)
    quoted = _dec(supplier.contract_price) if supplier and supplier.contract_price is not None else None
    g = engine.gap(result, quoted, annual_volume)
    out = g.as_dict()
    out["product_id"] = product_id
    out["as_of"] = as_of.isoformat()
    out["product_supplier_id"] = supplier.id if supplier else None
    return out


def sensitivity(db: Session, product_id: str, as_of: date, delta: float = 0.2) -> dict:
    bom = _bom_for(db, product_id)
    try:
        s = engine.sensitivity(_lines(db, bom, as_of), _params(bom), delta)
    except CostingError as e:
        raise ValidationError(str(e)) from e
    out = s.as_dict()
    out["product_id"] = product_id
    out["as_of"] = as_of.isoformat()
    return out


# --- analytics aggregations (for Power BI) ---------------------------------

def gap_by_supplier(db: Session, as_of: date) -> list[dict]:
    """Per-product gap, grouped by the preferred supplier — who's furthest above
    floor. Only products that have a BOM are included."""
    out: list[dict] = []
    boms = db.scalars(select(BOM)).all()
    for bom in boms:
        try:
            g = cost_gap(db, bom.product_id, as_of)
        except ValidationError:
            continue
        out.append({
            "product_id": bom.product_id,
            "product_supplier_id": g["product_supplier_id"],
            "should_cost_floor": g["should_cost_floor"],
            "target_price": g["target_price"],
            "quoted_price": g["quoted_price"],
            "gap_to_target_abs": g["gap_to_target_abs"],
            "gap_to_target_pct": g["gap_to_target_pct"],
        })
    return out


def savings_summary(db: Session, as_of: date) -> dict:
    """Total addressable negotiation savings across all BOM'd products (vs target)."""
    rows = gap_by_supplier(db, as_of)
    total = sum((r["gap_to_target_abs"] or 0) for r in rows if (r["gap_to_target_abs"] or 0) > 0)
    return {
        "as_of": as_of.isoformat(),
        "products_with_bom": len(rows),
        "products_above_target": sum(1 for r in rows if (r["gap_to_target_abs"] or 0) > 0),
        "total_gap_to_target": float(total),
    }
