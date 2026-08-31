"""User accounts, admin rights, and saved payment methods."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import audit
from ..models import AuditAction, PaymentMethod, PaymentMethodKind, User


class AccountError(RuntimeError):
    pass


_KIND_ALIASES = {
    "venmo": PaymentMethodKind.VENMO,
    "cashapp": PaymentMethodKind.CASHAPP,
    "cash": PaymentMethodKind.CASHAPP,
    "cash app": PaymentMethodKind.CASHAPP,
    "$cashapp": PaymentMethodKind.CASHAPP,
    "zelle": PaymentMethodKind.ZELLE,
    "paypal": PaymentMethodKind.PAYPAL,
    "other": PaymentMethodKind.OTHER,
}


def parse_kind(text: str) -> PaymentMethodKind:
    key = text.strip().lower().replace("-", "").replace("_", "")
    if key in _KIND_ALIASES:
        return _KIND_ALIASES[key]
    for alias, kind in _KIND_ALIASES.items():
        if alias.replace(" ", "") == key:
            return kind
    raise AccountError(
        f"unknown payment method “{text}”. Use Venmo, Cash App, Zelle, PayPal, or Other."
    )


def add_payment_method(
    session: Session,
    user: User,
    kind: PaymentMethodKind,
    handle: str,
    *,
    label: str | None = None,
    actor_user_id: int | None = None,
) -> PaymentMethod:
    handle = handle.strip()
    if not handle:
        raise AccountError("payment handle cannot be empty")

    existing = session.execute(
        select(PaymentMethod).where(
            PaymentMethod.user_id == user.user_id,
            PaymentMethod.kind == kind,
            PaymentMethod.handle == handle,
        )
    ).scalar_one_or_none()
    if existing is not None:
        existing.is_active = True
        return existing

    method = PaymentMethod(
        user_id=user.user_id, kind=kind, handle=handle, label=label, is_active=True
    )
    session.add(method)
    session.flush()
    audit.record(
        session,
        AuditAction.PAYMENT_METHOD_ADDED,
        actor_user_id=actor_user_id or user.user_id,
        entity_type="payment_method",
        entity_id=method.payment_method_id,
        detail=f"{user.label}: {kind.value} {handle}",
    )
    return method


def remove_payment_method(
    session: Session, method: PaymentMethod, *, actor_user_id: int | None = None
) -> None:
    """Deactivate rather than delete: settlements reference the method by id."""
    method.is_active = False
    audit.record(
        session,
        AuditAction.PAYMENT_METHOD_REMOVED,
        actor_user_id=actor_user_id,
        entity_type="payment_method",
        entity_id=method.payment_method_id,
        detail=f"{method.user.label}: {method.kind.value} {method.handle}",
    )


def list_payment_methods(
    session: Session, user_id: int, *, active_only: bool = True
) -> list[PaymentMethod]:
    stmt = select(PaymentMethod).where(PaymentMethod.user_id == user_id)
    if active_only:
        stmt = stmt.where(PaymentMethod.is_active.is_(True))
    return list(session.execute(stmt.order_by(PaymentMethod.payment_method_id)).scalars().all())


def set_admin(
    session: Session, user: User, is_admin: bool, *, actor_user_id: int | None = None
) -> None:
    user.is_admin = is_admin
    audit.record(
        session,
        AuditAction.BALANCE_MODIFIED,
        actor_user_id=actor_user_id,
        entity_type="user",
        entity_id=user.user_id,
        detail=f"{user.label} admin={is_admin}",
    )


def set_priority(
    session: Session, user: User, priority: int, *, actor_user_id: int | None = None
) -> None:
    """Recipient priority, consumed by AdminPriorityStrategy."""
    previous = user.priority
    user.priority = priority
    audit.record(
        session,
        AuditAction.BALANCE_MODIFIED,
        actor_user_id=actor_user_id,
        entity_type="user",
        entity_id=user.user_id,
        detail=f"{user.label} priority {previous} → {priority}",
    )


def find_user(session: Session, handle: str) -> User | None:
    cleaned = handle.strip().lstrip("@").lower()
    if cleaned.isdigit():
        by_id = session.get(User, int(cleaned))
        if by_id is not None:
            return by_id
    return session.execute(
        select(User).where(User.username == cleaned)
    ).scalar_one_or_none()


def is_admin(session: Session, telegram_id: int, bootstrap_ids: set[int]) -> bool:
    """Admin if flagged in the database, or listed in ``ADMIN_TELEGRAM_IDS``.

    The bootstrap list solves the cold-start problem: the first admin cannot be
    promoted by an admin who does not yet exist.
    """
    if telegram_id in bootstrap_ids:
        return True
    user = session.execute(
        select(User).where(User.telegram_id == telegram_id)
    ).scalar_one_or_none()
    return bool(user and user.is_admin)
