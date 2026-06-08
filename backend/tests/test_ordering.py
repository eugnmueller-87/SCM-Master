"""Package model + expansion service tests."""
from __future__ import annotations

import pytest

from app.models.catalog import Product
from app.services import ordering
from app.services.exceptions import NotFoundError, ValidationError


def _prod(db, code):
    p = Product(product_code=code, name=code)
    db.add(p)
    db.flush()
    return p


def test_create_and_get_package(db_session):
    a, b = _prod(db_session, "A"), _prod(db_session, "B")
    pkg = ordering.create_package(db_session, code="RACK", name="Rack",
        lines=[{"product_id": a.id, "quantity": 1}, {"product_id": b.id, "quantity": 4}])
    got = ordering.get_package(db_session, pkg.id)
    assert got.code == "RACK"
    assert len(got.lines) == 2


def test_expand_package_multiplies_by_packs(db_session):
    a, b = _prod(db_session, "A"), _prod(db_session, "B")
    pkg = ordering.create_package(db_session, code="RACK", name="Rack",
        lines=[{"product_id": a.id, "quantity": 2}, {"product_id": b.id, "quantity": 3}])
    lines = ordering.expand_package(db_session, pkg.id, packs=3)
    by = {ln["product_id"]: ln["quantity"] for ln in lines}
    assert by[a.id] == 6 and by[b.id] == 9   # ×3 packs


def test_expand_sums_duplicate_products(db_session):
    a = _prod(db_session, "A")
    pkg = ordering.create_package(db_session, code="DUP", name="Dup",
        lines=[{"product_id": a.id, "quantity": 1}, {"product_id": a.id, "quantity": 2}])
    lines = ordering.expand_package(db_session, pkg.id)
    assert len(lines) == 1 and lines[0]["quantity"] == 3


def test_create_package_rejects_empty(db_session):
    with pytest.raises(ValidationError, match="at least one line"):
        ordering.create_package(db_session, code="X", name="X", lines=[])


def test_create_package_rejects_dup_code(db_session):
    a = _prod(db_session, "A")
    ordering.create_package(db_session, code="C", name="C", lines=[{"product_id": a.id, "quantity": 1}])
    with pytest.raises(ValidationError, match="already exists"):
        ordering.create_package(db_session, code="C", name="C2", lines=[{"product_id": a.id, "quantity": 1}])


def test_create_package_rejects_unknown_product(db_session):
    with pytest.raises(NotFoundError):
        ordering.create_package(db_session, code="C", name="C",
            lines=[{"product_id": "nope", "quantity": 1}])


def test_list_packages_active_only(db_session):
    a = _prod(db_session, "A")
    p = ordering.create_package(db_session, code="ON", name="On", lines=[{"product_id": a.id, "quantity": 1}])
    off = ordering.create_package(db_session, code="OFF", name="Off", lines=[{"product_id": a.id, "quantity": 1}])
    off.active = False
    db_session.flush()
    codes = {x.code for x in ordering.list_packages(db_session)}
    assert "ON" in codes and "OFF" not in codes
    _ = p
