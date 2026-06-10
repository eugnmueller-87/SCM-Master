"""Per-supplier contract repository API: round-trip, guards, role gating.

Storage is pointed at a tmp dir so nothing touches the repo's default
./var/contracts. Skip-free — the table is built from Base.metadata.
"""
from __future__ import annotations

import pytest

from app.core.config import settings
from app.models.auth import Role

B = "/api/v1"
_PDF = b"%PDF-1.4\n%minimal valid-enough pdf body\n"


@pytest.fixture(autouse=True)
def _tmp_storage(tmp_path, monkeypatch):
    # Every test in this module writes blobs under a throwaway dir.
    monkeypatch.setattr(settings, "contract_storage_dir", str(tmp_path / "contracts"))
    monkeypatch.setattr(settings, "contract_storage_backend", "local")


def _make_supplier(client, name="Dell Technologies"):
    r = client.post(f"{B}/organizations", json={"name": name})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _upload(client, org_id, *, content=_PDF, filename="msa.pdf",
            content_type="application/pdf", kind=None):
    url = f"{B}/suppliers/{org_id}/contracts" + (f"?kind={kind}" if kind else "")
    return client.post(url, files={"file": (filename, content, content_type)})


def test_upload_list_download_delete_roundtrip(client):
    org = _make_supplier(client)

    up = _upload(client, org, kind="MSA")
    assert up.status_code == 201, up.text
    doc = up.json()
    assert doc["original_filename"] == "msa.pdf"
    assert doc["content_type"] == "application/pdf"
    assert doc["size_bytes"] == len(_PDF)
    assert doc["kind"] == "MSA"
    assert "storage_key" not in doc            # internal, never exposed

    lst = client.get(f"{B}/suppliers/{org}/contracts")
    assert lst.status_code == 200
    assert [d["id"] for d in lst.json()] == [doc["id"]]

    dl = client.get(f"{B}/suppliers/{org}/contracts/{doc['id']}/download")
    assert dl.status_code == 200
    assert dl.content == _PDF
    assert "msa.pdf" in dl.headers["content-disposition"]

    rm = client.delete(f"{B}/suppliers/{org}/contracts/{doc['id']}")
    assert rm.status_code == 204
    assert client.get(f"{B}/suppliers/{org}/contracts").json() == []


def test_repository_is_optional(client):
    org = _make_supplier(client)
    r = client.get(f"{B}/suppliers/{org}/contracts")
    assert r.status_code == 200 and r.json() == []   # no uploads is fine


def test_oversize_rejected(client, monkeypatch):
    monkeypatch.setattr(settings, "contract_max_bytes", 16)
    org = _make_supplier(client)
    r = _upload(client, org, content=b"%PDF-1.4" + b"x" * 100)
    assert r.status_code == 413


def test_non_pdf_content_type_rejected(client):
    org = _make_supplier(client)
    r = _upload(client, org, content=b"col1,col2\n1,2\n",
                filename="data.csv", content_type="text/csv")
    assert r.status_code == 422


def test_spoofed_pdf_rejected(client):
    # Declared application/pdf but the bytes aren't a PDF -> magic-byte guard.
    org = _make_supplier(client)
    r = _upload(client, org, content=b"not really a pdf", content_type="application/pdf")
    assert r.status_code == 422


def test_viewer_cannot_upload_or_delete(client):
    org = _make_supplier(client)
    viewer = client.as_role(Role.VIEWER)
    up = viewer.post(f"{B}/suppliers/{org}/contracts",
                     files={"file": ("x.pdf", _PDF, "application/pdf")})
    assert up.status_code == 403
    # procurement can
    proc = client.as_role(Role.PROCUREMENT)
    ok = proc.post(f"{B}/suppliers/{org}/contracts",
                   files={"file": ("x.pdf", _PDF, "application/pdf")})
    assert ok.status_code == 201
    doc_id = ok.json()["id"]
    assert viewer.delete(f"{B}/suppliers/{org}/contracts/{doc_id}").status_code == 403


def test_delete_succeeds_when_bytes_already_gone(client, tmp_path):
    org = _make_supplier(client)
    doc = _upload(client, org).json()
    # Wipe the underlying blob out from under the row.
    for p in (tmp_path / "contracts").rglob("*.pdf"):
        p.unlink()
    rm = client.delete(f"{B}/suppliers/{org}/contracts/{doc['id']}")
    assert rm.status_code == 204
    assert client.get(f"{B}/suppliers/{org}/contracts").json() == []


def test_download_missing_bytes_is_404_not_500(client, tmp_path):
    org = _make_supplier(client)
    doc = _upload(client, org).json()
    for p in (tmp_path / "contracts").rglob("*.pdf"):
        p.unlink()
    dl = client.get(f"{B}/suppliers/{org}/contracts/{doc['id']}/download")
    assert dl.status_code == 404


def test_upload_to_missing_supplier_404(client):
    r = _upload(client, "no-such-org")
    assert r.status_code == 404


def test_download_wrong_org_404(client):
    org_a = _make_supplier(client, "A")
    org_b = _make_supplier(client, "B")
    doc = _upload(client, org_a).json()
    # Same doc id, but under B's URL -> not found.
    dl = client.get(f"{B}/suppliers/{org_b}/contracts/{doc['id']}/download")
    assert dl.status_code == 404
