"""The payment queue: who gets paid next, and in what order.

People are paid in the order they were added to the payroll. A payroll does not
have to balance -- someone can be owed money before any payer has settled up --
so the queue is what turns "more owed out than in" from a problem into a
schedule. Whoever has waited longest is first in line for whatever money
arrives.

Position is *derived* from creation order rather than stored, so it can never
drift out of step with the ledger: pay someone off and everyone behind them
moves up automatically, with no field to update.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from .ledger import Balance, receivable_balance
from .models import (
    LedgerStatus,
    PaymentMethod,
    Receivable,
    Settlement,
    SettlementStatus,
    User,
)
from .money import ZERO, money


@dataclass
class QueueEntry:
    """One person's place in line."""

    position: int
    """1-based, among everyone still owed money."""

    user: User
    receivable: Receivable
    balance: Balance
    payment_methods: list[PaymentMethod]
    incoming: list[Settlement]
    """Settlements already routed to this person and not yet verified."""

    @property
    def still_owed(self) -> Decimal:
        """What this person is owed after verified payments."""
        return self.balance.remaining

    @property
    def unassigned(self) -> Decimal:
        """What is owed but not yet routed to any payer -- the queue's real need."""
        return self.balance.available

    @property
    def waiting_since(self) -> datetime:
        return self.receivable.created_at

    def waited_days(self, *, now: datetime | None = None) -> int:
        now = now or datetime.now(timezone.utc)
        created = self.receivable.created_at
        # Rows written before timezone awareness was consistent read back naive.
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        return max((now - created).days, 0)

    def method_kinds(self) -> frozenset[str]:
        return frozenset(m.kind.value for m in self.payment_methods)

    def shares_method_with(self, payer_methods: frozenset[str]) -> frozenset[str]:
        return self.method_kinds() & payer_methods


def build_queue(
    session: Session, batch_id: int, *, include_fully_assigned: bool = True
) -> list[QueueEntry]:
    """Everyone still owed money in this batch, longest wait first.

    ``include_fully_assigned`` keeps people whose full amount is already routed
    to a payer but not yet verified. They are still owed, so they stay visible
    in the queue -- just with nothing left to assign.
    """
    receivables = session.execute(
        select(Receivable)
        .where(
            Receivable.batch_id == batch_id,
            Receivable.status != LedgerStatus.CANCELLED,
        )
        .order_by(Receivable.created_at, Receivable.receivable_id)
    ).scalars().all()

    entries: list[QueueEntry] = []
    position = 0
    for receivable in receivables:
        balance = receivable_balance(receivable)
        if balance.remaining <= ZERO:
            continue  # Fully paid and verified; out of the queue.
        if not include_fully_assigned and balance.available <= ZERO:
            continue

        position += 1
        entries.append(
            QueueEntry(
                position=position,
                user=receivable.user,
                receivable=receivable,
                balance=balance,
                payment_methods=_active_methods(session, receivable.user_id),
                incoming=[
                    s
                    for s in receivable.settlements
                    if s.status
                    not in (SettlementStatus.CANCELLED, SettlementStatus.REJECTED)
                ],
            )
        )
    return entries


def queue_length(session: Session, batch_id: int) -> int:
    return len(build_queue(session, batch_id))


def entry_at(
    session: Session, batch_id: int, index: int
) -> tuple[QueueEntry | None, int]:
    """The queue entry at ``index`` (0-based), wrapping around.

    Returns the entry and the total queue length. Wrapping is what makes the
    skip button a carousel: stepping past the last person returns to the first,
    so an admin can cycle the whole queue looking for someone the payer can
    actually pay, and never hits a dead end.
    """
    entries = build_queue(session, batch_id)
    if not entries:
        return None, 0
    return entries[index % len(entries)], len(entries)


def next_payable_to(
    session: Session, batch_id: int, *, payer_methods: frozenset[str] | None = None
) -> QueueEntry | None:
    """The person at the front of the queue who can still be assigned money.

    When ``payer_methods`` is given, prefers the first person in line who shares
    a payment method with the payer -- honouring the queue while skipping people
    this particular payer has no way to pay.
    """
    entries = [e for e in build_queue(session, batch_id) if e.unassigned > ZERO]
    if not entries:
        return None
    if payer_methods:
        for entry in entries:
            if entry.shares_method_with(payer_methods):
                return entry
    return entries[0]


def total_outstanding(session: Session, batch_id: int) -> Decimal:
    """Everything still owed out across the whole queue."""
    return money(sum((e.still_owed for e in build_queue(session, batch_id)), ZERO))


def _active_methods(session: Session, user_id: int) -> list[PaymentMethod]:
    return list(
        session.execute(
            select(PaymentMethod)
            .where(
                PaymentMethod.user_id == user_id,
                PaymentMethod.is_active.is_(True),
            )
            .order_by(PaymentMethod.payment_method_id)
        ).scalars().all()
    )
