"""Attribution: turning a click into a durable source label.

Telegram deep links carry a single opaque payload: `t.me/<bot>?start=<payload>`.
Telegram allows only [A-Za-z0-9_-] there and caps it at 64 characters, so a
source code is normalised into that alphabet before it goes anywhere near a
link, and normalised again on the way back in. Same function both directions
means a code always round-trips to itself.
"""

from __future__ import annotations

import re
from urllib.parse import quote

from . import config

MAX_PAYLOAD = 64
_ALLOWED = re.compile(r"[^A-Za-z0-9_-]+")


def normalize_source(raw: str | None) -> str:
    """Collapse arbitrary text into a safe, comparable source code."""
    if not raw:
        return default_source()
    code = _ALLOWED.sub("-", raw.strip()).strip("-_").lower()
    code = re.sub(r"-{2,}", "-", code)
    return code[:MAX_PAYLOAD] or default_source()


def default_source() -> str:
    return (config.get("DEFAULT_SOURCE", "direct") or "direct").lower()


def deep_link(source: str, bot_username: str | None = None) -> str:
    """Build the t.me link that records `source` when opened."""
    username = bot_username or config.require("TELEGRAM_BOT_USERNAME")
    return f"https://t.me/{quote(username.lstrip('@'))}?start={normalize_source(source)}"


def landing_link(source: str, base_url: str | None = None) -> str:
    """Build the landing-page URL that forwards `source` into the deep link."""
    base = (base_url or config.get("LANDING_BASE_URL", "") or "").rstrip("/")
    return f"{base}/?s={normalize_source(source)}" if base else f"/?s={normalize_source(source)}"
