"""Settlement lifecycle: mark paid → confirm → verify, plus reassignment.

State machine::

    PENDING ──payer marks paid──> PAYER_MARKED_PAID
                                        │
                        recipient ──────┼────── recipient
                        confirms        │        denies
                                        ▼            ▼
                            RECIPIENT_CONFIRMED   RECIPIENT_DENIED
                                        │            │
                                        └──── admin ─┘
                                              │
                          ┌───────────────────┼───────────────────┐
                          ▼                   ▼                   ▼
                      VERIFIED            REJECTED            DISPUTED

Only the transition into ``VERIFIED`` moves finalized balances. Every other
transition is bookkeeping about a payment's *claimed* state.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import audit
from ..ledger import Balance, assert_capacity, payable_balance, receivable_balance, sync_remaining
from ..models import (
    AuditAction,
    BatchStatus,
    Payable,
    PaymentMethod,
    Receivable,
    Settlement,
    SettlementStatus,
    User,
    utcnow,
)
from ..money import ZERO, fmt, money


class SettlementError(RuntimeError):
    pass


#: Which statuses each transition may be applied from.
_ALLOWED_FROM = {
    SettlementStatus.PAYER_MARKED_PAID: {
        SettlementStatus.PENDING,
        SettlementStatus.RECIPIENT_DENIED,
        SettlementStatus.DISPUTED,
    },
    SettlementStatus.RECIPIENT_CONFIRMED: {
        SettlementStatus.PAYER_MARKED_PAID,
        SettlementStatus.RECIPIENT_DENIED,
        SettlementStatus.DISPUTED,
    },
    SettlementStatus.RECIPIENT_DENIED: {
        SettlementStatus.PAYER_MARKED_PAID,
        SettlementStatus.RECIPIENT_CONFIRMED,
    },
    SettlementStatus.VERIFIED: {
        SettlementStatus.PENDING,
        SettlementStatus.PAYER_MARKED_PAID,
        SettlementStatus.RECIPIENT_CONFIRMED,
        SettlementStatus.RECIPIENT_DENIED,
        SettlementStatus.DISPUTED,
    },
    SettlementStatus.REJECTED: {
        SettlementStatus.PENDING,
        SettlementStatus.PAYER_MARKED_PAID,
        SettlementStatus.RECIPIENT_CONFIRMED,
        SettlementStatus.RECIPIENT_DENIED,
        SettlementStatus.DISPUTED,
    },
    SettlementStatus.DISPUTED: {
        SettlementStatus.PENDING,
        SettlementStatus.PAYER_MARKED_PAID,
        SettlementStatus.RECIPIENT_CONFIRMED,
        SettlementStatus.RECIPIENT_DENIED,
    },
    SettlementStatus.CANCELLED: {
        SettlementStatus.PENDING,
        SettlementStatus.PAYER_MARKED_PAID,
        SettlementStatus.RECIPIENT_CONFIRMED,
        SettlementStatus.RECIPIENT_DENIED,
        SettlementStatus.DISPUTED,
    },
}


def _transition(settlement: Settlement, to_status: SettlementStatus) -> None:
    allowed = _ALLOWED_FROM.get(to_status, set())
    if settlement.status not in allowed:
        raise SettlementError(
            f"settlement #{settlement.settlement_id} cannot move from "
            f"{settlement.status.value} to {to_status.value}"
        )
    settlement.status = to_status


def _resync(session: Session, settlement: Settlement) -> None:
    """Refresh the cached remaining amounts on both linked ledger entries."""
    payable = session.get(Payable, settlement.payable_id)
    receivable = session.get(Receivable, settlement.receivable_id)
    if payable is not None:
        sync_remaining(payable)
    if receivable is not None:
        sync_remaining(receivable)


# --------------------------------------------------------------------------
# Payer actions
# --------------------------------------------------------------------------


def mark_paid(
    session: Session,
    settlement: Settlement,
    *,
    actor_user_id: int,
    transaction_reference: str | None = None,
    proof_file_id: str | None = None,
) -> Settlement:
    if actor_user_id != settlement.payer_user_id:
        raise SettlementError("only the payer may mark this settlement paid")

    _transition(settlement, SettlementStatus.PAYER_MARKED_PAID)
    settlement.payer_claimed_paid_at = utcnow()
    if transaction_reference:
        settlement.transaction_reference = transaction_reference
    if proof_file_id:
        settlement.proof_file_id = proof_file_id

    audit.record(
        session,
        AuditAction.PAYER_MARKED_PAID,
        actor_user_id=actor_user_id,
        batch_id=settlement.batch_id,
        entity_type="settlement",
        entity_id=settlement.settlement_id,
        amount=settlement.amount,
        detail=(
            f"{settlement.payer.label} claims paid "
            f"{fmt(settlement.amount, settlement.currency)} to "
            f"{settlement.recipient.label}"
            + (f" (ref {transaction_reference})" if transaction_reference else "")
            + (" [proof attached]" if proof_file_id else "")
        ),
    )
    return settlement


def attach_proof(
    session: Session,
    settlement: Settlement,
    *,
    actor_user_id: int,
    proof_file_id: str | None = None,
    transaction_reference: str | None = None,
) -> Settlement:
    """Add or replace proof on a settlement without changing its status."""
    if actor_user_id != settlement.payer_user_id:
        raise SettlementError("only the payer may attach proof")
    if proof_file_id:
        settlement.proof_file_id = proof_file_id
    if transaction_reference:
        settlement.transaction_reference = transaction_reference
    return settlement


# --------------------------------------------------------------------------
# Recipient actions
# --------------------------------------------------------------------------


def recipient_confirm(
    session: Session, settlement: Settlement, *, actor_user_id: int
) -> Settlement:
    if actor_user_id != settlement.recipient_user_id:
        raise SettlementError("only the recipient may confirm this settlement")

    _transition(settlement, SettlementStatus.RECIPIENT_CONFIRMED)
    settlement.recipient_confirmed_at = utcnow()
    audit.record(
        session,
        AuditAction.RECIPIENT_CONFIRMED,
        actor_user_id=actor_user_id,
        batch_id=settlement.batch_id,
        entity_type="settlement",
        entity_id=settlement.settlement_id,
        amount=settlement.amount,
        detail=(
            f"{settlement.recipient.label} confirms receipt of "
            f"{fmt(settlement.amount, settlement.currency)} from "
            f"{settlement.payer.label}"
        ),
    )
    return settlement


def recipient_deny(
    session: Session,
    settlement: Settlement,
    *,
    actor_user_id: int,
    reason: str | None = None,
) -> Settlement:
    if actor_user_id != settlement.recipient_user_id:
        raise SettlementError("only the recipient may deny this settlement")

    _transition(settlement, SettlementStatus.RECIPIENT_DENIED)
    settlement.recipient_confirmed_at = None
    audit.record(
        session,
        AuditAction.RECIPIENT_DENIED,
        actor_user_id=actor_user_id,
        batch_id=settlement.batch_id,
        entity_type="settlement",
        entity_id=settlement.settlement_id,
        amount=settlement.amount,
        detail=(
            f"{settlement.recipient.label} reports NOT received from "
            f"{settlement.payer.label}" + (f": {reason}" if reason else "")
        ),
    )
    return settlement


# --------------------------------------------------------------------------
# Admin actions
# --------------------------------------------------------------------------


def admin_verify(
    session: Session, settlement: Settlement, *, actor_user_id: int
) -> Settlement:
    """The only operation that discharges a payable and a receivable."""
    _transition(settlement, SettlementStatus.VERIFIED)
    settlement.admin_verified_at = utcnow()
    _resync(session, settlement)

    audit.record(
        session,
        AuditAction.ADMIN_VERIFIED,
        actor_user_id=actor_user_id,
        batch_id=settlement.batch_id,
        entity_type="settlement",
        entity_id=settlement.settlement_id,
        amount=settlement.amount,
        detail=(
            f"verified {settlement.payer.label} → {settlement.recipient.label} "
            f"{fmt(settlement.amount, settlement.currency)}"
        ),
    )
    maybe_complete_batch(session, settlement.batch_id, actor_user_id=actor_user_id)
    return settlement


def verify_partial(
    session: Session,
    settlement: Settlement,
    received_amount: Decimal,
    *,
    actor_user_id: int,
    reason: str | None = None,
) -> Settlement:
    """Verify a claimed payment for less than it was routed for.

    A settlement is routed for $500 but only $100 actually arrives. Verifying
    the whole thing would credit money nobody sent; rejecting it would discard
    the $100 that genuinely did.

    The settlement is reduced to what arrived and verified at that amount. The
    shortfall is *not* written off -- because both balances are derived from
    settlement amounts, cutting this settlement down returns the difference to
    the payer's and the recipient's available balances automatically, ready to
    be routed again. The recipient is still owed it; the payer still owes it.

    Use this to record what happened, never ``modify_balance``: changing the
    original amount would say the debt was always smaller, losing both the real
    figure and the fact that a payment was made.
    """
    received_amount = money(received_amount)
    if received_amount <= ZERO:
        raise SettlementError("received amount must be greater than zero")

    original = money(settlement.amount)
    if received_amount > original:
        raise SettlementError(
            f"received {fmt(received_amount, settlement.currency)} is more than "
            f"the {fmt(original, settlement.currency)} this settlement routes. "
            "Assign the extra separately."
        )
    if settlement.status is SettlementStatus.VERIFIED:
        raise SettlementError("this settlement is already verified")

    if received_amount == original:
        return admin_verify(session, settlement, actor_user_id=actor_user_id)

    shortfall = money(original - received_amount)
    settlement.amount = received_amount
    settlement.admin_notes = _append_note(
        settlement.admin_notes,
        f"partial: {fmt(received_amount, settlement.currency)} of "
        f"{fmt(original, settlement.currency)} received"
        + (f" ({reason})" if reason else ""),
    )

    _transition(settlement, SettlementStatus.VERIFIED)
    settlement.admin_verified_at = utcnow()
    _resync(session, settlement)

    audit.record(
        session,
        AuditAction.ADMIN_VERIFIED,
        actor_user_id=actor_user_id,
        batch_id=settlement.batch_id,
        entity_type="settlement",
        entity_id=settlement.settlement_id,
        amount=received_amount,
        detail=(
            f"partially verified {settlement.payer.label} → "
            f"{settlement.recipient.label}: "
            f"{fmt(received_amount, settlement.currency)} of "
            f"{fmt(original, settlement.currency)} received; "
            f"{fmt(shortfall, settlement.currency)} returned to both balances"
            + (f" ({reason})" if reason else "")
        ),
    )
    maybe_complete_batch(session, settlement.batch_id, actor_user_id=actor_user_id)
    return settlement


def record_payment_to(
    session: Session,
    batch_id: int,
    recipient_user_id: int,
    amount: Decimal,
    *,
    actor_user_id: int,
    reason: str | None = None,
) -> tuple[list[Settlement], Decimal]:
    """Credit ``amount`` against whatever is routed to this person.

    Spends the money across their open settlements oldest first, verifying each
    in full until the last, which is partially verified with whatever is left.
    Returns the settlements touched and any amount that could not be applied
    because nothing more is routed to them.

    This is the by-person entry point: an admin knows "@mike got $100", not
    which settlement id that lands on.
    """
    amount = money(amount)
    if amount <= ZERO:
        raise SettlementError("amount must be greater than zero")

    open_settlements = session.execute(
        select(Settlement)
        .where(
            Settlement.batch_id == batch_id,
            Settlement.recipient_user_id == recipient_user_id,
            Settlement.status.notin_(
                [
                    SettlementStatus.VERIFIED,
                    SettlementStatus.CANCELLED,
                    SettlementStatus.REJECTED,
                ]
            ),
        )
        .order_by(Settlement.settlement_id)
    ).scalars().all()

    touched: list[Settlement] = []
    remaining = amount

    for settlement in open_settlements:
        if remaining <= ZERO:
            break
        if settlement.amount <= remaining:
            remaining = money(remaining - settlement.amount)
            admin_verify(session, settlement, actor_user_id=actor_user_id)
        else:
            verify_partial(
                session, settlement, remaining, actor_user_id=actor_user_id, reason=reason
            )
            remaining = ZERO
        touched.append(settlement)

    return touched, remaining


def admin_reject(
    session: Session,
    settlement: Settlement,
    *,
    actor_user_id: int,
    reason: str | None = None,
) -> Settlement:
    """Reject a claimed payment. The amount returns to the pool for reassignment."""
    _transition(settlement, SettlementStatus.REJECTED)
    settlement.admin_notes = _append_note(settlement.admin_notes, reason)
    _resync(session, settlement)

    audit.record(
        session,
        AuditAction.SETTLEMENT_REJECTED,
        actor_user_id=actor_user_id,
        batch_id=settlement.batch_id,
        entity_type="settlement",
        entity_id=settlement.settlement_id,
        amount=settlement.amount,
        detail=(
            f"rejected {settlement.payer.label} → {settlement.recipient.label}"
            + (f": {reason}" if reason else "")
        ),
    )
    return settlement


def admin_dispute(
    session: Session,
    settlement: Settlement,
    *,
    actor_user_id: int,
    reason: str | None = None,
) -> Settlement:
    _transition(settlement, SettlementStatus.DISPUTED)
    settlement.admin_notes = _append_note(settlement.admin_notes, reason)
    audit.record(
        session,
        AuditAction.SETTLEMENT_DISPUTED,
        actor_user_id=actor_user_id,
        batch_id=settlement.batch_id,
        entity_type="settlement",
        entity_id=settlement.settlement_id,
        amount=settlement.amount,
        detail=(
            f"disputed {settlement.payer.label} → {settlement.recipient.label}"
            + (f": {reason}" if reason else "")
        ),
    )
    return settlement


def resolve_dispute(
    session: Session,
    settlement: Settlement,
    *,
    actor_user_id: int,
    outcome: SettlementStatus,
    reason: str | None = None,
) -> Settlement:
    """Close a dispute by verifying, rejecting, or cancelling it."""
    if settlement.status is not SettlementStatus.DISPUTED:
        raise SettlementError(
            f"settlement #{settlement.settlement_id} is not disputed"
        )
    if outcome not in (
        SettlementStatus.VERIFIED,
        SettlementStatus.REJECTED,
        SettlementStatus.CANCELLED,
    ):
        raise SettlementError(f"cannot resolve a dispute as {outcome.value}")

    audit.record(
        session,
        AuditAction.SETTLEMENT_RESOLVED,
        actor_user_id=actor_user_id,
        batch_id=settlement.batch_id,
        entity_type="settlement",
        entity_id=settlement.settlement_id,
        amount=settlement.amount,
        detail=f"dispute resolved as {outcome.value}" + (f": {reason}" if reason else ""),
    )

    if outcome is SettlementStatus.VERIFIED:
        return admin_verify(session, settlement, actor_user_id=actor_user_id)
    if outcome is SettlementStatus.REJECTED:
        return admin_reject(
            session, settlement, actor_user_id=actor_user_id, reason=reason
        )
    return cancel(session, settlement, actor_user_id=actor_user_id, reason=reason)


def cancel(
    session: Session,
    settlement: Settlement,
    *,
    actor_user_id: int,
    reason: str | None = None,
) -> Settlement:
    """Cancel a settlement, releasing its reservation back to both sides.

    This is what makes "if the other $400 settlement is cancelled, that $400
    becomes available for reassignment" true: the reservation is derived from
    settlement status, so flipping the status frees the capacity with no balance
    arithmetic to get wrong.
    """
    if settlement.status is SettlementStatus.VERIFIED:
        raise SettlementError(
            "a verified settlement cannot be cancelled; reject or dispute it instead"
        )
    _transition(settlement, SettlementStatus.CANCELLED)
    settlement.admin_notes = _append_note(settlement.admin_notes, reason)
    _resync(session, settlement)

    audit.record(
        session,
        AuditAction.SETTLEMENT_CANCELLED,
        actor_user_id=actor_user_id,
        batch_id=settlement.batch_id,
        entity_type="settlement",
        entity_id=settlement.settlement_id,
        amount=settlement.amount,
        detail=(
            f"cancelled {settlement.payer.label} → {settlement.recipient.label}"
            + (f": {reason}" if reason else "")
        ),
    )
    return settlement


def reassign(
    session: Session,
    settlement: Settlement,
    *,
    actor_user_id: int,
    new_recipient_receivable_id: int | None = None,
    new_payer_payable_id: int | None = None,
    new_amount: Decimal | None = None,
    reason: str | None = None,
) -> Settlement:
    """Manually re-route a settlement.

    Lets an admin replace "John → Mike: $500" with "John → Sarah: $500",
    provided the underlying payer and recipient balances still make the
    settlement mathematically valid. Capacity is rechecked against the *new*
    counterparties, excluding this settlement's own existing reservation.
    """
    if settlement.status is SettlementStatus.VERIFIED:
        raise SettlementError("a verified settlement cannot be reassigned")
    if settlement.status in (SettlementStatus.CANCELLED, SettlementStatus.REJECTED):
        raise SettlementError(
            f"settlement #{settlement.settlement_id} is {settlement.status.value.lower()}"
        )

    payable = session.get(
        Payable, new_payer_payable_id or settlement.payable_id
    )
    receivable = session.get(
        Receivable, new_recipient_receivable_id or settlement.receivable_id
    )
    if payable is None or receivable is None:
        raise SettlementError("target payable or receivable does not exist")
    if payable.batch_id != settlement.batch_id or receivable.batch_id != settlement.batch_id:
        raise SettlementError("cannot reassign across payroll batches")
    if payable.user_id == receivable.user_id:
        raise SettlementError("payer and recipient must be different people")

    amount = money(new_amount) if new_amount is not None else money(settlement.amount)
    assert_capacity(
        payable, receivable, amount, exclude_settlement_id=settlement.settlement_id
    )

    before = (
        f"{settlement.payer.label} → {settlement.recipient.label} "
        f"{fmt(settlement.amount, settlement.currency)}"
    )

    # Move through the relationships so both the old and new ledger entries'
    # ``settlements`` collections stay accurate; assigning the bare foreign keys
    # would leave the reservation attached to the previous counterparty in
    # memory.
    settlement.payable = payable
    settlement.payer = payable.user
    settlement.payer_user_id = payable.user_id
    settlement.receivable = receivable
    settlement.recipient = receivable.user
    settlement.recipient_user_id = receivable.user_id
    settlement.amount = amount

    # Re-derive the payment method and the review flag for the new pairing.
    payer_methods = _active_method_kinds(session, payable.user_id)
    recipient_methods = _active_method_kinds(session, receivable.user_id)
    shared = payer_methods & recipient_methods
    method = _first_method(session, receivable.user_id, sorted(shared)[0] if shared else None)
    settlement.payment_method_id = method.payment_method_id if method else None
    settlement.payment_method_note = method.display if method else None
    settlement.needs_admin_review = not shared

    # A re-routed payment is a different instruction; any prior claim of payment
    # no longer refers to it.
    settlement.status = SettlementStatus.PENDING
    settlement.payer_claimed_paid_at = None
    settlement.recipient_confirmed_at = None
    settlement.transaction_reference = None
    settlement.proof_file_id = None

    session.flush()
    _resync(session, settlement)

    after = (
        f"{payable.user.label} → {receivable.user.label} "
        f"{fmt(amount, settlement.currency)}"
    )
    audit.record(
        session,
        AuditAction.SETTLEMENT_REASSIGNED,
        actor_user_id=actor_user_id,
        batch_id=settlement.batch_id,
        entity_type="settlement",
        entity_id=settlement.settlement_id,
        amount=amount,
        detail=f"{before} → {after}" + (f" ({reason})" if reason else ""),
    )
    return settlement


def _active_method_kinds(session: Session, user_id: int) -> frozenset[str]:
    rows = session.execute(
        select(PaymentMethod).where(
            PaymentMethod.user_id == user_id, PaymentMethod.is_active.is_(True)
        )
    ).scalars().all()
    return frozenset(r.kind.value for r in rows)


def _first_method(
    session: Session, user_id: int, preferred_kind: str | None
) -> PaymentMethod | None:
    rows = session.execute(
        select(PaymentMethod).where(
            PaymentMethod.user_id == user_id, PaymentMethod.is_active.is_(True)
        )
    ).scalars().all()
    if not rows:
        return None
    if preferred_kind:
        for row in rows:
            if row.kind.value == preferred_kind:
                return row
    return rows[0]


def _append_note(existing: str | None, addition: str | None) -> str | None:
    if not addition:
        return existing
    stamp = utcnow().strftime("%Y-%m-%d %H:%M")
    line = f"[{stamp}] {addition}"
    return f"{existing}\n{line}" if existing else line


def maybe_complete_batch(
    session: Session, batch_id: int, *, actor_user_id: int | None = None
) -> None:
    """Mark a batch COMPLETED once every ledger entry is fully discharged."""
    from ..models import PayrollBatch
    from .payroll import open_payables, open_receivables, set_batch_status

    batch = session.get(PayrollBatch, batch_id)
    if batch is None or batch.status is not BatchStatus.IN_PROGRESS:
        return

    entries: list[Payable | Receivable] = [
        *open_payables(session, batch),
        *open_receivables(session, batch),
    ]
    if not entries:
        return

    for entry in entries:
        balance = (
            payable_balance(entry)
            if isinstance(entry, Payable)
            else receivable_balance(entry)
        )
        if balance.remaining > ZERO:
            return

    set_batch_status(session, batch, BatchStatus.COMPLETED, actor_user_id=actor_user_id)


# --------------------------------------------------------------------------
# Per-user views
# --------------------------------------------------------------------------


@dataclass
class UserPosition:
    """One user's full position in a batch, as shown by the admin user view."""

    user: User
    owes: Balance | None
    owed: Balance | None
    outgoing: list[Settlement]
    incoming: list[Settlement]


def user_position(session: Session, batch_id: int, user_id: int) -> UserPosition:
    from ..ledger import aggregate

    user = session.get(User, user_id)
    if user is None:
        raise SettlementError(f"no such user: {user_id}")

    payables = session.execute(
        select(Payable).where(
            Payable.batch_id == batch_id, Payable.user_id == user_id
        )
    ).scalars().all()
    receivables = session.execute(
        select(Receivable).where(
            Receivable.batch_id == batch_id, Receivable.user_id == user_id
        )
    ).scalars().all()

    outgoing = session.execute(
        select(Settlement)
        .where(
            Settlement.batch_id == batch_id,
            Settlement.payer_user_id == user_id,
            Settlement.status.notin_(
                [SettlementStatus.CANCELLED, SettlementStatus.REJECTED]
            ),
        )
        .order_by(Settlement.settlement_id)
    ).scalars().all()
    incoming = session.execute(
        select(Settlement)
        .where(
            Settlement.batch_id == batch_id,
            Settlement.recipient_user_id == user_id,
            Settlement.status.notin_(
                [SettlementStatus.CANCELLED, SettlementStatus.REJECTED]
            ),
        )
        .order_by(Settlement.settlement_id)
    ).scalars().all()

    return UserPosition(
        user=user,
        owes=aggregate([payable_balance(p) for p in payables]) if payables else None,
        owed=aggregate([receivable_balance(r) for r in receivables])
        if receivables
        else None,
        outgoing=list(outgoing),
        incoming=list(incoming),
    )


def settlements_awaiting_verification(
    session: Session, batch_id: int
) -> list[Settlement]:
    return list(
        session.execute(
            select(Settlement)
            .where(
                Settlement.batch_id == batch_id,
                Settlement.status.in_(
                    [
                        SettlementStatus.PAYER_MARKED_PAID,
                        SettlementStatus.RECIPIENT_CONFIRMED,
                        SettlementStatus.RECIPIENT_DENIED,
                    ]
                ),
            )
            .order_by(Settlement.payer_claimed_paid_at)
        ).scalars().all()
    )


def disputed_settlements(session: Session, batch_id: int) -> list[Settlement]:
    return list(
        session.execute(
            select(Settlement)
            .where(
                Settlement.batch_id == batch_id,
                Settlement.status == SettlementStatus.DISPUTED,
            )
            .order_by(Settlement.settlement_id)
        ).scalars().all()
    )
