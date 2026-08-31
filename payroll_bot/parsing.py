"""Parser for the admin ``/payroll`` entry format.

The admin enters *balances*, not relationships::

    OWES
    @john 500
    @chris 400

    OWED
    @mike 300
    @sarah 600

Anything that cannot be parsed is reported as a numbered error rather than
skipped, because a silently dropped line is a payroll that balances on screen
and not in reality.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal

from .money import ZERO, MoneyError, money, parse_amount

_OWES_HEADER = re.compile(r"^\s*(owes?|payables?|owing)\s*:?\s*$", re.IGNORECASE)
_OWED_HEADER = re.compile(
    r"^\s*(owed|receivables?|is\s+owed|to\s+receive)\s*:?\s*$", re.IGNORECASE
)

# "@john 500", "john 500 -- August bonus", "@john $1,250.50 travel reimbursement"
_ENTRY = re.compile(
    r"""^\s*
    @?(?P<handle>[A-Za-z0-9_]{1,64})
    [\s:,]+
    (?P<amount>-?\$?\s*[\d,]+(?:\.\d+)?)
    (?:[\s,:\-–—]+(?P<description>.*?))?
    \s*$""",
    re.VERBOSE,
)


@dataclass
class ParsedEntry:
    handle: str
    amount: Decimal
    description: str | None = None
    line_number: int = 0


@dataclass
class ParseError:
    line_number: int
    text: str
    reason: str

    def __str__(self) -> str:
        return f"Line {self.line_number}: {self.reason} — “{self.text.strip()}”"


@dataclass
class ParsedPayroll:
    payables: list[ParsedEntry] = field(default_factory=list)
    receivables: list[ParsedEntry] = field(default_factory=list)
    errors: list[ParseError] = field(default_factory=list)

    @property
    def total_owed(self) -> Decimal:
        return money(sum((e.amount for e in self.payables), ZERO))

    @property
    def total_receivable(self) -> Decimal:
        return money(sum((e.amount for e in self.receivables), ZERO))

    @property
    def difference(self) -> Decimal:
        """Positive when more is owed in than is owed out."""
        return money(self.total_owed - self.total_receivable)

    @property
    def balances(self) -> bool:
        return self.difference == ZERO

    @property
    def is_empty(self) -> bool:
        return not self.payables and not self.receivables


def parse_payroll(text: str) -> ParsedPayroll:
    """Parse an OWES/OWED block into structured entries."""
    result = ParsedPayroll()
    section: str | None = None

    for index, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue

        if _OWES_HEADER.match(line):
            section = "owes"
            continue
        if _OWED_HEADER.match(line):
            section = "owed"
            continue

        if section is None:
            result.errors.append(
                ParseError(index, raw_line, "expected an OWES or OWED heading first")
            )
            continue

        match = _ENTRY.match(line)
        if not match:
            result.errors.append(
                ParseError(index, raw_line, "expected “@username amount”")
            )
            continue

        try:
            amount = parse_amount(match.group("amount"))
        except MoneyError as exc:
            result.errors.append(ParseError(index, raw_line, str(exc)))
            continue

        description = (match.group("description") or "").strip() or None
        entry = ParsedEntry(
            handle=match.group("handle").lower(),
            amount=amount,
            description=description,
            line_number=index,
        )
        if section == "owes":
            result.payables.append(entry)
        else:
            result.receivables.append(entry)

    _flag_duplicates(result)
    return result


def _flag_duplicates(result: ParsedPayroll) -> None:
    """Duplicate handles within one section are almost always a paste error."""
    for label, entries in (("OWES", result.payables), ("OWED", result.receivables)):
        seen: dict[str, int] = {}
        for entry in entries:
            if entry.handle in seen:
                result.errors.append(
                    ParseError(
                        entry.line_number,
                        f"@{entry.handle} {entry.amount}",
                        f"@{entry.handle} appears twice under {label} "
                        f"(first on line {seen[entry.handle]})",
                    )
                )
            else:
                seen[entry.handle] = entry.line_number
