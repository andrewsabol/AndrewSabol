"""The three initial settlement strategies."""

from __future__ import annotations

from decimal import Decimal

from ..money import ZERO, money
from .base import FORBIDDEN, MatchContext, Party, SettlementStrategy


class MinimumTransfersStrategy(SettlementStrategy):
    """Reduce the number of individual payments people have to make.

    Two heuristics, in order of strength:

    1. An *exact* pairing (payer's outstanding equals recipient's outstanding)
       clears both sides in one transfer. Nothing beats that, so it gets a
       dominant bonus.
    2. Otherwise, move the largest possible chunk. Each greedy match zeroes at
       least one party, so maximising the chunk minimises how many parties are
       left needing another transfer.

    This is the standard greedy heuristic for the min-cash-flow problem. Finding
    a provably minimal set is NP-hard (it reduces to subset-sum), and the greedy
    bound of ``payers + recipients - 1`` transfers is what a payroll admin
    actually cares about.
    """

    name = "minimum-transfers"

    #: Weight of the exact-match bonus relative to the chunk-size score.
    EXACT_MATCH_BONUS = 100.0

    def score_pair(self, payer: Party, recipient: Party, context: MatchContext) -> float:
        if payer.available <= ZERO or recipient.available <= ZERO:
            return FORBIDDEN

        chunk = min(payer.available, recipient.available)
        scale = max(context.remaining_payable(), Decimal("1"))
        # Normalized 0..1 so the score stays comparable across payroll sizes.
        score = float(chunk / scale)

        if payer.available == recipient.available:
            score += self.EXACT_MATCH_BONUS
        return score


class PaymentCompatibilityStrategy(SettlementStrategy):
    """Prefer pairings where the payer can actually pay the recipient.

    If John has Venmo and Cash App, Mike accepts Venmo, and Sarah accepts only
    Zelle, routing John to Mike is worth far more than routing John to Sarah --
    the second instruction is one John cannot follow.

    In ``strict`` mode an incompatible pairing is vetoed outright. By default it
    is merely heavily penalised, so a payroll can still balance when someone has
    no compatible counterpart; the resulting settlement is flagged for admin
    review instead of being silently dropped.
    """

    name = "payment-compatibility"

    def __init__(self, *, strict: bool = False, bonus: float = 10.0) -> None:
        self.strict = strict
        self.bonus = bonus

    @staticmethod
    def shared_methods(payer: Party, recipient: Party) -> frozenset[str]:
        return payer.payment_methods & recipient.payment_methods

    def score_pair(self, payer: Party, recipient: Party, context: MatchContext) -> float:
        if self.shared_methods(payer, recipient):
            return self.bonus
        if self.strict:
            return FORBIDDEN
        # Not disqualifying: the pairing is allowed but will be flagged.
        return -self.bonus


class AdminPriorityStrategy(SettlementStrategy):
    """Pay higher-priority recipients first.

    Admins designate priority on the recipient (a contractor who must be paid
    before anyone else, say). Priority breaks ties; it does not override the
    hard capacity constraints the engine enforces.
    """

    name = "admin-priority"

    def __init__(self, *, weight_per_level: float = 5.0) -> None:
        self.weight_per_level = weight_per_level

    def score_pair(self, payer: Party, recipient: Party, context: MatchContext) -> float:
        return recipient.priority * self.weight_per_level


class MaxTransferSizeStrategy(SettlementStrategy):
    """Cap any single transfer, e.g. to stay under a payment app's limit.

    Included to demonstrate that ``propose_amount`` composes: it expresses an
    opinion on size without expressing one on pairing.
    """

    name = "max-transfer-size"

    def __init__(self, cap: Decimal | str) -> None:
        self.cap = money(cap)

    def score_pair(self, payer: Party, recipient: Party, context: MatchContext) -> float:
        return 0.0

    def propose_amount(
        self, payer: Party, recipient: Party, context: MatchContext
    ) -> Decimal | None:
        return self.cap
