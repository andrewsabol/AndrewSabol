"""Runtime configuration, read from the environment."""

from __future__ import annotations

import os
from dataclasses import dataclass, field


@dataclass
class Config:
    bot_token: str
    database_url: str = "sqlite:///payroll.db"
    admin_telegram_ids: set[int] = field(default_factory=set)
    currency: str = "USD"
    strict_payment_methods: bool = False
    """When true, the matcher refuses pairings with no shared payment method
    instead of flagging them for admin review."""

    echo_sql: bool = False

    @classmethod
    def from_env(cls) -> "Config":
        token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
        if not token:
            raise SystemExit(
                "TELEGRAM_BOT_TOKEN is not set. Copy .env.example to .env and fill it in."
            )

        raw_admins = os.environ.get("ADMIN_TELEGRAM_IDS", "")
        admins: set[int] = set()
        for chunk in raw_admins.replace(";", ",").split(","):
            chunk = chunk.strip()
            if not chunk:
                continue
            try:
                admins.add(int(chunk))
            except ValueError:
                raise SystemExit(
                    f"ADMIN_TELEGRAM_IDS contains a non-numeric entry: {chunk!r}"
                )

        return cls(
            bot_token=token,
            database_url=os.environ.get("DATABASE_URL", "sqlite:///payroll.db"),
            admin_telegram_ids=admins,
            currency=os.environ.get("PAYROLL_CURRENCY", "USD").upper(),
            strict_payment_methods=_flag(os.environ.get("STRICT_PAYMENT_METHODS")),
            echo_sql=_flag(os.environ.get("ECHO_SQL")),
        )


def _flag(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}
