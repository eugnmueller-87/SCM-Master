"""Should-cost engine — pure, deterministic, fully unit-testable.

This is the analogue of ``lifecycle.py``: the part that must never be wrong, so
it is written as pure functions over plain dataclasses with NO database access.
The service/API layer adapts ORM rows into these inputs and persists the result.

Every formula here mirrors ``docs/should_cost_model.md`` §3–§7. Money is handled
with ``Decimal`` (quantised to cents at the boundaries) so the negotiation
numbers are exact, not float-fuzzy.

Definitions (from the spec):
    teardown line:        material_now   = base_material_cost × (index_now / index_baseline)
                          component_cost = material_now + conversion_cost
                                         + material_now × overhead_pct
                          component_floor = component_cost × qty
    reference_price line: component_floor = list_price × (1 − discount_pct) × qty
    roll-up:              material_total       = Σ component_floor
                          assembly_integration = material_total × integration_pct
                          sga                  = (material_total + assembly) × sga_pct
                          should_cost_floor    = material_total + assembly + sga
                          target_price         = should_cost_floor × (1 + target_margin_pct)
    gap (headline vs target, backstop vs floor):
                          gap_to_target = quoted − target_price
                          gap_to_floor  = quoted − should_cost_floor
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Optional

from app.models.costing import CostingMethod

# ---- value objects (plain, DB-free) ---------------------------------------

_CENT = Decimal("0.01")


def _money(x) -> Decimal:
    """Quantise to cents, half-up."""
    return Decimal(str(x)).quantize(_CENT, rounding=ROUND_HALF_UP)


class CostingError(ValueError):
    """A should-cost input is invalid (maps to HTTP 422 at the API layer)."""


@dataclass(frozen=True)
class LineInput:
    """One BOM line, already resolved (commodity multiplier looked up upstream).

    ``index_multiplier`` is ``index_now / index_baseline`` for teardown lines
    (1.0 == at baseline); it is ignored for reference_price lines.
    """

    label: str
    method: CostingMethod
    qty: int
    # teardown
    base_material_cost: Optional[Decimal] = None
    conversion_cost: Decimal = Decimal("0")
    overhead_pct: Decimal = Decimal("0")
    index_multiplier: Decimal = Decimal("1")
    # reference_price
    list_price: Optional[Decimal] = None
    discount_pct: Optional[Decimal] = None


@dataclass(frozen=True)
class Params:
    integration_pct: Decimal = Decimal("0.06")
    sga_pct: Decimal = Decimal("0.08")
    target_margin_pct: Decimal = Decimal("0.10")


@dataclass(frozen=True)
class LineResult:
    label: str
    method: str
    qty: int
    component_floor: Decimal
    commodity_tracked: bool  # True for teardown (moves with the index)


@dataclass
class CostResult:
    lines: list[LineResult] = field(default_factory=list)
    material_total: Decimal = Decimal("0")
    assembly_integration: Decimal = Decimal("0")
    sga: Decimal = Decimal("0")
    should_cost_floor: Decimal = Decimal("0")
    target_price: Decimal = Decimal("0")

    def as_dict(self) -> dict:
        return {
            "lines": [
                {
                    "label": ln.label,
                    "method": ln.method,
                    "qty": ln.qty,
                    "component_floor": float(ln.component_floor),
                    "commodity_tracked": ln.commodity_tracked,
                }
                for ln in self.lines
            ],
            "material_total": float(self.material_total),
            "assembly_integration": float(self.assembly_integration),
            "sga": float(self.sga),
            "should_cost_floor": float(self.should_cost_floor),
            "target_price": float(self.target_price),
        }


# ---- component build-up (spec §3) -----------------------------------------

def component_floor(line: LineInput) -> Decimal:
    """The floor contribution of one line × its quantity.

    Raises CostingError on invalid input (negative qty, missing required field).
    qty == 0 is allowed and yields 0 (the line still appears in the breakdown).
    """
    if line.qty < 0:
        raise CostingError(f"{line.label!r}: qty must be >= 0")
    if line.qty == 0:
        return _money(0)

    if line.method is CostingMethod.teardown:
        if line.base_material_cost is None:
            raise CostingError(f"{line.label!r}: teardown line needs base_material_cost")
        if line.index_multiplier <= 0:
            raise CostingError(f"{line.label!r}: index_multiplier must be > 0")
        material_now = line.base_material_cost * line.index_multiplier
        per_unit = material_now + line.conversion_cost + material_now * line.overhead_pct
        return _money(per_unit * line.qty)

    if line.method is CostingMethod.reference_price:
        if line.list_price is None or line.discount_pct is None:
            raise CostingError(f"{line.label!r}: reference_price line needs list_price + discount_pct")
        if not (Decimal("0") <= line.discount_pct <= Decimal("1")):
            raise CostingError(f"{line.label!r}: discount_pct must be in [0, 1]")
        per_unit = line.list_price * (Decimal("1") - line.discount_pct)
        return _money(per_unit * line.qty)

    raise CostingError(f"{line.label!r}: unknown costing method {line.method!r}")


def _is_commodity_tracked(line: LineInput) -> bool:
    # A teardown line whose multiplier can move with the market.
    return line.method is CostingMethod.teardown


# ---- config roll-up (spec §4) ---------------------------------------------

def roll_up(lines: list[LineInput], params: Params) -> CostResult:
    """Build the full should-cost floor + target price for a config."""
    result = CostResult()
    for line in lines:
        floor = component_floor(line)
        result.lines.append(
            LineResult(
                label=line.label,
                method=line.method.value,
                qty=line.qty,
                component_floor=floor,
                commodity_tracked=_is_commodity_tracked(line),
            )
        )
        result.material_total += floor

    result.material_total = _money(result.material_total)
    result.assembly_integration = _money(result.material_total * params.integration_pct)
    works_cost = result.material_total + result.assembly_integration
    result.sga = _money(works_cost * params.sga_pct)
    result.should_cost_floor = _money(works_cost + result.sga)
    result.target_price = _money(result.should_cost_floor * (Decimal("1") + params.target_margin_pct))
    return result


# ---- negotiation gap (spec §5) --------------------------------------------

@dataclass(frozen=True)
class GapResult:
    quoted_price: Optional[Decimal]
    target_price: Decimal
    should_cost_floor: Decimal
    # headline: vs target
    gap_to_target_abs: Optional[Decimal]
    gap_to_target_pct: Optional[float]
    addressable_saving: Optional[Decimal]
    # backstop: vs floor (total margin stacked — a ranking signal, not a demand)
    gap_to_floor_abs: Optional[Decimal]
    gap_to_floor_pct: Optional[float]
    has_quote: bool

    def as_dict(self) -> dict:
        def m(x):
            return None if x is None else float(x)
        return {
            "quoted_price": m(self.quoted_price),
            "target_price": float(self.target_price),
            "should_cost_floor": float(self.should_cost_floor),
            "gap_to_target_abs": m(self.gap_to_target_abs),
            "gap_to_target_pct": self.gap_to_target_pct,
            "addressable_saving": m(self.addressable_saving),
            "gap_to_floor_abs": m(self.gap_to_floor_abs),
            "gap_to_floor_pct": self.gap_to_floor_pct,
            "has_quote": self.has_quote,
        }


def gap(result: CostResult, quoted_price: Optional[Decimal],
        annual_volume: int = 0) -> GapResult:
    """Compare a quote to the floor + target. With no quote, gaps are null but
    the floor/target still stand (spec edge-case: 'no quote on file')."""
    if quoted_price is None:
        return GapResult(
            quoted_price=None, target_price=result.target_price,
            should_cost_floor=result.should_cost_floor,
            gap_to_target_abs=None, gap_to_target_pct=None, addressable_saving=None,
            gap_to_floor_abs=None, gap_to_floor_pct=None, has_quote=False,
        )

    q = _money(quoted_price)
    to_target = _money(q - result.target_price)
    to_floor = _money(q - result.should_cost_floor)
    pct_t = float(to_target / q) if q != 0 else None
    pct_f = float(to_floor / q) if q != 0 else None
    addressable = _money(to_target * annual_volume) if annual_volume else _money(0)
    return GapResult(
        quoted_price=q, target_price=result.target_price,
        should_cost_floor=result.should_cost_floor,
        gap_to_target_abs=to_target, gap_to_target_pct=pct_t, addressable_saving=addressable,
        gap_to_floor_abs=to_floor, gap_to_floor_pct=pct_f, has_quote=True,
    )


# ---- commodity sensitivity (spec §6) --------------------------------------

@dataclass(frozen=True)
class SensitivityResult:
    delta: float
    floor_low: Decimal   # at index × (1 − delta)
    floor_base: Decimal
    floor_high: Decimal  # at index × (1 + delta)
    swing_abs: Decimal   # max(|high−base|, |base−low|)
    swing_pct: float

    def as_dict(self) -> dict:
        return {
            "delta": self.delta,
            "floor_low": float(self.floor_low),
            "floor_base": float(self.floor_base),
            "floor_high": float(self.floor_high),
            "swing_abs": float(self.swing_abs),
            "swing_pct": self.swing_pct,
        }


def _scale_commodity(lines: list[LineInput], factor: Decimal) -> list[LineInput]:
    """Return new lines with teardown index multipliers scaled by ``factor``.
    reference_price lines are untouched (silicon has no commodity exposure)."""
    out: list[LineInput] = []
    for ln in lines:
        if ln.method is CostingMethod.teardown:
            out.append(LineInput(
                label=ln.label, method=ln.method, qty=ln.qty,
                base_material_cost=ln.base_material_cost,
                conversion_cost=ln.conversion_cost, overhead_pct=ln.overhead_pct,
                index_multiplier=ln.index_multiplier * factor,
                list_price=ln.list_price, discount_pct=ln.discount_pct,
            ))
        else:
            out.append(ln)
    return out


def sensitivity(lines: list[LineInput], params: Params, delta: float = 0.2) -> SensitivityResult:
    """Recompute the floor at commodity index ±delta (teardown lines only)."""
    if not (0 < delta < 1):
        raise CostingError("sensitivity delta must be in (0, 1)")
    d = Decimal(str(delta))
    base = roll_up(lines, params).should_cost_floor
    low = roll_up(_scale_commodity(lines, Decimal("1") - d), params).should_cost_floor
    high = roll_up(_scale_commodity(lines, Decimal("1") + d), params).should_cost_floor
    swing = max(abs(high - base), abs(base - low))
    swing_pct = float(swing / base) if base != 0 else 0.0
    return SensitivityResult(
        delta=delta, floor_low=low, floor_base=base, floor_high=high,
        swing_abs=_money(swing), swing_pct=swing_pct,
    )


# ---- commodity price lookup (spec §7: step function, as-of) ---------------

def index_multiplier_as_of(prices: list[tuple[date, Decimal]], baseline: Decimal,
                           as_of: date) -> Decimal:
    """``index_now / baseline`` using the most recent price on or before as_of.

    ``prices`` is a list of (date, value). Raises CostingError if as_of precedes
    the earliest point (cannot index before the series starts) or baseline <= 0.
    """
    if baseline <= 0:
        raise CostingError("commodity baseline must be > 0")
    eligible = sorted((d, v) for d, v in prices if d <= as_of)
    if not eligible:
        raise CostingError(f"no commodity price on or before {as_of.isoformat()}")
    _, value = eligible[-1]  # most recent on/before as_of — step function
    return value / baseline
