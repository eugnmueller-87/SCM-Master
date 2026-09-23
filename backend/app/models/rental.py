"""Rental contracts: the DaaS scenario's reason a device leaves the warehouse.

One row per rental of one serial. A device on its second rental has two rows
(cycle_no 1 ended, cycle_no 2 running). The planned end is what the return
calendar is built from; the actual end and its reason are what the fleet
learns from (early returns, defects, swaps).
"""
from __future__ import annotations

import enum
from datetime import date
from typing import Optional

from sqlalchemy import Date, ForeignKey, Index, Integer, Numeric, String
from sqlalchemy import Enum as SAEnum
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base, IdMixin, TimestampMixin


class ContractStatus(str, enum.Enum):
    RUNNING = "RUNNING"
    ENDED = "ENDED"


class RentalContract(IdMixin, TimestampMixin, Base):
    __tablename__ = "rental_contract"
    # The return calendar, the due-in-N-days counts and the growth curve are range scans
    # over these pairs. Mirrored by migration f6a8b0c2d357.
    __table_args__ = (
        Index("ix_rental_status_planned_end", "status", "planned_end"),
        Index("ix_rental_status_actual_end", "status", "actual_end"),
        Index("ix_rental_cycle_start", "cycle_no", "start_date"),
        Index("ix_rental_start_actual_end", "start_date", "actual_end"),
    )

    asset_id: Mapped[str] = mapped_column(ForeignKey("asset.id"), index=True)
    customer_id: Mapped[str] = mapped_column(ForeignKey("organization.id"), index=True)
    cycle_no: Mapped[int] = mapped_column(Integer)                     # 1 first rental, 2 second rental
    start_date: Mapped[date] = mapped_column(Date, index=True)
    term_months: Mapped[int] = mapped_column(Integer)
    planned_end: Mapped[date] = mapped_column(Date, index=True)        # the return calendar reads this
    actual_end: Mapped[Optional[date]] = mapped_column(Date)
    end_reason: Mapped[Optional[str]] = mapped_column(String(24))      # planned | early | defect | swap
    rent_eur_month: Mapped[Optional[float]] = mapped_column(Numeric(14, 2))
    status: Mapped[ContractStatus] = mapped_column(SAEnum(ContractStatus), default=ContractStatus.RUNNING, index=True)

    asset = relationship("Asset")
    customer = relationship("Organization")
