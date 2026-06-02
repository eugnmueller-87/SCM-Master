"""Catalog routes: organizations, products, and product-supplier sources.

Handlers stay thin: validate via the schema, call the service, return the ORM
object (Pydantic serialises it via ``from_attributes``). Business rules and the
service exceptions live in app.services; mapping those to HTTP codes is done
once, centrally, in app.api.errors.
"""
from __future__ import annotations

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
)
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
    return product_supplier_service.create(db, payload.model_dump())


@router.get("/product-suppliers", response_model=List[ProductSupplierRead])
def list_product_suppliers(skip: int = 0, limit: int = 100, db: Session = Depends(get_db)):
    return product_supplier_service.list(db, skip=skip, limit=limit)


@router.get("/product-suppliers/{ps_id}", response_model=ProductSupplierRead)
def get_product_supplier(ps_id: str, db: Session = Depends(get_db)):
    return product_supplier_service.get_or_404(db, ps_id)


@router.patch("/product-suppliers/{ps_id}", response_model=ProductSupplierRead)
def update_product_supplier(ps_id: str, payload: ProductSupplierUpdate, db: Session = Depends(get_db)):
    obj = product_supplier_service.get_or_404(db, ps_id)
    return product_supplier_service.update(db, obj, payload.model_dump(exclude_unset=True))
