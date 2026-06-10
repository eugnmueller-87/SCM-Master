"""Catalog schemas: Organization, Product, ProductSupplier.

Each entity has three shapes:
  - ``*Create`` : the fields a client supplies to create one;
  - ``*Update`` : every field optional, for partial (PATCH) updates;
  - ``*Read``   : what the API returns (Create fields + id + timestamps).
"""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Optional

from pydantic import BaseModel, Field, field_validator

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
    # Onboarding / compliance gate.
    onboarding_status: str
    risk_level: Optional[str] = None
    risk_notes: Optional[str] = None
    risk_assessed_at: Optional[date] = None
    dpa_signed: bool = False
    dpa_signed_at: Optional[date] = None
    dpa_reference: Optional[str] = None
    nda_signed: bool = False
    nda_signed_at: Optional[date] = None
    nda_reference: Optional[str] = None
    is_orderable: bool = False
    # Provenance when synced from an upstream system (SAP/Coupa); null if born here.
    source_system: Optional[str] = None
    external_ref: Optional[str] = None


class SupplierRiskAssessment(BaseModel):
    """Record a quick supplier risk assessment (step 1 of onboarding)."""
    risk_level: str  # LOW / MEDIUM / HIGH
    risk_notes: Optional[str] = None

    @field_validator("risk_level")
    @classmethod
    def _valid_risk(cls, v: str) -> str:
        allowed = {"LOW", "MEDIUM", "HIGH"}
        u = v.strip().upper()
        if u not in allowed:
            raise ValueError(f"risk_level must be one of {sorted(allowed)}")
        return u


class SupplierDocument(BaseModel):
    """Mark a compliance document (DPA / NDA) as signed — metadata of record,
    no file bytes. ``reference`` is the filename / DocuSign ref / signer note."""
    signed: bool = True
    reference: Optional[str] = None
    signed_at: Optional[date] = None  # defaults to today when omitted


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
    # Provenance when synced from an upstream system (SAP/Coupa); null if born here.
    source_system: Optional[str] = None
    external_ref: Optional[str] = None


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
    # Contract lifecycle (all optional).
    contract_status: Optional[str] = None
    term_start: Optional[date] = None
    term_end: Optional[date] = None
    annual_budget: Optional[Decimal] = Field(default=None, ge=0)


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
    contract_status: Optional[str] = None
    term_start: Optional[date] = None
    term_end: Optional[date] = None
    annual_budget: Optional[Decimal] = Field(default=None, ge=0)


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
    # Contract lifecycle. contract_status is the stored value when present, else
    # derived server-side (see catalog service). ytd_spend is computed live.
    contract_status: Optional[str] = None
    term_start: Optional[date] = None
    term_end: Optional[date] = None
    annual_budget: Optional[Decimal] = None
    ytd_spend: Optional[Decimal] = None


class ContractDocumentRead(ReadBase):
    """An uploaded contract file's metadata. ``storage_key`` is intentionally NOT
    exposed — it's an internal store detail, never sent to clients."""

    organization_id: str
    original_filename: str
    content_type: str
    size_bytes: int
    kind: Optional[str] = None
    uploaded_at: datetime
