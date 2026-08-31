"""Currency handling.

Every monetary value in this system is a :class:`decimal.Decimal` quantized to
two places. Floating point is never used for money -- binary floats cannot
represent decimal cents exactly, and a payroll ledger that loses a cent is a
ledger that cannot be reconciled.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Iterable

#: All amounts are stored and compared at this exponent.
CENTS = Decimal("0.01")

ZERO = Decimal("0.00")


class MoneyError(ValueError):
    """Raised when a value cannot be interpreted as a monetary amount."""


def money(value: object) -> Decimal:
    """Coerce ``value`` into a two-place :class:`Decimal`.

    Accepts ``Decimal``, ``int``, and ``str``. ``float`` is rejected outright:
    accepting it would silently import binary rounding error into the ledger.
    """
    if isinstance(value, float):
        raise MoneyError(
            "refusing to build money from float; pass a str or Decimal instead"
        )
    if isinstance(value, Decimal):
        candidate = value
    elif isinstance(value, int):
        candidate = Decimal(value)
    elif isinstance(value, str):
        cleaned = value.strip().replace(",", "").replace("$", "")
        if not cleaned:
            raise MoneyError("empty monetary value")
        try:
            candidate = Decimal(cleaned)
        except InvalidOperation as exc:
            raise MoneyError(f"not a valid amount: {value!r}") from exc
    else:
        raise MoneyError(f"unsupported monetary type: {type(value).__name__}")

    if not candidate.is_finite():
        raise MoneyError(f"amount must be finite: {value!r}")
    return candidate.quantize(CENTS, rounding=ROUND_HALF_UP)


def parse_amount(text: str) -> Decimal:
    """Parse user-entered text into a positive amount.

    Used by the admin payroll-entry parser, so it is deliberately strict: a
    typo that yields a negative or zero amount is an error the admin should see
    rather than a silent no-op row.
    """
    amount = money(text)
    if amount <= ZERO:
        raise MoneyError(f"amount must be greater than zero: {text!r}")
    return amount


def total(amounts: Iterable[Decimal]) -> Decimal:
    """Sum an iterable of amounts, returning a quantized Decimal."""
    running = ZERO
    for amount in amounts:
        running += amount
    return money(running)


def fmt(amount: Decimal, currency: str = "USD") -> str:
    """Render an amount for display in a Telegram message."""
    amount = money(amount)
    sign = "-" if amount < ZERO else ""
    whole = abs(amount)
    symbol = _SYMBOLS.get(currency.upper())
    rendered = f"{whole:,.2f}"
    if symbol:
        return f"{sign}{symbol}{rendered}"
    return f"{sign}{rendered} {currency.upper()}"


_SYMBOLS = {
    "USD": "$",
    "EUR": "€",
    "GBP": "£",
}
