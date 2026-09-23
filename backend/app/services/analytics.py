"""Spend analytics, built on the never-broken asset -> order-line provenance.

Spend is computed from received assets, not just ordered quantities, so the
numbers reflect what actually arrived. Each asset carries its source order line
(``source_order_item_id``); the line's ``unit_price`` is the per-unit spend.
Grouping that by supplier / product / category answers "where did the money go".
"""
from __future__ import annotations

from collections import defaultdict
from decimal import Decimal

from sqlalchemy import and_, extract, func, or_, select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.catalog import Organization, Product
from app.models.flow import Asset
from app.models.procurement import OrderItem, PurchaseOrder


def _analytics_only_prefixes() -> tuple[str, ...]:
    """Product-code prefixes that are ANALYTICS-ONLY synthetic fixtures (the TCO
    and should-cost datasets). Shared with the procurement exclusion so the
    cockpit's spend and the buyer board hide the SAME synthetic products — a
    single source of truth. Their assets are real cost fixtures for the TCO /
    should-cost pages, but they are not real procurement spend, so they (and the
    synthetic 'vendor' that only supplies them) must not appear in spend
    analytics alongside real suppliers."""
    return tuple(
        p.strip() for p in settings.procurement_excluded_code_prefixes.split(",") if p.strip()
    )


def _received_spend_rows(db: Session, year: int | None = None):
    """Yield (asset, order_item, order, product) for every asset that traces to
    an order line. Assets with no provenance link are skipped, and assets of
    analytics-only synthetic products (TCO-*/SCN-*) are excluded so spend
    reflects only real, buyable products and their real suppliers.

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
    prefixes = _analytics_only_prefixes()
    if prefixes:
        # Keep a product only if its code matches NONE of the excluded prefixes
        # (AND of the NOT LIKEs), OR the code is NULL (a real product without a
        # code is not a synthetic fixture). A plain OR of the NOT LIKEs would let
        # every synthetic row through, since e.g. a 'TCO-' code still satisfies
        # 'NOT LIKE SCN-%'.
        stmt = stmt.where(
            or_(
                Product.product_code.is_(None),
                and_(*[Product.product_code.notlike(f"{p}%") for p in prefixes]),
            )
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
    stmt = (
        select(extract("year", Asset.received_date))
        .join(OrderItem, Asset.source_order_item_id == OrderItem.id)
        .join(Product, Asset.product_id == Product.id)
        .where(Asset.received_date.is_not(None))
        .distinct()
    )
    prefixes = _analytics_only_prefixes()
    if prefixes:
        # Same analytics-only exclusion as _received_spend_rows, so the year
        # selector never offers a year that exists only because of synthetic data.
        stmt = stmt.where(
            or_(
                Product.product_code.is_(None),
                and_(*[Product.product_code.notlike(f"{p}%") for p in prefixes]),
            )
        )
    rows = db.execute(stmt).all()
    return sorted({int(r[0]) for r in rows if r[0] is not None}, reverse=True)


def _unit_price(order_item: OrderItem) -> Decimal:
    return order_item.unit_price if order_item.unit_price is not None else Decimal("0")


def _supplier_names(db: Session, supplier_ids) -> dict[str, str | None]:
    """Resolve many supplier ids to names in ONE query (replaces a per-supplier
    ``db.get`` loop / N+1). Missing ids map to None, matching the old behaviour."""
    ids = [sid for sid in supplier_ids if sid is not None]
    if not ids:
        return {}
    rows = db.execute(
        select(Organization.id, Organization.name).where(Organization.id.in_(ids))
    ).all()
    return {oid: name for oid, name in rows}


def _spend_grouped(db: Session, year: int | None, *group_cols):
    """(group columns…, units, spend) straight from the database.

    Same join and same filter as ``_received_spend_rows``, but the counting happens in
    SQL. The row-by-row version is kept for callers that already hold the rows; it is
    only viable while the fleet is small — at 431,000 serials it returned 431,000
    four-entity ORM rows and took fifteen seconds per call.
    """
    stmt = (
        select(*group_cols, func.count(Asset.id), func.coalesce(func.sum(OrderItem.unit_price), 0))
        .select_from(Asset)
        .join(OrderItem, Asset.source_order_item_id == OrderItem.id)
        .join(PurchaseOrder, OrderItem.order_id == PurchaseOrder.id)
        .join(Product, Asset.product_id == Product.id)
        .group_by(*group_cols)
    )
    prefixes = _analytics_only_prefixes()
    if prefixes:
        stmt = stmt.where(or_(Product.product_code.is_(None),
                              and_(*[Product.product_code.notlike(f"{p}%") for p in prefixes])))
    if year is not None:
        stmt = stmt.where(Asset.received_date.is_not(None), extract("year", Asset.received_date) == year)
    return db.execute(stmt).all()


def spend_totals(db: Session, year: int | None = None) -> tuple[int, Decimal]:
    """(units, spend) over everything with provenance — one scalar read."""
    stmt = (
        select(func.count(Asset.id), func.coalesce(func.sum(OrderItem.unit_price), 0))
        .select_from(Asset)
        .join(OrderItem, Asset.source_order_item_id == OrderItem.id)
        .join(PurchaseOrder, OrderItem.order_id == PurchaseOrder.id)
        .join(Product, Asset.product_id == Product.id)
    )
    prefixes = _analytics_only_prefixes()
    if prefixes:
        stmt = stmt.where(or_(Product.product_code.is_(None),
                              and_(*[Product.product_code.notlike(f"{p}%") for p in prefixes])))
    if year is not None:
        stmt = stmt.where(Asset.received_date.is_not(None), extract("year", Asset.received_date) == year)
    units, spend = db.execute(stmt).one()
    return int(units or 0), Decimal(str(spend or 0))


def spend_by_supplier(db: Session, year: int | None = None, *, rows=None) -> list[dict]:
    if rows is None:
        grouped = _spend_grouped(db, year, PurchaseOrder.supplier_id)
        names = _supplier_names(db, [sid for sid, _u, _s in grouped])
        out = [{"supplier_id": sid, "supplier_name": names.get(sid), "units": int(u), "spend": Decimal(str(sp or 0))}
               for sid, u, sp in grouped]
        return sorted(out, key=lambda r: r["spend"], reverse=True)
    totals: dict[str, dict] = defaultdict(lambda: {"units": 0, "spend": Decimal("0"), "name": None})
    for _asset, oi, order, _product in rows:
        bucket = totals[order.supplier_id]
        bucket["units"] += 1
        bucket["spend"] += _unit_price(oi)
    names = _supplier_names(db, totals.keys())
    out = [
        {
            "supplier_id": supplier_id,
            "supplier_name": names.get(supplier_id),
            "units": b["units"],
            "spend": b["spend"],
        }
        for supplier_id, b in totals.items()
    ]
    return sorted(out, key=lambda r: r["spend"], reverse=True)


def spend_by_product(db: Session, year: int | None = None, *, rows=None) -> list[dict]:
    if rows is None:
        grouped = _spend_grouped(db, year, Asset.product_id, Product.name, Product.category)
        out = [{"product_id": pid, "product_name": name, "category": cat, "units": int(u), "spend": Decimal(str(sp or 0))}
               for pid, name, cat, u, sp in grouped]
        return sorted(out, key=lambda r: r["spend"], reverse=True)
    totals: dict[str, dict] = defaultdict(lambda: {"units": 0, "spend": Decimal("0"), "name": None, "category": None})
    for asset, oi, _order, product in rows:
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


def spend_by_category(db: Session, year: int | None = None, *, rows=None) -> list[dict]:
    if rows is None:
        grouped = _spend_grouped(db, year, Product.category)
        out = [{"category": cat or "(uncategorised)", "units": int(u), "spend": Decimal(str(sp or 0))}
               for cat, u, sp in grouped]
        return sorted(out, key=lambda r: r["spend"], reverse=True)
    totals: dict[str, dict] = defaultdict(lambda: {"units": 0, "spend": Decimal("0")})
    for _asset, oi, _order, product in rows:
        cat = product.category or "(uncategorised)"
        totals[cat]["units"] += 1
        totals[cat]["spend"] += _unit_price(oi)
    out = [
        {"category": cat, "units": b["units"], "spend": b["spend"]}
        for cat, b in totals.items()
    ]
    return sorted(out, key=lambda r: r["spend"], reverse=True)


def spend_summary(db: Session, year: int | None = None) -> dict:
    """Three grouped reads instead of one fleet-sized join.

    Each rollup aggregates in the database over exactly the same join and filter, so the
    totals agree with each other by construction rather than by shared Python state.
    """
    units, total = spend_totals(db, year)
    return {
        "total_units": units,
        "total_spend": total,
        "by_supplier": spend_by_supplier(db, year),
        "by_category": spend_by_category(db, year),
    }
