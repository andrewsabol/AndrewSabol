"""Administrator commands: payroll entry, plan approval, verification, overrides."""

from __future__ import annotations

import logging

from sqlalchemy import select
from telegram import Update
from telegram.ext import (
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from ... import audit
from ...db import session_scope
from ... import queue as payment_queue
from ...ledger import payable_balance, receivable_balance
from ...models import BatchStatus, Payable, Receivable, Settlement, User
from ...money import ZERO, MoneyError, money
from ...parsing import parse_payroll
from ...services import payroll as payroll_service
from ...services import settlement as settlement_service
from ...services import accounts as accounts_service
from ...services.accounts import find_user, set_admin, set_priority
from ...strategies import default_strategy
from .. import keyboards, notifications, views
from .common import config_of, deny, edit_or_reply, reply, require_admin, touch_user

log = logging.getLogger(__name__)

AWAITING_PAYROLL = 1

#: Keys used in ``context.user_data``.
PENDING_PARSE = "pending_parse"
PENDING_PLAN = "pending_plan"
PENDING_BATCH = "pending_batch"


# --------------------------------------------------------------------------
# /payroll -- balance entry
# --------------------------------------------------------------------------


async def payroll_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    with session_scope() as session:
        touch_user(session, update)
        if not require_admin(session, update, context):
            await deny(update)
            return ConversationHandler.END

    # Allow the block to be sent in the same message as the command.
    inline = (update.effective_message.text or "").split("\n", 1)
    if len(inline) > 1 and inline[1].strip():
        return await _handle_payroll_text(update, context, inline[1])

    await reply(
        update,
        "*PAYROLL ENTRY*\n\n"
        "Send the balances. Enter *balances*, not who-owes-whom:\n\n"
        "```\n"
        "OWES\n"
        "@john 500\n"
        "@chris 400\n"
        "\n"
        "OWED\n"
        "@mike 300\n"
        "@sarah 600\n"
        "```\n\n"
        "An optional description may follow the amount.\n"
        "Send /cancel to abort.",
    )
    return AWAITING_PAYROLL


async def payroll_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    return await _handle_payroll_text(update, context, update.effective_message.text or "")


async def _handle_payroll_text(
    update: Update, context: ContextTypes.DEFAULT_TYPE, text: str
) -> int:
    cfg = config_of(context)
    parsed = parse_payroll(text)

    if parsed.is_empty and not parsed.errors:
        await reply(
            update,
            "I could not find any entries. Start the block with `OWES` or `OWED`.",
        )
        return AWAITING_PAYROLL

    preview = views.payroll_preview(parsed, cfg.currency)

    if parsed.errors:
        await reply(update, preview)
        return AWAITING_PAYROLL

    # An unbalanced payroll is normal, not an error: people are often owed
    # before the payers covering them have settled up. The difference is
    # reported, never used to block entry.
    await _commit_payroll(update, context, parsed)
    return ConversationHandler.END


async def _commit_payroll(update: Update, context: ContextTypes.DEFAULT_TYPE, parsed) -> None:
    cfg = config_of(context)
    with session_scope() as session:
        actor = touch_user(session, update)
        batch = payroll_service.active_batch(session)
        if batch is None or batch.status is not BatchStatus.DRAFT:
            batch = payroll_service.create_batch(
                session, currency=cfg.currency, actor_user_id=actor.user_id
            )
        payroll_service.apply_parsed_payroll(
            session, batch, parsed, actor_user_id=actor.user_id
        )
        session.flush()
        label = batch.label
        totals = payroll_service.batch_totals(session, batch)
        summary = views.dashboard(label, totals, cfg.currency)

    context.user_data.pop(PENDING_PARSE, None)
    await reply(
        update,
        f"✅ Saved to payroll #{label}.\n\n{summary}",
        markup=keyboards.dashboard_keyboard(),
    )


async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.pop(PENDING_PARSE, None)
    await reply(update, "Cancelled.")
    return ConversationHandler.END


# --------------------------------------------------------------------------
# Settlement plan generation and approval
# --------------------------------------------------------------------------


async def generate_settlements(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cfg = config_of(context)
    if update.callback_query is not None:
        await update.callback_query.answer()

    with session_scope() as session:
        if not require_admin(session, update, context):
            await deny(update)
            return

        batch = payroll_service.active_batch(session)
        if batch is None:
            await reply(update, "No active payroll. Start one with /payroll.")
            return

        result = payroll_service.generate_plan(
            session,
            batch,
            strategy=default_strategy(
                strict_payment_methods=cfg.strict_payment_methods
            ),
        )
        batch_id = batch.batch_id

    if not result.proposals:
        await reply(
            update,
            "Nothing left to route — every balance is either settled or already "
            "assigned to a settlement.",
        )
        return

    context.user_data[PENDING_PLAN] = result
    context.user_data[PENDING_BATCH] = batch_id

    await reply(
        update,
        views.plan_preview(result, cfg.currency),
        markup=keyboards.plan_keyboard(),
    )


async def approve_plan(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    result = context.user_data.get(PENDING_PLAN)
    batch_id = context.user_data.get(PENDING_BATCH)
    if result is None or batch_id is None:
        await edit_or_reply(update, "That plan has expired. Press Generate Settlements again.")
        return

    with session_scope() as session:
        if not require_admin(session, update, context):
            await deny(update)
            return

        actor = touch_user(session, update)
        from ...models import PayrollBatch

        batch = session.get(PayrollBatch, batch_id)
        if batch is None:
            await edit_or_reply(update, "That payroll batch no longer exists.")
            return

        try:
            created = payroll_service.approve_plan(
                session, batch, result, actor_user_id=actor.user_id
            )
        except Exception as exc:
            # Balances moved between preview and approval; a stale plan must
            # never be forced through.
            await edit_or_reply(
                update,
                f"❌ Could not approve this plan:\n`{exc}`\n\n"
                "Press Generate Settlements again to rebuild it from current balances.",
            )
            return

        session.flush()
        count = len(created)
        report = await notifications.notify_plan_approved(
            context.bot, session, created
        )

    context.user_data.pop(PENDING_PLAN, None)
    context.user_data.pop(PENDING_BATCH, None)

    await edit_or_reply(
        update,
        f"✅ *Settlement plan approved.*\n\n"
        f"{count} settlement(s) created and sent out.\n{report.summary}",
    )


async def recalculate_plan(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await generate_settlements(update, context)


async def edit_plan(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    await reply(
        update,
        "*EDITING A PLAN*\n\n"
        "Approve the plan first, then re-route any individual settlement:\n\n"
        "`/reassign <settlement_id> to @user`\n"
        "`/reassign <settlement_id> to @user 250`\n"
        "`/cancelsettlement <settlement_id> [reason]`\n\n"
        "Re-routing is only accepted when the payer and recipient balances "
        "still make it mathematically valid.",
    )


async def cancel_plan(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    context.user_data.pop(PENDING_PLAN, None)
    context.user_data.pop(PENDING_BATCH, None)
    await edit_or_reply(update, "Plan discarded. Nothing was sent to anyone.")


# --------------------------------------------------------------------------
# /dashboard
# --------------------------------------------------------------------------


async def dashboard_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cfg = config_of(context)
    if update.callback_query is not None:
        await update.callback_query.answer()

    with session_scope() as session:
        if not require_admin(session, update, context):
            await deny(update)
            return
        batch = payroll_service.active_batch(session)
        if batch is None:
            await reply(update, "No active payroll. Start one with /payroll.")
            return
        totals = payroll_service.batch_totals(session, batch)
        text = views.dashboard(batch.label, totals, cfg.currency)

    await edit_or_reply(update, text, markup=keyboards.dashboard_keyboard())


async def dashboard_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles the People Who Owe / People Owed / Settlements / Disputes buttons."""
    query = update.callback_query
    await query.answer()
    action = query.data
    cfg = config_of(context)

    with session_scope() as session:
        if not require_admin(session, update, context):
            await deny(update)
            return
        batch = payroll_service.active_batch(session)
        if batch is None:
            await edit_or_reply(update, "No active payroll.")
            return

        if action == keyboards.DASH_OWING:
            entries = payroll_service.open_payables(session, batch)
            text = views.ledger_list(
                entries,
                [payable_balance(e) for e in entries],
                heading="PEOPLE WHO OWE",
                currency=cfg.currency,
            )
        elif action == keyboards.DASH_OWED:
            entries = payroll_service.open_receivables(session, batch)
            text = views.ledger_list(
                entries,
                [receivable_balance(e) for e in entries],
                heading="PEOPLE OWED",
                currency=cfg.currency,
            )
        elif action == keyboards.DASH_SETTLEMENTS:
            text = _settlement_list(session, batch.batch_id)
        elif action == keyboards.DASH_DISPUTES:
            disputes = settlement_service.disputed_settlements(session, batch.batch_id)
            text = _compact_list("DISPUTES", disputes)
        elif action == keyboards.DASH_REPORTS:
            text = _report(session, batch)
        else:  # pragma: no cover - defensive
            text = "Unknown action."

    await edit_or_reply(update, text, markup=keyboards.back_to_dashboard())


def _settlement_list(session, batch_id: int) -> str:
    rows = session.execute(
        select(Settlement)
        .where(Settlement.batch_id == batch_id)
        .order_by(Settlement.settlement_id)
    ).scalars().all()
    return _compact_list("SETTLEMENTS", rows)


def _compact_list(heading: str, rows: list[Settlement]) -> str:
    if not rows:
        return f"*{heading}*\n\n_None._"
    lines = [f"*{heading}*", ""]
    for settlement in rows:
        lines.append(
            f"#{settlement.settlement_id} {settlement.payer.label} → "
            f"{settlement.recipient.label} — "
            f"{views.fmt(settlement.amount, settlement.currency)} — "
            f"{views.status_label(settlement)}"
        )
    return "\n".join(lines)


def _report(session, batch) -> str:
    totals = payroll_service.batch_totals(session, batch)
    lines = [
        f"*PAYROLL #{batch.label}*",
        f"Status: {batch.status.value}",
        "",
        f"Total owed: {views.fmt(totals.payable.original)}",
        f"Total receivable: {views.fmt(totals.receivable.original)}",
        f"People owing: {totals.people_owing}",
        f"People receiving: {totals.people_owed}",
        f"Settlements generated: {totals.settlement_count}",
        f"Verified: {views.fmt(totals.payable.verified)}",
        f"Remaining: {views.fmt(totals.payable.remaining)}",
        f"In progress (assigned, unverified): {views.fmt(totals.payable.reserved)}",
        "",
        "*Recent activity:*",
    ]
    for entry in audit.history(session, batch_id=batch.batch_id, limit=12):
        stamp = entry.created_at.strftime("%m-%d %H:%M")
        lines.append(f"  `{stamp}` {entry.action.value}: {entry.detail or ''}")
    return "\n".join(lines)


async def needs_verification(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is not None:
        await query.answer()

    with session_scope() as session:
        if not require_admin(session, update, context):
            await deny(update)
            return
        batch = payroll_service.active_batch(session)
        if batch is None:
            await reply(update, "No active payroll.")
            return
        pending = settlement_service.settlements_awaiting_verification(
            session, batch.batch_id
        )
        if not pending:
            await edit_or_reply(
                update,
                "*NEEDS VERIFICATION*\n\n_Nothing awaiting review._",
                markup=keyboards.back_to_dashboard(),
            )
            return
        reviews = [
            (views.settlement_review(s), s.settlement_id) for s in pending[:10]
        ]
        overflow = len(pending) - len(reviews)

    for text, settlement_id in reviews:
        await reply(
            update, text, markup=keyboards.admin_review_keyboard(settlement_id)
        )
    if overflow > 0:
        await reply(update, f"_…and {overflow} more awaiting review._")


# --------------------------------------------------------------------------
# Admin verification actions
# --------------------------------------------------------------------------


async def admin_decision(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    action, args = keyboards.decode(query.data)
    settlement_id = int(args[0])

    with session_scope() as session:
        if not require_admin(session, update, context):
            await deny(update)
            return

        actor = touch_user(session, update)
        settlement = session.get(Settlement, settlement_id)
        if settlement is None:
            await edit_or_reply(update, "That settlement no longer exists.")
            return

        try:
            if action == keyboards.ADMIN_VERIFY:
                settlement_service.admin_verify(
                    session, settlement, actor_user_id=actor.user_id
                )
                outcome = "✅ VERIFIED"
                payer_note = (
                    f"✅ Your payment of "
                    f"{views.fmt(settlement.amount, settlement.currency)} to "
                    f"{settlement.recipient.label} has been verified."
                )
            elif action == keyboards.ADMIN_REJECT:
                settlement_service.admin_reject(
                    session, settlement, actor_user_id=actor.user_id
                )
                outcome = "❌ REJECTED"
                payer_note = (
                    f"❌ Your claimed payment of "
                    f"{views.fmt(settlement.amount, settlement.currency)} to "
                    f"{settlement.recipient.label} was rejected. "
                    "That amount is owed again and may be reassigned."
                )
            else:
                settlement_service.admin_dispute(
                    session, settlement, actor_user_id=actor.user_id
                )
                outcome = "⚠️ DISPUTED"
                payer_note = (
                    f"⚠️ Your payment of "
                    f"{views.fmt(settlement.amount, settlement.currency)} to "
                    f"{settlement.recipient.label} is under review."
                )
        except settlement_service.SettlementError as exc:
            await edit_or_reply(update, f"❌ {exc}")
            return

        session.flush()
        text = views.settlement_review(settlement)
        await notifications.notify_payer_result(
            context.bot, session, settlement, payer_note
        )

    await edit_or_reply(update, f"{text}\n\n*Decision: {outcome}*")


# --------------------------------------------------------------------------
# Manual overrides
# --------------------------------------------------------------------------


async def reassign_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """``/reassign <settlement_id> to @user [amount]``"""
    args = context.args or []
    usage = (
        "Usage: `/reassign <settlement_id> to @user [amount]`\n"
        "Example: `/reassign 5001 to @sarah 500`"
    )
    if len(args) < 3 or args[1].lower() != "to":
        await reply(update, usage)
        return

    try:
        settlement_id = int(args[0])
    except ValueError:
        await reply(update, usage)
        return

    handle = args[2]
    new_amount = None
    if len(args) >= 4:
        try:
            new_amount = money(args[3])
        except MoneyError as exc:
            await reply(update, f"❌ {exc}")
            return

    with session_scope() as session:
        if not require_admin(session, update, context):
            await deny(update)
            return

        actor = touch_user(session, update)
        settlement = session.get(Settlement, settlement_id)
        if settlement is None:
            await reply(update, f"No settlement #{settlement_id}.")
            return

        target = find_user(session, handle)
        if target is None:
            await reply(update, f"No user matching {handle}.")
            return

        receivable = session.execute(
            select(Receivable).where(
                Receivable.batch_id == settlement.batch_id,
                Receivable.user_id == target.user_id,
            )
        ).scalars().first()
        if receivable is None:
            await reply(
                update,
                f"{target.label} has no receivable in this payroll, so money "
                "cannot be routed to them.",
            )
            return

        old_recipient = settlement.recipient.label
        try:
            settlement_service.reassign(
                session,
                settlement,
                actor_user_id=actor.user_id,
                new_recipient_receivable_id=receivable.receivable_id,
                new_amount=new_amount,
                reason="admin manual reassignment",
            )
        except Exception as exc:
            await reply(update, f"❌ Cannot reassign: `{exc}`")
            return

        session.flush()
        text = (
            f"✅ Settlement #{settlement_id} re-routed.\n\n"
            f"Was: {settlement.payer.label} → {old_recipient}\n"
            f"Now: {settlement.payer.label} → {settlement.recipient.label} "
            f"{views.fmt(settlement.amount, settlement.currency)}"
        )
        await notifications.notify_user(
            context.bot,
            session,
            settlement.payer_user_id,
            f"🔄 Your payment instruction changed. Please now send "
            f"{views.fmt(settlement.amount, settlement.currency)} to "
            f"{settlement.recipient.label}"
            + (
                f"\n{settlement.payment_method_note}"
                if settlement.payment_method_note
                else ""
            ),
        )

    await reply(update, text)


async def cancel_settlement_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """``/cancelsettlement <settlement_id> [reason]``"""
    args = context.args or []
    if not args:
        await reply(update, "Usage: `/cancelsettlement <settlement_id> [reason]`")
        return
    try:
        settlement_id = int(args[0])
    except ValueError:
        await reply(update, "Usage: `/cancelsettlement <settlement_id> [reason]`")
        return
    reason = " ".join(args[1:]) or None

    with session_scope() as session:
        if not require_admin(session, update, context):
            await deny(update)
            return
        actor = touch_user(session, update)
        settlement = session.get(Settlement, settlement_id)
        if settlement is None:
            await reply(update, f"No settlement #{settlement_id}.")
            return
        try:
            settlement_service.cancel(
                session, settlement, actor_user_id=actor.user_id, reason=reason
            )
        except settlement_service.SettlementError as exc:
            await reply(update, f"❌ {exc}")
            return
        session.flush()
        amount = views.fmt(settlement.amount, settlement.currency)
        text = (
            f"🚫 Settlement #{settlement_id} cancelled. "
            f"{amount} is available for reassignment."
        )

    await reply(update, text)


async def user_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """``/user @handle`` -- full position for one person."""
    args = context.args or []
    if not args:
        await reply(update, "Usage: `/user @handle`")
        return

    with session_scope() as session:
        if not require_admin(session, update, context):
            await deny(update)
            return
        batch = payroll_service.active_batch(session)
        if batch is None:
            await reply(update, "No active payroll.")
            return
        target = find_user(session, args[0])
        if target is None:
            await reply(update, f"No user matching {args[0]}.")
            return
        position = settlement_service.user_position(
            session, batch.batch_id, target.user_id
        )
        text = views.user_position_view(position, batch.label)

    await reply(update, text)


async def promote_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """``/promote @handle`` -- grant admin rights."""
    args = context.args or []
    if not args:
        await reply(update, "Usage: `/promote @handle`")
        return
    with session_scope() as session:
        if not require_admin(session, update, context):
            await deny(update)
            return
        actor = touch_user(session, update)
        target = find_user(session, args[0])
        if target is None:
            await reply(update, f"No user matching {args[0]}.")
            return
        set_admin(session, target, True, actor_user_id=actor.user_id)
        label = target.label
    await reply(update, f"✅ {label} is now a payroll administrator.")


async def priority_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """``/priority @handle <level>`` -- recipient priority for the matcher."""
    args = context.args or []
    if len(args) < 2:
        await reply(update, "Usage: `/priority @handle <level>` (0 = normal)")
        return
    try:
        level = int(args[1])
    except ValueError:
        await reply(update, "Priority must be a whole number.")
        return

    with session_scope() as session:
        if not require_admin(session, update, context):
            await deny(update)
            return
        actor = touch_user(session, update)
        target = find_user(session, args[0])
        if target is None:
            await reply(update, f"No user matching {args[0]}.")
            return
        set_priority(session, target, level, actor_user_id=actor.user_id)
        label = target.label
    await reply(
        update,
        f"✅ {label} priority set to {level}. Higher-priority recipients are "
        "matched first when settlements are generated.",
    )


async def newpayroll_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """``/newpayroll [label]`` -- open a fresh batch."""
    cfg = config_of(context)
    label = " ".join(context.args or []) or None
    with session_scope() as session:
        if not require_admin(session, update, context):
            await deny(update)
            return
        actor = touch_user(session, update)
        try:
            batch = payroll_service.create_batch(
                session, label=label, currency=cfg.currency, actor_user_id=actor.user_id
            )
        except payroll_service.PayrollError as exc:
            await reply(update, f"❌ {exc}")
            return
        name = batch.label
    await reply(
        update, f"✅ Payroll #{name} created. Send /payroll to enter balances."
    )


async def audit_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """``/audit [settlement_id]`` -- immutable history."""
    args = context.args or []
    with session_scope() as session:
        if not require_admin(session, update, context):
            await deny(update)
            return
        if args:
            try:
                entity_id = int(args[0])
            except ValueError:
                await reply(update, "Usage: `/audit [settlement_id]`")
                return
            rows = audit.history(
                session, entity_type="settlement", entity_id=entity_id, limit=50
            )
            heading = f"AUDIT — settlement #{entity_id}"
        else:
            batch = payroll_service.active_batch(session)
            if batch is None:
                await reply(update, "No active payroll.")
                return
            rows = audit.history(session, batch_id=batch.batch_id, limit=50)
            heading = f"AUDIT — payroll #{batch.label}"

        lines = [f"*{heading}*", ""]
        if not rows:
            lines.append("_No entries._")
        for entry in rows:
            stamp = entry.created_at.strftime("%Y-%m-%d %H:%M")
            lines.append(f"`{stamp}` *{entry.action.value}*")
            if entry.detail:
                lines.append(f"   {entry.detail}")
        text = "\n".join(lines)

    await reply(update, text)


def build_handlers() -> list:
    payroll_conversation = ConversationHandler(
        entry_points=[CommandHandler("payroll", payroll_command)],
        states={
            AWAITING_PAYROLL: [
                CommandHandler("cancel", cancel_command),
                MessageHandler(filters.TEXT & ~filters.COMMAND, payroll_text),
            ]
        },
        fallbacks=[CommandHandler("cancel", cancel_command)],
        name="payroll_entry",
        persistent=False,
    )

    return [
        payroll_conversation,
        CommandHandler("dashboard", dashboard_command),
        CommandHandler("newpayroll", newpayroll_command),
        CommandHandler("reassign", reassign_command),
        CommandHandler("cancelsettlement", cancel_settlement_command),
        CommandHandler("user", user_command),
        CommandHandler("promote", promote_command),
        CommandHandler("priority", priority_command),
        CommandHandler("audit", audit_command),
        CommandHandler("generate", generate_settlements),
        CommandHandler("queue", queue_command),
        CommandHandler("setmethod", setmethod_command),
        CommandHandler("delmethod", delmethod_command),
        CommandHandler("next", next_command),
        CommandHandler("verify", needs_verification),
    ]


# --------------------------------------------------------------------------
# Payment queue
# --------------------------------------------------------------------------


async def queue_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """``/queue`` -- everyone waiting to be paid, longest wait first."""
    cfg = config_of(context)
    if update.callback_query is not None:
        await update.callback_query.answer()

    with session_scope() as session:
        if not require_admin(session, update, context):
            await deny(update)
            return
        batch = payroll_service.active_batch(session)
        if batch is None:
            await reply(update, "No active payroll. Start one with /payroll.")
            return
        entries = payment_queue.build_queue(session, batch.batch_id)
        text = views.queue_list(entries, cfg.currency)

    await edit_or_reply(update, text, markup=keyboards.queue_list_keyboard())


async def next_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """``/next [@payer]`` -- the person at the front of the queue.

    With a payer named, the card also says whether that payer can actually pay
    this person, and offers to assign the payment.
    """
    payer_handle = (context.args or [None])[0]
    await _show_queue_position(update, context, index=0, payer_handle=payer_handle)


async def queue_step(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Skip / previous. Wraps around so cycling the queue never dead-ends."""
    query = update.callback_query
    await query.answer()
    action, args = keyboards.decode(query.data)

    index = int(args[0])
    payer_user_id = int(args[1]) if len(args) > 1 else None
    index = index + 1 if action == keyboards.QUEUE_SKIP else index - 1

    await _show_queue_position(
        update, context, index=index, payer_user_id=payer_user_id, edit=True
    )


async def _show_queue_position(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    index: int,
    payer_handle: str | None = None,
    payer_user_id: int | None = None,
    edit: bool = False,
) -> None:
    cfg = config_of(context)

    with session_scope() as session:
        if not require_admin(session, update, context):
            await deny(update)
            return

        batch = payroll_service.active_batch(session)
        if batch is None:
            await reply(update, "No active payroll. Start one with /payroll.")
            return

        payer = None
        payer_methods: frozenset[str] = frozenset()
        payable = None

        if payer_handle:
            payer = find_user(session, payer_handle)
            if payer is None:
                await reply(update, f"No user matching {payer_handle}.")
                return
        elif payer_user_id:
            payer = session.get(User, payer_user_id)

        if payer is not None:
            payer_methods = frozenset(
                m.kind.value
                for m in accounts_service.list_payment_methods(session, payer.user_id)
            )
            payable = session.execute(
                select(Payable).where(
                    Payable.batch_id == batch.batch_id,
                    Payable.user_id == payer.user_id,
                )
            ).scalars().first()

        entry, total = payment_queue.entry_at(session, batch.batch_id, index)
        if entry is None:
            await edit_or_reply(
                update,
                "*PAYMENT QUEUE*\n\n_Nobody is waiting to be paid._",
                markup=keyboards.back_to_dashboard(),
            )
            return

        shared = entry.shares_method_with(payer_methods) if payer else None

        # Only offer to assign when the maths would actually allow it.
        can_assign = False
        if payer is not None and payable is not None:
            available = payable_balance(payable).available
            can_assign = available > ZERO and entry.unassigned > ZERO

        text = views.queue_card(
            entry, total, cfg.currency, payer=payer, shared=shared
        )
        if payer is not None and payable is None:
            text += f"\n\n⚠️ {payer.label} has no payable in this payroll."
        elif payer is not None and not can_assign:
            text += "\n\n_Nothing left to assign between these two._"

        markup = keyboards.queue_card_keyboard(
            index % total,
            total,
            payer_user_id=payer.user_id if payer else None,
            receivable_id=entry.receivable.receivable_id,
            can_assign=can_assign,
        )

    if edit:
        await edit_or_reply(update, text, markup=markup)
    else:
        await reply(update, text, markup=markup)


async def queue_assign(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Route as much as both sides allow from this payer to this recipient."""
    query = update.callback_query
    await query.answer()
    _, args = keyboards.decode(query.data)
    receivable_id, payer_user_id = int(args[0]), int(args[1])

    with session_scope() as session:
        if not require_admin(session, update, context):
            await deny(update)
            return

        actor = touch_user(session, update)
        receivable = session.get(Receivable, receivable_id)
        if receivable is None:
            await edit_or_reply(update, "That receivable no longer exists.")
            return

        payable = session.execute(
            select(Payable).where(
                Payable.batch_id == receivable.batch_id,
                Payable.user_id == payer_user_id,
            )
        ).scalars().first()
        if payable is None:
            await edit_or_reply(update, "That payer has no payable in this payroll.")
            return

        amount = min(
            payable_balance(payable).available, receivable_balance(receivable).available
        )
        if amount <= ZERO:
            await edit_or_reply(
                update, "Nothing left to assign between those two."
            )
            return

        try:
            settlement = payroll_service.assign_settlement(
                session, payable, receivable, amount, actor_user_id=actor.user_id
            )
        except Exception as exc:
            await edit_or_reply(update, f"❌ Could not assign: `{exc}`")
            return

        session.flush()
        text = (
            f"✅ *Assigned.*\n\n"
            f"{settlement.payer.label} → {settlement.recipient.label}: "
            f"{views.fmt(settlement.amount, settlement.currency)}"
        )
        if settlement.payment_method_note:
            text += f"\n{settlement.payment_method_note}"
        if settlement.needs_admin_review:
            text += "\n\n⚠️ No shared payment method — flagged for review."

        delivered = await notifications.notify_plan_approved(
            context.bot, session, [settlement]
        )
        text += f"\n\n{delivered.summary}"

    await edit_or_reply(update, text, markup=keyboards.queue_list_keyboard())


# --------------------------------------------------------------------------
# Payment methods on someone else's behalf
# --------------------------------------------------------------------------


async def setmethod_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """``/setmethod @handle <kind> <handle>`` -- record a method for someone else.

    Admins routinely know a person's Venmo before that person has ever opened
    the bot, and payroll is entered by @username well ahead of anyone joining.
    Without this, a recipient who never sends /methods can never be routed a
    compatible payment.
    """
    args = context.args or []
    usage = (
        "Usage: `/setmethod @handle <venmo|cashapp|zelle|paypal|other> <their handle>`\n"
        "Example: `/setmethod @mike venmo @MikeExample`"
    )
    if len(args) < 3:
        await reply(update, usage)
        return

    with session_scope() as session:
        if not require_admin(session, update, context):
            await deny(update)
            return

        actor = touch_user(session, update)
        target = find_user(session, args[0])
        if target is None:
            await reply(
                update,
                f"No user matching {args[0]}. They appear once they're named in "
                "a payroll, or when they send /start.",
            )
            return

        try:
            kind = accounts_service.parse_kind(args[1])
        except accounts_service.AccountError as exc:
            await reply(update, f"❌ {exc}")
            return

        handle = " ".join(args[2:])
        accounts_service.add_payment_method(
            session, target, kind, handle, actor_user_id=actor.user_id
        )
        session.flush()

        methods = accounts_service.list_payment_methods(session, target.user_id)
        lines = [
            f"✅ Saved for {target.label}: {kind.value.title()} {handle}",
            "",
            f"*{target.label} now accepts:*",
        ]
        lines += [f"  `{m.payment_method_id}` {m.display}" for m in methods]
        text = "\n".join(lines)

    await reply(update, text)


async def delmethod_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """``/delmethod @handle <id>`` -- remove someone else's payment method."""
    args = context.args or []
    if len(args) < 2 or not args[1].isdigit():
        await reply(update, "Usage: `/delmethod @handle <id>`")
        return

    with session_scope() as session:
        if not require_admin(session, update, context):
            await deny(update)
            return

        actor = touch_user(session, update)
        target = find_user(session, args[0])
        if target is None:
            await reply(update, f"No user matching {args[0]}.")
            return

        methods = accounts_service.list_payment_methods(session, target.user_id)
        method = next(
            (m for m in methods if m.payment_method_id == int(args[1])), None
        )
        if method is None:
            await reply(update, f"{target.label} has no payment method #{args[1]}.")
            return

        accounts_service.remove_payment_method(
            session, method, actor_user_id=actor.user_id
        )
        text = f"✅ Removed {method.display} from {target.label}."

    await reply(update, text)
