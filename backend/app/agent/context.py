"""Assemble a compact, live snapshot of the whole operation for the chat agent.

The chat assistant answers use-case questions grounded in real data, so it needs
a context bundle spanning every domain — catalog, contracts, procurement, assets
& lifecycle, capacity, inbound, spend, and logistics tracking. This pulls from
the EXISTING read services (no new queries of substance) and trims to what fits
comfortably in a prompt.
"""
from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.catalog import Organization, Product, ProductSupplier
from app.models.flow import Asset, AssetStatus
from app.models.procurement import OrderItem, PurchaseOrder
from app.services import analytics, contracts, planning, tracking


def build_context(db: Session) -> dict:
    """A JSON-serialisable snapshot the agent can reason over."""
    # Asset distribution by status
    by_status = {
        s.value: int(db.scalar(select(func.count(Asset.id)).where(Asset.status == s)) or 0)
        for s in AssetStatus
    }

    # Counts
    n_products = int(db.scalar(select(func.count(Product.id))) or 0)
    n_suppliers = int(db.scalar(select(func.count(Organization.id)).where(Organization.is_supplier.is_(True))) or 0)
    n_assets = sum(by_status.values())

    # Orders by status
    orders = db.scalars(select(PurchaseOrder)).all()
    orders_by_status: dict[str, int] = {}
    order_rows = []
    for o in orders:
        orders_by_status[o.status.value] = orders_by_status.get(o.status.value, 0) + 1
        n_lines = int(db.scalar(select(func.count(OrderItem.id)).where(OrderItem.order_id == o.id)) or 0)
        order_rows.append({"order_number": o.order_number, "status": o.status.value, "lines": n_lines})

    # Contracts (enriched: status + ytd vs budget)
    contract_rows = []
    for ps in db.scalars(select(ProductSupplier)).all():
        e = contracts.enrich(db, ps)
        prod = db.get(Product, ps.product_id)
        org = db.get(Organization, ps.supplier_id)
        contract_rows.append({
            "product": prod.name if prod else ps.product_id,
            "supplier": org.name if org else ps.supplier_id,
            "status": e["contract_status"],
            "price": float(e["contract_price"]) if e["contract_price"] is not None else None,
            "lead_time_days": e["standard_lead_time_days"],
            "preference_rank": e["preference_rank"],
            "annual_budget": float(e["annual_budget"]) if e["annual_budget"] is not None else None,
            "ytd_spend": float(e["ytd_spend"]) if e["ytd_spend"] is not None else 0.0,
            "term_end": e["term_end"].isoformat() if e["term_end"] else None,
        })

    # Spend
    spend = analytics.spend_summary(db)
    spend_ctx = {
        "total_units": spend["total_units"],
        "total_spend": float(spend["total_spend"]),
        "by_supplier": [{"supplier": r["supplier_name"], "units": r["units"], "spend": float(r["spend"])}
                        for r in spend["by_supplier"]],
        "by_category": [{"category": r["category"], "units": r["units"], "spend": float(r["spend"])}
                        for r in spend["by_category"]],
    }

    # Capacity (flag the tight ones)
    caps = planning.location_capacity(db)
    capacity_ctx = [{"code": c["code"], "name": c["name"], "used": c["used"],
                     "capacity": c["capacity"], "utilisation": c["utilisation"],
                     "over_capacity": c["over_capacity"]} for c in caps]

    # Inbound pipeline (open lines, overdue flags)
    inbound = planning.inbound_pipeline(db)
    inbound_ctx = [{"order_number": r["order_number"], "outstanding": r["outstanding"],
                    "eta": r["estimated_delivery_date"].isoformat() if r["estimated_delivery_date"] else None,
                    "overdue": r["overdue"]} for r in inbound]

    forecast = planning.deployment_forecast(db)

    # Logistics tracking (current positions)
    track = tracking.order_tracking(db)
    tracking_ctx = [{"po_id": t["po_id"], "supplier": t["supplier"], "mode": t["mode"],
                     "status": t["status_label"], "current_location": t["current_location"],
                     "delay_days": t["delay_days"]} for t in track]

    return {
        "totals": {"products": n_products, "suppliers": n_suppliers, "assets": n_assets},
        "assets_by_status": by_status,
        "deployment_forecast": forecast,
        "orders_by_status": orders_by_status,
        "orders": order_rows,
        "contracts": contract_rows,
        "spend": spend_ctx,
        "capacity": capacity_ctx,
        "inbound": inbound_ctx,
        "tracking": tracking_ctx,
    }
