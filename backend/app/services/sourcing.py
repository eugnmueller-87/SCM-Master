"""Sourcing intelligence: rank the candidate sources for a product.

Given a product, return its active ProductSuppliers ordered by how you'd pick
under spiky demand — preference rank first (the human-set call), then lead time,
then price. The route layer turns these into suggestions a buyer can act on
(and then re-source a line to, via PurchaseOrderService.resource_line).
"""
from __future__ import annotations

from typing import Optional, Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.catalog import Organization, Product, ProductSupplier
from app.services.exceptions import NotFoundError


def suggest_sources(db: Session, product_id: str, *,
                    include_inactive: bool = False) -> list[dict]:
    """Ranked sources for a product, best first.

    Sort key: (preference_rank asc, lead_time asc, price asc) — nulls sort last
    so a fully-specified source beats one missing data on a tie.
    """
    if db.get(Product, product_id) is None:
        raise NotFoundError(f"Product {product_id!r} not found")

    stmt = select(ProductSupplier).where(ProductSupplier.product_id == product_id)
    if not include_inactive:
        stmt = stmt.where(ProductSupplier.active.is_(True))
    sources: Sequence[ProductSupplier] = db.scalars(stmt).all()

    BIG = float("inf")

    def key(ps: ProductSupplier):
        return (
            ps.preference_rank if ps.preference_rank is not None else BIG,
            ps.standard_lead_time_days if ps.standard_lead_time_days is not None else BIG,
            float(ps.contract_price) if ps.contract_price is not None else BIG,
        )

    ranked = sorted(sources, key=key)
    out: list[dict] = []
    for rank, ps in enumerate(ranked, start=1):
        supplier: Optional[Organization] = db.get(Organization, ps.supplier_id)
        out.append({
            "rank": rank,
            "product_supplier_id": ps.id,
            "supplier_id": ps.supplier_id,
            "supplier_name": supplier.name if supplier else None,
            "preference_rank": ps.preference_rank,
            "standard_lead_time_days": ps.standard_lead_time_days,
            "min_order_quantity": ps.min_order_quantity,
            "contract_price": ps.contract_price,
            "currency_code": ps.currency_code,
            "active": ps.active,
        })
    return out
