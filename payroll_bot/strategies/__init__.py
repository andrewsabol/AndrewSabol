"""Settlement strategies.

The default strategy combines all three initial strategies: it minimises the
number of transfers, prefers payment-method-compatible pairings, and honours
admin-designated recipient priority.
"""

from .base import (
    FORBIDDEN,
    CompositeStrategy,
    MatchContext,
    Party,
    SettlementProposal,
    SettlementStrategy,
    WeightedStrategy,
)
from .builtin import (
    AdminPriorityStrategy,
    MaxTransferSizeStrategy,
    MinimumTransfersStrategy,
    PaymentCompatibilityStrategy,
)


def default_strategy(*, strict_payment_methods: bool = False) -> CompositeStrategy:
    """The strategy used when an admin presses *Generate Settlements*.

    Weights are ordered by how much each concern should dominate: an
    unpayable instruction is worse than an extra transfer, and priority only
    breaks ties.
    """
    return CompositeStrategy(
        WeightedStrategy(PaymentCompatibilityStrategy(strict=strict_payment_methods), 3.0),
        WeightedStrategy(AdminPriorityStrategy(), 2.0),
        WeightedStrategy(MinimumTransfersStrategy(), 1.0),
    )


__all__ = [
    "FORBIDDEN",
    "AdminPriorityStrategy",
    "CompositeStrategy",
    "MatchContext",
    "MaxTransferSizeStrategy",
    "MinimumTransfersStrategy",
    "Party",
    "PaymentCompatibilityStrategy",
    "SettlementProposal",
    "SettlementStrategy",
    "WeightedStrategy",
    "default_strategy",
]
