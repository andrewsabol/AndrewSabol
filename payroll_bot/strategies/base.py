"""The :class:`SettlementStrategy` interface and the composition machinery.

Strategies do not each reimplement a matching loop. Instead the engine owns one
greedy loop and asks strategies to *score* candidate pairings; that way any set
of strategies can be combined by summing weighted scores, which is what the
"architecture should allow these strategies to be combined later" requirement
needs.

A strategy contributes two things:

``score_pair``
    How desirable is routing money from this payer to this recipient? Higher is
    better. Returning :data:`FORBIDDEN` vetoes the pairing outright.

``propose_amount``
    Optionally cap how much to route. The engine takes the minimum of all
    proposals and the two sides' available capacity.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from decimal import Decimal

from ..money import ZERO, money

#: Sentinel score meaning "this pairing must not be made".
FORBIDDEN = float("-inf")


@dataclass
class Party:
    """One side of a candidate settlement, as the engine sees it.

    Deliberately decoupled from the ORM: the engine works on plain values so it
    can be unit-tested and so a future "what-if" planner can run it on
    hypothetical numbers that were never persisted.
    """

    user_id: int
    label: str
    entry_id: int
    """The payable_id or receivable_id backing this party."""

    available: Decimal
    currency: str = "USD"
    payment_methods: frozenset[str] = field(default_factory=frozenset)
    priority: int = 0

    def __post_init__(self) -> None:
        self.available = money(self.available)


@dataclass
class MatchContext:
    """Everything a strategy may consult while scoring."""

    payers: list[Party]
    recipients: list[Party]
    currency: str = "USD"

    def remaining_payable(self) -> Decimal:
        return money(sum((p.available for p in self.payers), ZERO))

    def remaining_receivable(self) -> Decimal:
        return money(sum((r.available for r in self.recipients), ZERO))


@dataclass
class SettlementProposal:
    """A single generated transfer instruction, before persistence."""

    payer_user_id: int
    payer_label: str
    payable_id: int
    recipient_user_id: int
    recipient_label: str
    receivable_id: int
    amount: Decimal
    currency: str = "USD"
    shared_method: str | None = None
    needs_admin_review: bool = False
    """True when payer and recipient share no compatible payment method."""

    def __post_init__(self) -> None:
        self.amount = money(self.amount)


class SettlementStrategy(ABC):
    """Scores candidate payer/recipient pairings for the matching engine."""

    #: Human-readable name, surfaced in the admin plan preview.
    name: str = "strategy"

    @abstractmethod
    def score_pair(self, payer: Party, recipient: Party, context: MatchContext) -> float:
        """Return the desirability of this pairing. Higher wins."""

    def propose_amount(
        self, payer: Party, recipient: Party, context: MatchContext
    ) -> Decimal | None:
        """Optionally cap the transfer size. ``None`` means "no opinion"."""
        return None

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"<{type(self).__name__}>"


@dataclass
class WeightedStrategy:
    strategy: SettlementStrategy
    weight: float = 1.0


class CompositeStrategy(SettlementStrategy):
    """Combines several strategies by weighted sum of their scores.

    A :data:`FORBIDDEN` from any component vetoes the pair regardless of what
    the others say -- a veto is a hard constraint, not an opinion to be
    outvoted.
    """

    def __init__(self, *components: SettlementStrategy | WeightedStrategy) -> None:
        self.components: list[WeightedStrategy] = [
            c if isinstance(c, WeightedStrategy) else WeightedStrategy(c)
            for c in components
        ]
        self.name = " + ".join(c.strategy.name for c in self.components) or "composite"

    def score_pair(self, payer: Party, recipient: Party, context: MatchContext) -> float:
        running = 0.0
        for component in self.components:
            score = component.strategy.score_pair(payer, recipient, context)
            if score == FORBIDDEN:
                return FORBIDDEN
            running += score * component.weight
        return running

    def propose_amount(
        self, payer: Party, recipient: Party, context: MatchContext
    ) -> Decimal | None:
        caps = [
            proposed
            for component in self.components
            if (proposed := component.strategy.propose_amount(payer, recipient, context))
            is not None
        ]
        return min(caps) if caps else None
