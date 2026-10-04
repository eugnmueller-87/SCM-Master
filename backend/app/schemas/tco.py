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
    """A design parameter: a placeholder until the owning role sets it, or a public price with its source."""
    id: str
    label: str
    unit: str
    owner: str
    placeholder: bool
    note: str
    value: Optional[float]
    by_family: Optional[Dict[str, float]]
    source: Optional[str] = None


class DeviceLayerDef(BaseModel):
    id: str
    label: str
    description: str


class DeviceComponent(BaseModel):
    """One source of a layer's number: a measured quantity, and either a measured cost or a rate."""
    id: str
    label: str
    basis: str            # "measured", "quantity measured, rate placeholder" or "quantity measured, rate from a public source"
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


class DeviceRent(BaseModel):
    """What the contracts earned, and the margin after the whole-life cost."""
    revenue: Optional[float]
    contracts_with_rent: int
    rent_months: float
    per_month: Optional[float]          # the average rent of a device-month
    per_device: Optional[float]
    margin: Optional[float]
    margin_per_device: Optional[float]
    margin_per_month: Optional[float]
    margin_share: Optional[float]       # margin over revenue
    reason: Optional[str]
    note: Optional[str]


class DeviceGroup(BaseModel):
    """One population: a model, a device class or the whole portfolio, with the counts behind every figure."""
    kind: str             # portfolio | class | model | device
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
    returns: int
    device_days_owned: int
    device_days_off_rent: Optional[int]
    layers: List[DeviceLayer]
    gross: Optional[float]
    credit: float
    net: Optional[float]
    rent: DeviceRent
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


class DeviceLifeEvent(BaseModel):
    """One dated step of a device's life: received, a rental, a repair or refurbishment, the sale."""
    date: date
    kind: str             # received | rental | repair | refurb | sold | recycled
    amount: Optional[float] = None    # the price paid, the rent a rental earned, an invoice, the proceeds
    cycle: Optional[int] = None
    end: Optional[date] = None
    reason: Optional[str] = None
    days: Optional[int] = None
    rent_eur_month: Optional[float] = None
    channel: Optional[str] = None


class DeviceOne(DeviceGroup):
    """One serial through the same figures as the fleet, with the dated life behind them."""
    as_of: date
    id: str
    serial_number: str
    product_id: str
    status: str
    grade: Optional[str]
    cycle_no: int
    finished: bool
    unit_price: Optional[float]
    received_date: Optional[date]
    sold_date: Optional[date]
    sale_price: Optional[float]
    sale_channel: Optional[str]
    life: List[DeviceLifeEvent]


class DeviceSerial(BaseModel):
    serial_number: str
    status: str
    cycle_no: int
    group: str            # finished | rented | on_hand


class DeviceSerials(BaseModel):
    product_id: str
    label: str
    family: Optional[str]
    serials: List[DeviceSerial]
