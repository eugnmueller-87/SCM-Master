"""Signal assembly for the agent — pure data gathering, no LLM, no DB writes.

These functions call the EXISTING read-only service functions in-process and
package their results into JSON-serialisable dicts the copilot reasons over.
Nothing here mutates state; everything is coerced to plain types (str/float/
int/bool/None, lists, dicts) so the output can be embedded directly in a prompt
or returned as JSON.
"""
from __future__ import annotations

import enum
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Optional

from sqlalchemy.orm import Session

from app.models.flow import AssetStatus
from app.services import analytics, planning, sourcing
from app.services.asset import asset_service


def _jsonable(value: Any) -> Any:
    """Recursively coerce service output into plain JSON-serialisable types."""
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, enum.Enum):
        return value.value
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    return value


def gather_sourcing_signals(db: Session, product_id: str,
                            desired_qty: Optional[int] = None) -> dict:
    """Assemble the five named signals for a sourcing decision on one product.

    Reads (no writes): sourcing.suggest_sources, planning.location_capacity,
    planning.inbound_pipeline (filtered to this product), planning.deployment_forecast.
    policy_context is a stub until authority/threshold rules land.
    """
    sources = sourcing.suggest_sources(db, product_id)
    capacity = planning.location_capacity(db)
    inbound_all = planning.inbound_pipeline(db)
    inbound_for_product = [row for row in inbound_all if row.get("product_id") == product_id]
    forecast = planning.deployment_forecast(db)

    return _jsonable({
        "source_context": {
            "product_id": product_id,
            "desired_qty": desired_qty,
            "ranked_sources": sources,
        },
        "capacity_context": {
            "locations": capacity,
        },
        "inbound_context": {
            "open_lines_for_product": inbound_for_product,
            "open_lines_total": len(inbound_all),
        },
        "demand_context": forecast,
        "policy_context": {"note": "authority/threshold check pending"},
    })


def gather_insight_signals(db: Session) -> dict:
    """Assemble portfolio-wide signals for insight generation.

    Spend by supplier/product/category from analytics, plus a cheap
    assets-by-status / by-location summary from asset_service.list.
    """
    by_status: dict[str, int] = {}
    by_location: dict[str, int] = {}
    # asset_service.list is a cheap existing read; tally a status/location summary.
    for status in AssetStatus:
        assets = asset_service.list(db, status=status, limit=100000)
        by_status[status.value] = len(assets)
    for asset in asset_service.list(db, limit=100000):
        loc = asset.current_location_id or "(unassigned)"
        by_location[loc] = by_location.get(loc, 0) + 1

    return _jsonable({
        "spend_by_supplier": analytics.spend_by_supplier(db),
        "spend_by_product": analytics.spend_by_product(db),
        "spend_by_category": analytics.spend_by_category(db),
        "assets_summary": {
            "by_status": by_status,
            "by_location": by_location,
        },
    })
