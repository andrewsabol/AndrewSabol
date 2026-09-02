from decimal import Decimal

from payroll_bot.parsing import parse_payroll

SPEC_BLOCK = """
OWES
@john 500
@chris 400
@david 250

OWED
@mike 300
@sarah 600
@alex 250
"""


def test_parses_the_spec_example_and_balances():
    parsed = parse_payroll(SPEC_BLOCK)
    assert not parsed.errors
    assert [e.handle for e in parsed.payables] == ["john", "chris", "david"]
    assert [e.handle for e in parsed.receivables] == ["mike", "sarah", "alex"]
    assert parsed.total_owed == Decimal("1150.00")
    assert parsed.total_receivable == Decimal("1150.00")
    assert parsed.difference == Decimal("0.00")
    assert parsed.balances


def test_detects_an_imbalance():
    parsed = parse_payroll("OWES\n@a 5420\nOWED\n@b 5370")
    assert not parsed.balances
    assert parsed.difference == Decimal("50.00")


def test_accepts_currency_symbols_and_separators():
    parsed = parse_payroll("OWES\n@john $1,250.50\nOWED\n@mike 1250.50")
    assert parsed.payables[0].amount == Decimal("1250.50")
    assert parsed.balances


def test_captures_optional_descriptions():
    parsed = parse_payroll("OWES\n@john 500 August advance\nOWED\n@mike 500 bonus")
    assert parsed.payables[0].description == "August advance"
    assert parsed.receivables[0].description == "bonus"


def test_handles_missing_at_sign_and_alternate_headings():
    parsed = parse_payroll("Payables:\njohn 100\nReceivables:\nmike 100")
    assert not parsed.errors
    assert parsed.payables[0].handle == "john"
    assert parsed.balances


def test_reports_unreadable_lines_rather_than_skipping_them():
    parsed = parse_payroll("OWES\n@john 500\nthis is not an entry\nOWED\n@mike 500")
    assert len(parsed.errors) == 1
    assert parsed.errors[0].line_number == 3


def test_entry_before_any_heading_is_an_error():
    parsed = parse_payroll("@john 500\nOWES\n@chris 100")
    assert parsed.errors
    assert "OWES or OWED" in parsed.errors[0].reason


def test_duplicate_handle_in_one_section_is_flagged():
    parsed = parse_payroll("OWES\n@john 500\n@john 200\nOWED\n@mike 700")
    assert any("twice" in e.reason for e in parsed.errors)


def test_same_person_may_owe_and_be_owed():
    """Distinct sections, so this is legitimate, not a duplicate."""
    parsed = parse_payroll("OWES\n@john 500\nOWED\n@john 200\n@mike 300")
    assert not parsed.errors


def test_zero_and_negative_amounts_are_errors():
    parsed = parse_payroll("OWES\n@john 0\n@chris -50\nOWED\n@mike 100")
    assert len(parsed.errors) == 2


def test_comments_and_blank_lines_are_ignored():
    parsed = parse_payroll("# August payroll\n\nOWES\n@john 100\n\nOWED\n@mike 100")
    assert not parsed.errors
    assert parsed.balances


def test_empty_input_is_empty_not_an_error():
    parsed = parse_payroll("")
    assert parsed.is_empty
    assert not parsed.errors


def test_ignores_a_bot_command_pasted_into_the_block():
    """People paste the command they were told to send along with the block."""
    parsed = parse_payroll(
        "OWES\n@john 500\n@chris 400\n\nOWED\n@mike 300\n@sarah 600\n/payroll"
    )
    assert not parsed.errors
    assert parsed.total_owed == Decimal("900.00")
    assert parsed.balances


def test_ignores_a_group_style_bot_command():
    """In groups Telegram sends commands as /payroll@BotName."""
    parsed = parse_payroll("/payroll@SettlementBot\nOWES\n@a 100\nOWED\n@b 100")
    assert not parsed.errors
    assert parsed.balances


def test_a_slash_does_not_hide_a_real_typo():
    """Only a bare command is skipped; a malformed entry still reports."""
    parsed = parse_payroll("OWES\n/john 500 oops\nOWED\n@b 100")
    assert parsed.errors
