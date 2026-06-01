"""Catalog schemas: Organization, Product, ProductSupplier.

Each entity has three shapes:
  - ``*Create`` : the fields a client supplies to create one;
  - ``*Update`` : every field optional, for partial (PATCH) updates;
  - ``*Read``   : what the API returns (Create fields + id + timestamps).
"""
from __future__ import annotations

from decimal import Decimal
from typing import Optional

from pydantic import BaseModel, Field

from app.schemas.base import ReadBase


# --- Organization ---------------------------------------------------------

class OrganizationCreate(BaseModel):
    name: str
    code: Optional[str] = None
    is_supplier: bool = True
    is_manufacturer: bool = False
    active: bool = True


class OrganizationUpdate(BaseModel):
    name: Optional[str] = None
    code: Optional[str] = None
    is_supplier: Optional[bool] = None
    is_manufacturer: Optional[bool] = None
    active: Optional[bool] = None


class OrganizationRead(ReadBase):
    name: str
    code: Optional[str]
    is_supplier: bool
    is_manufacturer: bool
    active: bool


# --- Product --------------------------------------------------------------

class ProductCreate(BaseModel):
    product_code: str
    name: str
    description: Optional[str] = None
    category: Optional[str] = None
    active: bool = True


class ProductUpdate(BaseModel):
    product_code: Optional[str] = None
    name: Optional[str] = None
    description: Optional[str] = None
    category: Optional[str] = None
    active: Optional[bool] = None


class ProductRead(ReadBase):
    product_code: str
    name: str
    description: Optional[str]
    category: Optional[str]
    active: bool


# --- ProductSupplier (a source for a product) -----------------------------

class ProductSupplierCreate(BaseModel):
    product_id: str
    supplier_id: str
    manufacturer_id: Optional[str] = None
    supplier_product_code: Optional[str] = None
    manufacturer_part_number: Optional[str] = None
    standard_lead_time_days: Optional[int] = Field(default=None, ge=0)
    min_order_quantity: Optional[int] = Field(default=None, ge=0)
    contract_price: Optional[Decimal] = Field(default=None, ge=0)
    currency_code: str = "EUR"
    preference_rank: int = 100
    active: bool = True


class ProductSupplierUpdate(BaseModel):
    supplier_id: Optional[str] = None
    manufacturer_id: Optional[str] = None
    supplier_product_code: Optional[str] = None
    manufacturer_part_number: Optional[str] = None
    standard_lead_time_days: Optional[int] = Field(default=None, ge=0)
    min_order_quantity: Optional[int] = Field(default=None, ge=0)
    contract_price: Optional[Decimal] = Field(default=None, ge=0)
    currency_code: Optional[str] = None
    preference_rank: Optional[int] = None
    active: Optional[bool] = None


class ProductSupplierRead(ReadBase):
    product_id: str
    supplier_id: str
    manufacturer_id: Optional[str]
    supplier_product_code: Optional[str]
    manufacturer_part_number: Optional[str]
    standard_lead_time_days: Optional[int]
    min_order_quantity: Optional[int]
    contract_price: Optional[Decimal]
    currency_code: str
    preference_rank: int
    active: bool
