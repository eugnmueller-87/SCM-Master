"""End-to-end tests for the Coupa import endpoint.

Drives the real route (multipart upload) through the real services, asserting:
dry-run persists nothing, a real import creates correctly-keyed rows, re-import
is idempotent, the role gate holds, and a bad file is a clean 422.
"""
from __future__ import annotations

from app.models.auth import Role

B = "/api/v1"

_CSV = (
    "po_number,po_status,order_date,currency,supplier_id,supplier_name,"
    "item_number,item_name,item_category,quantity,unit_price,need_by_date\n"
    "PO-2026-0001,issued,2026-05-20,EUR,SUP-DELL,Dell,MAT-SRV-1U,1U Server,server,10,4200.00,2026-06-15\n"
    "PO-2026-0001,issued,2026-05-20,EUR,SUP-DELL,Dell,MAT-NVME-4T,4TB NVMe,storage,40,520.00,2026-06-15\n"
    "PO-2026-0002,approved,2026-05-22,EUR,SUP-SMCI,Supermicro,MAT-SRV-2U,2U Server,server,6,5100.00,2026-06-20\n"
)


def _upload(client, *, dry_run: bool, text: str = _CSV):
    return client.post(
        f"{B}/integrations/coupa/import",
        params={"dry_run": dry_run},
        files={"file": ("coupa.csv", text, "text/csv")},
    )


def test_dry_run_reports_but_persists_nothing(client):
    r = _upload(client, dry_run=True)
    assert r.status_code == 200
    body = r.json()
    assert body["dry_run"] is True
    assert body["suppliers"]["created"] == 2
    assert body["materials"]["created"] == 3
    assert body["purchase_orders"]["created"] == 2

    # Nothing was actually written.
    assert client.get(f"{B}/organizations").json() == []
    assert client.get(f"{B}/products").json() == []
    assert client.get(f"{B}/purchase-orders").json() == []


def test_real_import_creates_keyed_rows(client):
    r = _upload(client, dry_run=False)
    assert r.status_code == 200
    assert r.json()["dry_run"] is False

    orgs = client.get(f"{B}/organizations").json()
    assert {o["name"] for o in orgs} == {"Dell", "Supermicro"}
    assert all(o["source_system"] == "coupa" for o in orgs)
    assert {o["external_ref"] for o in orgs} == {"SUP-DELL", "SUP-SMCI"}

    pos = client.get(f"{B}/purchase-orders").json()
    assert {p["order_number"] for p in pos} == {"PO-2026-0001", "PO-2026-0002"}
    dell_po = next(p for p in pos if p["order_number"] == "PO-2026-0001")
    assert dell_po["external_ref"] == "PO-2026-0001"
    assert dell_po["status"] == "PLACED"  # 'issued' -> PLACED
    assert len(dell_po["items"]) == 2


def test_reimport_is_idempotent(client):
    _upload(client, dry_run=False)
    # Second import of the SAME feed updates in place — no duplicates.
    r2 = _upload(client, dry_run=False)
    body = r2.json()
    assert body["suppliers"] == {"created": 0, "updated": 2}
    assert body["materials"] == {"created": 0, "updated": 3}
    assert body["purchase_orders"] == {"created": 0, "updated": 2}

    assert len(client.get(f"{B}/organizations").json()) == 2
    assert len(client.get(f"{B}/purchase-orders").json()) == 2


def test_changed_feed_updates_existing_row(client):
    _upload(client, dry_run=False)
    changed = _CSV.replace("Dell,MAT-SRV-1U,1U Server", "Dell Inc,MAT-SRV-1U,1U Server")
    _upload(client, dry_run=False, text=changed)
    dell = next(o for o in client.get(f"{B}/organizations").json() if o["external_ref"] == "SUP-DELL")
    assert dell["name"] == "Dell Inc"  # updated, not duplicated


def test_import_requires_procurement_role(client):
    viewer = client.as_role(Role.VIEWER)
    r = viewer.post(
        f"{B}/integrations/coupa/import",
        params={"dry_run": True},
        files={"file": ("coupa.csv", _CSV, "text/csv")},
    )
    assert r.status_code == 403


def test_bad_file_is_422(client):
    r = _upload(client, dry_run=True, text="not,a,coupa,file\n1,2,3,4\n")
    assert r.status_code == 422
    assert "missing column" in r.json()["detail"].lower()
