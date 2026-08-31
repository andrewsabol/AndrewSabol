"""Append-only audit trail.

Every financial mutation writes a row here in the same transaction that makes
the change. Nothing in this module updates or deletes an existing row -- a
correction is recorded as a new entry describing the correction, so the history
of a payroll period can always be replayed.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import AuditAction, AuditLog


def record(
    session: Session,
    action: AuditAction,
    *,
    actor_user_id: int | None = None,
    batch_id: int | None = None,
    entity_type: str | None = None,
    entity_id: int | None = None,
    amount: Decimal | None = None,
    detail: str | None = None,
) -> AuditLog:
    """Append one audit entry. Never call this outside the mutating transaction."""
    entry = AuditLog(
        action=action,
        actor_user_id=actor_user_id,
        batch_id=batch_id,
        entity_type=entity_type,
        entity_id=entity_id,
        amount=amount,
        detail=detail,
    )
    session.add(entry)
    return entry


def history(
    session: Session,
    *,
    batch_id: int | None = None,
    entity_type: str | None = None,
    entity_id: int | None = None,
    limit: int = 100,
) -> Sequence[AuditLog]:
    """Read the trail, most recent first."""
    stmt = select(AuditLog).order_by(AuditLog.created_at.desc(), AuditLog.audit_id.desc())
    if batch_id is not None:
        stmt = stmt.where(AuditLog.batch_id == batch_id)
    if entity_type is not None:
        stmt = stmt.where(AuditLog.entity_type == entity_type)
    if entity_id is not None:
        stmt = stmt.where(AuditLog.entity_id == entity_id)
    return session.execute(stmt.limit(limit)).scalars().all()
