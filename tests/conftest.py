from __future__ import annotations

import pytest

from payroll_bot.db import init_engine, session_scope
from payroll_bot.models import Base, PaymentMethodKind
from payroll_bot.services.accounts import add_payment_method
from payroll_bot.services.payroll import create_batch, get_or_create_user


@pytest.fixture()
def session():
    """A fresh in-memory database per test."""
    engine = init_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with session_scope() as s:
        yield s


@pytest.fixture()
def batch(session):
    return create_batch(session, label="TEST-2026-08-31")


@pytest.fixture()
def make_user(session):
    """Factory: ``make_user("john", methods=["VENMO"], priority=1)``."""

    def _make(handle: str, *, methods=(), priority: int = 0):
        user = get_or_create_user(session, username=handle)
        user.priority = priority
        for kind in methods:
            add_payment_method(
                session,
                user,
                kind if isinstance(kind, PaymentMethodKind) else PaymentMethodKind[kind],
                f"@{handle}",
            )
        session.flush()
        return user

    return _make
