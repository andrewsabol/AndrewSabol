"""Shared handler helpers: auth, user resolution, safe replies."""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from ...config import Config
from ...models import User
from ...services.accounts import is_admin as _is_admin
from ...services.payroll import get_or_create_user


def config_of(context: ContextTypes.DEFAULT_TYPE) -> Config:
    return context.application.bot_data["config"]


def touch_user(session: Session, update: Update) -> User:
    """Resolve the Telegram sender into a ``User`` row, creating or linking it.

    Payroll is entered by handle before people have opened the bot, so this both
    creates new rows and links an existing handle-only row to a telegram id on
    that person's first interaction.
    """
    tg_user = update.effective_user
    if tg_user is None:  # pragma: no cover - defensive
        raise RuntimeError("update has no sender")

    display = " ".join(filter(None, [tg_user.first_name, tg_user.last_name])) or None
    return get_or_create_user(
        session,
        username=tg_user.username,
        telegram_id=tg_user.id,
        display_name=display,
    )


def require_admin(session: Session, update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    tg_user = update.effective_user
    if tg_user is None:
        return False
    return _is_admin(session, tg_user.id, config_of(context).admin_telegram_ids)


async def deny(update: Update) -> None:
    """Refuse a non-admin, showing them the ID an admin would need to add.

    Bootstrapping the first admin is the most common setup stumble, and a bare
    refusal gives no way to tell a wrong ID from a missing one.
    """
    tg_user = update.effective_user
    detail = ""
    if tg_user is not None:
        detail = (
            f"\n\nYour Telegram ID is `{tg_user.id}`.\n"
            "To grant yourself access, put it in `ADMIN_TELEGRAM_IDS` in the "
            "`.env` file and restart the bot."
        )
    await reply(
        update, "🚫 This command is for payroll administrators only." + detail
    )


async def reply(
    update: Update,
    text: str,
    *,
    markup: Any | None = None,
    parse_mode: str | None = ParseMode.MARKDOWN,
) -> None:
    """Reply to whichever kind of update arrived (message or callback query)."""
    if update.callback_query is not None:
        await update.callback_query.message.reply_text(
            text, reply_markup=markup, parse_mode=parse_mode
        )
    elif update.effective_message is not None:
        await update.effective_message.reply_text(
            text, reply_markup=markup, parse_mode=parse_mode
        )


async def edit_or_reply(
    update: Update,
    text: str,
    *,
    markup: Any | None = None,
    parse_mode: str | None = ParseMode.MARKDOWN,
) -> None:
    """Edit the message a button lives on, falling back to a fresh reply.

    Telegram rejects an edit whose result is byte-identical to the current
    message; that is not an error worth surfacing to the user.
    """
    query = update.callback_query
    if query is None:
        await reply(update, text, markup=markup, parse_mode=parse_mode)
        return
    try:
        await query.edit_message_text(text, reply_markup=markup, parse_mode=parse_mode)
    except Exception:
        await query.message.reply_text(text, reply_markup=markup, parse_mode=parse_mode)
