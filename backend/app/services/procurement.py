"""Procurement service: PurchaseOrder (+ nested OrderItems).

Creating an order is a single atomic operation: validate the supplier, optional
destination, and every line's product/source, then build the order and its
lines together. A new order always starts PENDING — status advances through
dedicated transitions in Phase 3, never by a raw client write.
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.catalog import Organization, Product, ProductSupplier
from app.models.flow import Location
from app.models.procurement import OrderItem, OrderStatus, PurchaseOrder
from app.services.crud import CRUDService
from app.services.exceptions import ConflictError, NotFoundError, ValidationError

# Manual order-status transitions a user can drive directly. Receiving advances
# PLACED -> PARTIALLY_RECEIVED -> RECEIVED automatically (see AssetService), so
# those are not listed here. CANCELLED is reachable from any pre-receipt state.
_ORDER_TRANSITIONS: dict[OrderStatus, frozenset[OrderStatus]] = {
    OrderStatus.PENDING: frozenset({OrderStatus.APPROVED, OrderStatus.CANCELLED}),
    OrderStatus.APPROVED: frozenset({OrderStatus.PLACED, OrderStatus.CANCELLED}),
    OrderStatus.PLACED: frozenset({OrderStatus.CANCELLED}),
    OrderStatus.PARTIALLY_RECEIVED: frozenset(),
    OrderStatus.RECEIVED: frozenset(),
    OrderStatus.CANCELLED: frozenset(),
}


class PurchaseOrderService(CRUDService[PurchaseOrder]):
    def __init__(self):
        super().__init__(PurchaseOrder)

    def create(self, db: Session, data: dict) -> PurchaseOrder:
        order_number = data["order_number"]
        if db.scalar(select(PurchaseOrder).where(PurchaseOrder.order_number == order_number)):
            raise ConflictError(f"Order number {order_number!r} already exists")

        supplier = db.get(Organization, data["supplier_id"])
        if supplier is None:
            raise NotFoundError(f"Organization {data['supplier_id']!r} not found")
        if not supplier.is_supplier:
            raise ValidationError(f"Organization {supplier.name!r} is not a supplier")

        destination_id = data.get("destination_id")
        if destination_id and db.get(Location, destination_id) is None:
            raise NotFoundError(f"Location {destination_id!r} not found")

        items_data = data.pop("items", []) or []

        order = PurchaseOrder(**data, status=OrderStatus.PENDING)
        db.add(order)
        db.flush()  # assign order.id before attaching lines

        for item in items_data:
            self._validate_line(db, item)
            db.add(OrderItem(order_id=order.id, **item))

        db.flush()
        db.refresh(order)
        return order

    def _validate_line(self, db: Session, item: dict) -> None:
        if db.get(Product, item["product_id"]) is None:
            raise NotFoundError(f"Product {item['product_id']!r} not found")

        ps_id = item.get("product_supplier_id")
        if ps_id:
            ps = db.get(ProductSupplier, ps_id)
            if ps is None:
                raise NotFoundError(f"ProductSupplier {ps_id!r} not found")
            # The chosen source must actually be a source for this product.
            if ps.product_id != item["product_id"]:
                raise ValidationError(
                    "Chosen source is not a supplier of the line's product"
                )

    # --- status transitions (approval flow) ------------------------------

    def set_status(self, db: Session, order_id: str, target: OrderStatus) -> PurchaseOrder:
        """Drive an order through PENDING -> APPROVED -> PLACED (or CANCELLED).

        Receipt-driven statuses (PARTIALLY_RECEIVED / RECEIVED) are owned by the
        receiving flow and cannot be set here.
        """
        order = self.get_or_404(db, order_id)
        if target in (OrderStatus.PARTIALLY_RECEIVED, OrderStatus.RECEIVED):
            raise ValidationError(f"{target.value} is set by receiving, not directly")
        allowed = _ORDER_TRANSITIONS.get(order.status, frozenset())
        if target not in allowed:
            names = ", ".join(s.value for s in allowed) or "none"
            raise ValidationError(
                f"Illegal order transition {order.status.value} -> {target.value} "
                f"(allowed: {names})"
            )
        order.status = target
        db.flush()
        return order

    # --- integration sync (upstream ERP/P2P -> us) -----------------------

    def upsert_by_external_ref(
        self,
        db: Session,
        *,
        source_system: str,
        external_ref: str,
        header: dict,
        items: list[dict],
    ) -> tuple[PurchaseOrder, bool]:
        """Create or update a PO synced from an upstream P2P/ERP system.

        Keyed on (source_system, external_ref) — the upstream's own PO number.
        On first sync the header + lines are created; on re-sync the header
        fields are updated and the lines are fully replaced (the upstream system
        remains the source of truth for the PO's contents). The supplier and
        every line's product must already exist (resolve them via *their* own
        external refs before calling this). Returns (order, created).

        A received/cancelled order is treated as immutable: its lines are NOT
        replaced on re-sync, only header metadata is refreshed — we never rewrite
        a PO that goods or invoices have already matched against.
        """
        supplier_id = header["supplier_id"]
        supplier = db.get(Organization, supplier_id)
        if supplier is None:
            raise NotFoundError(f"Organization {supplier_id!r} not found")
        if not supplier.is_supplier:
            raise ValidationError(f"Organization {supplier.name!r} is not a supplier")

        existing = self.get_by_external_ref(
            db, source_system=source_system, external_ref=external_ref
        )

        if existing is None:
            order = PurchaseOrder(
                source_system=source_system,
                external_ref=external_ref,
                status=OrderStatus.PENDING,
                **header,
            )
            db.add(order)
            db.flush()
            for item in items:
                self._validate_line(db, item)
                db.add(OrderItem(order_id=order.id, **item))
            db.flush()
            db.refresh(order)
            return order, True

        # Re-sync: refresh header fields regardless.
        for field, value in header.items():
            setattr(existing, field, value)

        # Replace lines only while the PO is still open (pre-receipt). Once it is
        # PARTIALLY_RECEIVED/RECEIVED/CANCELLED, its lines are matched against
        # receipts/invoices and must not be rewritten.
        if existing.status in (OrderStatus.PENDING, OrderStatus.APPROVED, OrderStatus.PLACED):
            existing.items.clear()
            db.flush()
            for item in items:
                self._validate_line(db, item)
                db.add(OrderItem(order_id=existing.id, **item))
        db.flush()
        db.refresh(existing)
        return existing, False

    # --- supplier swap (re-sourcing a line) ------------------------------

    def resource_line(self, db: Session, order_id: str, order_item_id: str,
                      product_supplier_id: str) -> OrderItem:
        """Repoint an order line to a different source of the SAME product.

        This is the supplier-swap that multi-sourcing exists for: pick another
        ProductSupplier and the line follows, keeping the product identity. Only
        allowed before the order is placed/received — re-sourcing a line that is
        already in-flight would be meaningless.
        """
        order = self.get_or_404(db, order_id)
        if order.status not in (OrderStatus.PENDING, OrderStatus.APPROVED):
            raise ValidationError(
                f"Cannot re-source a line on a {order.status.value} order"
            )
        line = db.get(OrderItem, order_item_id)
        if line is None or line.order_id != order.id:
            raise NotFoundError(f"OrderItem {order_item_id!r} not on this order")

        ps = db.get(ProductSupplier, product_supplier_id)
        if ps is None:
            raise NotFoundError(f"ProductSupplier {product_supplier_id!r} not found")
        if ps.product_id != line.product_id:
            raise ValidationError("New source is not a supplier of the line's product")

        line.product_supplier_id = ps.id
        # Re-price the line from the new source's contract price when it has one.
        if ps.contract_price is not None:
            line.unit_price = ps.contract_price
        db.flush()
        return line


purchase_order_service = PurchaseOrderService()
