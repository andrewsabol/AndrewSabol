from decimal import Decimal

import pytest

from payroll_bot.money import MoneyError, fmt, money, parse_amount, total


def test_rejects_float_outright():
    # Accepting a float would import binary rounding error into the ledger.
    with pytest.raises(MoneyError):
        money(10.15)


def test_parses_strings_with_symbols_and_separators():
    assert money("$1,250.50") == Decimal("1250.50")
    assert money("  42 ") == Decimal("42.00")


def test_quantizes_to_cents_half_up():
    assert money(Decimal("10.005")) == Decimal("10.01")
    assert money(Decimal("10.004")) == Decimal("10.00")


def test_decimal_arithmetic_is_exact():
    # The canonical float failure: 0.1 + 0.2 != 0.3
    assert total([money("0.10"), money("0.20")]) == money("0.30")


def test_sum_of_many_cents_is_exact():
    amounts = [money("0.01")] * 1000
    assert total(amounts) == Decimal("10.00")


def test_parse_amount_rejects_zero_and_negative():
    with pytest.raises(MoneyError):
        parse_amount("0")
    with pytest.raises(MoneyError):
        parse_amount("-5")


def test_parse_amount_rejects_garbage():
    with pytest.raises(MoneyError):
        parse_amount("abc")


def test_formatting():
    assert fmt(Decimal("1234.5")) == "$1,234.50"
    assert fmt(Decimal("-20")) == "-$20.00"
    assert fmt(Decimal("5"), "GBP") == "£5.00"
    assert fmt(Decimal("5"), "JPY") == "5.00 JPY"
