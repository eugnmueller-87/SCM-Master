"""Unit tests for the pure should-cost engine (services/costing.py).

No DB — these exercise the formulas directly. Two of them reproduce the worked
examples in docs/should_cost_model.md to the cent, so the spec and the code
cannot silently drift apart.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from app.models.costing import CostingMethod
from app.services import costing as cc
from app.services.costing import CostingError, LineInput, Params

D = Decimal


def _teardown(label, qty, base, conv, ovh, mult):
    return LineInput(
        label=label, method=CostingMethod.teardown, qty=qty,
        base_material_cost=D(str(base)), conversion_cost=D(str(conv)),
        overhead_pct=D(str(ovh)), index_multiplier=D(str(mult)),
    )


def _ref(label, qty, lst, disc):
    return LineInput(
        label=label, method=CostingMethod.reference_price, qty=qty,
        list_price=D(str(lst)), discount_pct=D(str(disc)),
    )


# --- component build-up (§3) -----------------------------------------------

def test_teardown_component_floor():
    # 64GB DDR5 ×8, base 110, conv 6, ovh 12%, DRAM ×1.8  -> €1,822.08 (spec)
    line = _teardown("DDR5", 8, 110, 6, 0.12, 1.8)
    assert cc.component_floor(line) == D("1822.08")


def test_reference_price_component_floor():
    # CPU ×2, list 7120, discount 22% -> €11,107.20 (spec)
    assert cc.component_floor(_ref("CPU", 2, 7120, 0.22)) == D("11107.20")


def test_reference_price_zero_discount():
    # GPU band-bottom: list 6800, 0% -> €6,800.00 (spec secondary box)
    assert cc.component_floor(_ref("GPU", 1, 6800, 0.0)) == D("6800.00")


def test_baseline_multiplier_is_identity():
    # At baseline (mult 1.0) the floor == base build-up, no commodity inflation.
    line = _teardown("X", 1, 100, 10, 0.10, 1.0)
    assert cc.component_floor(line) == D("120.00")  # 100 + 10 + 10


# --- edge cases (§7) -------------------------------------------------------

def test_zero_qty_contributes_zero_but_line_survives():
    assert cc.component_floor(_teardown("X", 0, 100, 10, 0.1, 1.5)) == D("0.00")
    res = cc.roll_up([_teardown("X", 0, 100, 10, 0.1, 1.5)], Params())
    assert len(res.lines) == 1 and res.lines[0].component_floor == D("0.00")


def test_negative_qty_rejected():
    with pytest.raises(CostingError, match="qty"):
        cc.component_floor(_teardown("X", -1, 100, 0, 0, 1))


def test_teardown_missing_base_rejected():
    bad = LineInput(label="X", method=CostingMethod.teardown, qty=1)
    with pytest.raises(CostingError, match="base_material_cost"):
        cc.component_floor(bad)


def test_reference_missing_fields_rejected():
    bad = LineInput(label="X", method=CostingMethod.reference_price, qty=1, list_price=D("100"))
    with pytest.raises(CostingError, match="list_price|discount"):
        cc.component_floor(bad)


def test_discount_out_of_range_rejected():
    with pytest.raises(CostingError, match="discount_pct"):
        cc.component_floor(_ref("X", 1, 100, 1.5))


def test_nonpositive_multiplier_rejected():
    with pytest.raises(CostingError, match="index_multiplier"):
        cc.component_floor(_teardown("X", 1, 100, 0, 0, 0))


# --- as-of index lookup (§7 step function) ---------------------------------

def test_index_multiplier_step_function():
    prices = [(date(2026, 1, 1), D("1.0")), (date(2026, 3, 1), D("1.5")),
              (date(2026, 6, 1), D("1.8"))]
    # most recent on/before as_of, no interpolation
    assert cc.index_multiplier_as_of(prices, D("1.0"), date(2026, 4, 15)) == D("1.5")
    assert cc.index_multiplier_as_of(prices, D("1.0"), date(2026, 3, 1)) == D("1.5")


def test_index_as_of_before_series_rejected():
    prices = [(date(2026, 3, 1), D("1.5"))]
    with pytest.raises(CostingError, match="on or before"):
        cc.index_multiplier_as_of(prices, D("1.0"), date(2026, 1, 1))


def test_index_zero_baseline_rejected():
    with pytest.raises(CostingError, match="baseline"):
        cc.index_multiplier_as_of([(date(2026, 1, 1), D("1"))], D("0"), date(2026, 2, 1))


# --- roll-up + worked examples (§4, §8) ------------------------------------

def test_hero_storage_memory_config_matches_spec():
    """Hero example (§8a) — reproduce the floor/target to the cent."""
    lines = [
        _teardown("DDR5 ×24", 24, 110, 6, 0.12, 1.8),
        _teardown("NVMe ×24", 24, 90, 4, 0.10, 1.5),
        _teardown("chassis", 1, 180, 40, 0.10, 1.0),
        _teardown("PSU ×2", 2, 120, 18, 0.10, 1.05),
        _teardown("mobo", 1, 420, 60, 0.10, 1.0),
        _teardown("NIC", 1, 700, 40, 0.10, 1.0),
        _ref("CPU", 1, 7120, 0.22),
    ]
    res = cc.roll_up(lines, Params())
    assert res.material_total == D("16563.04")
    assert res.should_cost_floor == D("18961.37")
    assert res.target_price == D("20857.51")

    g = cc.gap(res, D("24650.00"), annual_volume=250)
    assert g.gap_to_target_abs == D("3792.49")
    assert round(g.gap_to_target_pct, 3) == 0.154
    assert g.addressable_saving == D("948122.50")
    assert g.gap_to_floor_abs == D("5688.63")


def test_secondary_gpu_config_matches_spec():
    """Secondary example (§8b) — silicon-dominated."""
    lines = [
        _teardown("DDR5 ×8", 8, 110, 6, 0.12, 1.8),
        _teardown("chassis", 1, 140, 30, 0.10, 1.0),
        _teardown("PSU ×2", 2, 120, 18, 0.10, 1.05),
        _teardown("NIC", 1, 700, 40, 0.10, 1.0),
        _ref("CPU ×2", 2, 7120, 0.22),
        _ref("GPU", 1, 6800, 0.0),
    ]
    res = cc.roll_up(lines, Params())
    assert res.should_cost_floor == D("24082.56")
    assert res.target_price == D("26490.82")


# --- gap edge cases (§5) ---------------------------------------------------

def test_gap_no_quote_keeps_floor_and_flags():
    res = cc.roll_up([_teardown("X", 1, 100, 0, 0, 1)], Params())
    g = cc.gap(res, None)
    assert g.has_quote is False
    assert g.gap_to_target_abs is None
    assert g.target_price == res.target_price  # floor/target still stand


def test_headline_is_vs_target_not_floor():
    res = cc.roll_up([_teardown("X", 1, 1000, 0, 0, 1)], Params())
    g = cc.gap(res, D("1500"))
    # gap_to_target must be smaller than gap_to_floor (target > floor)
    assert g.gap_to_target_abs < g.gap_to_floor_abs


# --- sensitivity (§6) ------------------------------------------------------

def test_sensitivity_moves_on_commodity_box():
    # Memory-heavy: a DRAM swing must move the floor materially.
    lines = [_teardown("DDR5 ×24", 24, 110, 6, 0.12, 1.8), _ref("CPU", 1, 7120, 0.22)]
    s = cc.sensitivity(lines, Params(), delta=0.2)
    assert s.floor_high > s.floor_base > s.floor_low
    assert s.swing_abs > 0


def test_sensitivity_flat_on_all_reference_box():
    # All silicon -> no commodity exposure -> zero swing (spec edge case).
    lines = [_ref("CPU", 2, 7120, 0.22), _ref("GPU", 1, 6800, 0.0)]
    s = cc.sensitivity(lines, Params(), delta=0.2)
    assert s.swing_abs == D("0.00")
    assert s.floor_low == s.floor_base == s.floor_high


def test_sensitivity_bad_delta_rejected():
    with pytest.raises(CostingError, match="delta"):
        cc.sensitivity([_ref("X", 1, 100, 0)], Params(), delta=1.5)
