#!/usr/bin/env python3
"""Walk the whole payroll lifecycle without Telegram.

    python demo.py

Uses an in-memory database, so it changes nothing and can be run repeatedly.
It exists to make the accounting visible: watch the balance *not* move when
settlements are generated, and move only when the admin verifies.
"""

from __future__ import annotations

from payroll_bot.db import init_engine, session_scope
from payroll_bot.ledger import payable_balance, receivable_balance
from payroll_bot.models import PaymentMethodKind, SettlementStatus
from payroll_bot.money import fmt
from payroll_bot.parsing import parse_payroll
from payroll_bot.services import payroll as payroll_service
from payroll_bot.services import settlement as settlement_service
from payroll_bot.services.accounts import add_payment_method

PAYROLL = """
OWES
@john 700
@chris 400

OWED
@mike 500
@sarah 350
@alex 250
"""

METHODS = {
    "john": [(PaymentMethodKind.VENMO, "@JohnExample")],
    "chris": [(PaymentMethodKind.ZELLE, "chris@example.com")],
    "mike": [(PaymentMethodKind.VENMO, "@MikeExample")],
    "sarah": [(PaymentMethodKind.ZELLE, "sarah@example.com")],
    "alex": [(PaymentMethodKind.VENMO, "@AlexExample")],
}


def rule(title: str) -> None:
    print(f"\n{'=' * 68}\n{title}\n{'=' * 68}")


def show_balances(session, batch) -> None:
    for payable in payroll_service.open_payables(session, batch):
        b = payable_balance(payable)
        print(
            f"  {payable.user.label:<8} OWES   original {fmt(b.original):>10} | "
            f"assigned {fmt(b.reserved):>10} | verified {fmt(b.verified):>10} | "
            f"remaining {fmt(b.remaining):>10}"
        )
    for receivable in payroll_service.open_receivables(session, batch):
        b = receivable_balance(receivable)
        print(
            f"  {receivable.user.label:<8} OWED   original {fmt(b.original):>10} | "
            f"assigned {fmt(b.reserved):>10} | verified {fmt(b.verified):>10} | "
            f"remaining {fmt(b.remaining):>10}"
        )


def main() -> None:
    init_engine("sqlite:///:memory:")

    with session_scope() as session:
        rule("1. ADMIN ENTERS BALANCES (not relationships)")
        parsed = parse_payroll(PAYROLL)
        print(f"  Total Owed:       {fmt(parsed.total_owed)}")
        print(f"  Total Receivable: {fmt(parsed.total_receivable)}")
        print(f"  Difference:       {fmt(parsed.difference)}")
        print(f"  Balances: {'YES' if parsed.balances else 'NO — would warn the admin'}")

        batch = payroll_service.create_batch(session, label="2026-08-31")
        payroll_service.apply_parsed_payroll(session, batch, parsed)
        session.flush()

        for handle, methods in METHODS.items():
            user = payroll_service.get_or_create_user(session, username=handle)
            for kind, handle_value in methods:
                add_payment_method(session, user, kind, handle_value)
        session.flush()

        rule("2. BALANCES AS ENTERED")
        show_balances(session, batch)

        rule("3. SETTLEMENT PLAN (preview only — nothing sent, nothing persisted)")
        result = payroll_service.generate_plan(session, batch)
        print(f"  Strategy: {result.strategy_name}")
        print(f"  Transfers: {result.transfer_count}   Total: {fmt(result.total_routed)}")
        for proposal in result.proposals:
            via = f"via {proposal.shared_method.title()}" if proposal.shared_method else "⚠️ no shared method"
            print(
                f"    {proposal.payer_label:>8} → {proposal.recipient_label:<8} "
                f"{fmt(proposal.amount):>10}  {via}"
            )

        rule("4. ADMIN APPROVES — settlements created and sent")
        settlements = payroll_service.approve_plan(session, batch, result)
        session.flush()
        print(f"  {len(settlements)} settlements created, all PENDING.")
        print("\n  Balances after generating settlements — note nothing moved:")
        show_balances(session, batch)

        rule("5. PAYER MARKS PAID, RECIPIENT CONFIRMS — still nothing moves")
        first = settlements[0]
        settlement_service.mark_paid(
            session,
            first,
            actor_user_id=first.payer_user_id,
            transaction_reference="VENMO-8842",
        )
        settlement_service.recipient_confirm(
            session, first, actor_user_id=first.recipient_user_id
        )
        session.flush()
        print(
            f"  Settlement #{first.settlement_id} "
            f"({first.payer.label} → {first.recipient.label}, {fmt(first.amount)}) "
            f"is {first.status.value}."
        )
        payer_payable = session.get(type(first.payable), first.payable_id)
        print(
            f"  {payer_payable.user.label} verified so far: "
            f"{fmt(payable_balance(payer_payable).verified)} — unchanged."
        )

        rule("6. ADMIN VERIFIES — only now do finalized balances move")
        settlement_service.admin_verify(session, first, actor_user_id=1)
        session.flush()
        show_balances(session, batch)

        rule("7. A CANCELLED SETTLEMENT FREES ITS AMOUNT FOR REASSIGNMENT")
        pending = next(
            s for s in settlements if s.status is SettlementStatus.PENDING
        )
        payable = session.get(type(pending.payable), pending.payable_id)
        before = payable_balance(payable)
        print(
            f"  Cancelling #{pending.settlement_id} "
            f"({pending.payer.label} → {pending.recipient.label}, {fmt(pending.amount)})"
        )
        print(f"  {payable.user.label} available before: {fmt(before.available)}")
        settlement_service.cancel(
            session, pending, actor_user_id=1, reason="recipient changed bank"
        )
        session.flush()
        after = payable_balance(payable)
        print(f"  {payable.user.label} available after:  {fmt(after.available)}")

        rule("8. VERIFY EVERYTHING ELSE AND CLOSE OUT")
        replan = payroll_service.generate_plan(session, batch)
        if replan.proposals:
            for s in payroll_service.approve_plan(session, batch, replan):
                settlements.append(s)
        session.flush()
        for s in settlements:
            if s.status not in (
                SettlementStatus.VERIFIED,
                SettlementStatus.CANCELLED,
            ):
                settlement_service.admin_verify(session, s, actor_user_id=1)
        session.flush()

        show_balances(session, batch)
        totals = payroll_service.batch_totals(session, batch)
        print(
            f"\n  Payroll #{batch.label}: {batch.status.value}  "
            f"verified {fmt(totals.payable.verified)}, "
            f"remaining {fmt(totals.payable.remaining)}"
        )

        rule("9. AUDIT TRAIL (immutable)")
        from payroll_bot import audit

        entries = audit.history(session, batch_id=batch.batch_id, limit=200)
        print(f"  {len(entries)} entries recorded. Most recent 10:")
        for entry in entries[:10]:
            print(f"    {entry.action.value:<26} {entry.detail or ''}")


if __name__ == "__main__":
    main()
