"""Builds and runs the Telegram application."""

from __future__ import annotations

import logging

from telegram import Update
from telegram.ext import Application, CallbackQueryHandler, ContextTypes

from ..config import Config
from ..db import init_engine
from . import keyboards
from .handlers import admin, member

log = logging.getLogger(__name__)


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Log the failure and tell the user, rather than dying silently.

    A handler that raises mid-flow leaves the person staring at a button that
    did nothing; the ledger itself is safe because ``session_scope`` rolls back.
    """
    log.exception("handler error", exc_info=context.error)

    if isinstance(update, Update):
        try:
            if update.callback_query is not None:
                await update.callback_query.answer("Something went wrong.", show_alert=True)
            elif update.effective_message is not None:
                await update.effective_message.reply_text(
                    "⚠️ Something went wrong handling that. Nothing was changed — "
                    "please try again, and tell your admin if it keeps happening."
                )
        except Exception:  # pragma: no cover - best effort
            log.exception("could not deliver error notice")


def build_application(config: Config) -> Application:
    init_engine(config.database_url, echo=config.echo_sql)

    application = Application.builder().token(config.bot_token).build()
    application.bot_data["config"] = config

    for handler in member.build_handlers():
        application.add_handler(handler)
    for handler in admin.build_handlers():
        application.add_handler(handler)

    # Plan lifecycle.
    application.add_handler(
        CallbackQueryHandler(admin.approve_plan, pattern=f"^{keyboards.PLAN_APPROVE}$")
    )
    application.add_handler(
        CallbackQueryHandler(admin.recalculate_plan, pattern=f"^{keyboards.PLAN_RECALC}$")
    )
    application.add_handler(
        CallbackQueryHandler(admin.edit_plan, pattern=f"^{keyboards.PLAN_EDIT}$")
    )
    application.add_handler(
        CallbackQueryHandler(admin.cancel_plan, pattern=f"^{keyboards.PLAN_CANCEL}$")
    )

    # Dashboard.
    application.add_handler(
        CallbackQueryHandler(admin.dashboard_command, pattern=f"^{keyboards.DASH_HOME}$")
    )
    application.add_handler(
        CallbackQueryHandler(
            admin.generate_settlements, pattern=f"^{keyboards.DASH_GENERATE}$"
        )
    )
    application.add_handler(
        CallbackQueryHandler(
            admin.needs_verification, pattern=f"^{keyboards.DASH_VERIFY}$"
        )
    )
    application.add_handler(
        CallbackQueryHandler(
            admin.dashboard_list,
            pattern=(
                f"^({keyboards.DASH_OWING}|{keyboards.DASH_OWED}|"
                f"{keyboards.DASH_SETTLEMENTS}|{keyboards.DASH_DISPUTES}|"
                f"{keyboards.DASH_REPORTS})$"
            ),
        )
    )

    # Payment queue.
    application.add_handler(
        CallbackQueryHandler(admin.queue_command, pattern=f"^{keyboards.DASH_QUEUE}$")
    )
    application.add_handler(
        CallbackQueryHandler(admin.queue_command, pattern=f"^{keyboards.QUEUE_LIST}$")
    )
    application.add_handler(
        CallbackQueryHandler(admin.next_command, pattern=f"^{keyboards.DASH_NEXT}$")
    )
    application.add_handler(
        CallbackQueryHandler(
            admin.queue_step,
            pattern=f"^({keyboards.QUEUE_SKIP}|{keyboards.QUEUE_BACK}):",
        )
    )
    application.add_handler(
        CallbackQueryHandler(admin.queue_assign, pattern=f"^{keyboards.QUEUE_ASSIGN}:")
    )

    # Settlement lifecycle.
    application.add_handler(
        CallbackQueryHandler(
            member.mark_paid_callback, pattern=f"^{keyboards.MARK_PAID}:"
        )
    )
    application.add_handler(
        CallbackQueryHandler(
            member.recipient_callback,
            pattern=f"^({keyboards.RECIPIENT_CONFIRM}|{keyboards.RECIPIENT_DENY}):",
        )
    )
    application.add_handler(
        CallbackQueryHandler(
            admin.admin_decision,
            pattern=(
                f"^({keyboards.ADMIN_VERIFY}|{keyboards.ADMIN_REJECT}|"
                f"{keyboards.ADMIN_DISPUTE}):"
            ),
        )
    )
    application.add_handler(
        CallbackQueryHandler(
            member.add_reference_callback, pattern=f"^{keyboards.ADD_REFERENCE}:"
        )
    )
    application.add_handler(
        CallbackQueryHandler(
            member.add_proof_callback, pattern=f"^{keyboards.ADD_PROOF}:"
        )
    )

    # Registered last in the group: the catch-all message handler that collects
    # a reference or proof image only fires when nothing above claimed the
    # update, so an in-flight /payroll entry always wins.
    for handler in member.build_detail_handlers():
        application.add_handler(handler)

    application.add_error_handler(on_error)
    return application


def run(config: Config | None = None) -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    )
    # httpx logs every Telegram poll at INFO; that buries everything else.
    logging.getLogger("httpx").setLevel(logging.WARNING)

    config = config or Config.from_env()
    application = build_application(config)

    log.info("payroll settlement bot starting (database: %s)", config.database_url)
    if config.admin_telegram_ids:
        log.info(
            "bootstrap admin Telegram IDs: %s",
            ", ".join(str(i) for i in sorted(config.admin_telegram_ids)),
        )
    else:
        # Without this nobody can run /payroll, and the only symptom is a
        # refusal message that looks like a permissions bug.
        log.warning(
            "ADMIN_TELEGRAM_IDS is empty - no one can use admin commands. "
            "Send /whoami to the bot to get your ID, put it in .env, restart."
        )
    application.run_polling(allowed_updates=Update.ALL_TYPES)
