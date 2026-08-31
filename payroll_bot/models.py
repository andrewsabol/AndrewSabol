"""Database schema for the payroll settlement ledger.

Three record types carry the accounting, and the distinction between them is
the whole point of the system:

* :class:`Payable` -- "this user owes $X into the payroll system".
* :class:`Receivable` -- "this user is owed $Y by the payroll system".
* :class:`Settlement` -- "this user should send $X directly to this recipient".

A settlement is only a *routing instruction* used to satisfy a payable and a
receivable. It is never the underlying debt. John does not owe Mike; John owes
the payroll system, Mike is owed by the payroll system, and a settlement is how
those two facts get discharged with one bank transfer instead of two.
"""

from __future__ import annotations

import enum
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Enum as SAEnum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    TypeDecorator,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from .money import ZERO, money


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class MoneyType(TypeDecorator):
    """Stores money as exact integer minor units, presents it as ``Decimal``.

    SQLAlchemy's ``Numeric`` on SQLite round-trips through ``float``, which is
    precisely the failure mode this system must not have. Integer cents keep
    the value exact both in Python and in SQL aggregate functions.
    """

    impl = BigInteger
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        return int(money(value).scaleb(2).to_integral_value())

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        return money(Decimal(value).scaleb(-2))


class Base(DeclarativeBase):
    pass


# --------------------------------------------------------------------------
# Enumerations
# --------------------------------------------------------------------------


class BatchStatus(str, enum.Enum):
    DRAFT = "DRAFT"
    """Balances are being entered; nothing has been routed to users."""

    PENDING_APPROVAL = "PENDING_APPROVAL"
    """A settlement plan exists but the admin has not approved it."""

    IN_PROGRESS = "IN_PROGRESS"
    """Approved and released to users; payments are being made."""

    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"


class LedgerStatus(str, enum.Enum):
    OPEN = "OPEN"
    """Some portion is still unsatisfied."""

    SETTLED = "SETTLED"
    """Fully discharged by verified settlements."""

    CANCELLED = "CANCELLED"


class SettlementStatus(str, enum.Enum):
    PENDING = "PENDING"
    """Generated and approved; payer has not yet claimed payment."""

    PAYER_MARKED_PAID = "PAYER_MARKED_PAID"
    RECIPIENT_CONFIRMED = "RECIPIENT_CONFIRMED"
    RECIPIENT_DENIED = "RECIPIENT_DENIED"
    VERIFIED = "VERIFIED"
    """Admin-verified. This is the *only* status that moves finalized balances."""

    REJECTED = "REJECTED"
    DISPUTED = "DISPUTED"
    CANCELLED = "CANCELLED"


#: Statuses in which a settlement still reserves capacity against its payable
#: and receivable, but has not yet been verified.
RESERVING_STATUSES = frozenset(
    {
        SettlementStatus.PENDING,
        SettlementStatus.PAYER_MARKED_PAID,
        SettlementStatus.RECIPIENT_CONFIRMED,
        SettlementStatus.RECIPIENT_DENIED,
        SettlementStatus.DISPUTED,
    }
)

#: Statuses that release their reservation -- the amount returns to the pool
#: and becomes available for reassignment.
RELEASED_STATUSES = frozenset(
    {
        SettlementStatus.REJECTED,
        SettlementStatus.CANCELLED,
    }
)

#: Statuses that are neither reserving nor released: the money has moved.
FINALIZED_STATUSES = frozenset({SettlementStatus.VERIFIED})


class PaymentMethodKind(str, enum.Enum):
    VENMO = "VENMO"
    CASHAPP = "CASHAPP"
    ZELLE = "ZELLE"
    PAYPAL = "PAYPAL"
    OTHER = "OTHER"


class AuditAction(str, enum.Enum):
    PAYROLL_CREATED = "PAYROLL_CREATED"
    PAYABLE_ADDED = "PAYABLE_ADDED"
    RECEIVABLE_ADDED = "RECEIVABLE_ADDED"
    BALANCE_MODIFIED = "BALANCE_MODIFIED"
    SETTLEMENT_GENERATED = "SETTLEMENT_GENERATED"
    SETTLEMENT_PLAN_APPROVED = "SETTLEMENT_PLAN_APPROVED"
    SETTLEMENT_REASSIGNED = "SETTLEMENT_REASSIGNED"
    PAYER_MARKED_PAID = "PAYER_MARKED_PAID"
    RECIPIENT_CONFIRMED = "RECIPIENT_CONFIRMED"
    RECIPIENT_DENIED = "RECIPIENT_DENIED"
    ADMIN_VERIFIED = "ADMIN_VERIFIED"
    SETTLEMENT_REJECTED = "SETTLEMENT_REJECTED"
    SETTLEMENT_CANCELLED = "SETTLEMENT_CANCELLED"
    SETTLEMENT_DISPUTED = "SETTLEMENT_DISPUTED"
    SETTLEMENT_RESOLVED = "SETTLEMENT_RESOLVED"
    PAYMENT_METHOD_ADDED = "PAYMENT_METHOD_ADDED"
    PAYMENT_METHOD_REMOVED = "PAYMENT_METHOD_REMOVED"
    BATCH_STATUS_CHANGED = "BATCH_STATUS_CHANGED"


# --------------------------------------------------------------------------
# Tables
# --------------------------------------------------------------------------


class User(Base):
    __tablename__ = "users"

    user_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    telegram_id: Mapped[int | None] = mapped_column(
        BigInteger, unique=True, index=True, nullable=True
    )
    username: Mapped[str | None] = mapped_column(String(64), index=True)
    display_name: Mapped[str | None] = mapped_column(String(128))
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    priority: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    """Higher values are preferred as recipients by AdminPriorityStrategy."""

    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    payment_methods: Mapped[list["PaymentMethod"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )

    @property
    def label(self) -> str:
        if self.username:
            return f"@{self.username}"
        return self.display_name or f"user#{self.user_id}"

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"<User {self.label}>"


class PaymentMethod(Base):
    __tablename__ = "payment_methods"
    __table_args__ = (
        UniqueConstraint("user_id", "kind", "handle", name="uq_payment_method"),
    )

    payment_method_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.user_id"), index=True)
    kind: Mapped[PaymentMethodKind] = mapped_column(SAEnum(PaymentMethodKind))
    handle: Mapped[str] = mapped_column(String(128))
    label: Mapped[str | None] = mapped_column(String(64))
    """Free-text descriptor, used when ``kind`` is OTHER."""

    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    user: Mapped[User] = relationship(back_populates="payment_methods")

    @property
    def display(self) -> str:
        name = self.label or self.kind.value.title()
        return f"{name}: {self.handle}"


class PayrollBatch(Base):
    __tablename__ = "payroll_batches"

    batch_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    label: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    currency: Mapped[str] = mapped_column(String(3), default="USD", nullable=False)
    status: Mapped[BatchStatus] = mapped_column(
        SAEnum(BatchStatus), default=BatchStatus.DRAFT, index=True
    )
    notes: Mapped[str | None] = mapped_column(Text)
    created_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.user_id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime)

    payables: Mapped[list["Payable"]] = relationship(back_populates="batch")
    receivables: Mapped[list["Receivable"]] = relationship(back_populates="batch")
    settlements: Mapped[list["Settlement"]] = relationship(back_populates="batch")


class Payable(Base):
    """A user owes money *into* the settlement system."""

    __tablename__ = "payables"

    payable_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    batch_id: Mapped[int] = mapped_column(
        ForeignKey("payroll_batches.batch_id"), index=True
    )
    user_id: Mapped[int] = mapped_column(ForeignKey("users.user_id"), index=True)
    original_amount: Mapped[Decimal] = mapped_column(MoneyType, nullable=False)
    remaining_amount: Mapped[Decimal] = mapped_column(MoneyType, nullable=False)
    """Original minus verified. Reduced *only* by admin verification."""

    currency: Mapped[str] = mapped_column(String(3), default="USD", nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    status: Mapped[LedgerStatus] = mapped_column(
        SAEnum(LedgerStatus), default=LedgerStatus.OPEN, index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    batch: Mapped[PayrollBatch] = relationship(back_populates="payables")
    user: Mapped[User] = relationship()
    settlements: Mapped[list["Settlement"]] = relationship(back_populates="payable")


class Receivable(Base):
    """A user is owed money *by* the settlement system."""

    __tablename__ = "receivables"

    receivable_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    batch_id: Mapped[int] = mapped_column(
        ForeignKey("payroll_batches.batch_id"), index=True
    )
    user_id: Mapped[int] = mapped_column(ForeignKey("users.user_id"), index=True)
    original_amount: Mapped[Decimal] = mapped_column(MoneyType, nullable=False)
    remaining_amount: Mapped[Decimal] = mapped_column(MoneyType, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), default="USD", nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    status: Mapped[LedgerStatus] = mapped_column(
        SAEnum(LedgerStatus), default=LedgerStatus.OPEN, index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    batch: Mapped[PayrollBatch] = relationship(back_populates="receivables")
    user: Mapped[User] = relationship()
    settlements: Mapped[list["Settlement"]] = relationship(back_populates="receivable")


class Settlement(Base):
    """A routing instruction linking one payable to one receivable."""

    __tablename__ = "settlements"
    __table_args__ = (
        Index("ix_settlement_batch_status", "batch_id", "status"),
    )

    settlement_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    batch_id: Mapped[int] = mapped_column(
        ForeignKey("payroll_batches.batch_id"), index=True
    )
    payer_user_id: Mapped[int] = mapped_column(ForeignKey("users.user_id"), index=True)
    recipient_user_id: Mapped[int] = mapped_column(
        ForeignKey("users.user_id"), index=True
    )
    amount: Mapped[Decimal] = mapped_column(MoneyType, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), default="USD", nullable=False)

    payable_id: Mapped[int] = mapped_column(ForeignKey("payables.payable_id"), index=True)
    receivable_id: Mapped[int] = mapped_column(
        ForeignKey("receivables.receivable_id"), index=True
    )

    payment_method_id: Mapped[int | None] = mapped_column(
        ForeignKey("payment_methods.payment_method_id")
    )
    payment_method_note: Mapped[str | None] = mapped_column(String(256))
    """Snapshot of the recipient's handle at generation time."""

    needs_admin_review: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False
    )
    """Set when no compatible payment method could be found for the pair."""

    status: Mapped[SettlementStatus] = mapped_column(
        SAEnum(SettlementStatus), default=SettlementStatus.PENDING, index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    payer_claimed_paid_at: Mapped[datetime | None] = mapped_column(DateTime)
    recipient_confirmed_at: Mapped[datetime | None] = mapped_column(DateTime)
    admin_verified_at: Mapped[datetime | None] = mapped_column(DateTime)
    transaction_reference: Mapped[str | None] = mapped_column(String(128))
    proof_file_id: Mapped[str | None] = mapped_column(String(256))
    """Telegram file ID of an uploaded screenshot / proof of payment."""

    admin_notes: Mapped[str | None] = mapped_column(Text)

    batch: Mapped[PayrollBatch] = relationship(back_populates="settlements")
    payable: Mapped[Payable] = relationship(back_populates="settlements")
    receivable: Mapped[Receivable] = relationship(back_populates="settlements")
    payer: Mapped[User] = relationship(foreign_keys=[payer_user_id])
    recipient: Mapped[User] = relationship(foreign_keys=[recipient_user_id])
    payment_method: Mapped[PaymentMethod | None] = relationship()

    @property
    def is_reserving(self) -> bool:
        return self.status in RESERVING_STATUSES

    @property
    def is_verified(self) -> bool:
        return self.status in FINALIZED_STATUSES


class AuditLog(Base):
    """Append-only history. Rows are never updated or deleted."""

    __tablename__ = "audit_log"

    audit_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    action: Mapped[AuditAction] = mapped_column(SAEnum(AuditAction), index=True)
    actor_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.user_id"))
    batch_id: Mapped[int | None] = mapped_column(
        ForeignKey("payroll_batches.batch_id"), index=True
    )
    entity_type: Mapped[str | None] = mapped_column(String(32))
    entity_id: Mapped[int | None] = mapped_column(Integer)
    amount: Mapped[Decimal | None] = mapped_column(MoneyType)
    detail: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)

    actor: Mapped[User | None] = relationship()


__all__ = [
    "Base",
    "MoneyType",
    "User",
    "PaymentMethod",
    "PaymentMethodKind",
    "PayrollBatch",
    "BatchStatus",
    "Payable",
    "Receivable",
    "LedgerStatus",
    "Settlement",
    "SettlementStatus",
    "AuditLog",
    "AuditAction",
    "RESERVING_STATUSES",
    "RELEASED_STATUSES",
    "FINALIZED_STATUSES",
    "ZERO",
    "utcnow",
]
