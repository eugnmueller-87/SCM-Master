"""The device cycle's write side: one function per business step of a rented device.

The state machine in ``lifecycle.py`` says which moves are physically possible, and the
asset service makes the move and logs it. What neither of them knows is what a move
means to the rest of the fleet: a rental is a contract, a return ends one, grading
records a grade and picks the next step, a repair is a partner's invoice, a sale is a
price and a channel. The seed writes those facts with the fleet; the console's transition
button does not write them at all, which is why a device rented from the Fleet tab has
no contract and never appears on the return calendar.

This module is the one place those facts are written, so that anything that moves a
device through the cycle, the simulation tab today, a receiving screen or a partner
integration tomorrow, produces the same rows the seed does. Every function goes through
``asset_service.transition``: an illegal step is refused by the state machine with its
reason before anything is written, and the dwell clock is stamped the way it is in real
use. The device-as-a-service fields are set alongside, never instead.

The next step after grading is the fleet's rule (``fleet.NEXT_STEP_SHARE``), spelled
out here per device: after a first rental grade A and B go to refurbishment for a second
rental, C to repair first, D to sale; after a second rental every device is cleared for
sale; a small share is recycled whatever the grade. The rule says where a device goes,
not how many go where; the shares are the expectation over the grade mix.
"""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.flow import Asset, AssetStatus, Location
from app.models.rental import ContractStatus, RentalContract
from app.models.tco import ServiceEvent, ServiceKind
from app.services import warehouse
from app.services.asset import asset_service
from app.services.exceptions import ValidationError

DAYS_PER_MONTH = 30.4375
GRADES = ("A", "B", "C", "D")
END_REASONS = ("planned", "early", "defect", "swap")


def stations(db: Session) -> dict[str, str]:
    """The station location of every compartment that has one, as {compartment code: location id}.

    A device moved into a compartment is placed at its station, the way the seed places
    it; a compartment without a station (a test fixture, an older dataset) leaves the
    location alone rather than inventing one.
    """
    codes = [c.code for c in warehouse.COMPARTMENTS]
    return {code: loc_id for code, loc_id in db.execute(select(Location.code, Location.id).where(Location.code.in_(codes))).all()}


def _station_for(db: Session, status: AssetStatus, location_id: Optional[str]) -> Optional[str]:
    """The location to move into: the caller's, else the station of the compartment the status belongs to."""
    if location_id is not None:
        return location_id
    comp = warehouse.STATION_OF_STATUS.get(status)
    if comp is None:
        return None
    return db.scalar(select(Location.id).where(Location.code == comp.code))


def _move(db: Session, asset_id: str, target: AssetStatus, *, location_id: Optional[str], actor: Optional[str],
          note: Optional[str], today: date) -> Asset:
    """One step through the asset service; the state machine refuses what it must."""
    return asset_service.transition(db, asset_id, target, location_id=_station_for(db, target, location_id),
                                    actor=actor, note=note, effective_date=today)


# ---------------------------------------------------------------------------
# rent and return


def rent(db: Session, asset_id: str, *, customer_id: str, term_months: int, rent_eur_month: Optional[float] = None,
         actor: Optional[str] = None, today: Optional[date] = None) -> RentalContract:
    """A device goes out to a customer: the next rental of its life starts, with a contract.

    The state machine decides where a rental can start from (new stock, second-life
    stock, sellable stock, the swap buffer). The cycle number advances, the customer is
    recorded on the device, the location is cleared because the device is no longer in
    the warehouse, and ``deployed_date`` is the start of this rental, as the seed writes
    it, so the turn and dock-to-deploy figures read the same for a simulated rental.
    """
    today = today or date.today()
    if term_months <= 0:
        raise ValidationError("a rental term has to be at least one month")
    asset = asset_service.get_or_404(db, asset_id)
    cycle_no = int(asset.cycle_no or 0) + 1
    _move(db, asset_id, AssetStatus.RENTED, location_id=None, actor=actor, note=f"rental {cycle_no} started", today=today)
    asset.cycle_no = cycle_no
    asset.customer_id = customer_id
    asset.current_location_id = None
    asset.deployed_date = today
    contract = RentalContract(
        asset_id=asset.id, product_id=asset.product_id, customer_id=customer_id, cycle_no=cycle_no,
        start_date=today, term_months=int(term_months),
        planned_end=today + timedelta(days=round(term_months * DAYS_PER_MONTH)),
        rent_eur_month=(Decimal(str(round(rent_eur_month, 2))) if rent_eur_month is not None else None),
        status=ContractStatus.RUNNING,
    )
    db.add(contract)
    db.flush()
    return contract


def running_contract(db: Session, asset_id: str) -> Optional[RentalContract]:
    return db.execute(select(RentalContract).where(RentalContract.asset_id == asset_id, RentalContract.status == ContractStatus.RUNNING)
                      .order_by(RentalContract.start_date.desc()).limit(1)).scalar_one_or_none()


def take_back(db: Session, asset_id: str, *, reason: str = "planned", contract: Optional[RentalContract] = None,
              location_id: Optional[str] = None, actor: Optional[str] = None, today: Optional[date] = None) -> RentalContract:
    """A rented device comes back: its running contract ends and the device is in returns intake.

    A device without a running contract is refused: a return that ends nothing is a
    device the fleet never rented, and the return calendar could not have expected it.
    """
    today = today or date.today()
    if reason not in END_REASONS:
        raise ValidationError(f"unknown end reason {reason!r}; one of {', '.join(END_REASONS)}")
    contract = contract or running_contract(db, asset_id)
    if contract is None or contract.asset_id != asset_id:
        raise ValidationError("no running rental contract: a device that was never rented cannot come back")
    asset = _move(db, asset_id, AssetStatus.RETURNED, location_id=location_id, actor=actor,
                  note=f"rental {contract.cycle_no} ended ({reason})", today=today)
    contract.actual_end = today
    contract.end_reason = reason
    contract.status = ContractStatus.ENDED
    asset.customer_id = None
    db.flush()
    return contract


# ---------------------------------------------------------------------------
# the return chain


def book_in(db: Session, asset_id: str, *, location_id: Optional[str] = None, actor: Optional[str] = None,
            today: Optional[date] = None) -> Asset:
    """Returns intake is done: the device waits for the old customer's MDM release."""
    return _move(db, asset_id, AssetStatus.MDM_RELEASE, location_id=location_id, actor=actor, note="booked in, MDM release pending",
                 today=today or date.today())


def release(db: Session, asset_id: str, *, location_id: Optional[str] = None, actor: Optional[str] = None,
            today: Optional[date] = None) -> Asset:
    """The old customer released the device from its MDM: on to wipe and grading."""
    return _move(db, asset_id, AssetStatus.WIPE_GRADING, location_id=location_id, actor=actor, note="released from MDM",
                 today=today or date.today())


def next_after_grading(cycle_no: int, grade: str, *, recycle: bool = False) -> AssetStatus:
    """Where a graded device goes, per device: the rule behind ``fleet.NEXT_STEP_SHARE``."""
    if recycle:
        return AssetStatus.RECYCLED
    if int(cycle_no or 0) >= 2:
        return AssetStatus.SELLABLE
    if grade in ("A", "B"):
        return AssetStatus.REFURB
    if grade == "C":
        return AssetStatus.REPAIR
    return AssetStatus.SELLABLE


def grade(db: Session, asset_id: str, *, grade: str, recycle: bool = False, location_id: Optional[str] = None,
          actor: Optional[str] = None, today: Optional[date] = None) -> Asset:
    """Wipe and grading is done: the grade is recorded and the device takes its next step."""
    today = today or date.today()
    if grade not in GRADES:
        raise ValidationError(f"unknown grade {grade!r}; one of A, B, C, D")
    asset = asset_service.get_or_404(db, asset_id)
    target = next_after_grading(asset.cycle_no, grade, recycle=recycle)
    asset = _move(db, asset_id, target, location_id=location_id, actor=actor, note=f"graded {grade}", today=today)
    asset.grade = grade
    if target == AssetStatus.RECYCLED:
        _exit(asset, today)
    db.flush()
    return asset


def _service_event(db: Session, asset: Asset, kind: ServiceKind, cost: float, today: date) -> ServiceEvent:
    """The partner's invoice for one device, for the rental it prepares (the next one)."""
    if cost < 0:
        raise ValidationError("a service cost cannot be negative")
    ev = ServiceEvent(asset_id=asset.id, product_id=asset.product_id, kind=kind, cycle_no=int(asset.cycle_no or 0) + 1,
                      event_date=today, cost=Decimal(str(round(cost, 2))), currency="EUR")
    db.add(ev)
    return ev


def repair_done(db: Session, asset_id: str, *, cost: float, location_id: Optional[str] = None, actor: Optional[str] = None,
                today: Optional[date] = None) -> ServiceEvent:
    """The repair partner finished: the invoice is written and the device goes on to refurbishment."""
    today = today or date.today()
    asset = _move(db, asset_id, AssetStatus.REFURB, location_id=location_id, actor=actor, note="repair finished", today=today)
    ev = _service_event(db, asset, ServiceKind.REPAIR, cost, today)
    db.flush()
    return ev


def refurb_done(db: Session, asset_id: str, *, cost: float, location_id: Optional[str] = None, actor: Optional[str] = None,
                today: Optional[date] = None) -> ServiceEvent:
    """Refurbishment finished: the invoice is written and the device waits in the second-life stock."""
    today = today or date.today()
    asset = _move(db, asset_id, AssetStatus.READY_SECOND, location_id=location_id, actor=actor, note="refurbishment finished", today=today)
    ev = _service_event(db, asset, ServiceKind.REFURB, cost, today)
    db.flush()
    return ev


# ---------------------------------------------------------------------------
# the exit


def _exit(asset: Asset, today: date) -> None:
    """A device leaves the fleet: the exit date is what the resale and recycling reads key on."""
    asset.sold_date = today
    asset.customer_id = None
    asset.current_location_id = None


def sell(db: Session, asset_id: str, *, channel: str, price: Optional[float], actor: Optional[str] = None,
         today: Optional[date] = None) -> Asset:
    """A sale completes: net proceeds and channel are recorded on the serial. A sale without a
    known price is allowed and leaves the price empty, so the resale KPI counts it as unpriced
    rather than as zero."""
    today = today or date.today()
    if price is not None and price < 0:
        raise ValidationError("a sale price cannot be negative")
    asset = _move(db, asset_id, AssetStatus.SOLD, location_id=None, actor=actor, note=f"sold via {channel}", today=today)
    _exit(asset, today)
    asset.sale_price = Decimal(str(round(price, 2))) if price is not None else None
    asset.sale_channel = channel
    db.flush()
    return asset


def recycle(db: Session, asset_id: str, *, actor: Optional[str] = None, today: Optional[date] = None) -> Asset:
    """A device is recycled: the terminal exit, dated like a sale so the exit reads count it."""
    today = today or date.today()
    asset = _move(db, asset_id, AssetStatus.RECYCLED, location_id=None, actor=actor, note="recycled", today=today)
    _exit(asset, today)
    if asset.decommissioned_date is None:
        asset.decommissioned_date = today
    db.flush()
    return asset
