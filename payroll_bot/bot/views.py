"""Message rendering.

Pure functions from domain objects to Telegram-ready text. Kept free of any
network or database access so the exact wording a user sees can be asserted in
tests.
"""

from __future__ import annotations

from decimal import Decimal

from ..ledger import Balance
from ..matching import MatchResult
from ..models import (
    Payable,
    Receivable,
    Settlement,
    SettlementStatus,
    User,
)
from ..money import ZERO, fmt
from ..parsing import ParsedPayroll
from ..services.payroll import BatchTotals
from ..services.settlement import UserPosition

_STATUS_ICON = {
    SettlementStatus.PENDING: "⏳",
    SettlementStatus.PAYER_MARKED_PAID: "📤",
    SettlementStatus.RECIPIENT_CONFIRMED: "📥",
    SettlementStatus.RECIPIENT_DENIED: "⚠️",
    SettlementStatus.VERIFIED: "✅",
    SettlementStatus.REJECTED: "❌",
    SettlementStatus.DISPUTED: "⚖️",
    SettlementStatus.CANCELLED: "🚫",
}

_STATUS_TEXT = {
    SettlementStatus.PENDING: "WAITING",
    SettlementStatus.PAYER_MARKED_PAID: "PAID — awaiting recipient",
    SettlementStatus.RECIPIENT_CONFIRMED: "CONFIRMED — awaiting admin",
    SettlementStatus.RECIPIENT_DENIED: "NOT RECEIVED",
    SettlementStatus.VERIFIED: "VERIFIED",
    SettlementStatus.REJECTED: "REJECTED",
    SettlementStatus.DISPUTED: "DISPUTED",
    SettlementStatus.CANCELLED: "CANCELLED",
}


def status_label(settlement: Settlement) -> str:
    return f"{_STATUS_ICON[settlement.status]} {_STATUS_TEXT[settlement.status]}"


# --------------------------------------------------------------------------
# Payroll entry
# --------------------------------------------------------------------------


def payroll_preview(parsed: ParsedPayroll, currency: str = "USD") -> str:
    lines = ["*PAYROLL ENTRY*", ""]

    if parsed.payables:
        lines.append("*OWES*")
        for entry in parsed.payables:
            lines.append(f"  @{entry.handle} — {fmt(entry.amount, currency)}")
        lines.append("")

    if parsed.receivables:
        lines.append("*OWED*")
        for entry in parsed.receivables:
            lines.append(f"  @{entry.handle} — {fmt(entry.amount, currency)}")
        lines.append("")

    lines.append(f"Total Owed: {fmt(parsed.total_owed, currency)}")
    lines.append(f"Total Receivable: {fmt(parsed.total_receivable, currency)}")
    lines.append(f"Difference: {fmt(parsed.difference, currency)}")
    lines.append("")

    if parsed.errors:
        lines.append("❌ *COULD NOT READ SOME LINES*")
        for error in parsed.errors:
            lines.append(f"  • {error}")
        lines.append("")
        lines.append("Fix these lines and send the payroll again.")
        return "\n".join(lines)

    lines.append(_imbalance_note(parsed.difference, currency))
    return "\n".join(lines)


def _imbalance_note(difference: Decimal, currency: str) -> str:
    """Describe the gap between the two sides.

    A payroll is not required to balance: people are routinely owed money
    before the payers covering them have settled up. The difference is a
    schedule, not an error, so it is described rather than flagged.
    """
    if difference == ZERO:
        return "✅ Both sides match exactly."
    if difference > ZERO:
        return (
            f"ℹ️ {fmt(difference, currency)} more is owed *in* than is owed out. "
            "The surplus stays unrouted until someone is owed it."
        )
    return (
        f"ℹ️ {fmt(abs(difference), currency)} more is owed *out* than has come in. "
        "The queue pays people in the order they were added, so this covers "
        "the front of the line first and the rest waits for later payers."
    )


# --------------------------------------------------------------------------
# Settlement plan
# --------------------------------------------------------------------------


def plan_preview(result: MatchResult, currency: str = "USD", limit: int = 40) -> str:
    payer_count = len({p.payer_user_id for p in result.proposals})
    recipient_count = len({p.recipient_user_id for p in result.proposals})

    lines = [
        "*PAYROLL SETTLEMENT PLAN*",
        "",
        f"{payer_count} people owe money",
        f"{recipient_count} people are owed money",
        f"Total: {fmt(result.total_routed, currency)}",
        f"Generated Transfers: {result.transfer_count}",
        f"Strategy: {result.strategy_name}",
        "",
    ]

    for proposal in result.proposals[:limit]:
        flag = " ⚠️" if proposal.needs_admin_review else ""
        via = f" _{proposal.shared_method.title()}_" if proposal.shared_method else ""
        lines.append(
            f"{proposal.payer_label} → {proposal.recipient_label}: "
            f"{fmt(proposal.amount, currency)}{via}{flag}"
        )

    if result.transfer_count > limit:
        lines.append(f"…and {result.transfer_count - limit} more")

    if result.flagged_count:
        lines.append("")
        lines.append(
            f"⚠️ {result.flagged_count} transfer(s) have no shared payment method "
            "and are flagged for your review."
        )

    if result.unmatched_payers:
        lines.append("")
        lines.append("*Unrouted amounts still owed in:*")
        for party in result.unmatched_payers:
            lines.append(f"  {party.label}: {fmt(party.available, currency)}")

    if result.unmatched_recipients:
        lines.append("")
        lines.append("*Unrouted amounts still owed out:*")
        for party in result.unmatched_recipients:
            lines.append(f"  {party.label}: {fmt(party.available, currency)}")

    lines.append("")
    lines.append("_Nothing has been sent to anyone yet._")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Dashboard
# --------------------------------------------------------------------------


def dashboard(batch_label: str, totals: BatchTotals, currency: str = "USD") -> str:
    lines = [
        "*CURRENT PAYROLL*",
        f"Payroll #{batch_label}",
        "",
        f"Total owed: {fmt(totals.payable.original, currency)}",
        f"Total receivable: {fmt(totals.receivable.original, currency)}",
        f"Verified: {fmt(totals.payable.verified, currency)}",
        f"Remaining: {fmt(totals.payable.remaining, currency)}",
        "",
        f"People owing: {totals.people_owing}",
        f"People owed: {totals.people_owed}",
        f"Settlements generated: {totals.settlement_count}",
        f"Payments awaiting verification: {totals.awaiting_verification}",
        f"Payments disputed: {totals.disputed}",
    ]
    if totals.flagged:
        lines.append(f"Flagged for review: {totals.flagged}")
    lines.append("")
    lines.append(_imbalance_note(totals.difference, currency))
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Payer / recipient views
# --------------------------------------------------------------------------


def payer_view(
    user: User, settlements: list[Settlement], balance: Balance | None, currency: str = "USD"
) -> str:
    if balance is None:
        return "You do not owe anything in the current payroll. ✅"

    lines = [
        "*PAYROLL PAYMENT*",
        "",
        f"You currently owe: {fmt(balance.remaining, currency)}",
        "",
    ]

    outstanding = [
        s
        for s in settlements
        if s.status
        not in (
            SettlementStatus.VERIFIED,
            SettlementStatus.CANCELLED,
            SettlementStatus.REJECTED,
        )
    ]

    if outstanding:
        lines.append("*Please make the following payments:*")
        lines.append("")
        for index, settlement in enumerate(outstanding, start=1):
            lines.append(f"{index}. {settlement.recipient.label}")
            lines.append(f"   {fmt(settlement.amount, settlement.currency)}")
            if settlement.payment_method_note:
                lines.append(f"   {settlement.payment_method_note}")
            else:
                lines.append("   ⚠️ No payment method on file — contact your admin")
            lines.append(f"   {status_label(settlement)}")
            lines.append("")

    verified = [s for s in settlements if s.status is SettlementStatus.VERIFIED]
    if verified:
        lines.append("*Verified payments:*")
        for settlement in verified:
            lines.append(
                f"  ✅ {settlement.recipient.label} — "
                f"{fmt(settlement.amount, settlement.currency)}"
            )
        lines.append("")

    lines.append(f"Original owed: {fmt(balance.original, currency)}")
    lines.append(f"Verified: {fmt(balance.verified, currency)}")
    lines.append(f"Remaining: {fmt(balance.remaining, currency)}")
    return "\n".join(lines)


def recipient_view(
    user: User, settlements: list[Settlement], balance: Balance | None, currency: str = "USD"
) -> str:
    if balance is None:
        return "You are not owed anything in the current payroll."

    lines = [
        "*PAYROLL RECEIVABLE*",
        "",
        f"You are owed: {fmt(balance.remaining, currency)}",
        "",
    ]

    incoming = [
        s
        for s in settlements
        if s.status
        not in (
            SettlementStatus.VERIFIED,
            SettlementStatus.CANCELLED,
            SettlementStatus.REJECTED,
        )
    ]
    if incoming:
        lines.append("*Incoming payments:*")
        for settlement in incoming:
            lines.append(
                f"  {settlement.payer.label} → You: "
                f"{fmt(settlement.amount, settlement.currency)} "
                f"— {status_label(settlement)}"
            )
        lines.append("")

    received = [s for s in settlements if s.status is SettlementStatus.VERIFIED]
    if received:
        lines.append("*Received:*")
        for settlement in received:
            lines.append(
                f"  ✅ {settlement.payer.label} — "
                f"{fmt(settlement.amount, settlement.currency)}"
            )
        lines.append("")

    lines.append(f"Original owed to you: {fmt(balance.original, currency)}")
    lines.append(f"Received + verified: {fmt(balance.verified, currency)}")
    lines.append(f"Remaining owed to you: {fmt(balance.remaining, currency)}")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Verification review
# --------------------------------------------------------------------------


def settlement_review(settlement: Settlement) -> str:
    lines = [
        f"*SETTLEMENT REVIEW #{settlement.settlement_id}*",
        "",
        f"{settlement.payer.label} → {settlement.recipient.label}",
        f"Amount: {fmt(settlement.amount, settlement.currency)}",
        "",
        f"Payer marked paid: {'✅' if settlement.payer_claimed_paid_at else '❌'}",
        f"Recipient confirmed: {'✅' if settlement.recipient_confirmed_at else '❌'}",
        f"Proof: {'✅' if settlement.proof_file_id else '❌'}",
    ]
    if settlement.transaction_reference:
        lines.append(f"Reference: `{settlement.transaction_reference}`")
    if settlement.payment_method_note:
        lines.append(f"Method: {settlement.payment_method_note}")
    if settlement.needs_admin_review:
        lines.append("⚠️ No shared payment method between these two.")
    lines.append("")
    lines.append(f"Status: {status_label(settlement)}")
    lines.append(
        f"Links: payable #{settlement.payable_id} / receivable #{settlement.receivable_id}"
    )
    if settlement.admin_notes:
        lines.append("")
        lines.append(f"_Notes:_\n{settlement.admin_notes}")
    return "\n".join(lines)


def user_position_view(position: UserPosition, batch_label: str) -> str:
    lines = [f"*{position.user.label.upper()}*", f"Payroll #{batch_label}", ""]

    if position.owes is not None:
        b = position.owes
        lines.extend(
            [
                f"Owes: {fmt(b.original)}",
                f"Assigned payments: {fmt(b.reserved + b.verified)}",
                f"Verified: {fmt(b.verified)}",
                f"Remaining: {fmt(b.remaining)}",
                "",
            ]
        )
        if position.outgoing:
            lines.append("*Outgoing settlements:*")
            for s in position.outgoing:
                lines.append(
                    f"  {s.recipient.label} — {fmt(s.amount, s.currency)} — "
                    f"{_STATUS_TEXT[s.status]}"
                )
            lines.append("")

    if position.owed is not None:
        b = position.owed
        lines.extend(
            [
                f"Owed: {fmt(b.original)}",
                f"Assigned incoming: {fmt(b.reserved + b.verified)}",
                f"Received + verified: {fmt(b.verified)}",
                f"Remaining: {fmt(b.remaining)}",
                "",
            ]
        )
        if position.incoming:
            lines.append("*Incoming settlements:*")
            for s in position.incoming:
                lines.append(
                    f"  {s.payer.label} — {fmt(s.amount, s.currency)} — "
                    f"{_STATUS_TEXT[s.status]}"
                )
            lines.append("")

    if position.owes is None and position.owed is None:
        lines.append("_No payroll activity in this batch._")

    return "\n".join(lines)


def ledger_list(
    entries: list[Payable] | list[Receivable],
    balances: list[Balance],
    *,
    heading: str,
    currency: str = "USD",
) -> str:
    if not entries:
        return f"*{heading}*\n\n_None._"

    lines = [f"*{heading}*", ""]
    for entry, balance in zip(entries, balances):
        lines.append(
            f"{entry.user.label} — {fmt(balance.original, currency)} "
            f"(verified {fmt(balance.verified, currency)}, "
            f"remaining {fmt(balance.remaining, currency)})"
        )
    return "\n".join(lines)


def escape_markdown(text: str) -> str:
    """Escape text that will be interpolated into a Markdown message."""
    for char in ("_", "*", "[", "]", "`"):
        text = text.replace(char, f"\\{char}")
    return text


# --------------------------------------------------------------------------
# Payment queue
# --------------------------------------------------------------------------


def queue_list(entries: list, currency: str = "USD", limit: int = 25) -> str:
    """The whole line, in order."""
    if not entries:
        return (
            "*PAYMENT QUEUE*\n\n_Nobody is waiting to be paid._\n\n"
            "Add people with /payroll under `OWED`."
        )

    total = sum((e.still_owed for e in entries), ZERO)
    lines = [
        "*PAYMENT QUEUE*",
        f"{len(entries)} waiting · {fmt(total, currency)} still owed",
        "_Longest wait first._",
        "",
    ]

    for entry in entries[:limit]:
        waited = entry.waited_days()
        age = "today" if waited == 0 else f"{waited}d"
        marker = "▸" if entry.unassigned > ZERO else "·"
        lines.append(
            f"`{entry.position:>2}` {marker} {entry.user.label} — "
            f"{fmt(entry.still_owed, currency)}  _{age}_"
        )
        if entry.unassigned <= ZERO:
            lines.append("      _fully assigned, awaiting payment_")

    if len(entries) > limit:
        lines.append(f"\n_…and {len(entries) - limit} more._")

    lines.append("")
    lines.append("▸ = still needs someone assigned to pay them")
    return "\n".join(lines)


def queue_card(
    entry,
    total_in_queue: int,
    currency: str = "USD",
    payer=None,
    shared: frozenset | None = None,
) -> str:
    """One person in the queue, with every way to pay them."""
    waited = entry.waited_days()
    age = "added today" if waited == 0 else f"waiting {waited} day{'s' if waited != 1 else ''}"

    lines = [
        f"*NEXT IN LINE* · {entry.position} of {total_in_queue}",
        "",
        f"*{entry.user.label}*",
        f"Still owed: {fmt(entry.still_owed, currency)}",
    ]

    if entry.unassigned != entry.still_owed:
        lines.append(f"Not yet assigned: {fmt(entry.unassigned, currency)}")
    if entry.balance.verified > ZERO:
        lines.append(f"Already received: {fmt(entry.balance.verified, currency)}")
    lines.append(f"_{age}_")
    lines.append("")

    if entry.payment_methods:
        lines.append("*Pay them with:*")
        for method in entry.payment_methods:
            mark = ""
            if shared is not None:
                mark = "  ✅" if method.kind.value in shared else "  ✗"
            lines.append(f"  {method.display}{mark}")
    else:
        lines.append("⚠️ *No payment methods on file.*")
        lines.append("They need to send `/methods add venmo @handle` to the bot.")
    lines.append("")

    if payer is not None:
        if shared:
            lines.append(
                f"{payer.label} can pay them by "
                + ", ".join(sorted(k.title() for k in shared))
                + "."
            )
        else:
            lines.append(
                f"⚠️ {payer.label} shares no payment method with them — "
                "skip to the next person, or assign anyway and sort it out manually."
            )

    if entry.incoming:
        lines.append("")
        lines.append("*Already routed to them:*")
        for settlement in entry.incoming:
            lines.append(
                f"  {settlement.payer.label} — "
                f"{fmt(settlement.amount, settlement.currency)} — "
                f"{_STATUS_TEXT[settlement.status]}"
            )

    return "\n".join(lines)
