"""Runtime configuration, read from the environment."""

from __future__ import annotations

import codecs
import os
from dataclasses import dataclass, field
from pathlib import Path


def _decode_env(raw: bytes) -> str:
    """Decode .env bytes, tolerating the encodings Windows tools produce.

    PowerShell's Out-File and Notepad write a byte-order mark, and PowerShell
    5.1 defaults Out-File to UTF-16. A stray BOM would corrupt the first key
    name, so the token would be silently ignored and the bot would report it as
    unset despite a correct file.
    """
    for bom, encoding in (
        (codecs.BOM_UTF8, "utf-8-sig"),
        (codecs.BOM_UTF16_LE, "utf-16"),
        (codecs.BOM_UTF16_BE, "utf-16"),
    ):
        if raw.startswith(bom):
            return raw.decode(encoding)

    if b"\x00" in raw:
        # UTF-16 with the BOM stripped. Decoding this as UTF-8 would push null
        # bytes into os.environ, which raises deep inside the stdlib with an
        # error naming neither the file nor the key.
        #
        # Endianness comes from where the nulls fall, not from trial decoding:
        # .env content is ASCII, so UTF-16-LE puts its nulls on odd offsets and
        # UTF-16-BE on even ones. Big-endian text decodes as *valid* little-
        # endian CJK, so a "did it decode?" check picks the wrong one.
        even_nulls = raw[0::2].count(0)
        odd_nulls = raw[1::2].count(0)
        encoding = "utf-16-be" if even_nulls > odd_nulls else "utf-16-le"
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            pass

    return raw.decode("utf-8")


def load_dotenv(path: str | os.PathLike[str] = ".env") -> None:
    """Load ``KEY=value`` pairs from a .env file into the environment.

    Deliberately dependency-free and deliberately non-overriding: a value
    already exported in the shell wins over the file, which is what you want
    when running the same checkout against a staging token.
    """
    env_path = Path(path)
    if not env_path.is_file():
        return

    for raw_line in _decode_env(env_path.read_bytes()).splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if not separator:
            continue
        key = key.strip()
        value = value.strip().strip("\"'")
        if key and key not in os.environ:
            os.environ[key] = value


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
    def from_env(cls, *, env_file: str | os.PathLike[str] = ".env") -> "Config":
        load_dotenv(env_file)

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
