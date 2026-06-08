"""Catalog routes: organizations, products, and product-supplier sources.

Handlers stay thin: validate via the schema, call the service, return the ORM
object (Pydantic serialises it via ``from_attributes``). Business rules and the
service exceptions live in app.services; mapping those to HTTP codes is done
once, centrally, in app.api.errors.
"""
from __future__ import annotations

from datetime import date as _date
from typing import List

from fastapi import APIRouter, Depends, status
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.schemas.catalog import (
    OrganizationCreate,
    OrganizationRead,
    OrganizationUpdate,
    ProductCreate,
    ProductRead,
    ProductSupplierCreate,
    ProductSupplierRead,
    ProductSupplierUpdate,
    ProductUpdate,
    SupplierDocument,
    SupplierRiskAssessment,
)
from app.services import contracts
from app.services.catalog import (
    organization_service,
    product_service,
    product_supplier_service,
)

router = APIRouter(tags=["catalog"])


# --- Organizations --------------------------------------------------------

@router.post("/organizations", response_model=OrganizationRead, status_code=status.HTTP_201_CREATED)
def create_organization(payload: OrganizationCreate, db: Session = Depends(get_db)):
    return organization_service.create(db, payload.model_dump())


@router.get("/organizations", response_model=List[OrganizationRead])
def list_organizations(skip: int = 0, limit: int = 100, db: Session = Depends(get_db)):
    return organization_service.list(db, skip=skip, limit=limit)


@router.get("/organizations/{org_id}", response_model=OrganizationRead)
def get_organization(org_id: str, db: Session = Depends(get_db)):
    return organization_service.get_or_404(db, org_id)


@router.patch("/organizations/{org_id}", response_model=OrganizationRead)
def update_organization(org_id: str, payload: OrganizationUpdate, db: Session = Depends(get_db)):
    obj = organization_service.get_or_404(db, org_id)
    return organization_service.update(db, obj, payload.model_dump(exclude_unset=True))


# --- Supplier onboarding (compliance gate) --------------------------------
# A supplier created here starts DRAFT and is NOT orderable until it clears the
# gate: a risk assessment + signed DPA and NDA, then explicit approval. Documents
# are metadata of record (signer/date/reference), not stored file bytes.

@router.post("/suppliers/onboard", response_model=OrganizationRead, status_code=status.HTTP_201_CREATED)
def onboard_supplier(payload: OrganizationCreate, db: Session = Depends(get_db)):
    """Register a new supplier in DRAFT (not yet orderable)."""
    return organization_service.onboard_new(db, payload.model_dump())


@router.post("/suppliers/{org_id}/risk-assessment", response_model=OrganizationRead)
def assess_supplier_risk(org_id: str, payload: SupplierRiskAssessment, db: Session = Depends(get_db)):
    org = organization_service.get_or_404(db, org_id)
    return organization_service.record_risk(
        db, org, risk_level=payload.risk_level, risk_notes=payload.risk_notes,
        assessed_at=_date.today())


@router.post("/suppliers/{org_id}/documents/{kind}", response_model=OrganizationRead)
def record_supplier_document(org_id: str, kind: str, payload: SupplierDocument,
                             db: Session = Depends(get_db)):
    """Record a DPA or NDA as signed (kind = 'dpa' | 'nda')."""
    org = organization_service.get_or_404(db, org_id)
    return organization_service.record_document(
        db, org, kind=kind.lower(), signed=payload.signed,
        reference=payload.reference, signed_at=payload.signed_at or _date.today())


@router.post("/suppliers/{org_id}/approve", response_model=OrganizationRead)
def approve_supplier(org_id: str, db: Session = Depends(get_db)):
    """Approve a supplier for ordering — enforces the hard gate."""
    org = organization_service.get_or_404(db, org_id)
    return organization_service.approve(db, org)


# --- Products -------------------------------------------------------------

@router.post("/products", response_model=ProductRead, status_code=status.HTTP_201_CREATED)
def create_product(payload: ProductCreate, db: Session = Depends(get_db)):
    return product_service.create(db, payload.model_dump())


@router.get("/products", response_model=List[ProductRead])
def list_products(skip: int = 0, limit: int = 100, db: Session = Depends(get_db)):
    return product_service.list(db, skip=skip, limit=limit)


@router.get("/products/{product_id}", response_model=ProductRead)
def get_product(product_id: str, db: Session = Depends(get_db)):
    return product_service.get_or_404(db, product_id)


@router.patch("/products/{product_id}", response_model=ProductRead)
def update_product(product_id: str, payload: ProductUpdate, db: Session = Depends(get_db)):
    obj = product_service.get_or_404(db, product_id)
    return product_service.update(db, obj, payload.model_dump(exclude_unset=True))


# --- Product sources ------------------------------------------------------

@router.post("/product-suppliers", response_model=ProductSupplierRead, status_code=status.HTTP_201_CREATED)
def create_product_supplier(payload: ProductSupplierCreate, db: Session = Depends(get_db)):
    ps = product_supplier_service.create(db, payload.model_dump())
    return contracts.enrich(db, ps)


@router.get("/product-suppliers", response_model=List[ProductSupplierRead])
def list_product_suppliers(skip: int = 0, limit: int = 100, db: Session = Depends(get_db)):
    return [contracts.enrich(db, ps) for ps in product_supplier_service.list(db, skip=skip, limit=limit)]


@router.get("/product-suppliers/{ps_id}", response_model=ProductSupplierRead)
def get_product_supplier(ps_id: str, db: Session = Depends(get_db)):
    return contracts.enrich(db, product_supplier_service.get_or_404(db, ps_id))


@router.patch("/product-suppliers/{ps_id}", response_model=ProductSupplierRead)
def update_product_supplier(ps_id: str, payload: ProductSupplierUpdate, db: Session = Depends(get_db)):
    obj = product_supplier_service.get_or_404(db, ps_id)
    updated = product_supplier_service.update(db, obj, payload.model_dump(exclude_unset=True))
    return contracts.enrich(db, updated)
