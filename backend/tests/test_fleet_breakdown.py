"""What the fleet is made of: manufacturer, class, model.

The trap this file exists for: a product can carry several sources, and for the
volume models it does (Apple and Samsung phones have a second reseller in the
seed). Joining asset to product_supplier would count those devices once per
source, silently, and only for the two manufacturers that matter most. The first
test buys exactly that setup and insists the count does not move.
"""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from app.models.catalog import Organization, Product, ProductSupplier
from app.models.flow import Asset, AssetStatus
from app.services import fleet

TODAY = date(2026, 10, 4)


def _save(db, obj):
    db.add(obj)
    db.flush()
    return obj


def _catalogue(db):
    """Two makers, three models, one of them dual sourced."""
    apple = _save(db, Organization(code="APPLE", name="Apple", is_manufacturer=True))
    samsung = _save(db, Organization(code="SAMSUNG", name="Samsung", is_manufacturer=True))
    dealer_a = _save(db, Organization(code="RS-A", name="Reseller A", is_supplier=True))
    dealer_b = _save(db, Organization(code="RS-B", name="Reseller B", is_supplier=True))

    phone = _save(db, Product(product_code="APL-IP16-128", name="iPhone 16 · 128 GB", category="Smartphone",
                              description="Launch RRP 949 EUR gross, Germany, 2024-09-20. Source: https://example.invalid/iphone16"))
    pad = _save(db, Product(product_code="APL-IPAD10-64", name="iPad (10th gen)", category="Tablet",
                            description="Launch RRP 579 EUR gross, Germany, 2022-10-26. Source: https://example.invalid/ipad10"))
    galaxy = _save(db, Product(product_code="SAM-S25-128", name="Galaxy S25 · 128 GB", category="Smartphone",
                               description="Launch RRP 899 EUR gross, Germany, 2025-02-07. Source: https://example.invalid/s25"))

    # The phone is bought from TWO sources, both of which name Apple as the maker.
    for dealer in (dealer_a, dealer_b):
        _save(db, ProductSupplier(product_id=phone.id, supplier_id=dealer.id, manufacturer_id=apple.id,
                                  contract_price=Decimal("800.00"), preference_rank=1 if dealer is dealer_a else 2))
    _save(db, ProductSupplier(product_id=pad.id, supplier_id=dealer_a.id, manufacturer_id=apple.id,
                              contract_price=Decimal("500.00"), preference_rank=1))
    _save(db, ProductSupplier(product_id=galaxy.id, supplier_id=dealer_b.id, manufacturer_id=samsung.id,
                              contract_price=Decimal("700.00"), preference_rank=1))
    return phone, pad, galaxy


def _assets(db, product, counts):
    for status, n in counts.items():
        for i in range(n):
            _save(db, Asset(serial_number=f"{product.product_code}-{status.value}-{i}", product_id=product.id,
                            status=status, cycle_no=1, received_date=TODAY - timedelta(days=200),
                            status_since=TODAY - timedelta(days=10)))


def _seed(db):
    phone, pad, galaxy = _catalogue(db)
    _assets(db, phone, {AssetStatus.RENTED: 10, AssetStatus.REPAIR: 2, AssetStatus.IN_STORAGE: 3, AssetStatus.SOLD: 1})
    _assets(db, pad, {AssetStatus.RENTED: 4, AssetStatus.RECYCLED: 1})
    _assets(db, galaxy, {AssetStatus.RENTED: 7, AssetStatus.SELLABLE: 2})
    db.flush()
    return phone, pad, galaxy


def test_a_dual_sourced_model_is_counted_once(db_session):
    """The whole reason this breakdown does not join through product_supplier."""
    phone, _pad, _galaxy = _seed(db_session)
    out = fleet.breakdown(db_session, today=TODAY)
    apple = next(m for m in out["manufacturers"] if m["name"] == "Apple")
    model = next(mo for f in apple["families"] for mo in f["models"] if mo["code"] == phone.product_code)
    # 10 rented + 2 repair + 3 storage + 1 sold, and NOT twice that
    assert model["total"] == 16
    assert model["by_status"]["RENTED"] == 10


def test_the_tree_adds_up_to_the_fleet(db_session):
    _seed(db_session)
    out = fleet.breakdown(db_session, today=TODAY)
    assert out["total"] == 16 + 5 + 9
    assert sum(m["total"] for m in out["manufacturers"]) == out["total"]
    for maker in out["manufacturers"]:
        assert sum(f["total"] for f in maker["families"]) == maker["total"]
        for fam in maker["families"]:
            assert sum(mo["total"] for mo in fam["models"]) == fam["total"]


def test_active_excludes_what_has_left_the_fleet(db_session):
    _seed(db_session)
    out = fleet.breakdown(db_session, today=TODAY)
    # one sold phone and one recycled tablet are gone
    assert out["active"] == out["total"] - 2
    apple = next(m for m in out["manufacturers"] if m["name"] == "Apple")
    assert apple["active"] == apple["total"] - 2


def test_the_order_follows_the_live_fleet_not_the_sold_one(db_session):
    """Sorted by active, never by total: a maker whose devices have mostly been
    sold on would otherwise outrank one with more devices still in service, and
    the list would answer a question nobody asked."""
    _seed(db_session)   # _seed baut den Katalog schon; ein zweiter Aufruf legt ihn doppelt an
    small = _save(db_session, Product(product_code="GHOST-1", name="Mostly sold", category="Smartphone"))
    ghost = _save(db_session, Organization(code="GHOST", name="Ghostmaker", is_manufacturer=True))
    _save(db_session, ProductSupplier(product_id=small.id, supplier_id=ghost.id, manufacturer_id=ghost.id,
                                      contract_price=Decimal("1.00"), preference_rank=1))
    _assets(db_session, small, {AssetStatus.SOLD: 500, AssetStatus.RENTED: 1})
    db_session.flush()
    out = fleet.breakdown(db_session, today=TODAY)
    order = [m["name"] for m in out["manufacturers"]]
    ghost_row = next(m for m in out["manufacturers"] if m["name"] == "Ghostmaker")
    assert ghost_row["total"] == 501 and ghost_row["active"] == 1
    # biggest by total, and still last
    assert order[-1] == "Ghostmaker"
    actives = [m["active"] for m in out["manufacturers"]]
    assert actives == sorted(actives, reverse=True)
    assert abs(sum(m["share_active"] for m in out["manufacturers"]) - 100.0) < 0.3


def test_the_datacenter_statuses_are_not_a_silent_hole(db_session):
    """seed_reset still detects a datacenter database. Without DEPLOYED and
    MAINTENANCE in the status list those devices would sit inside `total` and
    appear in no chip: counted in the sum, invisible in the breakdown."""
    phone, _pad, _galaxy = _catalogue(db_session)
    _assets(db_session, phone, {AssetStatus.DEPLOYED: 9, AssetStatus.MAINTENANCE: 2})
    db_session.flush()
    out = fleet.breakdown(db_session, today=TODAY)
    apple = next(m for m in out["manufacturers"] if m["name"] == "Apple")
    assert apple["by_status"]["DEPLOYED"] == 9
    assert apple["by_status"]["MAINTENANCE"] == 2
    assert sum(apple["by_status"].values()) == apple["total"]


def test_a_model_carries_its_launch_price_and_its_source(db_session):
    """The catalogue facts travel with the row, so a reader can check the number
    instead of trusting it."""
    _seed(db_session)
    out = fleet.breakdown(db_session, today=TODAY)
    apple = next(m for m in out["manufacturers"] if m["name"] == "Apple")
    phone = next(mo for f in apple["families"] for mo in f["models"] if mo["code"] == "APL-IP16-128")
    assert phone["launch_rrp_eur"] == 949.0
    assert phone["launch_date"] == "2024-09-20"
    assert phone["source"].startswith("https://")


def test_a_product_without_a_maker_is_shown_not_dropped(db_session):
    """A fleet that does not add up is worse than one with an honest unknown."""
    orphan = _save(db_session, Product(product_code="NO-MAKER", name="Unattributed device", category="Smartphone"))
    _assets(db_session, orphan, {AssetStatus.RENTED: 5})
    _seed(db_session)
    out = fleet.breakdown(db_session, today=TODAY)
    unknown = next(m for m in out["manufacturers"] if m["name"] == fleet.UNKNOWN_MAKER)
    assert unknown["total"] == 5
    assert sum(m["total"] for m in out["manufacturers"]) == out["total"]


def test_a_status_nobody_holds_is_absent_not_zero_filled(db_session):
    """Empty means empty: a status with no devices does not appear as a zero that
    reads like a measurement."""
    _seed(db_session)
    out = fleet.breakdown(db_session, today=TODAY)
    samsung = next(m for m in out["manufacturers"] if m["name"] == "Samsung")
    assert "REPAIR" not in samsung["by_status"]
    assert samsung["by_status"]["RENTED"] == 7


def test_an_empty_fleet_answers_without_inventing_anything(db_session):
    out = fleet.breakdown(db_session, today=TODAY)
    assert out["total"] == 0
    assert out["active"] == 0
    assert out["manufacturers"] == []
