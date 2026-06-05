"""Coupa adapter: maps a Coupa Purchase-Order export (CSV) onto canonical records.

Coupa's PO export is **denormalised** — one row per PO *line*, with the supplier
and item repeated on every line of the same order. This adapter:

  1. parses the CSV,
  2. de-duplicates suppliers and materials (each appears once in the batch),
  3. groups line rows back into PO headers + lines.

Expected columns (a representative subset of Coupa's standard PO export; extra
columns are ignored, so a fuller export still works):

    po_number, po_status, order_date, currency,
    supplier_id, supplier_name,
    item_number, item_name, item_category,
    quantity, unit_price, need_by_date

The ``*_id`` columns are Coupa's own identifiers and become our ``external_ref``
values; the human-readable ``*_number``/``name`` columns become our business
codes/names. Both ``supplier_id`` and ``item_number`` are required on every line.
"""
from __future__ import annotations

import csv
import io
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from app.integrations.base import Adapter, FeedParseError
from app.integrations.schemas import (
    FeedBatch,
    MaterialRecord,
    PoLineRecord,
    PurchaseOrderRecord,
    SupplierRecord,
)

# Columns we must see at least once to consider the file a Coupa PO export.
_REQUIRED_COLUMNS = {"po_number", "supplier_id", "item_number", "quantity"}


def _clean(value: str | None) -> str:
    return (value or "").strip()


def _parse_date(value: str | None) -> date | None:
    raw = _clean(value)
    if not raw:
        return None
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%d.%m.%Y"):
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    raise FeedParseError(f"Unrecognised date format: {raw!r}")


def _parse_decimal(value: str | None) -> Decimal | None:
    raw = _clean(value)
    if not raw:
        return None
    try:
        return Decimal(raw.replace(",", ""))
    except InvalidOperation:
        raise FeedParseError(f"Not a number: {value!r}")


def _parse_int(value: str | None, *, field: str) -> int:
    raw = _clean(value)
    if not raw:
        raise FeedParseError(f"Missing required numeric field {field!r}")
    try:
        return int(Decimal(raw))  # tolerate "5.0"
    except (InvalidOperation, ValueError):
        raise FeedParseError(f"{field!r} is not an integer: {value!r}")


class CoupaCsvAdapter(Adapter):
    source_system = "coupa"

    def parse(self, raw: str) -> FeedBatch:
        reader = csv.DictReader(io.StringIO(raw))
        if reader.fieldnames is None:
            raise FeedParseError("Empty file — no header row found")

        headers = {h.strip().lower() for h in reader.fieldnames}
        missing = _REQUIRED_COLUMNS - headers
        if missing:
            raise FeedParseError(
                "Not a Coupa PO export — missing column(s): "
                + ", ".join(sorted(missing))
            )

        suppliers: dict[str, SupplierRecord] = {}
        materials: dict[str, MaterialRecord] = {}
        orders: dict[str, PurchaseOrderRecord] = {}

        for i, row in enumerate(reader, start=2):  # row 1 is the header
            # Normalise keys to lower-case so column-case doesn't matter.
            r = {(k or "").strip().lower(): v for k, v in row.items()}

            po_number = _clean(r.get("po_number"))
            supplier_id = _clean(r.get("supplier_id"))
            item_number = _clean(r.get("item_number"))
            if not po_number or not supplier_id or not item_number:
                raise FeedParseError(
                    f"Row {i}: po_number, supplier_id and item_number are all required"
                )

            # Supplier (deduped on its Coupa id).
            if supplier_id not in suppliers:
                suppliers[supplier_id] = SupplierRecord(
                    external_ref=supplier_id,
                    name=_clean(r.get("supplier_name")) or supplier_id,
                    code=supplier_id,
                    currency_code=_clean(r.get("currency")) or "EUR",
                )

            # Material (deduped on its Coupa item number).
            if item_number not in materials:
                materials[item_number] = MaterialRecord(
                    external_ref=item_number,
                    product_code=item_number,
                    name=_clean(r.get("item_name")) or item_number,
                    category=_clean(r.get("item_category")) or None,
                )

            # PO header (created once per po_number) + this line.
            order = orders.get(po_number)
            if order is None:
                order = PurchaseOrderRecord(
                    external_ref=po_number,
                    supplier_external_ref=supplier_id,
                    order_number=po_number,
                    currency_code=_clean(r.get("currency")) or "EUR",
                    date_ordered=_parse_date(r.get("order_date")),
                    status=_clean(r.get("po_status")) or None,
                )
                orders[po_number] = order
            elif order.supplier_external_ref != supplier_id:
                # One PO per supplier is a hard invariant here (invoice matching).
                raise FeedParseError(
                    f"PO {po_number!r} has lines from more than one supplier "
                    f"({order.supplier_external_ref!r} vs {supplier_id!r})"
                )

            order.lines.append(
                PoLineRecord(
                    material_external_ref=item_number,
                    quantity=_parse_int(r.get("quantity"), field="quantity"),
                    unit_price=_parse_decimal(r.get("unit_price")),
                    expected_delivery_date=_parse_date(r.get("need_by_date")),
                )
            )

        return FeedBatch(
            suppliers=list(suppliers.values()),
            materials=list(materials.values()),
            purchase_orders=list(orders.values()),
        )
