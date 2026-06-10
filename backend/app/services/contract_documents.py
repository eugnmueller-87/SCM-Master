"""Service for the per-supplier contract repository.

Thin layer between the API and the pluggable ``ContractStore``: it owns the
storage-key generation, the write-bytes-then-row ordering (with best-effort
rollback of orphaned bytes), and the list/read/remove operations. No HTTP, no
decision logic — uploads are optional and nothing here gates anything.
"""
from __future__ import annotations

import uuid
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.catalog import ContractDocument
from app.services.contract_store import ContractStore, get_contract_store
from app.services.exceptions import NotFoundError


def _new_key(organization_id: str) -> str:
    # Server-generated, opaque, collision-free. Never the user's filename.
    return f"{organization_id}/{uuid.uuid4().hex}.pdf"


def save(db: Session, *, organization_id: str, filename: str, content_type: str,
         data: bytes, kind: Optional[str] = None,
         store: Optional[ContractStore] = None) -> ContractDocument:
    """Persist the bytes then the row. Rolls back orphaned bytes if the row fails."""
    store = store or get_contract_store()
    key = _new_key(organization_id)
    store.put(key, data)
    try:
        doc = ContractDocument(
            organization_id=organization_id,
            original_filename=filename,
            content_type=content_type,
            size_bytes=len(data),
            storage_key=key,
            kind=kind,
        )
        db.add(doc)
        db.flush()
        return doc
    except Exception:
        # The row didn't land — don't leave the blob orphaned in the store.
        try:
            store.delete(key)
        except Exception:  # noqa: BLE001  # nosec B110 — best-effort cleanup; re-raise the real error below
            pass
        raise


def list_for_org(db: Session, organization_id: str) -> list[ContractDocument]:
    return list(db.scalars(
        select(ContractDocument)
        .where(ContractDocument.organization_id == organization_id)
        .order_by(ContractDocument.uploaded_at.desc())
    ).all())


def get_or_404(db: Session, doc_id: str) -> ContractDocument:
    doc = db.get(ContractDocument, doc_id)
    if doc is None:
        raise NotFoundError(f"ContractDocument {doc_id!r} not found")
    return doc


def read_bytes(doc: ContractDocument, *, store: Optional[ContractStore] = None) -> bytes:
    store = store or get_contract_store()
    return store.get(doc.storage_key)


def remove(db: Session, doc: ContractDocument, *,
           store: Optional[ContractStore] = None) -> None:
    """Delete the blob (idempotent) and the row."""
    store = store or get_contract_store()
    store.delete(doc.storage_key)
    db.delete(doc)
    db.flush()
