"""End-to-end payroll lifecycle, reassignment, disputes, and the audit trail."""

from decimal import Decimal

import pytest

from payroll_bot import audit
from payroll_bot.ledger import payable_balance, receivable_balance
from payroll_bot.models import AuditAction, BatchStatus, SettlementStatus
from payroll_bot.parsing import parse_payroll
from payroll_bot.services import payroll as payroll_service
from payroll_bot.services import settlement as settlement_service


def test_full_payroll_from_text_to_completed(session, batch, make_user):
    """The spec's headline flow, start to finish."""
    parsed = parse_payroll("OWES\n@john 500\nOWED\n@mike 300\n@sarah 200")
    assert parsed.balances

    payroll_service.apply_parsed_payroll(session, batch, parsed)
    session.flush()

    result = payroll_service.generate_plan(session, batch)
    assert result.transfer_count == 2
    assert result.total_routed == Decimal("500.00")

    settlements = payroll_service.approve_plan(session, batch, result)
    session.flush()
    assert batch.status is BatchStatus.IN_PROGRESS

    # John did not originally owe Mike; he owed the system. Verify that the
    # settlements are routing instructions linked to both ledger entries.
    for s in settlements:
        assert s.payable_id is not None
        assert s.receivable_id is not None
        assert s.status is SettlementStatus.PENDING

    for s in settlements:
        settlement_service.mark_paid(session, s, actor_user_id=s.payer_user_id)
        settlement_service.recipient_confirm(
            session, s, actor_user_id=s.recipient_user_id
        )
        settlement_service.admin_verify(session, s, actor_user_id=1)
    session.flush()

    totals = payroll_service.batch_totals(session, batch)
    assert totals.payable.remaining == Decimal("0.00")
    assert totals.receivable.remaining == Decimal("0.00")
    assert totals.payable.verified == Decimal("500.00")
    assert batch.status is BatchStatus.COMPLETED


def test_partial_settlement_leaves_recipient_owed(session, batch, make_user):
    """John owes 500, Mike is owed 700 -> Mike still owed 200 after verification."""
    john = make_user("john")
    mike = make_user("mike")
    payable = payroll_service.add_payable(session, batch, john, Decimal("500"))
    receivable = payroll_service.add_receivable(session, batch, mike, Decimal("700"))
    session.flush()

    result = payroll_service.generate_plan(session, batch)
    settlements = payroll_service.approve_plan(session, batch, result)
    session.flush()

    settlement_service.admin_verify(session, settlements[0], actor_user_id=1)
    session.flush()

    assert payable_balance(payable).remaining == Decimal("0.00")
    assert receivable_balance(receivable).remaining == Decimal("200.00")
    # Not everyone is square, so the batch stays open.
    assert batch.status is BatchStatus.IN_PROGRESS


def test_admin_can_reroute_a_settlement(session, batch, make_user):
    """Replace 'John -> Mike: 500' with 'John -> Sarah: 500'."""
    john = make_user("john")
    mike = make_user("mike")
    sarah = make_user("sarah")

    payable = payroll_service.add_payable(session, batch, john, Decimal("500"))
    r_mike = payroll_service.add_receivable(session, batch, mike, Decimal("500"))
    r_sarah = payroll_service.add_receivable(session, batch, sarah, Decimal("500"))
    session.flush()

    result = payroll_service.generate_plan(session, batch)
    settlements = payroll_service.approve_plan(session, batch, result)
    session.flush()
    target = settlements[0]
    original_recipient = target.recipient_user_id

    other = r_sarah if original_recipient == mike.user_id else r_mike
    settlement_service.reassign(
        session,
        target,
        actor_user_id=1,
        new_recipient_receivable_id=other.receivable_id,
    )
    session.flush()

    assert target.recipient_user_id == other.user_id
    assert target.amount == Decimal("500.00")
    # The old recipient's capacity is released, the new one's is reserved.
    assert receivable_balance(other).reserved == Decimal("500.00")
    assert payable_balance(payable).reserved == Decimal("500.00")


def test_reassignment_that_breaks_the_maths_is_refused(session, batch, make_user):
    john = make_user("john")
    mike = make_user("mike")
    sarah = make_user("sarah")

    payroll_service.add_payable(session, batch, john, Decimal("500"))
    payroll_service.add_receivable(session, batch, mike, Decimal("400"))
    r_sarah = payroll_service.add_receivable(session, batch, sarah, Decimal("100"))
    session.flush()

    result = payroll_service.generate_plan(session, batch)
    settlements = payroll_service.approve_plan(session, batch, result)
    session.flush()

    big = next(s for s in settlements if s.amount == Decimal("400"))
    # Sarah can only receive 100, and 100 of hers is already reserved.
    with pytest.raises(Exception):
        settlement_service.reassign(
            session,
            big,
            actor_user_id=1,
            new_recipient_receivable_id=r_sarah.receivable_id,
        )


def test_reassignment_clears_a_stale_payment_claim(session, batch, make_user):
    john = make_user("john")
    mike = make_user("mike")
    sarah = make_user("sarah")
    payroll_service.add_payable(session, batch, john, Decimal("500"))
    r_mike = payroll_service.add_receivable(session, batch, mike, Decimal("500"))
    r_sarah = payroll_service.add_receivable(session, batch, sarah, Decimal("500"))
    session.flush()

    result = payroll_service.generate_plan(session, batch)
    settlements = payroll_service.approve_plan(session, batch, result)
    session.flush()
    target = settlements[0]

    settlement_service.mark_paid(
        session, target, actor_user_id=target.payer_user_id, transaction_reference="X1"
    )
    # Route to whichever of the two the matcher did not pick.
    other = r_sarah if target.recipient_user_id == mike.user_id else r_mike

    settlement_service.reassign(
        session, target, actor_user_id=1, new_recipient_receivable_id=other.receivable_id
    )
    session.flush()

    # A re-routed payment is a different instruction; the old claim must not
    # carry over to the new recipient.
    assert target.status is SettlementStatus.PENDING
    assert target.payer_claimed_paid_at is None
    assert target.transaction_reference is None


def test_recipient_denial_routes_to_admin_and_can_be_rejected(
    session, batch, make_user
):
    john = make_user("john")
    mike = make_user("mike")
    payable = payroll_service.add_payable(session, batch, john, Decimal("500"))
    payroll_service.add_receivable(session, batch, mike, Decimal("500"))
    session.flush()

    result = payroll_service.generate_plan(session, batch)
    settlement = payroll_service.approve_plan(session, batch, result)[0]
    session.flush()

    settlement_service.mark_paid(
        session, settlement, actor_user_id=settlement.payer_user_id
    )
    settlement_service.recipient_deny(
        session, settlement, actor_user_id=settlement.recipient_user_id, reason="nothing arrived"
    )
    assert settlement.status is SettlementStatus.RECIPIENT_DENIED

    pending = settlement_service.settlements_awaiting_verification(
        session, batch.batch_id
    )
    assert settlement in pending

    settlement_service.admin_reject(session, settlement, actor_user_id=1)
    session.flush()
    assert payable_balance(payable).available == Decimal("500.00")


def test_dispute_can_be_resolved_as_verified(session, batch, make_user):
    john = make_user("john")
    mike = make_user("mike")
    payable = payroll_service.add_payable(session, batch, john, Decimal("500"))
    payroll_service.add_receivable(session, batch, mike, Decimal("500"))
    session.flush()

    result = payroll_service.generate_plan(session, batch)
    settlement = payroll_service.approve_plan(session, batch, result)[0]
    session.flush()

    settlement_service.mark_paid(
        session, settlement, actor_user_id=settlement.payer_user_id
    )
    settlement_service.admin_dispute(session, settlement, actor_user_id=1, reason="unclear")
    assert settlement_service.disputed_settlements(session, batch.batch_id) == [settlement]

    settlement_service.resolve_dispute(
        session,
        settlement,
        actor_user_id=1,
        outcome=SettlementStatus.VERIFIED,
        reason="bank statement checked",
    )
    session.flush()

    assert settlement.status is SettlementStatus.VERIFIED
    assert payable_balance(payable).remaining == Decimal("0.00")


def test_only_the_payer_may_mark_paid(session, batch, make_user):
    john = make_user("john")
    mike = make_user("mike")
    payroll_service.add_payable(session, batch, john, Decimal("100"))
    payroll_service.add_receivable(session, batch, mike, Decimal("100"))
    session.flush()

    result = payroll_service.generate_plan(session, batch)
    settlement = payroll_service.approve_plan(session, batch, result)[0]
    session.flush()

    with pytest.raises(settlement_service.SettlementError):
        settlement_service.mark_paid(session, settlement, actor_user_id=mike.user_id)


def test_only_the_recipient_may_confirm(session, batch, make_user):
    john = make_user("john")
    mike = make_user("mike")
    payroll_service.add_payable(session, batch, john, Decimal("100"))
    payroll_service.add_receivable(session, batch, mike, Decimal("100"))
    session.flush()

    result = payroll_service.generate_plan(session, batch)
    settlement = payroll_service.approve_plan(session, batch, result)[0]
    session.flush()
    settlement_service.mark_paid(session, settlement, actor_user_id=john.user_id)

    with pytest.raises(settlement_service.SettlementError):
        settlement_service.recipient_confirm(
            session, settlement, actor_user_id=john.user_id
        )


def test_payment_methods_steer_the_routing(session, batch, make_user):
    john = make_user("john", methods=["VENMO", "CASHAPP"])
    mike = make_user("mike", methods=["VENMO"])
    sarah = make_user("sarah", methods=["ZELLE"])

    payroll_service.add_payable(session, batch, john, Decimal("300"))
    payroll_service.add_receivable(session, batch, mike, Decimal("300"))
    payroll_service.add_receivable(session, batch, sarah, Decimal("300"))
    session.flush()

    result = payroll_service.generate_plan(session, batch)
    assert result.proposals[0].recipient_user_id == mike.user_id

    settlements = payroll_service.approve_plan(session, batch, result)
    session.flush()
    # The recipient's handle is snapshotted onto the instruction.
    assert settlements[0].payment_method_note == "Venmo: @mike"
    assert not settlements[0].needs_admin_review


def test_settlement_without_shared_method_is_flagged(session, batch, make_user):
    john = make_user("john", methods=["VENMO"])
    sarah = make_user("sarah", methods=["ZELLE"])
    payroll_service.add_payable(session, batch, john, Decimal("100"))
    payroll_service.add_receivable(session, batch, sarah, Decimal("100"))
    session.flush()

    result = payroll_service.generate_plan(session, batch)
    settlements = payroll_service.approve_plan(session, batch, result)
    session.flush()

    assert settlements[0].needs_admin_review
    totals = payroll_service.batch_totals(session, batch)
    assert totals.flagged == 1


def test_user_position_reports_the_four_quantities(session, batch, make_user):
    john = make_user("john")
    mike = make_user("mike")
    sarah = make_user("sarah")
    payroll_service.add_payable(session, batch, john, Decimal("750"))
    payroll_service.add_receivable(session, batch, mike, Decimal("500"))
    payroll_service.add_receivable(session, batch, sarah, Decimal("250"))
    session.flush()

    result = payroll_service.generate_plan(session, batch)
    settlements = payroll_service.approve_plan(session, batch, result)
    session.flush()

    to_mike = next(s for s in settlements if s.recipient_user_id == mike.user_id)
    settlement_service.admin_verify(session, to_mike, actor_user_id=1)
    session.flush()

    position = settlement_service.user_position(session, batch.batch_id, john.user_id)
    assert position.owes.original == Decimal("750.00")
    assert position.owes.verified == Decimal("500.00")
    assert position.owes.remaining == Decimal("250.00")
    assert position.owes.reserved == Decimal("250.00")
    assert len(position.outgoing) == 2


def test_audit_trail_records_every_financial_event(session, batch, make_user):
    john = make_user("john")
    mike = make_user("mike")
    payroll_service.add_payable(session, batch, john, Decimal("100"))
    payroll_service.add_receivable(session, batch, mike, Decimal("100"))
    session.flush()

    result = payroll_service.generate_plan(session, batch)
    settlement = payroll_service.approve_plan(session, batch, result)[0]
    session.flush()

    settlement_service.mark_paid(session, settlement, actor_user_id=john.user_id)
    settlement_service.recipient_confirm(session, settlement, actor_user_id=mike.user_id)
    settlement_service.admin_verify(session, settlement, actor_user_id=1)
    session.flush()

    actions = {e.action for e in audit.history(session, batch_id=batch.batch_id, limit=200)}
    for expected in (
        AuditAction.PAYROLL_CREATED,
        AuditAction.PAYABLE_ADDED,
        AuditAction.RECEIVABLE_ADDED,
        AuditAction.SETTLEMENT_GENERATED,
        AuditAction.SETTLEMENT_PLAN_APPROVED,
        AuditAction.PAYER_MARKED_PAID,
        AuditAction.RECIPIENT_CONFIRMED,
        AuditAction.ADMIN_VERIFIED,
    ):
        assert expected in actions, f"missing audit entry: {expected}"


def test_audit_entries_are_scoped_to_one_settlement(session, batch, make_user):
    john = make_user("john")
    mike = make_user("mike")
    payroll_service.add_payable(session, batch, john, Decimal("100"))
    payroll_service.add_receivable(session, batch, mike, Decimal("100"))
    session.flush()

    result = payroll_service.generate_plan(session, batch)
    settlement = payroll_service.approve_plan(session, batch, result)[0]
    session.flush()
    settlement_service.mark_paid(session, settlement, actor_user_id=john.user_id)
    session.flush()

    rows = audit.history(
        session, entity_type="settlement", entity_id=settlement.settlement_id
    )
    assert rows
    assert all(r.entity_id == settlement.settlement_id for r in rows)


def test_unbalanced_payroll_routes_only_the_smaller_side(session, batch, make_user):
    john = make_user("john")
    mike = make_user("mike")
    payable = payroll_service.add_payable(session, batch, john, Decimal("5420"))
    payroll_service.add_receivable(session, batch, mike, Decimal("5370"))
    session.flush()

    totals = payroll_service.batch_totals(session, batch)
    assert not totals.balances
    assert totals.difference == Decimal("50.00")

    result = payroll_service.generate_plan(session, batch)
    assert result.total_routed == Decimal("5370.00")
    payroll_service.approve_plan(session, batch, result)
    session.flush()

    # The unroutable 50 stays visible as an unassigned obligation.
    assert payable_balance(payable).available == Decimal("50.00")


def test_money_survives_a_database_round_trip(session, batch, make_user):
    """Amounts must come back as exact Decimals, never floats."""
    john = make_user("john")
    payable = payroll_service.add_payable(session, batch, john, Decimal("1234.56"))
    session.flush()
    session.expire_all()

    reloaded = session.get(type(payable), payable.payable_id)
    assert isinstance(reloaded.original_amount, Decimal)
    assert reloaded.original_amount == Decimal("1234.56")


def test_awkward_cents_reconcile_exactly(session, batch, make_user):
    for handle, amount in (("a", "33.33"), ("b", "33.33"), ("c", "33.34")):
        payroll_service.add_payable(
            session, batch, make_user(handle), Decimal(amount)
        )
    for handle, amount in (("x", "50.00"), ("y", "50.00")):
        payroll_service.add_receivable(
            session, batch, make_user(handle), Decimal(amount)
        )
    session.flush()

    result = payroll_service.generate_plan(session, batch)
    assert result.total_routed == Decimal("100.00")

    settlements = payroll_service.approve_plan(session, batch, result)
    for s in settlements:
        settlement_service.admin_verify(session, s, actor_user_id=1)
    session.flush()

    totals = payroll_service.batch_totals(session, batch)
    assert totals.payable.remaining == Decimal("0.00")
    assert totals.receivable.remaining == Decimal("0.00")
