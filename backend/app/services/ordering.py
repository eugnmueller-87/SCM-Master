"""Ordering service — packages (reusable bundles) and their expansion to lines.

A package is a named list of (product, qty). ``expand_package`` turns it into a
flat list of order lines a manual order / requisition can consume, multiplied by
an optional pack count (order 3 racks at once). Pure reads + simple CRUD; the
capacity guard and requisition staging live on the order path that calls this.
"""
from __future__ import annotations

from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.catalog import Product
from app.models.ordering import Package, PackageLine
from app.services.exceptions import NotFoundError, ValidationError


def list_packages(db: Session, *, active_only: bool = True) -> list[Package]:
    stmt = select(Package)
    if active_only:
        stmt = stmt.where(Package.active.is_(True))
    return list(db.scalars(stmt.order_by(Package.name)).all())


def get_package(db: Session, package_id: str) -> Package:
    pkg = db.get(Package, package_id)
    if pkg is None:
        raise NotFoundError(f"Package {package_id!r} not found")
    return pkg


def create_package(db: Session, *, code: str, name: str,
                   lines: list[dict], description: Optional[str] = None) -> Package:
    """Create a package + its lines. ``lines`` items: {product_id, quantity}."""
    if not lines:
        raise ValidationError("A package needs at least one line.")
    if db.scalar(select(Package).where(Package.code == code)):
        raise ValidationError(f"Package code {code!r} already exists.")
    pkg = Package(code=code, name=name, description=description)
    db.add(pkg)
    db.flush()
    for ln in lines:
        if db.get(Product, ln["product_id"]) is None:
            raise NotFoundError(f"Product {ln['product_id']!r} not found")
        qty = int(ln.get("quantity", 1))
        if qty <= 0:
            raise ValidationError("Package line quantity must be positive.")
        db.add(PackageLine(package_id=pkg.id, product_id=ln["product_id"], quantity=qty))
    db.flush()
    db.refresh(pkg)
    return pkg


def stage_manual_order(db: Session, *, lines: Optional[list[dict]] = None,
                       package_id: Optional[str] = None, packs: int = 1,
                       actor: Optional[str] = None) -> dict:
    """Place a MANUAL order — staged as requisition(s) for human approval.

    Input is EITHER explicit ``lines`` ([{product_id, quantity}]) OR a
    ``package_id`` (expanded ×packs). Each product is sourced to its preferred
    ProductSupplier, lines are grouped per supplier, the over-order capacity GUARD
    is enforced on the total (refuses if it can't fit), then one STAGED
    PurchaseRequisition per supplier is created (reusing the approve→PO path).

    Returns {requisition_ids, total_units, capacity, orphans}. Orphans are
    products with no contracted source (can't be ordered — surfaced, not staged).
    """
    from collections import defaultdict

    from app.services import planning, sourcing
    from app.services.requisition import requisition_service

    if bool(lines) == bool(package_id):
        raise ValidationError("Provide exactly one of `lines` or `package_id`.")
    order_lines = expand_package(db, package_id, packs=packs) if package_id else list(lines)
    if not order_lines:
        raise ValidationError("Nothing to order.")

    total_units = sum(int(ln["quantity"]) for ln in order_lines)
    # GUARD: refuse the whole order if it can't fit the warehouse (fail-closed).
    planning.assert_order_fits(db, total_units)

    by_supplier: dict[str, list[dict]] = defaultdict(list)
    orphans: list[dict] = []
    for ln in order_lines:
        pid, qty = ln["product_id"], int(ln["quantity"])
        if qty <= 0:
            continue
        ranked = sourcing.suggest_sources(db, pid)
        if not ranked:
            orphans.append({"product_id": pid, "quantity": qty})
            continue
        src = ranked[0]
        unit_price = float(src["contract_price"]) if src.get("contract_price") is not None else 0.0
        by_supplier[src["supplier_id"]].append({
            "product_id": pid, "product_supplier_id": src["product_supplier_id"],
            "qty": qty, "unit_price": unit_price, "trigger_type": "manual",
            "line_confidence": 1.0, "rationale": "Manual order",
        })

    req_ids: list[str] = []
    for supplier_id, sup_lines in by_supplier.items():
        total = sum(ln["qty"] * ln["unit_price"] for ln in sup_lines)
        pr = requisition_service.stage(
            db, supplier_id=supplier_id, confidence=1.0, confidence_floor=1.0,
            tier="propose", rationale=f"Manual order · {len(sup_lines)} line(s) · total={total:.2f}",
            order_by=None, lines=sup_lines)
        req_ids.append(pr.id)

    return {
        "requisition_ids": req_ids,
        "total_units": total_units,
        "orphans": orphans,
        "capacity": planning.capacity_flow(db),
    }


def expand_package(db: Session, package_id: str, *, packs: int = 1) -> list[dict]:
    """Expand a package into order lines: [{product_id, quantity}] × packs.

    Lines for the same product are summed (a package can list a product once;
    `packs` multiplies the whole bundle). Raises if the package is empty/missing.
    """
    if packs < 1:
        raise ValidationError("packs must be >= 1.")
    pkg = get_package(db, package_id)
    out: dict[str, int] = {}
    for ln in pkg.lines:
        out[ln.product_id] = out.get(ln.product_id, 0) + ln.quantity * packs
    if not out:
        raise ValidationError(f"Package {pkg.code!r} has no lines to order.")
    return [{"product_id": pid, "quantity": qty} for pid, qty in out.items()]
