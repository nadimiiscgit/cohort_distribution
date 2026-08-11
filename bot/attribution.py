"""Attribution: turning a click into a durable source label.

Telegram deep links carry a single opaque payload: `t.me/<bot>?start=<payload>`.
Telegram allows only [A-Za-z0-9_-] there and caps it at 64 characters, so a
source code is normalised into that alphabet before it goes anywhere near a
link, and normalised again on the way back in. Same function both directions
means a code always round-trips to itself, and one campaign never splits into
two rows in the report.
"""

from __future__ import annotations

import re
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

from . import config

MAX_PAYLOAD = 64
DEFAULT_SOURCE = "direct"
_ALLOWED = re.compile(r"[^A-Za-z0-9_-]+")


def normalize_source(raw: str | None) -> str:
    """Collapse arbitrary text into a safe, comparable source code.

    Anything that normalises away to nothing — no payload, punctuation only —
    becomes "direct", so `source_channel` is never empty or NULL.
    """
    if not raw:
        return DEFAULT_SOURCE
    code = _ALLOWED.sub("-", raw.strip()).strip("-_").lower()
    code = re.sub(r"-{2,}", "-", code)
    return code[:MAX_PAYLOAD] or DEFAULT_SOURCE


def deep_link(source: str, bot_username: str | None = None) -> str:
    """Build the t.me link that records `source` when opened."""
    username = bot_username or config.require("TELEGRAM_BOT_USERNAME")
    return f"https://t.me/{quote(username.lstrip('@'))}?start={normalize_source(source)}"


def landing_link(source: str, base_url: str | None = None) -> str:
    """Build the landing-page URL that forwards `source` into the deep link."""
    base = (base_url or config.get("LANDING_BASE_URL", "") or "").rstrip("/")
    return f"{base}/?s={normalize_source(source)}" if base else f"/?s={normalize_source(source)}"


def cta_link(
    url: str,
    *,
    user_id: int,
    source_channel: str,
    question_id: str | None = None,
) -> str:
    """Stamp CTA_URL with who is clicking and where they came from.

    Telegram sends no update when a URL button is tapped (see NOTES.md), so the
    click can only be counted at the destination. These parameters are what
    makes that possible: `uid` joins back to users.user_id, `src` carries the
    attribution without a second lookup, `qid` says which question converted.

    Existing query parameters on CTA_URL are preserved — the operator may
    already have utm tags on it.
    """
    parts = urlsplit(url)
    params = parse_qsl(parts.query, keep_blank_values=True)
    params += [("src", normalize_source(source_channel)), ("uid", str(user_id))]
    if question_id:
        params.append(("qid", question_id))
    return urlunsplit(parts._replace(query=urlencode(params)))
