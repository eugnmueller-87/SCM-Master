"""Flow + lifecycle: receiving into the transit warehouse, then the one thing
OpenBoxes never modelled — the continuous identity of a unit from received
stock all the way to a decommissioned asset in a datacenter rack.

The Asset is the spine of the system. When a Receipt records arrival of a
serialised unit, an Asset is born (status RECEIVED). The SAME Asset row then
moves through the warehouse and into a rack — its location and status change,
but its identity (and link back to the PurchaseOrder line it came from) never
breaks. That single thread is what makes this different from both a warehouse
app and a plain CMDB.
"""
from __future__ import annotations

import enum
from datetime import date
from typing import Optional

from sqlalchemy import Date, ForeignKey, Integer, String, Text
from sqlalchemy import Enum as SAEnum
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base, IdMixin, TimestampMixin


class LocationType(str, enum.Enum):
    WAREHOUSE = "WAREHOUSE"   # the small, fast-turning transit warehouse
    DATACENTER = "DATACENTER"  # final home of an in-service asset
    RACK = "RACK"             # a position within a datacenter
    SUPPLIER = "SUPPLIER"     # external origin
    DISPOSAL = "DISPOSAL"     # decommission / RMA destination


class Location(IdMixin, TimestampMixin, Base):
    """A place. Self-referential so a RACK can nest under a DATACENTER, and the
    transit WAREHOUSE is just another location. ``capacity`` is intentionally
    nullable: the real limits aren't known yet, so it starts as an
    unknown the system will measure and the user will tune later."""

    __tablename__ = "location"

    code: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(255))
    location_type: Mapped[LocationType] = mapped_column(SAEnum(LocationType))
    parent_id: Mapped[Optional[str]] = mapped_column(ForeignKey("location.id"))
    # Tunable, unknown-today capacity knob (e.g. rack slots, warehouse floor units).
    capacity: Mapped[Optional[int]] = mapped_column(Integer)

    parent = relationship("Location", remote_side="Location.id")


class Receipt(IdMixin, TimestampMixin, Base):
    """An inbound receiving event against a PurchaseOrder."""

    __tablename__ = "receipt"

    purchase_order_id: Mapped[str] = mapped_column(ForeignKey("purchase_order.id"), index=True)
    received_at_id: Mapped[str] = mapped_column(ForeignKey("location.id"))
    receipt_date: Mapped[Optional[date]] = mapped_column(Date)

    purchase_order = relationship("PurchaseOrder")
    received_at = relationship("Location")
    items: Mapped[list["ReceiptItem"]] = relationship(
        back_populates="receipt",
        cascade="all, delete-orphan",
    )


class ReceiptItem(IdMixin, TimestampMixin, Base):
    __tablename__ = "receipt_item"

    receipt_id: Mapped[str] = mapped_column(ForeignKey("receipt.id"), index=True)
    # Indexed: every received-quantity rollup (planning + receiving guard) sums
    # ReceiptItem filtered by order_item_id, so this is a hot join/filter key.
    order_item_id: Mapped[str] = mapped_column(ForeignKey("order_item.id"), index=True)
    quantity_received: Mapped[int] = mapped_column(Integer)

    receipt: Mapped["Receipt"] = relationship(back_populates="items")
    order_item = relationship("OrderItem")


class AssetStatus(str, enum.Enum):
    RECEIVED = "RECEIVED"        # arrived at warehouse, on the floor
    IN_STORAGE = "IN_STORAGE"    # staged in transit warehouse
    DEPLOYED = "DEPLOYED"        # installed, in-service in a rack
    MAINTENANCE = "MAINTENANCE"
    DECOMMISSIONED = "DECOMMISSIONED"
    DISPOSED = "DISPOSED"


class Asset(IdMixin, TimestampMixin, Base):
    """A single, serial-tracked physical unit, followed for its whole life.

    Born at receipt; lives through the warehouse and into a rack; dies at
    decommission. Its link to the originating order line is never lost, so spend
    and provenance trace end-to-end.
    """

    __tablename__ = "asset"

    serial_number: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    product_id: Mapped[str] = mapped_column(ForeignKey("product.id"), index=True)
    # Indexed: on-hand/deployed capacity counts and the /assets?status= filter all
    # narrow by status, so it's a hot filter key at asset scale.
    status: Mapped[AssetStatus] = mapped_column(SAEnum(AssetStatus), default=AssetStatus.RECEIVED, index=True)

    # Current physical location (warehouse early in life, a rack once deployed).
    # Indexed: capacity group-bys and the location filter join on this.
    current_location_id: Mapped[Optional[str]] = mapped_column(ForeignKey("location.id"), index=True)
    # Provenance: which buy this unit came from. Never broken.
    # Indexed: the spend-analytics join and provenance lookup key on this.
    source_order_item_id: Mapped[Optional[str]] = mapped_column(ForeignKey("order_item.id"), index=True)

    received_date: Mapped[Optional[date]] = mapped_column(Date)
    deployed_date: Mapped[Optional[date]] = mapped_column(Date)
    warranty_end_date: Mapped[Optional[date]] = mapped_column(Date)
    decommissioned_date: Mapped[Optional[date]] = mapped_column(Date)
    notes: Mapped[Optional[str]] = mapped_column(Text)

    product = relationship("Product")
    current_location = relationship("Location")
    source_order_item = relationship("OrderItem")
    events: Mapped[list["AssetEvent"]] = relationship(
        back_populates="asset",
        cascade="all, delete-orphan",
        order_by="AssetEvent.date_created",
    )


class AssetEventType(str, enum.Enum):
    RECEIVED = "RECEIVED"        # asset born at receipt
    MOVED = "MOVED"             # relocated between locations
    STATUS_CHANGED = "STATUS_CHANGED"  # lifecycle status transition


class AssetEvent(IdMixin, TimestampMixin, Base):
    """Append-only history of everything that happens to an Asset.

    Current state lives on ``Asset`` (status + current_location); this table is
    the *trail* of how it got there — every status transition and every move,
    each capturing the from/to values and an optional actor. Nothing here is
    ever updated or deleted in normal operation: it is the audit spine that
    makes an asset's whole life reconstructable.
    """

    __tablename__ = "asset_event"

    asset_id: Mapped[str] = mapped_column(ForeignKey("asset.id"), index=True)
    event_type: Mapped[AssetEventType] = mapped_column(SAEnum(AssetEventType))

    # Status transition (null for a pure move).
    from_status: Mapped[Optional[AssetStatus]] = mapped_column(SAEnum(AssetStatus))
    to_status: Mapped[Optional[AssetStatus]] = mapped_column(SAEnum(AssetStatus))

    # Location change (null for a pure status change).
    from_location_id: Mapped[Optional[str]] = mapped_column(ForeignKey("location.id"))
    to_location_id: Mapped[Optional[str]] = mapped_column(ForeignKey("location.id"))

    # Who triggered it (free text for now; becomes a user FK with auth in Phase 5).
    actor: Mapped[Optional[str]] = mapped_column(String(128))
    note: Mapped[Optional[str]] = mapped_column(Text)

    asset: Mapped["Asset"] = relationship(back_populates="events")
    from_location = relationship("Location", foreign_keys=[from_location_id])
    to_location = relationship("Location", foreign_keys=[to_location_id])
