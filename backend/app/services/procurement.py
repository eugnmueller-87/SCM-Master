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


purchase_order_service = PurchaseOrderService()
