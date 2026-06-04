"""Contract-lifecycle reads over ProductSupplier (the sourcing contract).

A ProductSupplier IS the contract here. This module enriches a contract row for
the Contracts screen:
  - ``ytd_spend``      computed live from received-asset cost this calendar year
    that traces (via the never-broken provenance link) to this product+supplier;
  - ``contract_status`` taken from the stored column when set, else derived from
    the term dates and the ``active`` flag.

Read-only; no writes, no new tables (the columns live on product_supplier).
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.catalog import ProductSupplier
from app.models.flow import Asset
from app.models.procurement import OrderItem, PurchaseOrder

_RENEWAL_WINDOW_DAYS = 60   # term_end within this -> RENEWAL_DUE
_EXPIRING_DAYS = 30         # term_end within this -> EXPIRING


def ytd_spend(db: Session, ps: ProductSupplier, *, today: Optional[date] = None) -> Decimal:
    """Received-asset cost this year tracing to this contract's product+supplier.

    Walks Asset -> source OrderItem -> PurchaseOrder; counts an asset when its
    order line is for this product AND the order's supplier is this supplier AND
    it was received this calendar year. Spend = unit_price per received asset.
    """
    today = today or date.today()
    year_start = date(today.year, 1, 1)
    stmt = (
        select(Asset, OrderItem)
        .join(OrderItem, Asset.source_order_item_id == OrderItem.id)
        .join(PurchaseOrder, OrderItem.order_id == PurchaseOrder.id)
        .where(
            OrderItem.product_id == ps.product_id,
            PurchaseOrder.supplier_id == ps.supplier_id,
            Asset.received_date.is_not(None),
            Asset.received_date >= year_start,
        )
    )
    total = Decimal("0")
    for _asset, oi in db.execute(stmt).all():
        total += oi.unit_price if oi.unit_price is not None else Decimal("0")
    return total


def derive_status(ps: ProductSupplier, *, today: Optional[date] = None) -> str:
    """Stored contract_status wins; otherwise derive from term dates / active."""
    if ps.contract_status:
        return ps.contract_status
    today = today or date.today()
    if not ps.active:
        return "EXPIRED"
    if ps.term_start is None and ps.term_end is None:
        return "DRAFT"
    if ps.term_end is not None:
        if ps.term_end < today:
            return "EXPIRED"
        days_left = (ps.term_end - today).days
        if days_left <= _EXPIRING_DAYS:
            return "EXPIRING"
        if days_left <= _RENEWAL_WINDOW_DAYS:
            return "RENEWAL_DUE"
    return "ACTIVE"


def enrich(db: Session, ps: ProductSupplier, *, today: Optional[date] = None) -> dict:
    """ORM row -> dict for ProductSupplierRead, with computed contract fields."""
    return {
        "id": ps.id,
        "date_created": ps.date_created,
        "last_updated": ps.last_updated,
        "product_id": ps.product_id,
        "supplier_id": ps.supplier_id,
        "manufacturer_id": ps.manufacturer_id,
        "supplier_product_code": ps.supplier_product_code,
        "manufacturer_part_number": ps.manufacturer_part_number,
        "standard_lead_time_days": ps.standard_lead_time_days,
        "min_order_quantity": ps.min_order_quantity,
        "contract_price": ps.contract_price,
        "currency_code": ps.currency_code,
        "preference_rank": ps.preference_rank,
        "active": ps.active,
        "contract_status": derive_status(ps, today=today),
        "term_start": ps.term_start,
        "term_end": ps.term_end,
        "annual_budget": ps.annual_budget,
        "ytd_spend": ytd_spend(db, ps, today=today),
    }
