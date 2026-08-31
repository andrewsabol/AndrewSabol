"""Balance arithmetic.

The single rule this module exists to enforce: **generating a settlement never
moves a finalized balance.** Only admin verification does.

For any payable or receivable there are four distinct quantities, and conflating
any two of them corrupts the ledger:

``original``
    What was entered for this payroll period. Immutable once created (an admin
    correction writes a new amount *and* an audit row, never a silent edit).

``reserved``
    Sum of settlements that are routed but not yet verified. This is
    work-in-progress. It is not a reduction of what the user owes -- if the
    settlement is cancelled, this amount returns to ``available``.

``verified``
    Sum of admin-verified settlements. This is the only figure that discharges
    the obligation.

``remaining``
    ``original - verified``. What the user still genuinely owes or is owed.

``available``
    ``original - verified - reserved``. What the matching engine may still
    route. Always ``remaining - reserved``.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from .models import (
    FINALIZED_STATUSES,
    RESERVING_STATUSES,
    Payable,
    Receivable,
    Settlement,
)
from .money import ZERO, money


@dataclass(frozen=True)
class Balance:
    """A snapshot of one ledger entry's four quantities."""

    original: Decimal
    reserved: Decimal
    verified: Decimal

    @property
    def remaining(self) -> Decimal:
        """What is still genuinely owed: original minus verified."""
        return money(self.original - self.verified)

    @property
    def available(self) -> Decimal:
        """What may still be routed into a new settlement."""
        return money(self.original - self.verified - self.reserved)

    @property
    def is_settled(self) -> bool:
        return self.remaining <= ZERO

    @property
    def is_fully_assigned(self) -> bool:
        return self.available <= ZERO

    def __str__(self) -> str:  # pragma: no cover - debug helper
        return (
            f"Balance(original={self.original}, reserved={self.reserved}, "
            f"verified={self.verified}, remaining={self.remaining}, "
            f"available={self.available})"
        )


def _tally(settlements: list[Settlement]) -> tuple[Decimal, Decimal]:
    """Return ``(reserved, verified)`` across ``settlements``."""
    reserved = ZERO
    verified = ZERO
    for settlement in settlements:
        if settlement.status in RESERVING_STATUSES:
            reserved += settlement.amount
        elif settlement.status in FINALIZED_STATUSES:
            verified += settlement.amount
    return money(reserved), money(verified)


def payable_balance(payable: Payable) -> Balance:
    reserved, verified = _tally(list(payable.settlements))
    return Balance(
        original=money(payable.original_amount), reserved=reserved, verified=verified
    )


def receivable_balance(receivable: Receivable) -> Balance:
    reserved, verified = _tally(list(receivable.settlements))
    return Balance(
        original=money(receivable.original_amount),
        reserved=reserved,
        verified=verified,
    )


def sync_remaining(entry: Payable | Receivable) -> Decimal:
    """Recompute and persist ``entry.remaining_amount`` from its settlements.

    ``remaining_amount`` is a denormalized cache of ``original - verified``. It
    is refreshed here rather than decremented in place, so a mis-sequenced
    update can never drift the stored value away from the settlements that
    justify it.
    """
    balance = (
        payable_balance(entry) if isinstance(entry, Payable) else receivable_balance(entry)
    )
    entry.remaining_amount = balance.remaining

    from .models import LedgerStatus

    if entry.status is not LedgerStatus.CANCELLED:
        entry.status = (
            LedgerStatus.SETTLED if balance.is_settled else LedgerStatus.OPEN
        )
    return entry.remaining_amount


class InsufficientCapacity(ValueError):
    """Raised when a settlement would exceed a payable's or receivable's capacity."""


def assert_capacity(
    payable: Payable,
    receivable: Receivable,
    amount: Decimal,
    *,
    exclude_settlement_id: int | None = None,
) -> None:
    """Validate that ``amount`` can be routed from ``payable`` to ``receivable``.

    ``exclude_settlement_id`` lets a settlement be revalidated while ignoring
    its own current reservation -- needed when an admin edits an existing
    settlement in place.
    """
    amount = money(amount)
    if amount <= ZERO:
        raise InsufficientCapacity("settlement amount must be greater than zero")

    if payable.currency != receivable.currency:
        raise InsufficientCapacity(
            f"currency mismatch: payable is {payable.currency}, "
            f"receivable is {receivable.currency}"
        )

    payable_available = _available_excluding(payable, exclude_settlement_id)
    if amount > payable_available:
        raise InsufficientCapacity(
            f"{payable.user.label} has only {payable_available} unassigned "
            f"on payable #{payable.payable_id}, cannot route {amount}"
        )

    receivable_available = _available_excluding(receivable, exclude_settlement_id)
    if amount > receivable_available:
        raise InsufficientCapacity(
            f"{receivable.user.label} has only {receivable_available} unassigned "
            f"on receivable #{receivable.receivable_id}, cannot route {amount}"
        )


def _available_excluding(
    entry: Payable | Receivable, exclude_settlement_id: int | None
) -> Decimal:
    reserved = ZERO
    verified = ZERO
    for settlement in entry.settlements:
        if (
            exclude_settlement_id is not None
            and settlement.settlement_id == exclude_settlement_id
        ):
            continue
        if settlement.status in RESERVING_STATUSES:
            reserved += settlement.amount
        elif settlement.status in FINALIZED_STATUSES:
            verified += settlement.amount
    return money(entry.original_amount - verified - reserved)


@dataclass(frozen=True)
class UserBalanceSummary:
    """Aggregated view of one user's position within a batch."""

    user_id: int
    label: str
    owes: Balance | None
    owed: Balance | None

    @property
    def has_activity(self) -> bool:
        return self.owes is not None or self.owed is not None


def aggregate(balances: list[Balance]) -> Balance:
    """Combine several balances into one (used for per-user and batch totals)."""
    return Balance(
        original=money(sum((b.original for b in balances), ZERO)),
        reserved=money(sum((b.reserved for b in balances), ZERO)),
        verified=money(sum((b.verified for b in balances), ZERO)),
    )
