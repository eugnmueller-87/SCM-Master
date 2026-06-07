"""Schemas for the should-cost / clean-sheet costing domain."""
from __future__ import annotations

from datetime import date
from typing import Optional

from pydantic import BaseModel, ConfigDict

from app.models.costing import CostingMethod

# --- commodity catalog -----------------------------------------------------


class CommodityCreate(BaseModel):
    code: str
    name: str
    unit: str = "index"
    baseline_value: float = 1.0


class CommodityRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    code: str
    name: str
    unit: str
    baseline_value: float


class CommodityPriceCreate(BaseModel):
    price_date: date
    value: float


class CommodityPriceRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    commodity_id: str
    price_date: date
    value: float


# --- BOM -------------------------------------------------------------------


class BOMLineIn(BaseModel):
    component_class_code: str
    label: str
    qty: int = 1
    base_material_cost: Optional[float] = None
    conversion_cost: Optional[float] = 0.0
    overhead_pct: Optional[float] = 0.0
    list_price: Optional[float] = None
    discount_pct: Optional[float] = None


class CostParamsIn(BaseModel):
    integration_pct: float = 0.06
    sga_pct: float = 0.08
    target_margin_pct: float = 0.10


class BOMIn(BaseModel):
    notes: Optional[str] = None
    params: Optional[CostParamsIn] = None
    lines: list[BOMLineIn]


class BOMLineRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    label: str
    qty: int
    component_class_id: str
    base_material_cost: Optional[float]
    conversion_cost: Optional[float]
    overhead_pct: Optional[float]
    list_price: Optional[float]
    discount_pct: Optional[float]


class BOMRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    product_id: str
    notes: Optional[str]
    lines: list[BOMLineRead]


# --- computed outputs (mirror the engine's as_dict) ------------------------


class ShouldCostLine(BaseModel):
    label: str
    method: str
    qty: int
    component_floor: float
    commodity_tracked: bool


class ShouldCostResult(BaseModel):
    product_id: str
    as_of: str
    lines: list[ShouldCostLine]
    material_total: float
    assembly_integration: float
    sga: float
    should_cost_floor: float
    target_price: float
    run_id: Optional[str] = None


class CostGapResult(BaseModel):
    product_id: str
    as_of: str
    product_supplier_id: Optional[str]
    quoted_price: Optional[float]
    target_price: float
    should_cost_floor: float
    gap_to_target_abs: Optional[float]
    gap_to_target_pct: Optional[float]
    addressable_saving: Optional[float]
    gap_to_floor_abs: Optional[float]
    gap_to_floor_pct: Optional[float]
    has_quote: bool


class SensitivityResult(BaseModel):
    product_id: str
    as_of: str
    delta: float
    floor_low: float
    floor_base: float
    floor_high: float
    swing_abs: float
    swing_pct: float


class SupplierGapRow(BaseModel):
    product_id: str
    product_supplier_id: Optional[str]
    should_cost_floor: float
    target_price: float
    quoted_price: Optional[float]
    gap_to_target_abs: Optional[float]
    gap_to_target_pct: Optional[float]


class SavingsSummary(BaseModel):
    as_of: str
    products_with_bom: int
    products_above_target: int
    total_gap_to_target: float


# Method re-exported for callers/tests that want the enum.
__all__ = [n for n in dir() if n[0].isupper()] + ["CostingMethod"]
