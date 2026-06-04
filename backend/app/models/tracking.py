"""Logistics / control-tower schema — separate from the core /api/v1 domain.

This mirrors the supplied scm_tracking_seed.sql (suppliers · purchase_orders ·
shipments · shipment_events) but lives in its own tables, prefixed ``trk_`` so
it never collides with the procurement domain (which has its own purchase_order
and organization). It is deliberately a thin, read-mostly logistics model: the
control-tower screen reads a rolled-up "where is it now" view plus the scan-by-
scan event trail.

Portable across SQLite (dev) and Postgres (prod): plain columns, no DB-specific
generated columns or views — the rollup/derivations are computed in the service
layer instead.
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Optional

from sqlalchemy import Boolean, Date, DateTime, Float, ForeignKey, Integer, Numeric, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base


class TrkSupplier(Base):
    __tablename__ = "trk_supplier"
    supplier_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    name: Mapped[str] = mapped_column(String(255))
    country: Mapped[str] = mapped_column(String(2))
    tier: Mapped[Optional[int]] = mapped_column(Integer)
    payment_terms: Mapped[Optional[str]] = mapped_column(String(16))
    currency: Mapped[str] = mapped_column(String(3), default="EUR")


class TrkPurchaseOrder(Base):
    __tablename__ = "trk_purchase_order"
    po_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    supplier_id: Mapped[str] = mapped_column(ForeignKey("trk_supplier.supplier_id"), index=True)
    order_date: Mapped[Optional[date]] = mapped_column(Date)
    expected_delivery: Mapped[Optional[date]] = mapped_column(Date)
    currency: Mapped[str] = mapped_column(String(3), default="EUR")
    total_value: Mapped[float] = mapped_column(Numeric(14, 2), default=0)
    status: Mapped[str] = mapped_column(String(16), default="open")

    supplier = relationship("TrkSupplier")


class Shipment(Base):
    __tablename__ = "trk_shipment"
    shipment_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    po_id: Mapped[str] = mapped_column(ForeignKey("trk_purchase_order.po_id"), index=True)
    mode: Mapped[str] = mapped_column(String(8))   # road/ocean/air/rail
    carrier: Mapped[Optional[str]] = mapped_column(String(64))
    current_status: Mapped[str] = mapped_column(String(24))
    progress_idx: Mapped[int] = mapped_column(Integer, default=0)
    current_location: Mapped[Optional[str]] = mapped_column(String(128))
    current_lat: Mapped[Optional[float]] = mapped_column(Float)
    current_lng: Mapped[Optional[float]] = mapped_column(Float)
    last_event_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    eta_original: Mapped[Optional[date]] = mapped_column(Date)
    eta_current: Mapped[Optional[date]] = mapped_column(Date)
    exception_flag: Mapped[bool] = mapped_column(Boolean, default=False)
    exception_reason: Mapped[Optional[str]] = mapped_column(String(255))

    po = relationship("TrkPurchaseOrder")
    events: Mapped[list["ShipmentEvent"]] = relationship(
        back_populates="shipment", cascade="all, delete-orphan",
        order_by="ShipmentEvent.seq",
    )


class ShipmentEvent(Base):
    __tablename__ = "trk_shipment_event"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    shipment_id: Mapped[str] = mapped_column(ForeignKey("trk_shipment.shipment_id"), index=True)
    seq: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(24))
    location_name: Mapped[str] = mapped_column(String(128))
    lat: Mapped[Optional[float]] = mapped_column(Float)
    lng: Mapped[Optional[float]] = mapped_column(Float)
    event_ts: Mapped[Optional[datetime]] = mapped_column(DateTime)
    notes: Mapped[Optional[str]] = mapped_column(String(255))

    shipment = relationship("Shipment", back_populates="events")
