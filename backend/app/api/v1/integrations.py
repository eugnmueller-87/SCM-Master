"""Integration routes: ingest an upstream ERP/P2P feed into SCM-Master.

The endpoint accepts a feed file (today: a Coupa PO export CSV), runs it through
the matching adapter and the sync engine, and returns a :class:`SyncReport`.

``dry_run`` (default **true**) is a real preview: the sync runs in full inside a
SAVEPOINT that is rolled back, so the returned counts/warnings are exactly what a
commit would produce — nothing is persisted. Send ``dry_run=false`` to apply it.

Importing a feed mutates the catalog and orders, so it is gated to the
PROCUREMENT role (ADMIN passes too).
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile, status
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, get_db, require_role
from app.integrations.base import FeedParseError
from app.integrations.coupa import CoupaCsvAdapter
from app.integrations.sync import sync_feed
from app.models.auth import Role

router = APIRouter(tags=["integrations"], prefix="/integrations",
                   dependencies=[Depends(get_current_user)])

_import_role = require_role(Role.PROCUREMENT)

_MAX_BYTES = 5 * 1024 * 1024  # 5 MB — a PO export is small; reject anything huge.


@router.post("/coupa/import", dependencies=[Depends(_import_role)])
async def import_coupa(
    file: UploadFile = File(..., description="Coupa PO export, CSV"),
    dry_run: bool = Query(True, description="Preview without persisting (default)"),
    db: Session = Depends(get_db),
):
    raw_bytes = await file.read()
    if len(raw_bytes) > _MAX_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail="Feed file too large (limit 5 MB)",
        )
    try:
        text = raw_bytes.decode("utf-8-sig")  # tolerate a BOM from Excel exports
    except UnicodeDecodeError:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="File is not UTF-8 text",
        )

    adapter = CoupaCsvAdapter()
    try:
        batch = adapter.parse(text)
    except FeedParseError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))

    # Run the sync inside a SAVEPOINT so a dry-run can roll back its effects while
    # the report still reflects exactly what would have happened. On a real run we
    # release the savepoint and let get_db commit the outer transaction.
    nested = db.begin_nested()
    report = sync_feed(db, batch, source_system=adapter.source_system, dry_run=dry_run)
    if dry_run:
        nested.rollback()
    else:
        nested.commit()

    return report.as_dict()
