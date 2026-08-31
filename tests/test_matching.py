"""Matching engine behaviour, including the spec's worked examples."""

from decimal import Decimal

import pytest

from payroll_bot.matching import MatchingEngine, MatchingError
from payroll_bot.money import ZERO
from payroll_bot.strategies import (
    AdminPriorityStrategy,
    CompositeStrategy,
    MaxTransferSizeStrategy,
    MinimumTransfersStrategy,
    Party,
    WeightedStrategy,
    default_strategy,
)


def P(uid, label, amount, methods=(), priority=0):
    return Party(
        user_id=uid,
        label=label,
        entry_id=uid,
        available=Decimal(amount),
        payment_methods=frozenset(methods),
        priority=priority,
    )


def routed(result):
    return {(p.payer_label, p.recipient_label): p.amount for p in result.proposals}


def test_spec_example_balances_everyone_to_zero():
    """OWES John 700, Chris 400 / OWED Mike 500, Sarah 350, Alex 250."""
    payers = [P(1, "John", "700"), P(2, "Chris", "400")]
    recipients = [P(3, "Mike", "500"), P(4, "Sarah", "350"), P(5, "Alex", "250")]

    result = MatchingEngine(MinimumTransfersStrategy()).match(payers, recipients)

    assert result.total_routed == Decimal("1100.00")
    assert result.is_complete
    # Every party fully consumed.
    assert not result.unmatched_payers
    assert not result.unmatched_recipients

    # Each side's totals are respected.
    per_payer = {}
    per_recipient = {}
    for proposal in result.proposals:
        per_payer[proposal.payer_label] = (
            per_payer.get(proposal.payer_label, ZERO) + proposal.amount
        )
        per_recipient[proposal.recipient_label] = (
            per_recipient.get(proposal.recipient_label, ZERO) + proposal.amount
        )
    assert per_payer == {"John": Decimal("700"), "Chris": Decimal("400")}
    assert per_recipient == {
        "Mike": Decimal("500"),
        "Sarah": Decimal("350"),
        "Alex": Decimal("250"),
    }


def test_simple_split_matches_spec():
    """John owes 500; Mike owed 300, Sarah owed 200."""
    result = MatchingEngine(MinimumTransfersStrategy()).match(
        [P(1, "John", "500")], [P(2, "Mike", "300"), P(3, "Sarah", "200")]
    )
    assert routed(result) == {
        ("John", "Mike"): Decimal("300.00"),
        ("John", "Sarah"): Decimal("200.00"),
    }


def test_partial_settlement_leaves_recipient_outstanding():
    """John owes 500, Mike is owed 700 -> route 500, Mike still owed 200."""
    result = MatchingEngine(MinimumTransfersStrategy()).match(
        [P(1, "John", "500")], [P(2, "Mike", "700")]
    )
    assert routed(result) == {("John", "Mike"): Decimal("500.00")}
    assert not result.unmatched_payers
    assert len(result.unmatched_recipients) == 1
    assert result.unmatched_recipients[0].available == Decimal("200.00")
    assert not result.is_complete


def test_exact_matches_are_preferred_to_minimise_transfers():
    """An exact pairing clears two parties in one transfer; it should win."""
    payers = [P(1, "A", "300"), P(2, "B", "700")]
    recipients = [P(3, "X", "700"), P(4, "Y", "300")]

    result = MatchingEngine(MinimumTransfersStrategy()).match(payers, recipients)

    # Two exact pairings exist; the greedy matcher must find both.
    assert result.transfer_count == 2
    assert routed(result) == {
        ("B", "X"): Decimal("700.00"),
        ("A", "Y"): Decimal("300.00"),
    }


def test_transfer_count_stays_within_greedy_bound():
    payers = [P(i, f"P{i}", "100") for i in range(1, 6)]
    recipients = [P(10 + i, f"R{i}", "100") for i in range(1, 6)]
    result = MatchingEngine(MinimumTransfersStrategy()).match(payers, recipients)
    assert result.transfer_count <= len(payers) + len(recipients) - 1
    assert result.is_complete


def test_prefers_compatible_payment_methods():
    """John has Venmo+CashApp; Mike takes Venmo, Sarah takes Zelle only."""
    payers = [P(1, "John", "300", ["VENMO", "CASHAPP"])]
    recipients = [P(2, "Mike", "300", ["VENMO"]), P(3, "Sarah", "300", ["ZELLE"])]

    result = MatchingEngine(default_strategy()).match(payers, recipients)

    assert routed(result) == {("John", "Mike"): Decimal("300.00")}
    assert result.proposals[0].shared_method == "VENMO"
    assert not result.proposals[0].needs_admin_review


def test_incompatible_pairing_is_routed_but_flagged():
    result = MatchingEngine(default_strategy()).match(
        [P(1, "John", "100", ["VENMO"])], [P(2, "Sarah", "100", ["ZELLE"])]
    )
    assert result.transfer_count == 1
    assert result.proposals[0].needs_admin_review
    assert result.flagged_count == 1


def test_strict_mode_refuses_incompatible_pairing():
    strategy = default_strategy(strict_payment_methods=True)
    result = MatchingEngine(strategy).match(
        [P(1, "John", "100", ["VENMO"])], [P(2, "Sarah", "100", ["ZELLE"])]
    )
    assert result.transfer_count == 0
    assert result.unmatched_payers and result.unmatched_recipients


def test_admin_priority_pays_higher_priority_recipient_first():
    payers = [P(1, "John", "300")]
    recipients = [
        P(2, "Normal", "300", priority=0),
        P(3, "Urgent", "300", priority=5),
    ]
    strategy = CompositeStrategy(
        WeightedStrategy(AdminPriorityStrategy(), 10.0),
        WeightedStrategy(MinimumTransfersStrategy(), 1.0),
    )
    result = MatchingEngine(strategy).match(payers, recipients)
    assert result.proposals[0].recipient_label == "Urgent"


def test_max_transfer_size_caps_each_payment():
    strategy = CompositeStrategy(
        WeightedStrategy(MinimumTransfersStrategy(), 1.0),
        WeightedStrategy(MaxTransferSizeStrategy("200"), 1.0),
    )
    result = MatchingEngine(strategy).match(
        [P(1, "John", "500")], [P(2, "Mike", "500")]
    )
    assert all(p.amount <= Decimal("200") for p in result.proposals)
    assert result.total_routed == Decimal("500.00")
    assert result.transfer_count == 3  # 200 + 200 + 100


def test_never_routes_a_user_to_themselves():
    """Someone who both owes and is owed must not be told to pay themselves."""
    payers = [P(1, "John", "100")]
    recipients = [P(1, "John", "100"), P(2, "Mike", "100")]
    result = MatchingEngine(MinimumTransfersStrategy()).match(payers, recipients)
    assert all(
        p.payer_user_id != p.recipient_user_id for p in result.proposals
    )
    assert routed(result) == {("John", "Mike"): Decimal("100.00")}


def test_engine_does_not_mutate_caller_parties():
    """A preview that is never approved must leave the inputs untouched."""
    payers = [P(1, "John", "500")]
    recipients = [P(2, "Mike", "500")]
    MatchingEngine(MinimumTransfersStrategy()).match(payers, recipients)
    assert payers[0].available == Decimal("500.00")
    assert recipients[0].available == Decimal("500.00")


def test_unbalanced_payroll_routes_the_smaller_side():
    payers = [P(1, "John", "5420")]
    recipients = [P(2, "Mike", "5370")]
    result = MatchingEngine(MinimumTransfersStrategy()).match(payers, recipients)
    assert result.total_routed == Decimal("5370.00")
    assert result.unmatched_payers[0].available == Decimal("50.00")


def test_cross_currency_matching_is_refused():
    payers = [P(1, "John", "100")]
    recipients = [Party(2, "Mike", 2, Decimal("100"), currency="EUR")]
    with pytest.raises(MatchingError):
        MatchingEngine(MinimumTransfersStrategy()).match(payers, recipients)


def test_amounts_stay_exact_with_awkward_cents():
    payers = [P(1, "A", "33.33"), P(2, "B", "33.33"), P(3, "C", "33.34")]
    recipients = [P(4, "X", "50.00"), P(5, "Y", "50.00")]
    result = MatchingEngine(MinimumTransfersStrategy()).match(payers, recipients)
    assert result.total_routed == Decimal("100.00")
    assert result.is_complete
