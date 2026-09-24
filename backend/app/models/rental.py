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
    # over these pairs. Mirrored by migration f6a8b0c2d357. The two product pairs are
    # the device TCO's months in service per model, read from the index alone; mirrored
    # by migration c9d1e3f5a680.
    __table_args__ = (
        Index("ix_rental_status_planned_end", "status", "planned_end"),
        Index("ix_rental_status_actual_end", "status", "actual_end"),
        Index("ix_rental_cycle_start", "cycle_no", "start_date"),
        Index("ix_rental_start_actual_end", "start_date", "actual_end"),
        Index("ix_rental_product_cycle_start", "product_id", "cycle_no", "start_date"),
        Index("ix_rental_product_cycle_end", "product_id", "cycle_no", "actual_end", "end_reason"),
        # A device's rental history, read from the index alone. Leads with asset_id, so it is
        # also the lookup index for the foreign key (the single-column one it replaced served
        # nothing this does not); the rest is what the device TCO sums when it joins the
        # 31,200 finished devices to their contracts.
        Index("ix_rental_asset_life", "asset_id", "cycle_no", "start_date", "actual_end", "end_reason"),
        # The capacity plan's mean rental term, by cycle, from the index alone. The status
        # index gave the running contracts' ids and reading the term behind each of the
        # 300,000 took 3.4 seconds cold on the full fleet; this makes it six rows.
        # Mirrored by migration d2e4f6a8b0c1.
        Index("ix_rental_status_cycle_term", "status", "cycle_no", "term_months"),
    )

    asset_id: Mapped[str] = mapped_column(ForeignKey("asset.id"))
    # The model of the rented device, copied from the asset when the contract is written.
    # A device never changes model, so the copy cannot drift; what it buys is that every
    # per-model question over the contracts (months in service, defects, swaps) is answered
    # from an index instead of probing 500,000 asset rows one by one, which took two
    # seconds on the full fleet. Nullable: a contract written by an older path has none,
    # and the readers fall back to the join for those rows.
    product_id: Mapped[Optional[str]] = mapped_column(ForeignKey("product.id"), index=True)
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
    product = relationship("Product")
    customer = relationship("Organization")
