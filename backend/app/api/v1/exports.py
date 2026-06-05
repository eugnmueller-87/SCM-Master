"""Flat analytics exports for BI tools (Power BI / Tableau).

These endpoints return **flat CSV** — one row per fact, stable column order — so a
BI tool can connect with its plain Web/URL connector and refresh on a schedule.
That's deliberately the lowest-friction path: it works against the live demo with
no database credentials to share. (For a production install, the same facts would
be exposed as Postgres ``fact_*`` views for DirectQuery — see
``docs/powerbi-analytics.md``.)

Three facts:
  - ``forecast-accuracy.csv`` — the agent's demand forecast backtested against
    actual deployments (one row per as-of date × product);
  - ``demand-history.csv``    — monthly actual deployments per product (the time
    series the forecast learns from);
  - ``spend.csv``             — received spend per supplier (from the asset→order
    provenance).

Reads, so any authenticated user may pull them.
"""
from __future__ import annotations

import csv
import io

from fastapi import APIRouter, Depends
from fastapi.responses import Response
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, get_db
from app.models.auth import User
from app.services import accuracy, analytics

router = APIRouter(tags=["exports"], prefix="/analytics/exports")


def _csv_response(rows: list[dict], columns: list[str], filename: str) -> Response:
    """Render rows (list of dicts) to a CSV Response with a fixed column order.

    A header row is always emitted (even with zero data rows) so a BI tool can
    still infer the schema. Values are written as-is; None becomes an empty cell.
    """
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=columns, extrasaction="ignore")
    writer.writeheader()
    for r in rows:
        writer.writerow({c: ("" if r.get(c) is None else r.get(c)) for c in columns})
    return Response(
        content=buf.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/forecast-accuracy.csv")
def forecast_accuracy_csv(db: Session = Depends(get_db),
                          _user: User = Depends(get_current_user)):
    """Backtest: predicted vs actual demand per as-of date × product, with error."""
    rows = accuracy.backtest(db)
    columns = [
        "as_of_date", "horizon_days", "window_end",
        "product_code", "name", "category",
        "usage_rate_per_day", "predicted_demand", "actual_demand",
        "abs_error", "ape",
    ]
    return _csv_response(rows, columns, "forecast-accuracy.csv")


@router.get("/demand-history.csv")
def demand_history_csv(db: Session = Depends(get_db),
                       _user: User = Depends(get_current_user)):
    """Monthly actual deployments per product — the demand time series."""
    rows = accuracy.monthly_demand_history(db)
    columns = ["month", "month_start", "product_code", "name", "category", "units_deployed"]
    return _csv_response(rows, columns, "demand-history.csv")


@router.get("/spend.csv")
def spend_csv(db: Session = Depends(get_db),
              _user: User = Depends(get_current_user)):
    """Received spend per supplier (units + spend), from asset→order provenance."""
    rows = analytics.spend_by_supplier(db)
    columns = ["supplier_id", "supplier_name", "units", "spend"]
    return _csv_response(rows, columns, "spend.csv")
