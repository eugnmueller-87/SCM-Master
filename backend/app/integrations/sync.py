"""The sync engine: persist a canonical FeedBatch through the existing services.

This is the only part of the integration layer that touches the database, and it
does so exclusively through the same domain services the rest of the app uses —
so every business rule (a PO's supplier must have the supplier role, a line's
source must match its product, …) is enforced identically whether a record was
typed in the UI or synced from Coupa. No logic is duplicated here.

Everything keys on the ``(source_system, external_ref)`` pair, making a sync
**idempotent**: replaying the same feed updates rows in place. The caller (the
API route) owns the transaction, so a ``dry_run`` is just "run the sync, then
roll back instead of commit" — the returned :class:`SyncReport` is identical
either way, which is what makes the preview trustworthy.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from app.integrations.schemas import FeedBatch
from app.models.procurement import OrderStatus
from app.services.catalog import organization_service, product_service
from app.services.exceptions import NotFoundError, ValidationError
from app.services.procurement import purchase_order_service

# Coupa/SAP PO statuses -> our OrderStatus. Anything unmapped stays PENDING and
# is surfaced as a warning rather than silently dropped.
_STATUS_MAP = {
    "draft": OrderStatus.PENDING,
    "pending_approval": OrderStatus.PENDING,
    "pending": OrderStatus.PENDING,
    "approved": OrderStatus.APPROVED,
    "issued": OrderStatus.PLACED,
    "ordered": OrderStatus.PLACED,
    "sent": OrderStatus.PLACED,
    "received": OrderStatus.RECEIVED,
    "closed": OrderStatus.RECEIVED,
    "cancelled": OrderStatus.CANCELLED,
    "canceled": OrderStatus.CANCELLED,
}


@dataclass
class EntityCounts:
    created: int = 0
    updated: int = 0

    def record(self, created: bool) -> None:
        if created:
            self.created += 1
        else:
            self.updated += 1


@dataclass
class SyncReport:
    """What a sync did (or, on dry-run, *would* do). Serialisable to JSON."""

    source_system: str
    dry_run: bool
    suppliers: EntityCounts = field(default_factory=EntityCounts)
    materials: EntityCounts = field(default_factory=EntityCounts)
    purchase_orders: EntityCounts = field(default_factory=EntityCounts)
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        def block(c: EntityCounts) -> dict:
            return {"created": c.created, "updated": c.updated}

        return {
            "source_system": self.source_system,
            "dry_run": self.dry_run,
            "suppliers": block(self.suppliers),
            "materials": block(self.materials),
            "purchase_orders": block(self.purchase_orders),
            "warnings": self.warnings,
        }


def sync_feed(
    db: Session, batch: FeedBatch, *, source_system: str, dry_run: bool
) -> SyncReport:
    """Upsert every record in ``batch``. Order matters: suppliers and materials
    first (POs reference them), then POs. The transaction is the caller's; we
    only ``flush`` here, so a dry-run rollback leaves no trace."""
    report = SyncReport(source_system=source_system, dry_run=dry_run)

    # 1. Suppliers -> Organization (with the supplier role).
    supplier_id_by_ref: dict[str, str] = {}
    for s in batch.suppliers:
        org, created = organization_service.upsert_by_external_ref(
            db,
            source_system=source_system,
            external_ref=s.external_ref,
            data={
                "name": s.name,
                "code": s.code,
                "is_supplier": True,
                "active": s.active,
            },
        )
        supplier_id_by_ref[s.external_ref] = org.id
        report.suppliers.record(created)

    # 2. Materials -> Product.
    product_id_by_ref: dict[str, str] = {}
    for m in batch.materials:
        product, created = product_service.upsert_by_external_ref(
            db,
            source_system=source_system,
            external_ref=m.external_ref,
            data={
                "product_code": m.product_code,
                "name": m.name,
                "category": m.category,
                "description": m.description,
            },
        )
        product_id_by_ref[m.external_ref] = product.id
        report.materials.record(created)

    # 3. Purchase orders -> PurchaseOrder + OrderItems.
    for po in batch.purchase_orders:
        supplier_id = supplier_id_by_ref.get(po.supplier_external_ref)
        if supplier_id is None:
            report.warnings.append(
                f"PO {po.external_ref}: supplier {po.supplier_external_ref!r} "
                "not in this feed — skipped"
            )
            continue

        items: list[dict] = []
        skip = False
        for ln in po.lines:
            product_id = product_id_by_ref.get(ln.material_external_ref)
            if product_id is None:
                report.warnings.append(
                    f"PO {po.external_ref}: item {ln.material_external_ref!r} "
                    "not in this feed — PO skipped"
                )
                skip = True
                break
            items.append(
                {
                    "product_id": product_id,
                    "quantity": ln.quantity,
                    "unit_price": ln.unit_price,
                    "estimated_delivery_date": ln.expected_delivery_date,
                }
            )
        if skip:
            continue

        header = {
            "order_number": po.order_number,
            "supplier_id": supplier_id,
            "currency_code": po.currency_code,
            "date_ordered": po.date_ordered,
        }
        try:
            order, created = purchase_order_service.upsert_by_external_ref(
                db,
                source_system=source_system,
                external_ref=po.external_ref,
                header=header,
                items=items,
            )
        except (NotFoundError, ValidationError) as exc:
            report.warnings.append(f"PO {po.external_ref}: {exc} — skipped")
            continue

        # Map upstream status onto ours where we can (informational; the import
        # never forces an illegal transition — it sets status directly on sync).
        if po.status:
            mapped = _STATUS_MAP.get(po.status.strip().lower())
            if mapped is None:
                report.warnings.append(
                    f"PO {po.external_ref}: unknown upstream status {po.status!r} "
                    "— left as-is"
                )
            else:
                order.status = mapped
                db.flush()
        report.purchase_orders.record(created)

    return report
