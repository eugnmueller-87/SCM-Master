"""Total Cost of Ownership (TCO) layers — the cost of an asset beyond its price.

Acquisition (what we actually paid) is NOT stored here; it is read from the
existing provenance chain (asset → source_order_item → OrderItem.unit_price).
These tables capture the *additional* lifetime cost layers that stack on top of
acquisition:

    TCO = acquisition + Σlanded + Σdeployment + Σopex + Σeol − recovery_value

Two scenarios share this module. The five layer tables above belong to the
datacenter operation (power, cooling, racking). The device-as-a-service fleet
stores only one cost fact per device, the ``ServiceEvent`` at the end of the
file: what a repair or a refurbishment cost. Every other layer of the device TCO
is a measured quantity (a price on the order line, months on the contracts, days
in a compartment, proceeds of a sale) times a rate, and is computed, not stored;
see ``services/tco_device.py``.

Design (agreed in Phase 0):
  - One table per layer, FK → asset.id (String(36) UUID), repo conventions
    (IdMixin/TimestampMixin, Numeric money, SAEnum). The per-asset roll-up is a
    SERVICE (Phase 2), not a table/view — so the money math stays Decimal-exact
    and unit-testable.
  - landed_cost / deployment_cost are MULTI-ROW (multiple freight legs, staged
    labour) — no one-row-per-type constraint. The TCO service sums per asset.
  - currency is recorded as an attribute (DC hardware is often USD-invoiced) but
    amounts are assumed already in EUR for the sum; the service fails loud on a
    non-EUR row rather than silently mixing (Phase 2). Default EUR everywhere.
  - Acquisition is anchored on actual-paid; the should-cost target stays a
    read-only comparison, surfaced as a derived variance in the service.
"""
from __future__ import annotations

import enum
from datetime import date
from typing import Optional

from sqlalchemy import Date, ForeignKey, Index, Integer, Numeric, String
from sqlalchemy import Enum as SAEnum
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base, IdMixin, TimestampMixin


class LandedCostType(str, enum.Enum):
    FREIGHT = "FREIGHT"
    INSURANCE = "INSURANCE"
    DUTY = "DUTY"          # tariff-scenario component — excludable at query time
    HANDLING = "HANDLING"


class LandedCost(IdMixin, TimestampMixin, Base):
    """A landed-cost component for getting an asset to the dock/warehouse.

    Multi-row: an asset can have several (two freight legs, separate insurance +
    duty + handling). ``cost_type`` (not ``type`` — that shadows a builtin)
    drives the optional query-time exclusion (e.g. drop DUTY for a tariff view).
    """

    __tablename__ = "landed_cost"

    asset_id: Mapped[str] = mapped_column(ForeignKey("asset.id"), index=True)
    cost_type: Mapped[LandedCostType] = mapped_column(SAEnum(LandedCostType))
    amount: Mapped[float] = mapped_column(Numeric(14, 2))
    currency: Mapped[str] = mapped_column(String(3), default="EUR")
    incoterm: Mapped[Optional[str]] = mapped_column(String(8))  # EXW/FOB/DDP/…
    incurred_date: Mapped[Optional[date]] = mapped_column(Date)

    asset = relationship("Asset")


class DeploymentTask(str, enum.Enum):
    RECEIVING = "RECEIVING"
    RACKING = "RACKING"
    CABLING = "CABLING"
    IMAGING = "IMAGING"


class DeploymentCost(IdMixin, TimestampMixin, Base):
    """A labour line for bringing an asset into service.

    Multi-row: staged labour (receiving now, racking + cabling + imaging later)
    each as its own line. ``amount`` is stored (not derived) so a manual
    override or a fixed-fee line is possible; the generator sets amount =
    labor_hours × rate.
    """

    __tablename__ = "deployment_cost"

    asset_id: Mapped[str] = mapped_column(ForeignKey("asset.id"), index=True)
    task: Mapped[DeploymentTask] = mapped_column(SAEnum(DeploymentTask))
    labor_hours: Mapped[Optional[float]] = mapped_column(Numeric(8, 2))
    rate: Mapped[Optional[float]] = mapped_column(Numeric(8, 2))
    amount: Mapped[float] = mapped_column(Numeric(14, 2))
    currency: Mapped[str] = mapped_column(String(3), default="EUR")
    incurred_date: Mapped[Optional[date]] = mapped_column(Date)

    asset = relationship("Asset")


class OpexLedger(IdMixin, TimestampMixin, Base):
    """One month of run-time operating cost for an in-service asset.

    A ~60-row time-series per asset (5y service life). Power cost is
    power_kwh × pue × energy_rate; cooling/maintenance/license are stored
    explicitly so the service just sums. ``period`` is the month-start date.
    """

    __tablename__ = "opex_ledger"

    asset_id: Mapped[str] = mapped_column(ForeignKey("asset.id"), index=True)
    period: Mapped[date] = mapped_column(Date, index=True)  # month-start
    power_kwh: Mapped[float] = mapped_column(Numeric(12, 2), default=0)
    pue: Mapped[float] = mapped_column(Numeric(5, 3), default=1)
    energy_rate: Mapped[float] = mapped_column(Numeric(8, 4), default=0)  # €/kWh
    cooling: Mapped[float] = mapped_column(Numeric(12, 2), default=0)
    maintenance: Mapped[float] = mapped_column(Numeric(12, 2), default=0)
    license: Mapped[float] = mapped_column(Numeric(12, 2), default=0)
    currency: Mapped[str] = mapped_column(String(3), default="EUR")

    asset = relationship("Asset")


class EolCost(IdMixin, TimestampMixin, Base):
    """End-of-life cost for an asset (one row per asset)."""

    __tablename__ = "eol_cost"

    asset_id: Mapped[str] = mapped_column(ForeignKey("asset.id"), unique=True, index=True)
    decommission: Mapped[float] = mapped_column(Numeric(12, 2), default=0)
    data_destruction: Mapped[float] = mapped_column(Numeric(12, 2), default=0)
    weee: Mapped[float] = mapped_column(Numeric(12, 2), default=0)  # WEEE recycling
    itad_fee: Mapped[float] = mapped_column(Numeric(12, 2), default=0)  # IT asset disposition
    currency: Mapped[str] = mapped_column(String(3), default="EUR")
    eol_date: Mapped[Optional[date]] = mapped_column(Date)

    asset = relationship("Asset")


class DepreciationMethod(str, enum.Enum):
    STRAIGHT_LINE = "STRAIGHT_LINE"
    DECLINING = "DECLINING"
    NONE = "NONE"


class RecoveryValue(IdMixin, TimestampMixin, Base):
    """Residual / resale recovery for an asset (one row per asset).

    Subtracts in the TCO sum — money back, not money out.
    """

    __tablename__ = "recovery_value"

    asset_id: Mapped[str] = mapped_column(ForeignKey("asset.id"), unique=True, index=True)
    residual_value: Mapped[float] = mapped_column(Numeric(14, 2), default=0)
    resale_channel: Mapped[Optional[str]] = mapped_column(String(64))
    depr_method: Mapped[DepreciationMethod] = mapped_column(
        SAEnum(DepreciationMethod), default=DepreciationMethod.STRAIGHT_LINE
    )
    currency: Mapped[str] = mapped_column(String(3), default="EUR")
    recovery_date: Mapped[Optional[date]] = mapped_column(Date)

    asset = relationship("Asset")


class ServiceKind(str, enum.Enum):
    REPAIR = "REPAIR"    # grade C or a defect: the repair partner's invoice
    REFURB = "REFURB"    # made ready for the next rental: the refurbishment partner's invoice


class ServiceEvent(IdMixin, TimestampMixin, Base):
    """One repair or refurbishment of one device, at the cost the partner invoiced.

    The device-as-a-service scenario's only stored cost layer. It exists because the
    fleet records where a device is, not what was done to it: a device on its second
    rental says nothing about the refurbishment that got it there, and the cost of that
    refurbishment is not a rate times a count, it is an invoice per device. One row per
    event, written when the partner's invoice is known; a device still on the bench has
    no row yet and is reported as in progress.

    ``product_id`` is copied from the asset for the same reason as on the rental
    contract: the TCO groups these by model, and a device never changes model. Both
    composite indexes carry the cost so that a sum, per model or per device, is read
    from the index alone without touching a row per event; the one that leads with
    ``asset_id`` is also the foreign key's lookup index.
    """

    __tablename__ = "service_event"
    __table_args__ = (
        Index("ix_service_event_product_kind_cost", "product_id", "kind", "cost"),
        Index("ix_service_event_asset_kind_cost", "asset_id", "kind", "cost"),
    )

    asset_id: Mapped[str] = mapped_column(ForeignKey("asset.id"))
    product_id: Mapped[str] = mapped_column(ForeignKey("product.id"), index=True)
    kind: Mapped[ServiceKind] = mapped_column(SAEnum(ServiceKind))
    cycle_no: Mapped[int] = mapped_column(Integer)                 # the rental this event prepared the device for
    event_date: Mapped[date] = mapped_column(Date)
    cost: Mapped[float] = mapped_column(Numeric(12, 2))
    currency: Mapped[str] = mapped_column(String(3), default="EUR")

    asset = relationship("Asset")
    product = relationship("Product")
