"""Inline keyboard construction and callback-data encoding.

Callback data is capped at 64 bytes by Telegram, so it is kept to a compact
``action:arg1:arg2`` form rather than anything JSON-shaped.
"""

from __future__ import annotations

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from ..models import Settlement
from ..money import fmt

# Callback action names.
PLAN_APPROVE = "plan_ok"
PLAN_RECALC = "plan_recalc"
PLAN_EDIT = "plan_edit"
PLAN_CANCEL = "plan_cancel"

MARK_PAID = "paid"
ADD_REFERENCE = "ref"
ADD_PROOF = "proof"

RECIPIENT_CONFIRM = "rcv_ok"
RECIPIENT_DENY = "rcv_no"

ADMIN_VERIFY = "adm_ver"
ADMIN_REJECT = "adm_rej"
ADMIN_DISPUTE = "adm_dis"

DASH_OWING = "d_owing"
DASH_OWED = "d_owed"
DASH_SETTLEMENTS = "d_setl"
DASH_VERIFY = "d_verify"
DASH_DISPUTES = "d_disp"
DASH_GENERATE = "d_gen"
DASH_REPORTS = "d_rep"
DASH_HOME = "d_home"
DASH_QUEUE = "d_queue"
DASH_NEXT = "d_next"

QUEUE_SKIP = "q_skip"
QUEUE_BACK = "q_back"
QUEUE_ASSIGN = "q_assign"
QUEUE_LIST = "q_list"


def encode(action: str, *args: object) -> str:
    return ":".join([action, *(str(a) for a in args)])


def decode(data: str) -> tuple[str, list[str]]:
    parts = data.split(":")
    return parts[0], parts[1:]


def plan_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("✅ Approve Settlement Plan", callback_data=PLAN_APPROVE)],
            [
                InlineKeyboardButton("🔄 Recalculate", callback_data=PLAN_RECALC),
                InlineKeyboardButton("✏️ Edit", callback_data=PLAN_EDIT),
            ],
            [InlineKeyboardButton("❌ Cancel", callback_data=PLAN_CANCEL)],
        ]
    )


def payer_keyboard(settlements: list[Settlement]) -> InlineKeyboardMarkup:
    rows = []
    for settlement in settlements:
        rows.append(
            [
                InlineKeyboardButton(
                    f"Mark {fmt(settlement.amount, settlement.currency)} Paid "
                    f"→ {settlement.recipient.label}",
                    callback_data=encode(MARK_PAID, settlement.settlement_id),
                )
            ]
        )
    return InlineKeyboardMarkup(rows)


def payment_detail_keyboard(settlement_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "🔖 Add reference", callback_data=encode(ADD_REFERENCE, settlement_id)
                ),
                InlineKeyboardButton(
                    "📎 Upload proof", callback_data=encode(ADD_PROOF, settlement_id)
                ),
            ]
        ]
    )


def recipient_keyboard(settlement_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "✅ Received", callback_data=encode(RECIPIENT_CONFIRM, settlement_id)
                ),
                InlineKeyboardButton(
                    "❌ Not Received", callback_data=encode(RECIPIENT_DENY, settlement_id)
                ),
            ]
        ]
    )


def admin_review_keyboard(settlement_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "✅ VERIFY", callback_data=encode(ADMIN_VERIFY, settlement_id)
                ),
                InlineKeyboardButton(
                    "❌ REJECT", callback_data=encode(ADMIN_REJECT, settlement_id)
                ),
            ],
            [
                InlineKeyboardButton(
                    "⚠️ DISPUTE", callback_data=encode(ADMIN_DISPUTE, settlement_id)
                )
            ],
        ]
    )


def dashboard_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("People Who Owe", callback_data=DASH_OWING),
                InlineKeyboardButton("People Owed", callback_data=DASH_OWED),
            ],
            [
                InlineKeyboardButton("Settlements", callback_data=DASH_SETTLEMENTS),
                InlineKeyboardButton("Needs Verification", callback_data=DASH_VERIFY),
            ],
            [
                InlineKeyboardButton("Disputes", callback_data=DASH_DISPUTES),
                InlineKeyboardButton("Reports", callback_data=DASH_REPORTS),
            ],
            [
                InlineKeyboardButton("⏭ Next In Line", callback_data=DASH_NEXT),
                InlineKeyboardButton("📋 Queue", callback_data=DASH_QUEUE),
            ],
            [
                InlineKeyboardButton(
                    "⚙️ Generate Settlements", callback_data=DASH_GENERATE
                )
            ],
        ]
    )


def queue_card_keyboard(
    index: int,
    total: int,
    *,
    payer_user_id: int | None = None,
    receivable_id: int | None = None,
    can_assign: bool = False,
) -> InlineKeyboardMarkup:
    """Controls for one person in the queue.

    Skip steps to the next person and wraps at the end, so an admin cycling to
    find someone a payer can actually pay never reaches a dead end.
    """
    suffix = [payer_user_id] if payer_user_id else []
    rows: list[list[InlineKeyboardButton]] = []

    if can_assign and receivable_id is not None and payer_user_id:
        rows.append(
            [
                InlineKeyboardButton(
                    "✅ Assign this payment",
                    callback_data=encode(QUEUE_ASSIGN, receivable_id, payer_user_id),
                )
            ]
        )

    nav = []
    if total > 1:
        nav.append(
            InlineKeyboardButton(
                "⬅️ Previous", callback_data=encode(QUEUE_BACK, index, *suffix)
            )
        )
        nav.append(
            InlineKeyboardButton(
                "Skip ➡️", callback_data=encode(QUEUE_SKIP, index, *suffix)
            )
        )
    if nav:
        rows.append(nav)

    rows.append(
        [
            InlineKeyboardButton("📋 Whole queue", callback_data=QUEUE_LIST),
            InlineKeyboardButton("⬅️ Dashboard", callback_data=DASH_HOME),
        ]
    )
    return InlineKeyboardMarkup(rows)


def queue_list_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("⏭ Next in line", callback_data=DASH_NEXT),
                InlineKeyboardButton("⬅️ Dashboard", callback_data=DASH_HOME),
            ]
        ]
    )


def back_to_dashboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("⬅️ Dashboard", callback_data=DASH_HOME)]]
    )
