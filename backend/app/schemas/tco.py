"""Schemas for the TCO read API (mirror the service's dict output)."""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel


class TCOWaterfall(BaseModel):
    acquisition: float
    landed: float
    deployment: float
    opex: float
    eol: float
    recovery: float  # negative step (money back)


class ShouldCostVariance(BaseModel):
    should_cost_target: float
    actual_acquisition: float
    variance_abs: float
    variance_pct: Optional[float]


class AssetTCO(BaseModel):
    asset_id: str
    serial_number: str
    product_id: str
    waterfall: TCOWaterfall
    tco_total: float
    should_cost_variance: Optional[ShouldCostVariance]
    excluded_landed_types: list[str]


class PortfolioSubtotals(BaseModel):
    acquisition: float
    landed: float
    deployment: float
    opex: float
    eol: float
    recovery: float


class PortfolioTCO(BaseModel):
    assets: int
    baseline: float
    subtotals: PortfolioSubtotals
    tco_total: float
    total_cost_pct: float  # ΣTCO / baseline (includes hardware)
    tscmc_pct: float       # Σ(TCO − acquisition) / baseline (SCOR: excludes acquisition)
    excluded_landed_types: list[str]


class TCOByClassRow(BaseModel):
    category: str
    assets: int
    acquisition: float
    landed: float
    deployment: float
    opex: float
    eol: float
    recovery: float
    tco_total: float
    avg_tco: float
