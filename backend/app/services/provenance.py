"""Provenance — the never-broken thread the whole system exists to provide.

Two directions:
  - ``trace_asset``      : given an asset, walk back to its order line, the
    purchase order, the chosen supplier, and the unit spend.
  - ``assets_for_line``  : given an order line, list every asset it produced.

These return plain dicts (assembled into Pydantic schemas at the route layer),
keeping the query logic free of HTTP concerns.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Optional, Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.catalog import Organization, Product
from app.models.flow import Asset
from app.models.procurement import OrderItem, PurchaseOrder
from app.services.exceptions import NotFoundError


def trace_asset(db: Session, asset_id: str) -> dict:
    """Full back-trace for one asset: product, order line, order, supplier, spend."""
    asset = db.get(Asset, asset_id)
    if asset is None:
        raise NotFoundError(f"Asset {asset_id!r} not found")

    product = db.get(Product, asset.product_id)

    order_item: Optional[OrderItem] = None
    order: Optional[PurchaseOrder] = None
    supplier: Optional[Organization] = None
    unit_price: Optional[Decimal] = None

    if asset.source_order_item_id:
        order_item = db.get(OrderItem, asset.source_order_item_id)
    if order_item is not None:
        unit_price = order_item.unit_price
        order = db.get(PurchaseOrder, order_item.order_id)
    if order is not None:
        supplier = db.get(Organization, order.supplier_id)

    return {
        "asset_id": asset.id,
        "serial_number": asset.serial_number,
        "status": asset.status,
        "product_id": asset.product_id,
        "product_name": product.name if product else None,
        "order_item_id": asset.source_order_item_id,
        "order_id": order.id if order else None,
        "order_number": order.order_number if order else None,
        "supplier_id": supplier.id if supplier else None,
        "supplier_name": supplier.name if supplier else None,
        "unit_price": unit_price,
    }


def assets_for_line(db: Session, order_item_id: str) -> Sequence[Asset]:
    """Every asset produced by a given order line."""
    if db.get(OrderItem, order_item_id) is None:
        raise NotFoundError(f"OrderItem {order_item_id!r} not found")
    return db.scalars(
        select(Asset).where(Asset.source_order_item_id == order_item_id)
        .order_by(Asset.serial_number)
    ).all()
