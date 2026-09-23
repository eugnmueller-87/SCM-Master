"""Schemas for the TCO read API (mirror the service's dict output)."""
from __future__ import annotations

from datetime import date
from typing import Dict, List, Optional

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


# ---- the device fleet (services/tco_device.py) ------------------------------

class DeviceRate(BaseModel):
    """A design parameter: a placeholder until the owning role sets it."""
    id: str
    label: str
    unit: str
    owner: str
    placeholder: bool
    note: str
    value: Optional[float]
    by_family: Optional[Dict[str, float]]


class DeviceLayerDef(BaseModel):
    id: str
    label: str
    description: str


class DeviceComponent(BaseModel):
    """One source of a layer's number: a measured quantity, and either a measured cost or a rate."""
    id: str
    label: str
    basis: str            # "measured" or "quantity measured, rate placeholder"
    quantity: Optional[float]
    unit: str
    rate: Optional[float]
    rate_id: Optional[str]
    total: Optional[float]   # a credit is negative
    reason: Optional[str]
    note: Optional[str]


class DeviceLayer(BaseModel):
    id: str
    label: str
    total: Optional[float]
    per_device: Optional[float]
    per_month: Optional[float]
    reason: Optional[str]
    components: List[DeviceComponent]


class DeviceMoney(BaseModel):
    gross: Optional[float]
    credit: Optional[float]
    net: Optional[float]


class DeviceResale(BaseModel):
    sold: int
    sold_priced: int
    proceeds: float
    acquisition_of_sold: float
    credit_share_of_acquisition: Optional[float]
    reason: Optional[str]


class DeviceGroup(BaseModel):
    """One population: a model, a device class or the whole portfolio, with the counts behind every figure."""
    kind: str             # portfolio | class | model
    key: str
    label: str
    family: Optional[str]
    product_code: Optional[str]
    devices: int
    rented: int
    on_hand: int
    sold: int
    recycled: int
    priced: int
    contracts: int
    contracts_cycle2: int
    device_months: float
    device_months_cycle2: float
    months_per_device: Optional[float]
    second_life_share_of_months: Optional[float]
    repairs: int
    refurbs: int
    in_repair: int
    in_refurb: int
    swap_events: int
    layers: List[DeviceLayer]
    gross: Optional[float]
    credit: float
    net: Optional[float]
    per_device: DeviceMoney
    per_month: DeviceMoney
    per_month_reason: Optional[str]
    resale: DeviceResale
    unmeasured: List[str]
    reason: Optional[str]


class DeviceCohort(BaseModel):
    id: str
    label: str
    description: str
    portfolio: DeviceGroup
    classes: List[DeviceGroup]
    models: List[DeviceGroup]


class DeviceTCO(BaseModel):
    scenario: str
    as_of: date
    reason: Optional[str]
    basis: str
    rates: List[DeviceRate]
    layers: List[DeviceLayerDef]
    cohorts: Dict[str, DeviceCohort]
