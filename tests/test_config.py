"""Configuration loading, including the encodings Windows tools emit."""


import pytest

from payroll_bot.config import Config, load_dotenv

ENV_BODY = "TELEGRAM_BOT_TOKEN=1234567890:AAFakeTestTokenNotARealBotToken12\nADMIN_TELEGRAM_IDS=7393421242\n"


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch):
    for key in (
        "TELEGRAM_BOT_TOKEN",
        "ADMIN_TELEGRAM_IDS",
        "PAYROLL_CURRENCY",
        "DATABASE_URL",
        "STRICT_PAYMENT_METHODS",
        "ECHO_SQL",
    ):
        monkeypatch.delenv(key, raising=False)


@pytest.mark.parametrize(
    "encoding",
    ["utf-8", "utf-8-sig", "utf-16", "utf-16-le", "utf-16-be"],
)
def test_loads_env_written_by_windows_tools(tmp_path, encoding):
    """PowerShell Out-File and Notepad emit BOMs and UTF-16."""
    env_file = tmp_path / ".env"
    env_file.write_bytes(ENV_BODY.encode(encoding))

    config = Config.from_env(env_file=env_file)

    assert config.bot_token == "1234567890:AAFakeTestTokenNotARealBotToken12"
    assert config.admin_telegram_ids == {7393421242}


def test_shell_environment_wins_over_the_file(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text(ENV_BODY + "PAYROLL_CURRENCY=USD\n", encoding="utf-8")
    monkeypatch.setenv("PAYROLL_CURRENCY", "GBP")

    assert Config.from_env(env_file=env_file).currency == "GBP"


def test_comments_blank_lines_and_quotes(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "# a comment\n\nTELEGRAM_BOT_TOKEN=\"1234567890:AAFakeTestTokenNotARealBotToken12\"\n"
        "ADMIN_TELEGRAM_IDS='7393421242'\n",
        encoding="utf-8",
    )
    config = Config.from_env(env_file=env_file)
    assert config.bot_token == "1234567890:AAFakeTestTokenNotARealBotToken12"
    assert config.admin_telegram_ids == {7393421242}


def test_windows_line_endings(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_bytes(ENV_BODY.replace("\n", "\r\n").encode("utf-8"))
    assert Config.from_env(env_file=env_file).bot_token == "1234567890:AAFakeTestTokenNotARealBotToken12"


def test_missing_token_exits_with_a_useful_message(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("ADMIN_TELEGRAM_IDS=1\n", encoding="utf-8")
    with pytest.raises(SystemExit) as excinfo:
        Config.from_env(env_file=env_file)
    assert "TELEGRAM_BOT_TOKEN" in str(excinfo.value)


def test_non_numeric_admin_id_is_rejected(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "TELEGRAM_BOT_TOKEN=1234567890:AAFakeTestTokenNotARealBotToken12\nADMIN_TELEGRAM_IDS=@andrew\n", encoding="utf-8"
    )
    with pytest.raises(SystemExit):
        Config.from_env(env_file=env_file)


def test_absent_env_file_is_not_an_error(tmp_path):
    load_dotenv(tmp_path / "nope.env")  # must not raise


def test_placeholder_token_is_rejected_with_guidance(tmp_path):
    """The copy-paste placeholder must fail with advice, not an InvalidToken."""
    env_file = tmp_path / ".env"
    env_file.write_text(
        "TELEGRAM_BOT_TOKEN=PASTE_YOUR_NEW_TOKEN_HERE\nADMIN_TELEGRAM_IDS=1\n",
        encoding="utf-8",
    )
    with pytest.raises(SystemExit) as excinfo:
        Config.from_env(env_file=env_file)

    message = str(excinfo.value)
    assert "does not look like a Telegram token" in message
    assert "@BotFather" in message


def test_a_real_shaped_token_is_accepted(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "TELEGRAM_BOT_TOKEN=1234567890:AAFakeTestTokenNotARealBotToken12\n"
        "ADMIN_TELEGRAM_IDS=7393421242\n",
        encoding="utf-8",
    )
    assert Config.from_env(env_file=env_file).bot_token.startswith("1234567890:")
