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

from sqlalchemy import Date, Float, ForeignKey, Index, Integer, Numeric, String, Text
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
    # Who decided the capacity. Empty means the number is still the design parameter the
    # seed wrote, and the capacity plan shows it as assumed; a name means a person set it.
    # "How much room do we assume" is the owner's question, so the answer has to say which.
    capacity_set_by: Mapped[Optional[str]] = mapped_column(String(128))

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
    # --- the original datacenter flow (kept: the tests and the datacenter scenario use it)
    RECEIVED = "RECEIVED"        # arrived at warehouse, on the floor
    IN_STORAGE = "IN_STORAGE"    # staged in transit warehouse; in the DaaS scenario: a new device before its first rental
    DEPLOYED = "DEPLOYED"        # installed, in-service in a rack
    MAINTENANCE = "MAINTENANCE"
    DECOMMISSIONED = "DECOMMISSIONED"
    DISPOSED = "DISPOSED"
    # --- the Device-as-a-Service cycle: buy, rent, take back, refurbish, rent again, sell
    RENTED = "RENTED"            # at a customer under a rental contract (cycle_no says which rental)
    RETURNED = "RETURNED"        # back in the warehouse: receipt and lock
    MDM_RELEASE = "MDM_RELEASE"  # waiting for the old customer to release the device from its MDM
    WIPE_GRADING = "WIPE_GRADING"  # certified wipe, function test, grade A to D
    REPAIR = "REPAIR"            # grade C or a defect: at the repair partner
    REFURB = "REFURB"            # refurbishment for the next rental: the process, not the stock
    READY_SECOND = "READY_SECOND"  # refurbished, grade A or B, waiting for its second customer: the second-life stock
    SELLABLE = "SELLABLE"        # graded and cleared for resale, waiting for a channel
    SWAP_BUFFER = "SWAP_BUFFER"  # replacement device held ready for a customer defect
    SOLD = "SOLD"                # resold; terminal
    RECYCLED = "RECYCLED"        # recycled; terminal


# The warehouse in the DaaS scenario: every status that means "the device is physically with us".
WAREHOUSE_STATUSES = frozenset({
    AssetStatus.RECEIVED, AssetStatus.IN_STORAGE, AssetStatus.RETURNED, AssetStatus.MDM_RELEASE,
    AssetStatus.WIPE_GRADING, AssetStatus.REPAIR, AssetStatus.REFURB, AssetStatus.READY_SECOND, AssetStatus.SELLABLE,
    AssetStatus.SWAP_BUFFER,
})
# At a customer: the rack in the datacenter scenario, the rental in the DaaS scenario.
IN_USE_STATUSES = frozenset({AssetStatus.DEPLOYED, AssetStatus.RENTED, AssetStatus.MAINTENANCE})
# Stock that can go out next: new units, and refurbished units cleared for the next rental.
# A unit still in refurbishment cannot go out next, and sellable stock and the swap buffer
# are reserved for something else, so none of those count.
DEPLOYABLE_STATUSES = frozenset({AssetStatus.RECEIVED, AssetStatus.IN_STORAGE, AssetStatus.READY_SECOND})
GONE_STATUSES = frozenset({AssetStatus.DECOMMISSIONED, AssetStatus.DISPOSED, AssetStatus.SOLD, AssetStatus.RECYCLED})


class Asset(IdMixin, TimestampMixin, Base):
    """A single, serial-tracked physical unit, followed for its whole life.

    Born at receipt; lives through the warehouse and into a rack; dies at
    decommission. Its link to the originating order line is never lost, so spend
    and provenance trace end-to-end.
    """

    __tablename__ = "asset"
    # Composite indexes for a fleet of 400,000: every screen asks how many devices are
    # in a station and how long they have been there, or what a product deployed
    # recently. Single-column indexes left those queries touching the whole table.
    # Mirrored by migration f6a8b0c2d357 for databases that already exist.
    __table_args__ = (
        Index("ix_asset_status_status_since", "status", "status_since"),
        Index("ix_asset_product_deployed", "product_id", "deployed_date"),
        Index("ix_asset_deployed_date", "deployed_date"),
        Index("ix_asset_sold_date", "sold_date"),
        Index("ix_asset_status_product", "status", "product_id"),
        Index("ix_asset_status_cycle", "status", "cycle_no"),
        Index("ix_asset_id_product", "id", "product_id"),
        # The device TCO (migration c9d1e3f5a680). The id is a text key that no index carried next
        # to status, so a join driven from a status set (the 31,200 finished lives into their
        # contracts and service events) fetched the row behind every device just for its id; the
        # first index makes that side index-only. The second makes the dwell and resale reads by
        # order line index-only, sale price included: 100,000 rows on hand answered from the index.
        Index("ix_asset_status_id_product", "status", "id", "product_id"),
        Index("ix_asset_status_line_since_sale", "status", "source_order_item_id", "status_since", "sale_price"),
    )

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

    # --- DaaS scenario fields (nullable; the datacenter flow leaves them empty)
    cycle_no: Mapped[int] = mapped_column(Integer, default=0)                 # 0 never rented, 1 first rental, 2 second rental
    grade: Mapped[Optional[str]] = mapped_column(String(1))                    # A B C D, set at wipe and grading
    battery_health: Mapped[Optional[float]] = mapped_column(Float)             # 0..1, read at grading
    customer_id: Mapped[Optional[str]] = mapped_column(ForeignKey("organization.id"), index=True)   # while RENTED
    status_since: Mapped[Optional[date]] = mapped_column(Date)                 # when the current status began (dwell per station)
    sold_date: Mapped[Optional[date]] = mapped_column(Date)
    sale_price: Mapped[Optional[float]] = mapped_column(Numeric(14, 2))        # net proceeds after channel fee
    sale_channel: Mapped[Optional[str]] = mapped_column(String(32))            # marketplace | b2b_wholesale | employee_buyout | as_is

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
