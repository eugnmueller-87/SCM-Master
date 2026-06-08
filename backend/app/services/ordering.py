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
