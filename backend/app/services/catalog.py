"""Catalog services: Organization, Product, ProductSupplier.

Business rules enforced here (not in the DB or the routes):
  - unique business codes return a friendly 409 instead of an IntegrityError;
  - a ProductSupplier must point at a real Product and a real supplier Org, and
    that org must actually have the supplier role.
"""
from __future__ import annotations

from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.catalog import Organization, Product, ProductSupplier
from app.services.crud import CRUDService
from app.services.exceptions import ConflictError, NotFoundError, ValidationError


class OrganizationService(CRUDService[Organization]):
    def __init__(self):
        super().__init__(Organization)

    def create(self, db: Session, data: dict) -> Organization:
        code = data.get("code")
        if code and db.scalar(select(Organization).where(Organization.code == code)):
            raise ConflictError(f"Organization code {code!r} already exists")
        return super().create(db, data)


class ProductService(CRUDService[Product]):
    def __init__(self):
        super().__init__(Product)

    def create(self, db: Session, data: dict) -> Product:
        code = data["product_code"]
        if db.scalar(select(Product).where(Product.product_code == code)):
            raise ConflictError(f"Product code {code!r} already exists")
        return super().create(db, data)


class ProductSupplierService(CRUDService[ProductSupplier]):
    def __init__(self):
        super().__init__(ProductSupplier)

    def create(self, db: Session, data: dict) -> ProductSupplier:
        product = db.get(Product, data["product_id"])
        if product is None:
            raise NotFoundError(f"Product {data['product_id']!r} not found")

        supplier = db.get(Organization, data["supplier_id"])
        if supplier is None:
            raise NotFoundError(f"Organization {data['supplier_id']!r} not found")
        if not supplier.is_supplier:
            raise ValidationError(
                f"Organization {supplier.name!r} is not flagged as a supplier"
            )

        manufacturer_id: Optional[str] = data.get("manufacturer_id")
        if manufacturer_id and db.get(Organization, manufacturer_id) is None:
            raise NotFoundError(f"Organization {manufacturer_id!r} not found")

        return super().create(db, data)


organization_service = OrganizationService()
product_service = ProductService()
product_supplier_service = ProductSupplierService()
