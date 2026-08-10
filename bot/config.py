"""Environment loading and validation.

Reads a dotenv-style file from the repo root without pulling in a dependency:
the format we need is `KEY=VALUE`, `#` comments, and optional surrounding
quotes. Real process environment always wins over the file, so systemd's
`EnvironmentFile=` and a local `.env` behave the same way.
"""

from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = ROOT / ".env"

_loaded = False


def load_env(path: Path | None = None) -> None:
    """Populate os.environ from a .env file. Existing vars are not overwritten."""
    global _loaded
    env_file = path or ENV_PATH
    if env_file.exists():
        for raw in env_file.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            os.environ.setdefault(key, value)
    _loaded = True


def _ensure_loaded() -> None:
    if not _loaded:
        load_env()


def get(name: str, default: str | None = None) -> str | None:
    _ensure_loaded()
    value = os.environ.get(name, default)
    return value if value != "" else default


def require(name: str) -> str:
    value = get(name)
    if not value:
        raise ConfigError(
            f"{name} is not set. Copy .env.example to .env and fill it in."
        )
    return value


def get_int(name: str, default: int) -> int:
    raw = get(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from exc


def get_float(name: str, default: float) -> float:
    raw = get(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a number, got {raw!r}") from exc


def get_bool(name: str, default: bool = False) -> bool:
    raw = get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def get_path(name: str, default: str) -> Path:
    """Resolve a configured path, treating relative paths as repo-relative."""
    raw = get(name, default) or default
    path = Path(raw).expanduser()
    return path if path.is_absolute() else (ROOT / path)


class ConfigError(RuntimeError):
    """Raised when the environment is missing or malformed."""


# --------------------------------------------------------------------------
# The five variables the bot itself runs on
# --------------------------------------------------------------------------


def bot_token() -> str:
    return require("BOT_TOKEN")


def admin_id() -> int | None:
    """The single Telegram user id allowed to run /stats. None disables it."""
    raw = get("ADMIN_ID")
    if not raw:
        return None
    try:
        return int(raw.strip())
    except ValueError as exc:
        raise ConfigError(f"ADMIN_ID must be a Telegram user id, got {raw!r}") from exc


def is_admin(user_id: int | None) -> bool:
    """False when ADMIN_ID is unset — an unconfigured admin is not everyone."""
    admin = admin_id()
    return admin is not None and user_id == admin


def cta_url() -> str | None:
    return get("CTA_URL")


def database_path() -> Path:
    return get_path("DB_PATH", "data/cohort.db")


def broadcast_hour() -> int:
    return get_int("BROADCAST_HOUR", 9)


# --------------------------------------------------------------------------
# Supporting configuration for the scripts around the bot
# --------------------------------------------------------------------------


def backup_dir() -> Path:
    return get_path("BACKUP_DIR", "backups")


def questions_csv() -> Path:
    return get_path("QUESTIONS_CSV", "data/questions.csv")


def validate() -> list[str]:
    """Return a list of human-readable problems. Empty list means all good."""
    problems: list[str] = []
    _ensure_loaded()

    token = get("BOT_TOKEN")
    if not token:
        problems.append("BOT_TOKEN is missing")
    elif ":" not in token or not token.split(":", 1)[0].isdigit():
        problems.append("BOT_TOKEN does not look like a BotFather token")
    elif token.startswith("123456789:"):
        problems.append("BOT_TOKEN is still the .env.example placeholder")

    username = get("TELEGRAM_BOT_USERNAME")
    if not username:
        problems.append("TELEGRAM_BOT_USERNAME is missing")
    elif username.startswith("@"):
        problems.append("TELEGRAM_BOT_USERNAME must not include the leading '@'")
    elif username == "your_bot_username":
        problems.append("TELEGRAM_BOT_USERNAME is still the .env.example placeholder")

    url = get("CTA_URL")
    if url and not url.startswith(("http://", "https://")):
        problems.append(f"CTA_URL must start with http:// or https://, got {url!r}")

    for name, caster in (
        ("BROADCAST_RATE_LIMIT", get_float),
        ("BROADCAST_HOUR", get_int),
        ("BACKUP_RETENTION_DAYS", get_int),
    ):
        try:
            caster(name, 0)  # type: ignore[operator]
        except ConfigError as exc:
            problems.append(str(exc))

    try:
        hour = broadcast_hour()
    except ConfigError:
        hour = None  # already reported above
    if hour is not None and not 0 <= hour <= 23:
        problems.append(f"BROADCAST_HOUR must be 0-23, got {hour}")

    try:
        admin_id()
    except ConfigError as exc:
        problems.append(str(exc))

    return problems


def advisories() -> list[str]:
    """Things that are legal but probably not what you meant.

    Separate from validate() because neither of these should stop the bot from
    starting: a bot with no CTA still teaches, it just stops measuring the one
    thing the experiment exists to measure.
    """
    notes: list[str] = []
    if not get("CTA_URL"):
        notes.append("CTA_URL is unset — answered questions will have no CTA button")
    try:
        if admin_id() is None:
            notes.append("ADMIN_ID is unset — /stats will answer nobody")
    except ConfigError:
        pass  # malformed is a failure, reported by validate()
    return notes
