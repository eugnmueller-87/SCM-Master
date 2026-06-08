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

    def onboard_new(self, db: Session, data: dict) -> Organization:
        """Create a supplier that must clear onboarding before it can be ordered
        from. Forces ``onboarding_status=DRAFT`` regardless of input, so a new
        supplier is never silently orderable. (Plain ``create`` keeps the
        APPROVED default for seeded/imported orgs and back-compat.)"""
        data = {**data, "is_supplier": True, "onboarding_status": "DRAFT"}
        return self.create(db, data)

    def record_risk(self, db: Session, org: Organization, *, risk_level: str,
                    risk_notes: Optional[str], assessed_at) -> Organization:
        org.risk_level = risk_level
        org.risk_notes = risk_notes
        org.risk_assessed_at = assessed_at
        if org.onboarding_status == "DRAFT":
            org.onboarding_status = "IN_REVIEW"
        db.flush()
        return org

    def record_document(self, db: Session, org: Organization, *, kind: str,
                        signed: bool, reference: Optional[str], signed_at) -> Organization:
        """Record a DPA or NDA as signed (metadata of record, no file bytes)."""
        if kind not in ("dpa", "nda"):
            raise ValidationError(f"Unknown document kind {kind!r} (expected dpa/nda)")
        setattr(org, f"{kind}_signed", signed)
        setattr(org, f"{kind}_signed_at", signed_at if signed else None)
        setattr(org, f"{kind}_reference", reference if signed else None)
        if signed and org.onboarding_status == "DRAFT":
            org.onboarding_status = "IN_REVIEW"
        db.flush()
        return org

    def approve(self, db: Session, org: Organization) -> Organization:
        """Approve a supplier for ordering — only if the hard gate is satisfied
        (risk assessed AND both DPA and NDA signed)."""
        if not org.onboarding_complete:
            missing = []
            if org.risk_level is None:
                missing.append("risk assessment")
            if not org.dpa_signed:
                missing.append("signed DPA")
            if not org.nda_signed:
                missing.append("signed NDA")
            raise ValidationError(
                "Cannot approve supplier — onboarding incomplete. Missing: "
                + ", ".join(missing) + "."
            )
        org.onboarding_status = "APPROVED"
        db.flush()
        return org

    def upsert_by_external_ref(
        self, db: Session, *, source_system: str, external_ref: str, data: dict
    ) -> tuple[Organization, bool]:
        """Create or update a supplier synced from an upstream system.

        Keyed on (source_system, external_ref) — the upstream's own supplier id.
        Returns (org, created). Sidesteps the ``code`` uniqueness check on
        re-sync, since the row is identified by its external key, not its code.
        """
        existing = self.get_by_external_ref(
            db, source_system=source_system, external_ref=external_ref
        )
        if existing is not None:
            return self.update(db, existing, data), False
        obj = Organization(source_system=source_system, external_ref=external_ref, **data)
        db.add(obj)
        db.flush()
        return obj, True


class ProductService(CRUDService[Product]):
    def __init__(self):
        super().__init__(Product)

    def create(self, db: Session, data: dict) -> Product:
        code = data["product_code"]
        if db.scalar(select(Product).where(Product.product_code == code)):
            raise ConflictError(f"Product code {code!r} already exists")
        return super().create(db, data)

    def upsert_by_external_ref(
        self, db: Session, *, source_system: str, external_ref: str, data: dict
    ) -> tuple[Product, bool]:
        """Create or update a material synced from an upstream system, keyed on
        (source_system, external_ref). Returns (product, created)."""
        existing = self.get_by_external_ref(
            db, source_system=source_system, external_ref=external_ref
        )
        if existing is not None:
            return self.update(db, existing, data), False
        obj = Product(source_system=source_system, external_ref=external_ref, **data)
        db.add(obj)
        db.flush()
        return obj, True


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
