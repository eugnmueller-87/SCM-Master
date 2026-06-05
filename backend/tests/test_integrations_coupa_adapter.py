"""Unit tests for the Coupa CSV adapter — parsing & mapping only, no DB."""
from __future__ import annotations

from decimal import Decimal

import pytest

from app.integrations.base import FeedParseError
from app.integrations.coupa import CoupaCsvAdapter

_HEADER = (
    "po_number,po_status,order_date,currency,supplier_id,supplier_name,"
    "item_number,item_name,item_category,quantity,unit_price,need_by_date"
)


def _csv(*rows: str) -> str:
    return "\n".join([_HEADER, *rows]) + "\n"


def test_parses_and_dedupes_suppliers_and_materials():
    raw = _csv(
        "PO-1,issued,2026-05-20,EUR,SUP-A,Alpha,MAT-1,Server,server,2,1000.00,2026-06-01",
        "PO-1,issued,2026-05-20,EUR,SUP-A,Alpha,MAT-2,SSD,storage,4,200.00,2026-06-01",
        "PO-2,approved,2026-05-21,EUR,SUP-B,Beta,MAT-1,Server,server,1,1000.00,2026-06-02",
    )
    batch = CoupaCsvAdapter().parse(raw)

    # Two distinct suppliers, two distinct materials (MAT-1 shared, deduped).
    assert {s.external_ref for s in batch.suppliers} == {"SUP-A", "SUP-B"}
    assert {m.external_ref for m in batch.materials} == {"MAT-1", "MAT-2"}

    # Two POs; PO-1 has both its lines grouped under one header.
    by_ref = {po.external_ref: po for po in batch.purchase_orders}
    assert set(by_ref) == {"PO-1", "PO-2"}
    assert len(by_ref["PO-1"].lines) == 2
    assert by_ref["PO-1"].supplier_external_ref == "SUP-A"
    assert by_ref["PO-1"].lines[0].unit_price == Decimal("1000.00")


def test_missing_required_columns_is_rejected():
    bad = "po_number,supplier_id\nPO-1,SUP-A\n"  # no item_number / quantity
    with pytest.raises(FeedParseError) as exc:
        CoupaCsvAdapter().parse(bad)
    assert "missing column" in str(exc.value).lower()


def test_one_po_two_suppliers_is_rejected():
    # Invoice matching depends on one-PO-per-supplier; mixing suppliers is invalid.
    raw = _csv(
        "PO-9,issued,2026-05-20,EUR,SUP-A,Alpha,MAT-1,Server,server,1,1000.00,2026-06-01",
        "PO-9,issued,2026-05-20,EUR,SUP-B,Beta,MAT-1,Server,server,1,1000.00,2026-06-01",
    )
    with pytest.raises(FeedParseError) as exc:
        CoupaCsvAdapter().parse(raw)
    assert "more than one supplier" in str(exc.value)


def test_blank_required_cell_is_rejected():
    raw = _csv("PO-1,issued,2026-05-20,EUR,,Alpha,MAT-1,Server,server,1,1000.00,2026-06-01")
    with pytest.raises(FeedParseError):
        CoupaCsvAdapter().parse(raw)


def test_tolerates_us_date_and_thousands_separator():
    # unit_price "1,200.00" carries a thousands separator, so the cell is quoted.
    raw = _csv('PO-1,issued,05/20/2026,EUR,SUP-A,Alpha,MAT-1,Server,server,3,"1,200.00",06/01/2026')
    batch = CoupaCsvAdapter().parse(raw)
    po = batch.purchase_orders[0]
    assert po.date_ordered.isoformat() == "2026-05-20"
    assert po.lines[0].unit_price == Decimal("1200.00")
    assert po.lines[0].quantity == 3
