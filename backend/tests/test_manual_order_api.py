"""Manual-order API: products or package → staged requisition, capacity-guarded."""
from __future__ import annotations

from app.models.catalog import Organization, Product, ProductSupplier
from app.models.flow import Asset, AssetStatus, Location, LocationType
from app.services import ordering

B = "/api/v1"


def _sourced_product(db, code, *, price="100.00"):
    """A product with an active preferred supplier (so it can be sourced/ordered)."""
    sup = Organization(code=f"S-{code}", name=f"Sup {code}", is_supplier=True)
    prod = Product(product_code=code, name=code)
    db.add_all([sup, prod])
    db.flush()
    db.add(ProductSupplier(product_id=prod.id, supplier_id=sup.id,
                           contract_price=price, standard_lead_time_days=10,
                           min_order_quantity=1, preference_rank=1))
    db.flush()
    return prod


def _warehouse(db, code, cap, used=0):
    wh = Location(code=code, name=code, location_type=LocationType.WAREHOUSE, capacity=cap)
    db.add(wh)
    db.flush()
    if used:
        f = Product(product_code=f"F-{code}", name="F")
        db.add(f)
        db.flush()
        for i in range(used):
            db.add(Asset(serial_number=f"S-{code}-{i}", product_id=f.id,
                         status=AssetStatus.IN_STORAGE, current_location_id=wh.id))
        db.flush()
    return wh


def test_manual_order_by_lines_stages_requisition(client, db_session):
    p = _sourced_product(db_session, "M1")
    db_session.commit()
    r = client.post(f"{B}/requisitions/manual", json={"lines": [{"product_id": p.id, "quantity": 5}]})
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body["requisition_ids"]) == 1
    assert body["total_units"] == 5
    # the requisition really exists and is STAGED
    rid = body["requisition_ids"][0]
    got = client.get(f"{B}/requisitions/{rid}")
    assert got.status_code == 200 and got.json()["status"] == "STAGED"


def test_manual_order_by_package(client, db_session):
    a = _sourced_product(db_session, "PA")
    b = _sourced_product(db_session, "PB")
    pkg = ordering.create_package(db_session, code="RK", name="Rack",
        lines=[{"product_id": a.id, "quantity": 1}, {"product_id": b.id, "quantity": 2}])
    db_session.commit()
    r = client.post(f"{B}/requisitions/manual", json={"package_id": pkg.id, "packs": 2})
    assert r.status_code == 200, r.text
    assert r.json()["total_units"] == 6   # (1+2) × 2 packs


def test_manual_order_over_capacity_refused(client, db_session):
    # warehouse cap 10, 8 used → free 2. Ordering 5 must be REFUSED (422).
    _warehouse(db_session, "WH", 10, used=8)
    p = _sourced_product(db_session, "OC")
    db_session.commit()
    r = client.post(f"{B}/requisitions/manual", json={"lines": [{"product_id": p.id, "quantity": 5}]})
    assert r.status_code == 422, r.text
    assert "capacity" in r.json()["detail"].lower()


def test_manual_order_orphan_no_source(client, db_session):
    # a product with NO supplier can't be sourced → reported as orphan, not staged.
    prod = Product(product_code="ORPH", name="Orphan")
    db_session.add(prod)
    db_session.commit()
    r = client.post(f"{B}/requisitions/manual", json={"lines": [{"product_id": prod.id, "quantity": 2}]})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["requisition_ids"] == []
    assert body["orphans"][0]["product_id"] == prod.id


def test_packages_endpoint_lists_seeded_bundles(client, db_session):
    a = _sourced_product(db_session, "L1")
    ordering.create_package(db_session, code="LP", name="List pkg",
        lines=[{"product_id": a.id, "quantity": 1}])
    db_session.commit()
    r = client.get(f"{B}/requisitions/packages")
    assert r.status_code == 200, r.text
    assert any(p["code"] == "LP" for p in r.json())


def test_manual_order_requires_procurement(client, db_session):
    from app.models.auth import Role
    p = _sourced_product(db_session, "RP")
    db_session.commit()
    viewer = client.as_role(Role.VIEWER)
    r = viewer.post(f"{B}/requisitions/manual", json={"lines": [{"product_id": p.id, "quantity": 1}]})
    assert r.status_code == 403
