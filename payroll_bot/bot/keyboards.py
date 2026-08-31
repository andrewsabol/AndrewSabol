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
PAYROLL_CONFIRM_IMBALANCE = "pr_imbal"

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


def imbalance_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "⚠️ Save anyway (I understand)",
                    callback_data=PAYROLL_CONFIRM_IMBALANCE,
                )
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
                InlineKeyboardButton(
                    "⚙️ Generate Settlements", callback_data=DASH_GENERATE
                )
            ],
        ]
    )


def back_to_dashboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("⬅️ Dashboard", callback_data=DASH_HOME)]]
    )
