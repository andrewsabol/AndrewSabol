"""Payroll batch lifecycle: create, enter balances, plan, approve."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import audit
from ..ledger import (
    Balance,
    aggregate,
    assert_capacity,
    payable_balance,
    receivable_balance,
    sync_remaining,
)
from ..matching import MatchingEngine, MatchResult
from ..models import (
    AuditAction,
    BatchStatus,
    LedgerStatus,
    Payable,
    PaymentMethod,
    PayrollBatch,
    Receivable,
    Settlement,
    SettlementStatus,
    User,
    utcnow,
)
from ..money import ZERO, fmt, money
from ..parsing import ParsedPayroll
from ..strategies import Party, SettlementStrategy


class PayrollError(RuntimeError):
    pass


# --------------------------------------------------------------------------
# Users
# --------------------------------------------------------------------------


def get_or_create_user(
    session: Session,
    *,
    username: str | None = None,
    telegram_id: int | None = None,
    display_name: str | None = None,
) -> User:
    """Look a user up by telegram id, falling back to username.

    Payroll is usually entered by handle before those people have ever opened
    the bot, so a user row can exist with a username and no telegram id; it is
    linked up on their first ``/start``.
    """
    user: User | None = None
    if telegram_id is not None:
        user = session.execute(
            select(User).where(User.telegram_id == telegram_id)
        ).scalar_one_or_none()

    if user is None and username:
        user = session.execute(
            select(User).where(User.username == username.lower().lstrip("@"))
        ).scalar_one_or_none()

    if user is None:
        user = User(
            telegram_id=telegram_id,
            username=username.lower().lstrip("@") if username else None,
            display_name=display_name,
        )
        session.add(user)
        session.flush()
        return user

    # Backfill identifiers learned since the row was created.
    if telegram_id is not None and user.telegram_id is None:
        user.telegram_id = telegram_id
    if username and not user.username:
        user.username = username.lower().lstrip("@")
    if display_name and not user.display_name:
        user.display_name = display_name
    return user


# --------------------------------------------------------------------------
# Batches
# --------------------------------------------------------------------------


def create_batch(
    session: Session,
    *,
    label: str | None = None,
    currency: str = "USD",
    actor_user_id: int | None = None,
) -> PayrollBatch:
    label = label or _default_label(session)
    existing = session.execute(
        select(PayrollBatch).where(PayrollBatch.label == label)
    ).scalar_one_or_none()
    if existing is not None:
        raise PayrollError(f"payroll batch “{label}” already exists")

    batch = PayrollBatch(
        label=label,
        currency=currency,
        status=BatchStatus.DRAFT,
        created_by_user_id=actor_user_id,
    )
    session.add(batch)
    session.flush()
    audit.record(
        session,
        AuditAction.PAYROLL_CREATED,
        actor_user_id=actor_user_id,
        batch_id=batch.batch_id,
        entity_type="batch",
        entity_id=batch.batch_id,
        detail=f"created payroll {label} ({currency})",
    )
    return batch


def _default_label(session: Session) -> str:
    base = date.today().isoformat()
    label = base
    suffix = 2
    while session.execute(
        select(PayrollBatch).where(PayrollBatch.label == label)
    ).scalar_one_or_none():
        label = f"{base}-{suffix}"
        suffix += 1
    return label


def active_batch(session: Session) -> PayrollBatch | None:
    """The batch admins are currently working in: newest not-yet-closed batch."""
    return session.execute(
        select(PayrollBatch)
        .where(
            PayrollBatch.status.in_(
                [BatchStatus.DRAFT, BatchStatus.PENDING_APPROVAL, BatchStatus.IN_PROGRESS]
            )
        )
        .order_by(PayrollBatch.created_at.desc(), PayrollBatch.batch_id.desc())
    ).scalars().first()


def set_batch_status(
    session: Session,
    batch: PayrollBatch,
    status: BatchStatus,
    *,
    actor_user_id: int | None = None,
) -> None:
    previous = batch.status
    if previous is status:
        return
    batch.status = status
    if status is BatchStatus.COMPLETED:
        batch.completed_at = utcnow()
    audit.record(
        session,
        AuditAction.BATCH_STATUS_CHANGED,
        actor_user_id=actor_user_id,
        batch_id=batch.batch_id,
        entity_type="batch",
        entity_id=batch.batch_id,
        detail=f"{previous.value} → {status.value}",
    )


# --------------------------------------------------------------------------
# Ledger entry
# --------------------------------------------------------------------------


def add_payable(
    session: Session,
    batch: PayrollBatch,
    user: User,
    amount: Decimal,
    *,
    description: str | None = None,
    actor_user_id: int | None = None,
) -> Payable:
    amount = money(amount)
    if amount <= ZERO:
        raise PayrollError("payable amount must be greater than zero")

    payable = Payable(
        batch_id=batch.batch_id,
        user_id=user.user_id,
        original_amount=amount,
        remaining_amount=amount,
        currency=batch.currency,
        description=description,
    )
    session.add(payable)
    session.flush()
    audit.record(
        session,
        AuditAction.PAYABLE_ADDED,
        actor_user_id=actor_user_id,
        batch_id=batch.batch_id,
        entity_type="payable",
        entity_id=payable.payable_id,
        amount=amount,
        detail=f"{user.label} owes {fmt(amount, batch.currency)}",
    )
    return payable


def add_receivable(
    session: Session,
    batch: PayrollBatch,
    user: User,
    amount: Decimal,
    *,
    description: str | None = None,
    actor_user_id: int | None = None,
) -> Receivable:
    amount = money(amount)
    if amount <= ZERO:
        raise PayrollError("receivable amount must be greater than zero")

    receivable = Receivable(
        batch_id=batch.batch_id,
        user_id=user.user_id,
        original_amount=amount,
        remaining_amount=amount,
        currency=batch.currency,
        description=description,
    )
    session.add(receivable)
    session.flush()
    audit.record(
        session,
        AuditAction.RECEIVABLE_ADDED,
        actor_user_id=actor_user_id,
        batch_id=batch.batch_id,
        entity_type="receivable",
        entity_id=receivable.receivable_id,
        amount=amount,
        detail=f"{user.label} is owed {fmt(amount, batch.currency)}",
    )
    return receivable


def apply_parsed_payroll(
    session: Session,
    batch: PayrollBatch,
    parsed: ParsedPayroll,
    *,
    actor_user_id: int | None = None,
) -> tuple[list[Payable], list[Receivable]]:
    """Persist a parsed OWES/OWED block into the batch.

    Refuses to write anything if the block had parse errors -- a half-applied
    payroll is worse than a rejected one.
    """
    if parsed.errors:
        raise PayrollError(
            "cannot apply a payroll with parse errors: "
            + "; ".join(str(e) for e in parsed.errors)
        )

    payables: list[Payable] = []
    receivables: list[Receivable] = []

    for entry in parsed.payables:
        user = get_or_create_user(session, username=entry.handle)
        payables.append(
            add_payable(
                session,
                batch,
                user,
                entry.amount,
                description=entry.description,
                actor_user_id=actor_user_id,
            )
        )

    for entry in parsed.receivables:
        user = get_or_create_user(session, username=entry.handle)
        receivables.append(
            add_receivable(
                session,
                batch,
                user,
                entry.amount,
                description=entry.description,
                actor_user_id=actor_user_id,
            )
        )

    return payables, receivables


def modify_balance(
    session: Session,
    entry: Payable | Receivable,
    new_amount: Decimal,
    *,
    actor_user_id: int | None = None,
    reason: str | None = None,
) -> None:
    """Change an entered balance, with an audit row explaining the change.

    Refuses to shrink an entry below what its settlements already reserve or
    have verified -- that would leave the ledger internally inconsistent.
    """
    new_amount = money(new_amount)
    if new_amount <= ZERO:
        raise PayrollError("balance must be greater than zero; cancel the entry instead")

    balance = (
        payable_balance(entry) if isinstance(entry, Payable) else receivable_balance(entry)
    )
    committed = money(balance.verified + balance.reserved)
    if new_amount < committed:
        raise PayrollError(
            f"cannot reduce to {fmt(new_amount, entry.currency)}: "
            f"{fmt(committed, entry.currency)} is already verified or assigned. "
            "Cancel the affected settlements first."
        )

    previous = money(entry.original_amount)
    entry.original_amount = new_amount
    sync_remaining(entry)

    is_payable = isinstance(entry, Payable)
    audit.record(
        session,
        AuditAction.BALANCE_MODIFIED,
        actor_user_id=actor_user_id,
        batch_id=entry.batch_id,
        entity_type="payable" if is_payable else "receivable",
        entity_id=entry.payable_id if is_payable else entry.receivable_id,
        amount=new_amount,
        detail=(
            f"{entry.user.label}: {fmt(previous, entry.currency)} → "
            f"{fmt(new_amount, entry.currency)}"
            + (f" ({reason})" if reason else "")
        ),
    )


# --------------------------------------------------------------------------
# Balance reporting
# --------------------------------------------------------------------------


@dataclass
class BatchTotals:
    payable: Balance
    receivable: Balance
    people_owing: int
    people_owed: int
    settlement_count: int
    awaiting_verification: int
    disputed: int
    flagged: int

    @property
    def balances(self) -> bool:
        return self.payable.original == self.receivable.original

    @property
    def difference(self) -> Decimal:
        return money(self.payable.original - self.receivable.original)


def batch_totals(session: Session, batch: PayrollBatch) -> BatchTotals:
    payables = open_payables(session, batch)
    receivables = open_receivables(session, batch)
    settlements = session.execute(
        select(Settlement).where(Settlement.batch_id == batch.batch_id)
    ).scalars().all()

    awaiting = sum(
        1
        for s in settlements
        if s.status
        in (
            SettlementStatus.PAYER_MARKED_PAID,
            SettlementStatus.RECIPIENT_CONFIRMED,
            SettlementStatus.RECIPIENT_DENIED,
        )
    )
    disputed = sum(1 for s in settlements if s.status is SettlementStatus.DISPUTED)
    flagged = sum(
        1
        for s in settlements
        if s.needs_admin_review and s.status in (SettlementStatus.PENDING,)
    )
    active = [
        s
        for s in settlements
        if s.status
        not in (SettlementStatus.CANCELLED, SettlementStatus.REJECTED)
    ]

    return BatchTotals(
        payable=aggregate([payable_balance(p) for p in payables]),
        receivable=aggregate([receivable_balance(r) for r in receivables]),
        people_owing=len({p.user_id for p in payables}),
        people_owed=len({r.user_id for r in receivables}),
        settlement_count=len(active),
        awaiting_verification=awaiting,
        disputed=disputed,
        flagged=flagged,
    )


def open_payables(session: Session, batch: PayrollBatch) -> list[Payable]:
    return list(
        session.execute(
            select(Payable)
            .where(Payable.batch_id == batch.batch_id)
            .where(Payable.status != LedgerStatus.CANCELLED)
            .order_by(Payable.payable_id)
        ).scalars().all()
    )


def open_receivables(session: Session, batch: PayrollBatch) -> list[Receivable]:
    return list(
        session.execute(
            select(Receivable)
            .where(Receivable.batch_id == batch.batch_id)
            .where(Receivable.status != LedgerStatus.CANCELLED)
            .order_by(Receivable.receivable_id)
        ).scalars().all()
    )


# --------------------------------------------------------------------------
# Plan generation
# --------------------------------------------------------------------------


def _methods_for(session: Session, user_id: int) -> frozenset[str]:
    rows = session.execute(
        select(PaymentMethod).where(
            PaymentMethod.user_id == user_id, PaymentMethod.is_active.is_(True)
        )
    ).scalars().all()
    return frozenset(m.kind.value for m in rows)


def build_parties(
    session: Session, batch: PayrollBatch
) -> tuple[list[Party], list[Party]]:
    """Convert persisted ledger entries into matching-engine parties.

    ``available`` is the *unassigned* capacity, so regenerating a plan while
    some settlements are already verified or in flight routes only what is
    genuinely left rather than double-assigning.
    """
    payers: list[Party] = []
    for payable in open_payables(session, batch):
        balance = payable_balance(payable)
        if balance.available <= ZERO:
            continue
        payers.append(
            Party(
                user_id=payable.user_id,
                label=payable.user.label,
                entry_id=payable.payable_id,
                available=balance.available,
                currency=payable.currency,
                payment_methods=_methods_for(session, payable.user_id),
                priority=payable.user.priority,
            )
        )

    recipients: list[Party] = []
    for receivable in open_receivables(session, batch):
        balance = receivable_balance(receivable)
        if balance.available <= ZERO:
            continue
        recipients.append(
            Party(
                user_id=receivable.user_id,
                label=receivable.user.label,
                entry_id=receivable.receivable_id,
                available=balance.available,
                currency=receivable.currency,
                payment_methods=_methods_for(session, receivable.user_id),
                priority=receivable.user.priority,
            )
        )

    return payers, recipients


def generate_plan(
    session: Session,
    batch: PayrollBatch,
    *,
    strategy: SettlementStrategy | None = None,
) -> MatchResult:
    """Compute a settlement plan **without persisting anything**.

    Nothing reaches a user until an admin approves; this returns a preview only.
    """
    payers, recipients = build_parties(session, batch)
    engine = MatchingEngine(strategy)
    return engine.match(payers, recipients)


def approve_plan(
    session: Session,
    batch: PayrollBatch,
    result: MatchResult,
    *,
    actor_user_id: int | None = None,
) -> list[Settlement]:
    """Persist an approved plan as PENDING settlements.

    Each proposal is re-validated against live capacity before it is written:
    the preview may have been generated minutes ago, and a settlement verified
    in the meantime must not be double-counted.
    """
    created: list[Settlement] = []

    for proposal in result.proposals:
        payable = session.get(Payable, proposal.payable_id)
        receivable = session.get(Receivable, proposal.receivable_id)
        if payable is None or receivable is None:
            raise PayrollError(
                f"plan references a ledger entry that no longer exists "
                f"(payable #{proposal.payable_id}, receivable #{proposal.receivable_id})"
            )

        assert_capacity(payable, receivable, proposal.amount)

        method = _pick_method(session, receivable.user_id, proposal.shared_method)
        settlement = Settlement(
            batch_id=batch.batch_id,
            payer_user_id=proposal.payer_user_id,
            recipient_user_id=proposal.recipient_user_id,
            amount=proposal.amount,
            currency=proposal.currency,
            # Assign through the relationships, not the raw foreign keys: the
            # payable and receivable are already loaded here, and setting only
            # the id would leave their ``settlements`` collections stale, so the
            # next balance read would not see this reservation.
            payable=payable,
            receivable=receivable,
            payment_method_id=method.payment_method_id if method else None,
            payment_method_note=method.display if method else None,
            needs_admin_review=proposal.needs_admin_review,
            status=SettlementStatus.PENDING,
        )
        session.add(settlement)
        session.flush()
        created.append(settlement)

        audit.record(
            session,
            AuditAction.SETTLEMENT_GENERATED,
            actor_user_id=actor_user_id,
            batch_id=batch.batch_id,
            entity_type="settlement",
            entity_id=settlement.settlement_id,
            amount=settlement.amount,
            detail=(
                f"{proposal.payer_label} → {proposal.recipient_label} "
                f"{fmt(proposal.amount, proposal.currency)} "
                f"(payable #{payable.payable_id} / receivable #{receivable.receivable_id})"
                + (" [needs review: no shared payment method]" if proposal.needs_admin_review else "")
            ),
        )

    batch.approved_at = utcnow()
    set_batch_status(session, batch, BatchStatus.IN_PROGRESS, actor_user_id=actor_user_id)
    audit.record(
        session,
        AuditAction.SETTLEMENT_PLAN_APPROVED,
        actor_user_id=actor_user_id,
        batch_id=batch.batch_id,
        entity_type="batch",
        entity_id=batch.batch_id,
        amount=result.total_routed,
        detail=f"approved {len(created)} settlements via {result.strategy_name}",
    )
    return created


def _pick_method(
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
