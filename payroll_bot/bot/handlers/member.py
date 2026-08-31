"""Commands for everyone: balances, payment methods, mark-paid, confirm receipt."""

from __future__ import annotations

import logging

from sqlalchemy import select
from telegram import Update
from telegram.ext import CommandHandler, ContextTypes, MessageHandler, filters

from ...db import session_scope
from ...ledger import aggregate, payable_balance, receivable_balance
from ...models import Payable, Receivable, Settlement, SettlementStatus
from ...services import payroll as payroll_service
from ...services import settlement as settlement_service
from ...services.accounts import (
    AccountError,
    add_payment_method,
    list_payment_methods,
    parse_kind,
    remove_payment_method,
)
from .. import keyboards, notifications, views
from .common import config_of, edit_or_reply, reply, require_admin, touch_user

log = logging.getLogger(__name__)

#: ``context.user_data`` key holding ``(kind, settlement_id)`` while we wait
#: for the payer to send a reference string or a proof image.
PENDING_DETAIL = "pending_detail"

WELCOME = """*Payroll Settlement Bot*

This bot records payroll balances and tells you exactly who to pay. It never
holds or transfers money — you pay people directly.

*Your commands*
/balance — what you owe and what you're owed
/methods — manage your Venmo / Cash App / Zelle handles
/help — show this message

Amounts you owe are settled by paying other people directly; the bot works out
who, so the fewest possible transfers are needed."""

ADMIN_HELP = """

*Administrator commands*
/payroll — enter OWES / OWED balances
/dashboard — current payroll overview
/generate — build a settlement plan
/verify — review payments awaiting verification
/user @handle — one person's full position
/reassign <id> to @user [amount] — re-route a settlement
/cancelsettlement <id> [reason] — free an amount for reassignment
/priority @handle <level> — recipient priority
/promote @handle — grant admin rights
/newpayroll [label] — start a new batch
/audit [settlement_id] — immutable history"""


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    with session_scope() as session:
        touch_user(session, update)
        admin = require_admin(session, update, context)
    await reply(update, WELCOME + (ADMIN_HELP if admin else ""))


async def balance_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cfg = config_of(context)
    with session_scope() as session:
        user = touch_user(session, update)
        batch = payroll_service.active_batch(session)
        if batch is None:
            await reply(update, "There is no active payroll right now.")
            return

        payables = session.execute(
            select(Payable).where(
                Payable.batch_id == batch.batch_id, Payable.user_id == user.user_id
            )
        ).scalars().all()
        receivables = session.execute(
            select(Receivable).where(
                Receivable.batch_id == batch.batch_id,
                Receivable.user_id == user.user_id,
            )
        ).scalars().all()

        position = settlement_service.user_position(
            session, batch.batch_id, user.user_id
        )

        messages: list[tuple[str, object]] = []

        if payables:
            balance = aggregate([payable_balance(p) for p in payables])
            text = views.payer_view(user, position.outgoing, balance, cfg.currency)
            actionable = [
                s
                for s in position.outgoing
                if s.status is SettlementStatus.PENDING
                or s.status is SettlementStatus.RECIPIENT_DENIED
            ]
            markup = keyboards.payer_keyboard(actionable) if actionable else None
            messages.append((text, markup))

        if receivables:
            balance = aggregate([receivable_balance(r) for r in receivables])
            messages.append(
                (
                    views.recipient_view(
                        user, position.incoming, balance, cfg.currency
                    ),
                    None,
                )
            )

        if not messages:
            messages.append(
                (
                    f"You have no balances in payroll #{batch.label}.",
                    None,
                )
            )

    for text, markup in messages:
        await reply(update, text, markup=markup)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await start_command(update, context)


# --------------------------------------------------------------------------
# Payment methods
# --------------------------------------------------------------------------


async def methods_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """``/methods`` lists; ``/methods add venmo @handle``; ``/methods remove <id>``."""
    args = context.args or []

    with session_scope() as session:
        user = touch_user(session, update)

        if args and args[0].lower() == "add":
            if len(args) < 3:
                await reply(
                    update,
                    "Usage: `/methods add <venmo|cashapp|zelle|paypal|other> <handle>`",
                )
                return
            try:
                kind = parse_kind(args[1])
            except AccountError as exc:
                await reply(update, f"❌ {exc}")
                return
            handle = " ".join(args[2:])
            add_payment_method(session, user, kind, handle, actor_user_id=user.user_id)
            await reply(update, f"✅ Saved {kind.value.title()}: {handle}")
            return

        if args and args[0].lower() in ("remove", "delete"):
            if len(args) < 2 or not args[1].isdigit():
                await reply(update, "Usage: `/methods remove <id>`")
                return
            methods = list_payment_methods(session, user.user_id)
            target = next(
                (m for m in methods if m.payment_method_id == int(args[1])), None
            )
            if target is None:
                await reply(update, "No such payment method of yours.")
                return
            remove_payment_method(session, target, actor_user_id=user.user_id)
            await reply(update, "✅ Removed.")
            return

        methods = list_payment_methods(session, user.user_id)
        lines = ["*YOUR PAYMENT METHODS*", ""]
        if methods:
            for method in methods:
                lines.append(f"  `{method.payment_method_id}` {method.display}")
        else:
            lines.append("_None saved._")
        lines.extend(
            [
                "",
                "Add one: `/methods add venmo @YourHandle`",
                "Remove:  `/methods remove <id>`",
                "",
                "The matcher prefers routing payments between people who share "
                "a payment method, so keeping these current means fewer manual "
                "fixes.",
            ]
        )
        text = "\n".join(lines)

    await reply(update, text)


# --------------------------------------------------------------------------
# Payer: mark paid
# --------------------------------------------------------------------------


async def mark_paid_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    _, args = keyboards.decode(query.data)
    settlement_id = int(args[0])

    with session_scope() as session:
        user = touch_user(session, update)
        settlement = session.get(Settlement, settlement_id)
        if settlement is None:
            await edit_or_reply(update, "That settlement no longer exists.")
            return

        try:
            settlement_service.mark_paid(
                session, settlement, actor_user_id=user.user_id
            )
        except settlement_service.SettlementError as exc:
            await edit_or_reply(update, f"❌ {exc}")
            return

        session.flush()
        amount = views.fmt(settlement.amount, settlement.currency)
        recipient_label = settlement.recipient.label
        delivered = await notifications.request_recipient_confirmation(
            context.bot, session, settlement
        )

    note = (
        f"✅ Marked {amount} to {recipient_label} as paid.\n\n"
        + (
            "They've been asked to confirm."
            if delivered
            else "⚠️ They haven't opened the bot yet, so an admin will verify manually."
        )
        + "\n\nYou can add a reference or proof below — it speeds up verification."
    )
    await edit_or_reply(
        update, note, markup=keyboards.payment_detail_keyboard(settlement_id)
    )


async def add_reference_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    _, args = keyboards.decode(query.data)
    context.user_data[PENDING_DETAIL] = ("reference", int(args[0]))
    await reply(
        update, "Send the transaction / reference ID for this payment, or /cancel."
    )


async def add_proof_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    _, args = keyboards.decode(query.data)
    context.user_data[PENDING_DETAIL] = ("proof", int(args[0]))
    await reply(update, "Send a screenshot of the payment, or /cancel.")


async def receive_detail(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Consume the next message when the payer owes us a reference or a proof.

    Registered after the admin handlers in the same group, so an active
    /payroll conversation still takes precedence; this only fires when the user
    has actually pressed one of the detail buttons.
    """
    pending = context.user_data.get(PENDING_DETAIL)
    if pending is None:
        return

    kind, settlement_id = pending
    message = update.effective_message

    reference = None
    file_id = None
    if kind == "reference":
        reference = (message.text or "").strip()
        if not reference:
            await reply(update, "That was empty. Send the reference, or /cancel.")
            return
    else:
        if message.photo:
            # Telegram sends several sizes; the last is the highest resolution.
            file_id = message.photo[-1].file_id
        elif message.document:
            file_id = message.document.file_id
        else:
            await reply(update, "That wasn't an image. Send a screenshot, or /cancel.")
            return

    context.user_data.pop(PENDING_DETAIL, None)

    with session_scope() as session:
        user = touch_user(session, update)
        settlement = session.get(Settlement, settlement_id)
        if settlement is None:
            await reply(update, "That settlement no longer exists.")
            return
        try:
            settlement_service.attach_proof(
                session,
                settlement,
                actor_user_id=user.user_id,
                transaction_reference=reference,
                proof_file_id=file_id,
            )
        except settlement_service.SettlementError as exc:
            await reply(update, f"❌ {exc}")
            return

    if kind == "reference":
        await reply(update, f"✅ Reference saved: `{reference}`")
    else:
        await reply(update, "✅ Proof attached.")


async def cancel_detail(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if context.user_data.pop(PENDING_DETAIL, None) is None:
        return
    await reply(update, "Cancelled.")


# --------------------------------------------------------------------------
# Recipient: confirm / deny
# --------------------------------------------------------------------------


async def recipient_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    action, args = keyboards.decode(query.data)
    settlement_id = int(args[0])

    cfg = config_of(context)
    with session_scope() as session:
        user = touch_user(session, update)
        settlement = session.get(Settlement, settlement_id)
        if settlement is None:
            await edit_or_reply(update, "That settlement no longer exists.")
            return

        try:
            if action == keyboards.RECIPIENT_CONFIRM:
                settlement_service.recipient_confirm(
                    session, settlement, actor_user_id=user.user_id
                )
                note = (
                    f"✅ Thanks — confirmed receipt of "
                    f"{views.fmt(settlement.amount, settlement.currency)} from "
                    f"{settlement.payer.label}.\n\n"
                    "An administrator will do the final verification. Your "
                    "balance updates once they do."
                )
            else:
                settlement_service.recipient_deny(
                    session, settlement, actor_user_id=user.user_id
                )
                note = (
                    f"Recorded as *not received*. An administrator has been "
                    f"notified and will follow up with {settlement.payer.label}."
                )
        except settlement_service.SettlementError as exc:
            await edit_or_reply(update, f"❌ {exc}")
            return

        session.flush()
        await notifications.notify_admins_for_review(
            context.bot, session, settlement, cfg.admin_telegram_ids
        )

    await edit_or_reply(update, note)


def build_handlers() -> list:
    """Handlers every user gets. Order matters: see ``receive_detail``."""
    return [
        CommandHandler("start", start_command),
        CommandHandler("help", help_command),
        CommandHandler("balance", balance_command),
        CommandHandler("methods", methods_command),
    ]


def build_detail_handlers() -> list:
    """Registered *after* the admin handlers so /payroll wins any conflict."""
    return [
        CommandHandler("cancel", cancel_detail),
        MessageHandler(
            (filters.TEXT | filters.PHOTO | filters.Document.ALL) & ~filters.COMMAND,
            receive_detail,
        ),
    ]
