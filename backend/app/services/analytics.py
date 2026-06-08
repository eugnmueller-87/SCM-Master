"""Spend analytics, built on the never-broken asset -> order-line provenance.

Spend is computed from received assets, not just ordered quantities, so the
numbers reflect what actually arrived. Each asset carries its source order line
(``source_order_item_id``); the line's ``unit_price`` is the per-unit spend.
Grouping that by supplier / product / category answers "where did the money go".
"""
from __future__ import annotations

from collections import defaultdict
from decimal import Decimal

from sqlalchemy import extract, select
from sqlalchemy.orm import Session

from app.models.catalog import Organization, Product
from app.models.flow import Asset
from app.models.procurement import OrderItem, PurchaseOrder


def _received_spend_rows(db: Session, year: int | None = None):
    """Yield (asset, order_item, order, product) for every asset that traces to
    an order line. Assets with no provenance link are skipped.

    When ``year`` is given, only assets *received* in that calendar year are
    included (by ``Asset.received_date``); assets with no received date are
    excluded from any specific year but still appear in the unfiltered total.
    """
    stmt = (
        select(Asset, OrderItem, PurchaseOrder, Product)
        .join(OrderItem, Asset.source_order_item_id == OrderItem.id)
        .join(PurchaseOrder, OrderItem.order_id == PurchaseOrder.id)
        .join(Product, Asset.product_id == Product.id)
    )
    if year is not None:
        stmt = stmt.where(
            Asset.received_date.is_not(None),
            extract("year", Asset.received_date) == year,
        )
    return db.execute(stmt).all()


def spend_years(db: Session) -> list[int]:
    """Distinct calendar years in which assets were received, newest first.

    Drives the cockpit's year selector so it only ever offers years that have
    data, instead of a hardcoded range.
    """
    rows = db.execute(
        select(extract("year", Asset.received_date))
        .join(OrderItem, Asset.source_order_item_id == OrderItem.id)
        .where(Asset.received_date.is_not(None))
        .distinct()
    ).all()
    return sorted({int(r[0]) for r in rows if r[0] is not None}, reverse=True)


def _unit_price(order_item: OrderItem) -> Decimal:
    return order_item.unit_price if order_item.unit_price is not None else Decimal("0")


def spend_by_supplier(db: Session, year: int | None = None) -> list[dict]:
    totals: dict[str, dict] = defaultdict(lambda: {"units": 0, "spend": Decimal("0"), "name": None})
    for asset, oi, order, _product in _received_spend_rows(db, year):
        bucket = totals[order.supplier_id]
        bucket["units"] += 1
        bucket["spend"] += _unit_price(oi)
    # resolve names
    out = []
    for supplier_id, b in totals.items():
        org = db.get(Organization, supplier_id)
        out.append({
            "supplier_id": supplier_id,
            "supplier_name": org.name if org else None,
            "units": b["units"],
            "spend": b["spend"],
        })
    return sorted(out, key=lambda r: r["spend"], reverse=True)


def spend_by_product(db: Session, year: int | None = None) -> list[dict]:
    totals: dict[str, dict] = defaultdict(lambda: {"units": 0, "spend": Decimal("0"), "name": None, "category": None})
    for asset, oi, _order, product in _received_spend_rows(db, year):
        b = totals[asset.product_id]
        b["units"] += 1
        b["spend"] += _unit_price(oi)
        b["name"] = product.name
        b["category"] = product.category
    out = [
        {"product_id": pid, "product_name": b["name"], "category": b["category"],
         "units": b["units"], "spend": b["spend"]}
        for pid, b in totals.items()
    ]
    return sorted(out, key=lambda r: r["spend"], reverse=True)


def spend_by_category(db: Session, year: int | None = None) -> list[dict]:
    totals: dict[str, dict] = defaultdict(lambda: {"units": 0, "spend": Decimal("0")})
    for asset, oi, _order, product in _received_spend_rows(db, year):
        cat = product.category or "(uncategorised)"
        totals[cat]["units"] += 1
        totals[cat]["spend"] += _unit_price(oi)
    out = [
        {"category": cat, "units": b["units"], "spend": b["spend"]}
        for cat, b in totals.items()
    ]
    return sorted(out, key=lambda r: r["spend"], reverse=True)


def spend_summary(db: Session, year: int | None = None) -> dict:
    rows = _received_spend_rows(db, year)
    total = sum((_unit_price(oi) for _a, oi, _o, _p in rows), Decimal("0"))
    return {
        "total_units": len(rows),
        "total_spend": total,
        "by_supplier": spend_by_supplier(db, year),
        "by_category": spend_by_category(db, year),
    }
