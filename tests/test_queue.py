"""The payment queue, and payrolls that deliberately do not balance."""

from decimal import Decimal

import pytest

from payroll_bot import queue as payment_queue
from payroll_bot.ledger import receivable_balance
from payroll_bot.services import payroll as payroll_service
from payroll_bot.services import settlement as settlement_service


@pytest.fixture()
def shortfall(session, batch, make_user):
    """More owed out than in: $400 available against $900 of claims."""
    john = make_user("john", methods=["VENMO"])
    mike = make_user("mike", methods=["VENMO"])
    sarah = make_user("sarah", methods=["ZELLE"])
    alex = make_user("alex", methods=["VENMO"])

    payable = payroll_service.add_payable(session, batch, john, Decimal("400"))
    # Added in this order, so this is the queue order.
    r_mike = payroll_service.add_receivable(session, batch, mike, Decimal("300"))
    r_sarah = payroll_service.add_receivable(session, batch, sarah, Decimal("400"))
    r_alex = payroll_service.add_receivable(session, batch, alex, Decimal("200"))
    session.flush()
    return {
        "batch": batch,
        "john": john, "mike": mike, "sarah": sarah, "alex": alex,
        "payable": payable,
        "r_mike": r_mike, "r_sarah": r_sarah, "r_alex": r_alex,
    }


# --------------------------------------------------------------------------
# Unbalanced payroll
# --------------------------------------------------------------------------


def test_an_unbalanced_payroll_is_recorded_in_full(session, shortfall):
    """Being owed more than has come in is a schedule, not an error."""
    totals = payroll_service.batch_totals(session, shortfall["batch"])
    assert totals.payable.original == Decimal("400.00")
    assert totals.receivable.original == Decimal("900.00")
    assert not totals.balances
    assert totals.difference == Decimal("-500.00")


def test_shortfall_routes_what_exists_and_queues_the_rest(session, shortfall):
    result = payroll_service.generate_plan(session, shortfall["batch"])
    assert result.total_routed == Decimal("400.00")
    # Nobody is asked to pay money they do not owe.
    assert not result.unmatched_payers
    assert result.unmatched_recipients


def test_surplus_side_is_left_unrouted(session, batch, make_user):
    """More owed in than out: the extra simply stays unassigned."""
    payroll_service.add_payable(
        session, batch, make_user("john"), Decimal("900")
    )
    payroll_service.add_receivable(session, batch, make_user("mike"), Decimal("400"))
    session.flush()

    result = payroll_service.generate_plan(session, batch)
    assert result.total_routed == Decimal("400.00")
    assert result.unmatched_payers[0].available == Decimal("500.00")


# --------------------------------------------------------------------------
# Queue ordering
# --------------------------------------------------------------------------


def test_queue_is_ordered_by_when_people_were_added(session, shortfall):
    entries = payment_queue.build_queue(session, shortfall["batch"].batch_id)
    assert [e.user.label for e in entries] == ["@mike", "@sarah", "@alex"]
    assert [e.position for e in entries] == [1, 2, 3]


def test_queue_reports_what_each_person_is_still_owed(session, shortfall):
    entries = payment_queue.build_queue(session, shortfall["batch"].batch_id)
    assert entries[0].still_owed == Decimal("300.00")
    assert entries[0].unassigned == Decimal("300.00")


def test_paying_someone_off_removes_them_and_renumbers(session, shortfall):
    """The queue is derived, so positions cannot drift out of step."""
    payable = shortfall["payable"]
    settlement = payroll_service.assign_settlement(
        session, payable, shortfall["r_mike"], Decimal("300")
    )
    settlement_service.admin_verify(session, settlement, actor_user_id=1)
    session.flush()

    entries = payment_queue.build_queue(session, shortfall["batch"].batch_id)
    assert [e.user.label for e in entries] == ["@sarah", "@alex"]
    assert [e.position for e in entries] == [1, 2]


def test_partially_paid_person_keeps_their_place(session, shortfall):
    """Paying part of what someone is owed must not send them to the back."""
    settlement = payroll_service.assign_settlement(
        session, shortfall["payable"], shortfall["r_mike"], Decimal("100")
    )
    settlement_service.admin_verify(session, settlement, actor_user_id=1)
    session.flush()

    entries = payment_queue.build_queue(session, shortfall["batch"].batch_id)
    assert entries[0].user.label == "@mike"
    assert entries[0].still_owed == Decimal("200.00")


def test_assigned_but_unverified_person_stays_visible(session, shortfall):
    """Routed money is not received money; they are still owed and still queued."""
    payroll_service.assign_settlement(
        session, shortfall["payable"], shortfall["r_mike"], Decimal("300")
    )
    session.flush()

    entries = payment_queue.build_queue(session, shortfall["batch"].batch_id)
    assert entries[0].user.label == "@mike"
    assert entries[0].still_owed == Decimal("300.00")
    assert entries[0].unassigned == Decimal("0.00")


def test_queue_can_hide_people_with_nothing_left_to_assign(session, shortfall):
    payroll_service.assign_settlement(
        session, shortfall["payable"], shortfall["r_mike"], Decimal("300")
    )
    session.flush()

    entries = payment_queue.build_queue(
        session, shortfall["batch"].batch_id, include_fully_assigned=False
    )
    assert [e.user.label for e in entries] == ["@sarah", "@alex"]


# --------------------------------------------------------------------------
# Stepping through the queue
# --------------------------------------------------------------------------


def test_stepping_wraps_around_so_cycling_never_dead_ends(session, shortfall):
    batch_id = shortfall["batch"].batch_id
    first, total = payment_queue.entry_at(session, batch_id, 0)
    assert total == 3
    assert first.user.label == "@mike"

    past_end, _ = payment_queue.entry_at(session, batch_id, 3)
    assert past_end.user.label == "@mike"

    before_start, _ = payment_queue.entry_at(session, batch_id, -1)
    assert before_start.user.label == "@alex"


def test_empty_queue_reports_no_entry(session, batch):
    entry, total = payment_queue.entry_at(session, batch.batch_id, 0)
    assert entry is None
    assert total == 0


def test_next_payable_prefers_someone_the_payer_can_actually_pay(session, shortfall):
    """Mike is first, but a Zelle-only payer should be steered to Sarah."""
    batch_id = shortfall["batch"].batch_id

    venmo = payment_queue.next_payable_to(
        session, batch_id, payer_methods=frozenset({"VENMO"})
    )
    assert venmo.user.label == "@mike"

    zelle = payment_queue.next_payable_to(
        session, batch_id, payer_methods=frozenset({"ZELLE"})
    )
    assert zelle.user.label == "@sarah"


def test_next_payable_falls_back_to_the_front_of_the_line(session, shortfall):
    """With no compatible match, the queue order still wins."""
    entry = payment_queue.next_payable_to(
        session, shortfall["batch"].batch_id, payer_methods=frozenset({"PAYPAL"})
    )
    assert entry.user.label == "@mike"


def test_shared_method_detection(session, shortfall):
    entries = payment_queue.build_queue(session, shortfall["batch"].batch_id)
    mike, sarah = entries[0], entries[1]
    assert mike.shares_method_with(frozenset({"VENMO"})) == {"VENMO"}
    assert sarah.shares_method_with(frozenset({"VENMO"})) == frozenset()


def test_total_outstanding_across_the_queue(session, shortfall):
    assert payment_queue.total_outstanding(
        session, shortfall["batch"].batch_id
    ) == Decimal("900.00")


# --------------------------------------------------------------------------
# Manual assignment
# --------------------------------------------------------------------------


def test_manual_assignment_creates_a_settlement(session, shortfall):
    settlement = payroll_service.assign_settlement(
        session, shortfall["payable"], shortfall["r_sarah"], Decimal("400")
    )
    session.flush()
    assert settlement.payer_user_id == shortfall["john"].user_id
    assert settlement.recipient_user_id == shortfall["sarah"].user_id
    assert settlement.amount == Decimal("400.00")


def test_manual_assignment_flags_an_incompatible_pairing(session, shortfall):
    """John is Venmo-only; Sarah takes Zelle. Allowed, but flagged."""
    settlement = payroll_service.assign_settlement(
        session, shortfall["payable"], shortfall["r_sarah"], Decimal("400")
    )
    assert settlement.needs_admin_review


def test_manual_assignment_snapshots_a_usable_handle(session, shortfall):
    settlement = payroll_service.assign_settlement(
        session, shortfall["payable"], shortfall["r_mike"], Decimal("300")
    )
    assert settlement.payment_method_note == "Venmo: @mike"
    assert not settlement.needs_admin_review


def test_manual_assignment_cannot_overdraw_the_payer(session, shortfall):
    """A hand-picked pairing gets no licence the matcher would not have."""
    from payroll_bot.ledger import InsufficientCapacity

    with pytest.raises(InsufficientCapacity):
        payroll_service.assign_settlement(
            session, shortfall["payable"], shortfall["r_sarah"], Decimal("500")
        )


def test_manual_assignment_cannot_overdraw_the_recipient(session, shortfall):
    from payroll_bot.ledger import InsufficientCapacity

    with pytest.raises(InsufficientCapacity):
        payroll_service.assign_settlement(
            session, shortfall["payable"], shortfall["r_alex"], Decimal("250")
        )


def test_assignments_accumulate_without_overdrawing(session, shortfall):
    """John's $400 split across two people in the queue."""
    payroll_service.assign_settlement(
        session, shortfall["payable"], shortfall["r_mike"], Decimal("300")
    )
    payroll_service.assign_settlement(
        session, shortfall["payable"], shortfall["r_alex"], Decimal("100")
    )
    session.flush()

    from payroll_bot.ledger import payable_balance

    assert payable_balance(shortfall["payable"]).available == Decimal("0.00")
    assert receivable_balance(shortfall["r_alex"]).available == Decimal("100.00")
    # Sarah was skipped entirely, so her whole claim is still queued.
    assert receivable_balance(shortfall["r_sarah"]).available == Decimal("400.00")


# --------------------------------------------------------------------------
# Payment methods recorded on someone else's behalf
# --------------------------------------------------------------------------


def test_admin_can_record_a_method_for_someone_who_never_opened_the_bot(
    session, batch, make_user
):
    """Payroll names people by @username long before they join."""
    from payroll_bot.models import PaymentMethodKind
    from payroll_bot.services import accounts

    mike = make_user("mike")  # no methods, no telegram_id
    assert accounts.list_payment_methods(session, mike.user_id) == []

    accounts.add_payment_method(
        session, mike, PaymentMethodKind.VENMO, "@MikeExample"
    )
    session.flush()

    methods = accounts.list_payment_methods(session, mike.user_id)
    assert [m.display for m in methods] == ["Venmo: @MikeExample"]


def test_a_recorded_method_makes_the_pairing_compatible(session, batch, make_user):
    """The point of recording it: routing stops being flagged."""
    from payroll_bot.models import PaymentMethodKind
    from payroll_bot.services import accounts

    john = make_user("john", methods=["VENMO"])
    mike = make_user("mike")

    payable = payroll_service.add_payable(session, batch, john, Decimal("100"))
    r_mike = payroll_service.add_receivable(session, batch, mike, Decimal("100"))
    session.flush()

    flagged = payroll_service.assign_settlement(
        session, payable, r_mike, Decimal("50")
    )
    assert flagged.needs_admin_review

    accounts.add_payment_method(
        session, mike, PaymentMethodKind.VENMO, "@MikeExample"
    )
    session.flush()

    ok = payroll_service.assign_settlement(session, payable, r_mike, Decimal("50"))
    assert not ok.needs_admin_review
    assert ok.payment_method_note == "Venmo: @MikeExample"


def test_removing_a_method_deactivates_rather_than_deletes(session, make_user):
    """Settlements reference the method by id, so the row must survive."""
    from payroll_bot.services import accounts

    mike = make_user("mike", methods=["VENMO"])
    method = accounts.list_payment_methods(session, mike.user_id)[0]

    accounts.remove_payment_method(session, method)
    session.flush()

    assert accounts.list_payment_methods(session, mike.user_id) == []
    assert accounts.list_payment_methods(session, mike.user_id, active_only=False)


# --------------------------------------------------------------------------
# Partial payments
# --------------------------------------------------------------------------


def test_partial_payment_credits_only_what_arrived(session, batch, make_user):
    """Routed $500, only $100 sent: credit $100, keep owing $400."""
    from payroll_bot.ledger import payable_balance

    john = make_user("john", methods=["VENMO"])
    mike = make_user("mike", methods=["VENMO"])
    payable = payroll_service.add_payable(session, batch, john, Decimal("500"))
    r_mike = payroll_service.add_receivable(session, batch, mike, Decimal("500"))
    session.flush()

    settlement = payroll_service.assign_settlement(
        session, payable, r_mike, Decimal("500")
    )
    session.flush()

    settlement_service.verify_partial(
        session, settlement, Decimal("100"), actor_user_id=1
    )
    session.flush()

    recipient = receivable_balance(r_mike)
    assert recipient.verified == Decimal("100.00")
    assert recipient.remaining == Decimal("400.00")

    # The payer is credited for what they actually sent, and still owes the rest.
    payer = payable_balance(payable)
    assert payer.verified == Decimal("100.00")
    assert payer.remaining == Decimal("400.00")


def test_the_shortfall_returns_to_the_pool_for_rerouting(session, batch, make_user):
    """The unpaid $400 is not written off - it can be routed again."""
    from payroll_bot.ledger import payable_balance

    john = make_user("john")
    mike = make_user("mike")
    payable = payroll_service.add_payable(session, batch, john, Decimal("500"))
    r_mike = payroll_service.add_receivable(session, batch, mike, Decimal("500"))
    session.flush()

    settlement = payroll_service.assign_settlement(
        session, payable, r_mike, Decimal("500")
    )
    settlement_service.verify_partial(
        session, settlement, Decimal("100"), actor_user_id=1
    )
    session.flush()

    assert payable_balance(payable).available == Decimal("400.00")
    assert receivable_balance(r_mike).available == Decimal("400.00")

    # And it really can be re-routed.
    again = payroll_service.assign_settlement(
        session, payable, r_mike, Decimal("400")
    )
    assert again.amount == Decimal("400.00")


def test_partially_paid_person_stays_in_the_queue(session, batch, make_user):
    john = make_user("john")
    mike = make_user("mike")
    payable = payroll_service.add_payable(session, batch, john, Decimal("500"))
    r_mike = payroll_service.add_receivable(session, batch, mike, Decimal("500"))
    session.flush()

    settlement = payroll_service.assign_settlement(
        session, payable, r_mike, Decimal("500")
    )
    settlement_service.verify_partial(
        session, settlement, Decimal("100"), actor_user_id=1
    )
    session.flush()

    entries = payment_queue.build_queue(session, batch.batch_id)
    assert entries[0].user.label == "@mike"
    assert entries[0].still_owed == Decimal("400.00")


def test_partial_for_the_full_amount_is_a_normal_verification(session, batch, make_user):
    john = make_user("john")
    mike = make_user("mike")
    payable = payroll_service.add_payable(session, batch, john, Decimal("500"))
    r_mike = payroll_service.add_receivable(session, batch, mike, Decimal("500"))
    session.flush()

    settlement = payroll_service.assign_settlement(
        session, payable, r_mike, Decimal("500")
    )
    settlement_service.verify_partial(
        session, settlement, Decimal("500"), actor_user_id=1
    )
    session.flush()
    assert receivable_balance(r_mike).remaining == Decimal("0.00")


def test_cannot_credit_more_than_was_routed(session, batch, make_user):
    john = make_user("john")
    mike = make_user("mike")
    payable = payroll_service.add_payable(session, batch, john, Decimal("500"))
    r_mike = payroll_service.add_receivable(session, batch, mike, Decimal("500"))
    session.flush()

    settlement = payroll_service.assign_settlement(
        session, payable, r_mike, Decimal("200")
    )
    with pytest.raises(settlement_service.SettlementError):
        settlement_service.verify_partial(
            session, settlement, Decimal("300"), actor_user_id=1
        )


def test_partial_records_both_figures_in_the_audit_trail(session, batch, make_user):
    """The original routed amount must survive, not just what arrived."""
    from payroll_bot import audit

    john = make_user("john")
    mike = make_user("mike")
    payable = payroll_service.add_payable(session, batch, john, Decimal("500"))
    r_mike = payroll_service.add_receivable(session, batch, mike, Decimal("500"))
    session.flush()

    settlement = payroll_service.assign_settlement(
        session, payable, r_mike, Decimal("500")
    )
    settlement_service.verify_partial(
        session, settlement, Decimal("100"), actor_user_id=1, reason="sent wrong amount"
    )
    session.flush()

    detail = " ".join(
        e.detail or ""
        for e in audit.history(
            session, entity_type="settlement", entity_id=settlement.settlement_id
        )
    )
    assert "$100.00" in detail and "$500.00" in detail
    assert "sent wrong amount" in detail


# --------------------------------------------------------------------------
# Recording payments by person
# --------------------------------------------------------------------------


def test_paid_by_person_spends_across_routed_settlements(session, batch, make_user):
    """@mike is owed via two routed payments; $350 clears the first and dents the second."""
    john = make_user("john")
    chris = make_user("chris")
    mike = make_user("mike")
    p_john = payroll_service.add_payable(session, batch, john, Decimal("300"))
    p_chris = payroll_service.add_payable(session, batch, chris, Decimal("300"))
    r_mike = payroll_service.add_receivable(session, batch, mike, Decimal("600"))
    session.flush()

    payroll_service.assign_settlement(session, p_john, r_mike, Decimal("300"))
    payroll_service.assign_settlement(session, p_chris, r_mike, Decimal("300"))
    session.flush()

    touched, leftover = settlement_service.record_payment_to(
        session, batch.batch_id, mike.user_id, Decimal("350"), actor_user_id=1
    )
    session.flush()

    assert len(touched) == 2
    assert leftover == Decimal("0.00")
    assert receivable_balance(r_mike).verified == Decimal("350.00")
    assert receivable_balance(r_mike).remaining == Decimal("250.00")


def test_paid_by_person_reports_what_it_could_not_apply(session, batch, make_user):
    """More arrived than was ever routed - say so rather than inventing credit."""
    john = make_user("john")
    mike = make_user("mike")
    p_john = payroll_service.add_payable(session, batch, john, Decimal("100"))
    r_mike = payroll_service.add_receivable(session, batch, mike, Decimal("500"))
    session.flush()
    payroll_service.assign_settlement(session, p_john, r_mike, Decimal("100"))
    session.flush()

    touched, leftover = settlement_service.record_payment_to(
        session, batch.batch_id, mike.user_id, Decimal("400"), actor_user_id=1
    )
    session.flush()

    assert len(touched) == 1
    assert leftover == Decimal("300.00")
    assert receivable_balance(r_mike).verified == Decimal("100.00")


def test_paid_by_person_with_nothing_routed_applies_nothing(session, batch, make_user):
    mike = make_user("mike")
    payroll_service.add_receivable(session, batch, mike, Decimal("500"))
    session.flush()

    touched, leftover = settlement_service.record_payment_to(
        session, batch.batch_id, mike.user_id, Decimal("100"), actor_user_id=1
    )
    assert touched == []
    assert leftover == Decimal("100.00")


def test_paid_partially_clears_a_single_settlement(session, batch, make_user):
    """The screenshot case: routed 500, only 100 arrived."""
    john = make_user("john")
    mike = make_user("mike")
    p_john = payroll_service.add_payable(session, batch, john, Decimal("500"))
    r_mike = payroll_service.add_receivable(session, batch, mike, Decimal("500"))
    session.flush()
    payroll_service.assign_settlement(session, p_john, r_mike, Decimal("500"))
    session.flush()

    touched, leftover = settlement_service.record_payment_to(
        session, batch.batch_id, mike.user_id, Decimal("100"), actor_user_id=1
    )
    session.flush()

    assert leftover == Decimal("0.00")
    assert touched[0].amount == Decimal("100.00")
    assert receivable_balance(r_mike).remaining == Decimal("400.00")
    # And the unpaid 400 is routable again on both sides.
    from payroll_bot.ledger import payable_balance
    assert payable_balance(session.get(type(p_john), p_john.payable_id)).available == Decimal("400.00")


# --------------------------------------------------------------------------
# Writing off a balance
# --------------------------------------------------------------------------


def test_clearing_a_payable_zeroes_what_they_owe(session, batch, make_user):
    from payroll_bot.ledger import payable_balance

    john = make_user("john")
    payable = payroll_service.add_payable(session, batch, john, Decimal("500"))
    session.flush()

    cleared = payroll_service.write_off(session, payable, actor_user_id=1)
    session.flush()

    assert cleared == Decimal("500.00")
    assert payable_balance(payable).remaining == Decimal("0.00")


def test_clearing_keeps_payments_that_really_happened(session, batch, make_user):
    """A write-off drops the unpaid remainder, never the verified history."""
    from payroll_bot.ledger import payable_balance

    john = make_user("john")
    mike = make_user("mike")
    payable = payroll_service.add_payable(session, batch, john, Decimal("500"))
    r_mike = payroll_service.add_receivable(session, batch, mike, Decimal("500"))
    session.flush()

    settlement = payroll_service.assign_settlement(
        session, payable, r_mike, Decimal("200")
    )
    settlement_service.admin_verify(session, settlement, actor_user_id=1)
    session.flush()

    cleared = payroll_service.write_off(session, payable, actor_user_id=1)
    session.flush()

    assert cleared == Decimal("300.00")
    balance = payable_balance(payable)
    assert balance.verified == Decimal("200.00")
    assert balance.remaining == Decimal("0.00")


def test_clearing_is_refused_while_a_settlement_is_routed(session, batch, make_user):
    """Writing off money someone was told to pay would strand the instruction."""
    john = make_user("john")
    mike = make_user("mike")
    payable = payroll_service.add_payable(session, batch, john, Decimal("500"))
    r_mike = payroll_service.add_receivable(session, batch, mike, Decimal("500"))
    session.flush()
    payroll_service.assign_settlement(session, payable, r_mike, Decimal("500"))
    session.flush()

    with pytest.raises(payroll_service.PayrollError, match="cancel"):
        payroll_service.write_off(session, payable, actor_user_id=1)


def test_clearing_a_receivable_removes_them_from_the_queue(session, batch, make_user):
    mike = make_user("mike")
    receivable = payroll_service.add_receivable(session, batch, mike, Decimal("500"))
    session.flush()

    payroll_service.write_off(session, receivable, actor_user_id=1, reason="paid in cash")
    session.flush()

    assert payment_queue.build_queue(session, batch.batch_id) == []


def test_clearing_nothing_outstanding_is_refused(session, batch, make_user):
    john = make_user("john")
    payable = payroll_service.add_payable(session, batch, john, Decimal("100"))
    session.flush()
    payroll_service.write_off(session, payable, actor_user_id=1)
    session.flush()

    with pytest.raises(payroll_service.PayrollError, match="nothing outstanding"):
        payroll_service.write_off(session, payable, actor_user_id=1)


def test_a_write_off_is_recorded_in_the_audit_trail(session, batch, make_user):
    from payroll_bot import audit

    john = make_user("john")
    payable = payroll_service.add_payable(session, batch, john, Decimal("500"))
    session.flush()
    payroll_service.write_off(
        session, payable, actor_user_id=1, reason="settled outside payroll"
    )
    session.flush()

    detail = " ".join(
        e.detail or "" for e in audit.history(session, batch_id=batch.batch_id)
    )
    assert "wrote off" in detail
    assert "settled outside payroll" in detail


# --------------------------------------------------------------------------
# Adding to a running ledger
# --------------------------------------------------------------------------


def test_a_second_entry_combines_into_one_balance(session, batch, make_user):
    """One person carries one balance per side; entering more raises it."""
    from payroll_bot.ledger import payable_balance

    john = make_user("john")
    first = payroll_service.add_payable(session, batch, john, Decimal("500"))
    second = payroll_service.add_payable(
        session, batch, john, Decimal("200"), description="late fee"
    )
    session.flush()

    assert second is first, "a second entry must fold into the first"
    assert payable_balance(first).original == Decimal("700.00")

    totals = payroll_service.batch_totals(session, batch)
    assert totals.payable.original == Decimal("700.00")
    assert totals.people_owing == 1


def test_combining_never_shows_someone_twice_in_the_queue(session, batch, make_user):
    mike = make_user("mike")
    sarah = make_user("sarah")
    payroll_service.add_receivable(session, batch, mike, Decimal("300"))
    payroll_service.add_receivable(session, batch, sarah, Decimal("400"))
    payroll_service.add_receivable(session, batch, mike, Decimal("100"))
    session.flush()

    entries = payment_queue.build_queue(session, batch.batch_id)
    assert [e.user.label for e in entries] == ["@mike", "@sarah"]
    assert entries[0].still_owed == Decimal("400.00")
    assert payment_queue.total_outstanding(session, batch.batch_id) == Decimal("800.00")


def test_topping_up_keeps_a_persons_place_in_the_queue(session, batch, make_user):
    """Being owed more on Friday must not cost someone their Monday place."""
    mike = make_user("mike")
    sarah = make_user("sarah")
    payroll_service.add_receivable(session, batch, mike, Decimal("300"))
    payroll_service.add_receivable(session, batch, sarah, Decimal("400"))
    session.flush()
    payroll_service.add_receivable(session, batch, mike, Decimal("500"))
    session.flush()

    entries = payment_queue.build_queue(session, batch.batch_id)
    assert entries[0].user.label == "@mike"
    assert entries[0].still_owed == Decimal("800.00")


def test_a_written_off_balance_leaves_the_batch_totals(session, batch, make_user):
    john = make_user("john")
    chris = make_user("chris")
    keep = payroll_service.add_payable(session, batch, john, Decimal("500"))
    drop = payroll_service.add_payable(session, batch, chris, Decimal("200"))
    session.flush()

    payroll_service.write_off(session, drop, actor_user_id=1, reason="entered twice")
    session.flush()

    totals = payroll_service.batch_totals(session, batch)
    assert totals.payable.original == Decimal("500.00")
    assert totals.payable.remaining == Decimal("500.00")
    assert keep.status.value == "OPEN"


def test_a_new_entry_after_a_write_off_starts_fresh(session, batch, make_user):
    """A cleared balance is deliberately closed and must not be revived."""
    john = make_user("john")
    first = payroll_service.add_payable(session, batch, john, Decimal("500"))
    payroll_service.write_off(session, first, actor_user_id=1)
    session.flush()

    second = payroll_service.add_payable(session, batch, john, Decimal("200"))
    session.flush()

    assert second is not first
    assert payroll_service.batch_totals(session, batch).payable.original == Decimal("200.00")


# --------------------------------------------------------------------------
# Knocking part of a balance off
# --------------------------------------------------------------------------


def test_clearing_part_of_a_balance_leaves_the_rest(session, batch, make_user):
    """The headline case: owed 100, knock 15 off, 85 left."""
    from payroll_bot.ledger import payable_balance

    jlee = make_user("jlee")
    payable = payroll_service.add_payable(session, batch, jlee, Decimal("100"))
    session.flush()

    cleared = payroll_service.write_off(
        session, payable, Decimal("15"), actor_user_id=1, reason="paid in cash"
    )
    session.flush()

    assert cleared == Decimal("15.00")
    balance = payable_balance(payable)
    assert balance.original == Decimal("85.00")
    assert balance.remaining == Decimal("85.00")
    assert payable.status.value == "OPEN"


def test_clearing_more_than_is_outstanding_is_refused(session, batch, make_user):
    jlee = make_user("jlee")
    payable = payroll_service.add_payable(session, batch, jlee, Decimal("100"))
    session.flush()

    with pytest.raises(payroll_service.PayrollError, match="outstanding"):
        payroll_service.write_off(session, payable, Decimal("150"), actor_user_id=1)


def test_clearing_the_exact_outstanding_amount_closes_it(session, batch, make_user):
    from payroll_bot.ledger import payable_balance

    jlee = make_user("jlee")
    payable = payroll_service.add_payable(session, batch, jlee, Decimal("100"))
    session.flush()

    payroll_service.write_off(session, payable, Decimal("100"), actor_user_id=1)
    session.flush()

    assert payable_balance(payable).remaining == Decimal("0.00")
    assert payable.status.value == "CANCELLED"


def test_a_partial_clear_is_recorded_with_both_figures(session, batch, make_user):
    from payroll_bot import audit

    jlee = make_user("jlee")
    payable = payroll_service.add_payable(session, batch, jlee, Decimal("100"))
    session.flush()
    payroll_service.write_off(
        session, payable, Decimal("15"), actor_user_id=1, reason="paid in cash"
    )
    session.flush()

    detail = " ".join(
        e.detail or "" for e in audit.history(session, batch_id=batch.batch_id)
    )
    assert "wrote off" in detail and "$15.00" in detail
    assert "paid in cash" in detail
