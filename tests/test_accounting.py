"""Balance accounting invariants.

These encode the spec's central rule: generating a settlement must not reduce a
finalized balance. Only admin verification may.
"""

from decimal import Decimal

import pytest

from payroll_bot.ledger import (
    InsufficientCapacity,
    assert_capacity,
    payable_balance,
    receivable_balance,
)
from payroll_bot.models import LedgerStatus
from payroll_bot.services import payroll as payroll_service
from payroll_bot.services import settlement as settlement_service


@pytest.fixture()
def john_owes_1000(session, batch, make_user):
    """The spec's worked example: John owes $1,000, routed as $600 + $400."""
    john = make_user("john")
    mike = make_user("mike")
    sarah = make_user("sarah")

    payable = payroll_service.add_payable(session, batch, john, Decimal("1000"))
    r_mike = payroll_service.add_receivable(session, batch, mike, Decimal("600"))
    r_sarah = payroll_service.add_receivable(session, batch, sarah, Decimal("400"))
    session.flush()

    result = payroll_service.generate_plan(session, batch)
    created = payroll_service.approve_plan(session, batch, result)
    session.flush()
    return {
        "john": john,
        "payable": payable,
        "r_mike": r_mike,
        "r_sarah": r_sarah,
        "settlements": created,
    }


def test_generating_settlements_does_not_reduce_the_balance(john_owes_1000):
    """Original 1000, assigned 1000, verified 0, remaining obligation 1000."""
    balance = payable_balance(john_owes_1000["payable"])
    assert balance.original == Decimal("1000.00")
    assert balance.reserved == Decimal("1000.00")
    assert balance.verified == Decimal("0.00")
    assert balance.remaining == Decimal("1000.00")
    assert balance.available == Decimal("0.00")


def test_verification_is_what_moves_the_balance(session, john_owes_1000):
    settlements = john_owes_1000["settlements"]
    first = next(s for s in settlements if s.amount == Decimal("600"))

    settlement_service.mark_paid(session, first, actor_user_id=first.payer_user_id)
    settlement_service.recipient_confirm(
        session, first, actor_user_id=first.recipient_user_id
    )

    # Still nothing moved: the payer and recipient both say it happened, but the
    # admin has not verified.
    balance = payable_balance(john_owes_1000["payable"])
    assert balance.verified == Decimal("0.00")
    assert balance.remaining == Decimal("1000.00")

    settlement_service.admin_verify(session, first, actor_user_id=1)
    session.flush()

    balance = payable_balance(john_owes_1000["payable"])
    assert balance.original == Decimal("1000.00")
    assert balance.verified == Decimal("600.00")
    assert balance.remaining == Decimal("400.00")


def test_cancelling_returns_the_amount_for_reassignment(session, john_owes_1000):
    """"If the other $400 settlement is cancelled, that $400 becomes available."""
    settlements = john_owes_1000["settlements"]
    first = next(s for s in settlements if s.amount == Decimal("600"))
    second = next(s for s in settlements if s.amount == Decimal("400"))

    settlement_service.admin_verify(session, first, actor_user_id=1)
    settlement_service.cancel(session, second, actor_user_id=1, reason="wrong person")
    session.flush()

    balance = payable_balance(john_owes_1000["payable"])
    assert balance.verified == Decimal("600.00")
    assert balance.reserved == Decimal("0.00")
    assert balance.remaining == Decimal("400.00")
    # The freed 400 is routable again.
    assert balance.available == Decimal("400.00")


def test_freed_amount_can_actually_be_rerouted(session, batch, john_owes_1000):
    second = next(
        s for s in john_owes_1000["settlements"] if s.amount == Decimal("400")
    )
    settlement_service.cancel(session, second, actor_user_id=1)
    session.flush()

    # Sarah's receivable is free again too, so a fresh plan re-routes the 400.
    result = payroll_service.generate_plan(session, batch)
    assert result.total_routed == Decimal("400.00")

    payroll_service.approve_plan(session, batch, result)
    session.flush()
    assert payable_balance(john_owes_1000["payable"]).available == Decimal("0.00")


def test_rejected_settlement_also_frees_capacity(session, john_owes_1000):
    second = next(
        s for s in john_owes_1000["settlements"] if s.amount == Decimal("400")
    )
    settlement_service.mark_paid(session, second, actor_user_id=second.payer_user_id)
    settlement_service.admin_reject(session, second, actor_user_id=1, reason="no proof")
    session.flush()

    balance = payable_balance(john_owes_1000["payable"])
    # The 600 settlement is still pending, so it alone reserves; the rejected
    # 400 is released and routable again.
    assert balance.reserved == Decimal("600.00")
    assert balance.available == Decimal("400.00")
    assert balance.remaining == Decimal("1000.00")


def test_disputed_settlement_still_reserves(session, john_owes_1000):
    """A dispute is unresolved, so its amount must not be freed for reuse."""
    first = next(
        s for s in john_owes_1000["settlements"] if s.amount == Decimal("600")
    )
    second = next(
        s for s in john_owes_1000["settlements"] if s.amount == Decimal("400")
    )
    # Clear the 600 out of the way so the disputed 400 is the only reservation.
    settlement_service.admin_verify(session, first, actor_user_id=1)
    settlement_service.mark_paid(session, second, actor_user_id=second.payer_user_id)
    settlement_service.admin_dispute(session, second, actor_user_id=1)
    session.flush()

    balance = payable_balance(john_owes_1000["payable"])
    assert balance.verified == Decimal("600.00")
    assert balance.reserved == Decimal("400.00")
    assert balance.available == Decimal("0.00")
    assert balance.remaining == Decimal("400.00")


def test_recipient_balance_tracks_independently(session, john_owes_1000):
    """Mike's receivable is discharged by verification, not by John's intent."""
    r_mike = john_owes_1000["r_mike"]
    assert receivable_balance(r_mike).remaining == Decimal("600.00")

    first = next(
        s for s in john_owes_1000["settlements"] if s.amount == Decimal("600")
    )
    settlement_service.admin_verify(session, first, actor_user_id=1)
    session.flush()

    balance = receivable_balance(r_mike)
    assert balance.verified == Decimal("600.00")
    assert balance.remaining == Decimal("0.00")
    assert balance.is_settled


def test_cannot_over_assign_a_payable(session, batch, make_user):
    john = make_user("john")
    mike = make_user("mike")
    payable = payroll_service.add_payable(session, batch, john, Decimal("100"))
    receivable = payroll_service.add_receivable(session, batch, mike, Decimal("500"))
    session.flush()

    assert_capacity(payable, receivable, Decimal("100"))
    with pytest.raises(InsufficientCapacity):
        assert_capacity(payable, receivable, Decimal("100.01"))


def test_cannot_over_assign_a_receivable(session, batch, make_user):
    john = make_user("john")
    mike = make_user("mike")
    payable = payroll_service.add_payable(session, batch, john, Decimal("500"))
    receivable = payroll_service.add_receivable(session, batch, mike, Decimal("100"))
    session.flush()

    with pytest.raises(InsufficientCapacity):
        assert_capacity(payable, receivable, Decimal("200"))


def test_remaining_amount_column_tracks_verified_only(session, john_owes_1000):
    payable = john_owes_1000["payable"]
    assert payable.remaining_amount == Decimal("1000.00")

    first = next(
        s for s in john_owes_1000["settlements"] if s.amount == Decimal("600")
    )
    settlement_service.admin_verify(session, first, actor_user_id=1)
    session.flush()

    assert payable.remaining_amount == Decimal("400.00")
    assert payable.status is LedgerStatus.OPEN


def test_ledger_entry_settles_when_fully_verified(session, john_owes_1000):
    for settlement in john_owes_1000["settlements"]:
        settlement_service.admin_verify(session, settlement, actor_user_id=1)
    session.flush()

    payable = john_owes_1000["payable"]
    assert payable.remaining_amount == Decimal("0.00")
    assert payable.status is LedgerStatus.SETTLED
    assert receivable_balance(john_owes_1000["r_mike"]).is_settled
    assert receivable_balance(john_owes_1000["r_sarah"]).is_settled


def test_verified_settlement_cannot_be_cancelled(session, john_owes_1000):
    first = john_owes_1000["settlements"][0]
    settlement_service.admin_verify(session, first, actor_user_id=1)
    with pytest.raises(settlement_service.SettlementError):
        settlement_service.cancel(session, first, actor_user_id=1)


def test_modifying_a_balance_below_committed_is_refused(session, john_owes_1000):
    """1000 is fully assigned; shrinking to 500 would corrupt the ledger."""
    with pytest.raises(payroll_service.PayrollError):
        payroll_service.modify_balance(
            session, john_owes_1000["payable"], Decimal("500")
        )


def test_modifying_a_balance_upward_is_allowed(session, john_owes_1000):
    payroll_service.modify_balance(
        session, john_owes_1000["payable"], Decimal("1500"), reason="missed hours"
    )
    session.flush()
    balance = payable_balance(john_owes_1000["payable"])
    assert balance.original == Decimal("1500.00")
    assert balance.available == Decimal("500.00")


def test_regenerating_a_plan_does_not_double_assign(session, batch, john_owes_1000):
    """Everything is already routed, so a second plan must be empty."""
    result = payroll_service.generate_plan(session, batch)
    assert result.proposals == []
