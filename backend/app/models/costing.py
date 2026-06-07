"""Costing: the should-cost / clean-sheet teardown domain.

Decomposes a server config into component costs driven by commodity indices, to
produce a *defensible cost floor* to negotiate against — instead of accepting a
vendor quote at face value. The cost math lives in ``services/costing.py`` (pure
and deterministic); this module is only the persistence model.

The full model is specified in ``docs/should_cost_model.md`` — the 5-element
clean-sheet teardown, the teardown-vs-reference-price split, and the worked
examples. Nothing here should diverge from that spec without updating it first.

Entities:
    Commodity        a tracked index (DRAM_DDR5, NAND_TLC, STEEL_CR, …)
    CommodityPrice   a time-series point for a commodity (recompute as-of-date)
    ComponentClass   CPU/MEMORY/STORAGE/… → its commodity driver + costing method
    BOM              one decomposed config, per server Product
    BOMLine          a component in the config (qty, costs, commodity link)
    CostParams       tunable knobs per BOM (integration / SG&A / target margin)
    ShouldCostRun    a persisted computed estimate (breakdown JSON + totals)
"""
from __future__ import annotations

import enum
from datetime import date
from typing import Optional

from sqlalchemy import JSON, Date, ForeignKey, Integer, Numeric, String, Text
from sqlalchemy import Enum as SAEnum
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base, IdMixin, TimestampMixin


class CostingMethod(str, enum.Enum):
    """How a component class derives its should-cost.

    teardown        — index-driven material build-up (DRAM, NAND, metals, PCB).
    reference_price — vendor list × expected discount band (CPU, GPU). Silicon
                      doesn't track a public commodity, so this is a negotiated-
                      price benchmark, NOT a fabricated material teardown.
    """

    teardown = "teardown"
    reference_price = "reference_price"


class Commodity(IdMixin, TimestampMixin, Base):
    """A tracked commodity index (e.g. DRAM_DDR5, NAND_TLC, STEEL_CR, COPPER_LME).

    ``baseline_value`` is the index level the per-component ``base_material_cost``
    is quoted at; the as-of multiplier is ``price_now / baseline_value``.
    """

    __tablename__ = "commodity"

    code: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(128))
    unit: Mapped[str] = mapped_column(String(32), default="index")  # €/GB, index, …
    baseline_value: Mapped[float] = mapped_column(Numeric(14, 4), default=1)

    prices: Mapped[list["CommodityPrice"]] = relationship(
        back_populates="commodity",
        cascade="all, delete-orphan",
    )


class CommodityPrice(IdMixin, TimestampMixin, Base):
    """A single (date, value) point on a commodity's series.

    The engine reads the most recent point on or before the as-of date (a step
    function, not interpolation — see the spec's edge-case table)."""

    __tablename__ = "commodity_price"

    commodity_id: Mapped[str] = mapped_column(ForeignKey("commodity.id"), index=True)
    price_date: Mapped[date] = mapped_column(Date, index=True)
    value: Mapped[float] = mapped_column(Numeric(14, 4))

    commodity: Mapped["Commodity"] = relationship(back_populates="prices")


class ComponentClass(IdMixin, TimestampMixin, Base):
    """A kind of component (CPU, MEMORY, STORAGE, CHASSIS, PSU, NIC, MOTHERBOARD,
    GPU) → its costing method and (for teardown classes) its commodity driver."""

    __tablename__ = "component_class"

    code: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(128))
    method: Mapped[CostingMethod] = mapped_column(SAEnum(CostingMethod))
    # Required for teardown classes; null/ignored for reference_price classes.
    commodity_id: Mapped[Optional[str]] = mapped_column(ForeignKey("commodity.id"))

    commodity: Mapped[Optional["Commodity"]] = relationship()


class BOM(IdMixin, TimestampMixin, Base):
    """One decomposed configuration, attached to a server ``Product``."""

    __tablename__ = "bom"

    product_id: Mapped[str] = mapped_column(ForeignKey("product.id"), unique=True, index=True)
    notes: Mapped[Optional[str]] = mapped_column(Text)

    lines: Mapped[list["BOMLine"]] = relationship(
        back_populates="bom",
        cascade="all, delete-orphan",
    )
    params: Mapped[Optional["CostParams"]] = relationship(
        back_populates="bom",
        cascade="all, delete-orphan",
        uselist=False,
    )


class BOMLine(IdMixin, TimestampMixin, Base):
    """A component in a config.

    teardown lines use base_material_cost + conversion_cost + overhead_pct,
    indexed by the linked commodity. reference_price lines use list_price ×
    (1 − discount_pct). The line's ``component_class`` decides which.
    """

    __tablename__ = "bom_line"

    bom_id: Mapped[str] = mapped_column(ForeignKey("bom.id"), index=True)
    component_class_id: Mapped[str] = mapped_column(ForeignKey("component_class.id"), index=True)

    label: Mapped[str] = mapped_column(String(255))  # "64GB DDR5 RDIMM"
    qty: Mapped[int] = mapped_column(Integer, default=1)

    # teardown inputs
    base_material_cost: Mapped[Optional[float]] = mapped_column(Numeric(14, 4))
    conversion_cost: Mapped[Optional[float]] = mapped_column(Numeric(14, 4), default=0)
    overhead_pct: Mapped[Optional[float]] = mapped_column(Numeric(6, 4), default=0)

    # reference_price inputs (CPU/GPU)
    list_price: Mapped[Optional[float]] = mapped_column(Numeric(14, 4))
    discount_pct: Mapped[Optional[float]] = mapped_column(Numeric(6, 4))

    bom: Mapped["BOM"] = relationship(back_populates="lines")
    component_class: Mapped["ComponentClass"] = relationship()


class CostParams(IdMixin, TimestampMixin, Base):
    """Tunable roll-up knobs for a BOM (config roll-up §4 of the spec)."""

    __tablename__ = "cost_params"

    bom_id: Mapped[str] = mapped_column(ForeignKey("bom.id"), unique=True, index=True)
    integration_pct: Mapped[float] = mapped_column(Numeric(6, 4), default=0.06)
    sga_pct: Mapped[float] = mapped_column(Numeric(6, 4), default=0.08)
    target_margin_pct: Mapped[float] = mapped_column(Numeric(6, 4), default=0.10)

    bom: Mapped["BOM"] = relationship(back_populates="params")


class ShouldCostRun(IdMixin, TimestampMixin, Base):
    """A persisted computed estimate — so floors can be trended and compared to
    quotes over time. The full per-line breakdown is stored as JSON."""

    __tablename__ = "should_cost_run"

    product_id: Mapped[str] = mapped_column(ForeignKey("product.id"), index=True)
    as_of: Mapped[date] = mapped_column(Date)
    # The ProductSupplier whose contract price the floor was compared against —
    # ties the gap into the existing spend-provenance chain.
    product_supplier_id: Mapped[Optional[str]] = mapped_column(ForeignKey("product_supplier.id"))

    should_cost_floor: Mapped[float] = mapped_column(Numeric(14, 2))
    target_price: Mapped[float] = mapped_column(Numeric(14, 2))
    quoted_price: Mapped[Optional[float]] = mapped_column(Numeric(14, 2))
    breakdown: Mapped[dict] = mapped_column(JSON)  # full line-by-line detail
