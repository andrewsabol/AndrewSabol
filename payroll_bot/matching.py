"""The settlement matching engine.

Takes the set of amounts owed *into* the system and the set of amounts owed
*by* the system, and produces a reasonable set of direct payer-to-recipient
transfers.

The engine deliberately knows nothing about the database or Telegram. It
consumes :class:`Party` values and emits :class:`SettlementProposal` values, so
the admin preview can be generated, recalculated and discarded without touching
persisted balances -- which matters, because nothing may reach a user before the
admin approves the plan.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from .money import ZERO, money
from .strategies import (
    FORBIDDEN,
    MatchContext,
    Party,
    PaymentCompatibilityStrategy,
    SettlementProposal,
    SettlementStrategy,
    default_strategy,
)


class MatchingError(RuntimeError):
    pass


@dataclass
class MatchResult:
    proposals: list[SettlementProposal]
    unmatched_payers: list[Party]
    unmatched_recipients: list[Party]
    strategy_name: str

    @property
    def transfer_count(self) -> int:
        return len(self.proposals)

    @property
    def total_routed(self) -> Decimal:
        return money(sum((p.amount for p in self.proposals), ZERO))

    @property
    def flagged_count(self) -> int:
        return sum(1 for p in self.proposals if p.needs_admin_review)

    @property
    def is_complete(self) -> bool:
        """True when every party was fully routed."""
        return not self.unmatched_payers and not self.unmatched_recipients


class MatchingEngine:
    """Greedy, strategy-driven bipartite matcher.

    Each iteration scores every viable (payer, recipient) pair and takes the
    best one, routing the largest amount both sides can support. Because every
    match exhausts at least one party, the loop terminates in at most
    ``len(payers) + len(recipients) - 1`` iterations.

    Partial settlement falls out of this naturally: if John owes $500 and Mike
    is owed $700, the match routes $500 and leaves Mike with $200 still
    outstanding, reported in ``unmatched_recipients``.
    """

    def __init__(self, strategy: SettlementStrategy | None = None) -> None:
        self.strategy = strategy or default_strategy()

    def match(self, payers: list[Party], recipients: list[Party]) -> MatchResult:
        # Work on copies: the caller's parties describe persisted state and must
        # not be mutated by a preview that may never be approved.
        working_payers = [_clone(p) for p in payers if p.available > ZERO]
        working_recipients = [_clone(r) for r in recipients if r.available > ZERO]

        currency = _single_currency(working_payers + working_recipients)
        context = MatchContext(
            payers=working_payers, recipients=working_recipients, currency=currency
        )
        proposals: list[SettlementProposal] = []

        max_iterations = (len(working_payers) + len(working_recipients) + 1) ** 2
        iterations = 0

        while True:
            iterations += 1
            if iterations > max_iterations:  # pragma: no cover - safety valve
                raise MatchingError(
                    "matching failed to converge; this indicates a strategy that "
                    "never exhausts a party"
                )

            pair = self._best_pair(context)
            if pair is None:
                break
            payer, recipient, _score = pair

            amount = self._transfer_amount(payer, recipient, context)
            if amount <= ZERO:
                # No strategy permits a positive transfer for the best pair;
                # stop rather than spin.
                break

            shared = PaymentCompatibilityStrategy.shared_methods(payer, recipient)
            proposals.append(
                SettlementProposal(
                    payer_user_id=payer.user_id,
                    payer_label=payer.label,
                    payable_id=payer.entry_id,
                    recipient_user_id=recipient.user_id,
                    recipient_label=recipient.label,
                    receivable_id=recipient.entry_id,
                    amount=amount,
                    currency=currency,
                    shared_method=sorted(shared)[0] if shared else None,
                    needs_admin_review=not shared,
                )
            )

            payer.available = money(payer.available - amount)
            recipient.available = money(recipient.available - amount)

            context.payers = [p for p in context.payers if p.available > ZERO]
            context.recipients = [r for r in context.recipients if r.available > ZERO]

        return MatchResult(
            proposals=proposals,
            unmatched_payers=list(context.payers),
            unmatched_recipients=list(context.recipients),
            strategy_name=getattr(self.strategy, "name", type(self.strategy).__name__),
        )

    def _best_pair(
        self, context: MatchContext
    ) -> tuple[Party, Party, float] | None:
        best: tuple[Party, Party, float] | None = None
        for payer in context.payers:
            for recipient in context.recipients:
                if payer.user_id == recipient.user_id:
                    # A user who both owes and is owed nets out; routing money
                    # to themselves is never a real instruction.
                    continue
                score = self.strategy.score_pair(payer, recipient, context)
                if score == FORBIDDEN:
                    continue
                if best is None or score > best[2]:
                    best = (payer, recipient, score)
        return best

    def _transfer_amount(
        self, payer: Party, recipient: Party, context: MatchContext
    ) -> Decimal:
        amount = min(payer.available, recipient.available)
        proposed = self.strategy.propose_amount(payer, recipient, context)
        if proposed is not None:
            amount = min(amount, money(proposed))
        return money(amount)


def _clone(party: Party) -> Party:
    return Party(
        user_id=party.user_id,
        label=party.label,
        entry_id=party.entry_id,
        available=party.available,
        currency=party.currency,
        payment_methods=party.payment_methods,
        priority=party.priority,
    )


def _single_currency(parties: list[Party]) -> str:
    currencies = {p.currency for p in parties}
    if len(currencies) > 1:
        raise MatchingError(
            f"cannot match across currencies: {', '.join(sorted(currencies))}"
        )
    return currencies.pop() if currencies else "USD"
