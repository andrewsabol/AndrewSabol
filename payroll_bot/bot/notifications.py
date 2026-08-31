"""Outbound messages to payers, recipients, and admins.

Every send is individually guarded: a user who has never opened the bot, or who
has blocked it, has no reachable chat, and that must not abort the notification
run for everyone else in the payroll.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session
from telegram import Bot
from telegram.constants import ParseMode

from ..models import Settlement, User
from ..money import fmt
from . import keyboards
from .views import settlement_review

log = logging.getLogger(__name__)


@dataclass
class DeliveryReport:
    delivered: int = 0
    unreachable: list[str] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.unreachable is None:
            self.unreachable = []

    @property
    def summary(self) -> str:
        text = f"Notified {self.delivered} user(s)."
        if self.unreachable:
            text += (
                f"\n\n⚠️ Could not reach {len(self.unreachable)}: "
                + ", ".join(self.unreachable)
                + "\nThey need to send /start to the bot first."
            )
        return text


async def _send(bot: Bot, user: User, text: str, markup=None) -> bool:
    if user.telegram_id is None:
        return False
    try:
        await bot.send_message(
            chat_id=user.telegram_id,
            text=text,
            reply_markup=markup,
            parse_mode=ParseMode.MARKDOWN,
        )
        return True
    except Exception as exc:  # pragma: no cover - network dependent
        log.warning("could not message %s: %s", user.label, exc)
        return False


async def notify_plan_approved(
    bot: Bot, session: Session, settlements: list[Settlement]
) -> DeliveryReport:
    """Tell each payer what to send, and each recipient what to expect."""
    report = DeliveryReport()

    by_payer: dict[int, list[Settlement]] = {}
    by_recipient: dict[int, list[Settlement]] = {}
    for settlement in settlements:
        by_payer.setdefault(settlement.payer_user_id, []).append(settlement)
        by_recipient.setdefault(settlement.recipient_user_id, []).append(settlement)

    for user_id, items in by_payer.items():
        user = session.get(User, user_id)
        if user is None:
            continue
        total = sum((s.amount for s in items), start=items[0].amount * 0)
        lines = [
            "*PAYROLL PAYMENT*",
            "",
            f"You currently owe: {fmt(total, items[0].currency)}",
            "",
            "*Please make the following payments:*",
            "",
        ]
        for index, settlement in enumerate(items, start=1):
            lines.append(f"{index}. {settlement.recipient.label}")
            lines.append(f"   {fmt(settlement.amount, settlement.currency)}")
            lines.append(
                f"   {settlement.payment_method_note}"
                if settlement.payment_method_note
                else "   ⚠️ No payment method on file — contact your admin"
            )
            lines.append("")
        lines.append("_This bot never holds or transfers money. Pay the person directly._")

        ok = await _send(
            bot, user, "\n".join(lines), keyboards.payer_keyboard(items)
        )
        report.delivered += int(ok)
        if not ok:
            report.unreachable.append(user.label)

    for user_id, items in by_recipient.items():
        user = session.get(User, user_id)
        if user is None:
            continue
        total = sum((s.amount for s in items), start=items[0].amount * 0)
        lines = [
            "*PAYROLL RECEIVABLE*",
            "",
            f"You are owed: {fmt(total, items[0].currency)}",
            "",
            "*Incoming payments:*",
        ]
        for settlement in items:
            lines.append(
                f"  {settlement.payer.label} → You: "
                f"{fmt(settlement.amount, settlement.currency)}"
            )
        lines.append("")
        lines.append("You'll be asked to confirm each payment once the sender marks it paid.")

        ok = await _send(bot, user, "\n".join(lines))
        report.delivered += int(ok)
        if not ok:
            report.unreachable.append(user.label)

    return report


async def request_recipient_confirmation(
    bot: Bot, session: Session, settlement: Settlement
) -> bool:
    """Ask the recipient to confirm a payment the payer says they made."""
    recipient = session.get(User, settlement.recipient_user_id)
    if recipient is None:
        return False

    lines = [
        "*PAYMENT CONFIRMATION NEEDED*",
        "",
        f"{settlement.payer.label} says they sent you "
        f"{fmt(settlement.amount, settlement.currency)}.",
    ]
    if settlement.payment_method_note:
        lines.append(f"Method: {settlement.payment_method_note}")
    if settlement.transaction_reference:
        lines.append(f"Reference: `{settlement.transaction_reference}`")
    lines.append("")
    lines.append("Did you receive it?")

    sent = await _send(
        bot,
        recipient,
        "\n".join(lines),
        keyboards.recipient_keyboard(settlement.settlement_id),
    )

    if sent and settlement.proof_file_id:
        try:  # pragma: no cover - network dependent
            await bot.send_photo(
                chat_id=recipient.telegram_id,
                photo=settlement.proof_file_id,
                caption="Proof of payment supplied by the sender.",
            )
        except Exception as exc:
            log.warning("could not forward proof: %s", exc)

    return sent


async def notify_admins_for_review(
    bot: Bot, session: Session, settlement: Settlement, bootstrap_ids: set[int]
) -> None:
    """Push a settlement to admins once both sides have had their say."""
    text = settlement_review(settlement)
    markup = keyboards.admin_review_keyboard(settlement.settlement_id)

    for admin in _admin_users(session, bootstrap_ids):
        await _send(bot, admin, text, markup)


async def notify_payer_result(
    bot: Bot, session: Session, settlement: Settlement, message: str
) -> None:
    payer = session.get(User, settlement.payer_user_id)
    if payer is not None:
        await _send(bot, payer, message)


async def notify_user(bot: Bot, session: Session, user_id: int, message: str) -> bool:
    user = session.get(User, user_id)
    if user is None:
        return False
    return await _send(bot, user, message)


def _admin_users(session: Session, bootstrap_ids: set[int]) -> list[User]:
    admins = list(
        session.execute(select(User).where(User.is_admin.is_(True))).scalars().all()
    )
    known = {a.telegram_id for a in admins}
    for telegram_id in bootstrap_ids:
        if telegram_id in known:
            continue
        user = session.execute(
            select(User).where(User.telegram_id == telegram_id)
        ).scalar_one_or_none()
        if user is not None:
            admins.append(user)
    return admins
