"""Schemas for capacity & flow planning views."""
from __future__ import annotations

from datetime import date
from typing import Literal, Optional

from pydantic import BaseModel

from app.models.flow import LocationType
from app.models.procurement import OrderStatus


class InboundLine(BaseModel):
    order_id: str
    order_number: str
    order_status: OrderStatus
    order_item_id: str
    product_id: str
    ordered: int
    received: int
    outstanding: int
    estimated_delivery_date: Optional[date]
    overdue: bool


class LocationCapacity(BaseModel):
    location_id: str
    code: str
    name: str
    location_type: LocationType
    capacity: Optional[int]
    used: int
    free: Optional[int]
    utilisation: Optional[float]
    over_capacity: bool


class DeploymentForecast(BaseModel):
    on_hand: int
    inbound: int
    deployed: int
    forecast_deployable: int


class DemandForecastItem(BaseModel):
    product_id: str
    product_code: Optional[str]
    name: Optional[str]
    category: Optional[str]
    usage_rate_per_day: float       # recency-weighted deployments/day
    horizon_days: int
    projected_usage: float          # usage_rate x horizon
    eol_replacement: int            # refresh demand from ageing fleet within horizon
    projected_demand: float         # usage + eol
    on_hand: int
    on_order: int
    available: int                  # on_hand + on_order
    projected_shortfall: float      # max(0, demand - available)
    recommended_order_qty: int      # shortfall rounded to MOQ
    order_by: Optional[date]        # place by this date to cover the horizon
    lead_time_days: int
    unit_price: Optional[float]


class CapacityCause(BaseModel):
    name: str
    units: int


class CapacityPoCause(BaseModel):
    order_number: str
    units: int


class CapacityRoom(BaseModel):
    code: str
    free: int


class CapacityDiagnosis(BaseModel):
    location_id: str
    code: str
    name: str
    location_type: LocationType
    used: int
    capacity: Optional[int]
    utilisation: Optional[float]
    over_capacity: bool
    near_capacity: bool
    overflow: int
    inbound_units: int
    inbound_pos: list[str]
    by_product: list[CapacityCause]
    by_source_po: list[CapacityPoCause]
    by_status: dict[str, int]
    room_elsewhere: int
    rebalance_targets: list[CapacityRoom]
    recommended_action: str   # rebalance / hold_inbound / add_capacity / watch
    summary: str


class StorageZone(BaseModel):
    code: str
    name: str
    capacity: int
    used: int
    free: Optional[int]
    inbound: int
    storable: int


class StorageHeadroom(BaseModel):
    storable_max: Optional[int]  # units we could order and still store; None = no defined limit
    free_now: int
    committed_inbound: int
    zones: list[StorageZone]


class CapacityFlow(BaseModel):
    """One warehouse capacity-vs-flow picture (the over-order guard reads this)."""
    as_of: str
    capacity: int
    on_hand: int
    inbound: int
    committed: int                       # on_hand + inbound
    free_to_order: Optional[int]         # hard cap a new order must respect; None = no limit
    committed_pct: Optional[float]
    daily_in: float                      # incoming units/day
    daily_out: float                     # outgoing units/day (burn)
    net_flow_per_day: float              # >0 filling, <0 draining
    weeks_of_cover: Optional[float]
    days_to_depletion: Optional[float]
    zones: list[StorageZone]


class RebalanceTarget(BaseModel):
    code: str
    moved: int


class RebalanceResult(BaseModel):
    moved: int
    source: str
    targets: list[RebalanceTarget]
    remaining_over: int = 0
    message: str


class RecoveryOption(BaseModel):
    """One enumerated recovery lever for a line that will stock out before inbound."""
    lever: Literal["expedite", "bridge_buy"]
    source: Optional[str] = None              # supplier name
    qty: int
    unit_cost: Optional[float] = None
    landed_cost: Optional[float] = None       # qty×unit + adder; None when unpriced
    land_date: Optional[date] = None          # when this option's units would arrive
    feasible: bool                            # lands on/before the dry-out date?
    unpriced: bool = False                    # cost inputs missing → surfaced, not dropped


class RecoveryRecommendation(BaseModel):
    """Deterministic recovery policy for a line at stock-out risk before inbound.

    Survival and buffer-rebuild are DISTINCT components on purpose — the planner
    must see "survive: X / rebuild buffer: Y", not a merged number. Every figure
    here is code-computed; an LLM may narrate over this object but emit no number
    that isn't in it (see app.agent.grounding).
    """
    at_risk: bool
    dry_out_date: Optional[date] = None       # when on-hand hits 0 at current burn
    inbound_land_date: Optional[date] = None  # when the open PO arrives
    gap_days: int = 0                         # dry-out → inbound window to bridge
    survival_qty: int = 0                     # ceil(burn × gap_days) — the don't-hit-zero floor
    buffer_rebuild_qty: int = 0               # service-level safety over the bridge window (distinct)
    recommended: Optional[RecoveryOption] = None   # cheapest feasible lever
    options: list[RecoveryOption] = []        # all levers, incl. unpriced / infeasible
    assumptions: list[str] = []               # which inputs were defaulted/assumed
    summary: str = ""                         # deterministic, decision-complete one-liner


class PositionPoLine(BaseModel):
    """One open PO line behind a product's on_order (the drill-down / audit trail)."""
    order_number: str
    ordered: int
    received: int
    outstanding: int
    unit_price: Optional[float] = None
    eta: Optional[date] = None


class InventoryPositionRow(BaseModel):
    """One product's MRP position. Mirrors planning.PositionRow — the SINGLE source
    the agent's netting and this overview both read, so they cannot disagree."""
    product_id: str
    name: Optional[str] = None
    category: Optional[str] = None
    gross_demand: int          # projected demand over the horizon (Need)
    on_hand: int
    on_order: int
    position: int              # on_hand + on_order
    safety_stock: int
    net_requirement: int       # Missing = max(0, gross - position - safety)
    staged_planned: int        # open STAGED requisition qty (planned)
    capacity_avail: int        # global shared storable headroom
    product_capacity: int      # this product's own capacity (per-product cap)
    new_proposal: int
    proposing: int             # orderable now (has capacity)
    deferred: int              # capacity-blocked
    unit_price: Optional[float] = None
    committed_value: float     # on_order × landed unit
    proposing_value: float     # proposing × unit price
    daily_burn: float = 0.0
    cover_days: Optional[int] = None       # how long on_hand lasts
    lands_in_days: Optional[int] = None    # how long the inbound takes to land
    at_risk: bool = False                  # runs dry before inbound (recovery predicate)
    po_lines: list[PositionPoLine] = []   # drill-down: the POs behind on_order


class InventoryItem(BaseModel):
    product_id: str
    product_code: Optional[str]
    name: Optional[str]
    category: Optional[str]
    on_hand: int
    capacity: int            # derived proxy (no per-product capacity in the model)
    safety_stock: int        # derived (~half of lead-time demand)
    daily_burn: float        # real (deployed in trailing window / window days)
    lead_time_days: int      # real (preferred source)
    on_order: int            # real (open inbound)
    next_eta: Optional[date]
    unit_price: Optional[float]
    # Populated only for lines that will stock out before their inbound lands.
    recovery: Optional[RecoveryRecommendation] = None
