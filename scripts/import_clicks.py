#!/usr/bin/env python3
"""Tally landing-page clicks per channel from web server access logs.

    python scripts/import_clicks.py                            # dry run
    python scripts/import_clicks.py --write
    python scripts/import_clicks.py '/var/log/nginx/access.log*' --write
    python scripts/import_clicks.py --write --since 2026-08-01
    python scripts/import_clicks.py --write --force            # allow lower counts

`scripts/attribution.py report` can tell you how many people pressed /start on
a channel tag, but not how many saw the link and did nothing. Without that
denominator a link with twelve clicks and eight users looks identical to one
with two thousand clicks and eight users, which is the difference between a
channel worth paying for and one worth dropping.

The landing page carries the tag as `/?s=<code>`, so every click is already a
line in the access log. This reads those lines and stores a per-day tally in
`link_clicks` — no analytics script on the page, no third party, and no
per-visitor row anywhere.

Runs hourly from cron. Re-running is safe: each date present in the input
replaces that date's tally rather than adding to it.
"""

from __future__ import annotations

import _bootstrap  # noqa: F401  (sys.path side effect)

import argparse
import glob
import gzip
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from bot import attribution, config, db

# Combined log format, which is the nginx and Apache default:
#   IP - user [10/Aug/2026:13:55:36 +0530] "GET /?s=x HTTP/1.1" 200 512 "ref" "ua"
# Tolerant on both ends: the request line may lack an HTTP version, and some
# configurations append extra fields after the user agent.
LINE = re.compile(
    r'\[(?P<ts>[^\]]+)\]\s+'
    r'"(?P<method>[A-Z]+)\s+(?P<target>\S+)[^"]*"\s+'
    r'(?P<status>\d{3})\s+\S+\s+'
    r'"[^"]*"\s+"(?P<ua>[^"]*)"'
)

# Month names are matched by hand rather than with strptime's %b, which is
# locale-dependent: the log is always English, the server's locale may not be.
MONTHS = {
    m: i
    for i, m in enumerate(
        "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split(), start=1
    )
}

# Static assets inherit the query string from the page that referenced them in
# some setups, and would otherwise multiply every real click by however many
# files the page loads.
ASSET = re.compile(r"\.(css|js|ico|png|jpe?g|gif|svg|webp|woff2?|txt|xml|map)$", re.I)

# Substring tokens, not a `\bbot\b` regex. Word boundaries get this wrong in
# both directions: they miss "TelegramBot" (no boundary before "Bot") and they
# match phone models like CUBOT that appear in real user agents.
#
# Telegram, WhatsApp and Slack fetch every URL that passes through them to
# build a link preview, which means pasting a campaign link into the channel
# you are measuring registers a click before any human has seen it. Filtering
# these is not tidying — it is the difference between a real number and one
# that is mostly your own paste.
BOT_TOKENS = (
    "telegrambot", "twitterbot", "facebookexternalhit", "whatsapp", "slackbot",
    "discordbot", "linkedinbot", "redditbot", "skypeuripreview", "embedly",
    "pinterest", "vkshare", "quora link preview", "bitlybot", "nuzzel",
    "googlebot", "bingbot", "yandexbot", "duckduckbot", "baiduspider",
    "applebot", "ahrefsbot", "semrushbot", "mj12bot", "petalbot", "dotbot",
    "crawler", "spider", "preview", "scraper", "archiver",
    "curl/", "wget/", "python-requests", "python-urllib", "go-http-client",
    "java/", "okhttp", "libwww-perl", "headlesschrome", "phantomjs",
    "uptime", "pingdom", "monitoring", "statuscake",
)


class ParseError(ValueError):
    """A log line that could not be understood."""


def is_bot(user_agent: str) -> bool:
    """True for link-preview fetchers, crawlers, and scripted clients."""
    ua = user_agent.strip().lower()
    if not ua or ua == "-":
        return True
    return any(token in ua for token in BOT_TOKENS)


def parse_timestamp(raw: str) -> str:
    """`10/Aug/2026:13:55:36 +0530` -> a UTC ISO string matching db.utcnow()."""
    try:
        date_part, _, offset = raw.partition(" ")
        day, month, rest = date_part.split("/", 2)
        year, hour, minute, second = rest.split(":")
        sign = -1 if offset.startswith("-") else 1
        offset_minutes = sign * (int(offset[1:3]) * 60 + int(offset[3:5]))
        stamp = datetime(
            int(year),
            MONTHS[month],
            int(day),
            int(hour),
            int(minute),
            int(second),
            tzinfo=timezone.utc,
        )
    except (KeyError, ValueError) as exc:
        raise ParseError(f"unparseable timestamp {raw!r}") from exc
    # The offset says how far ahead of UTC the wall clock was, so subtract it.
    return (stamp - timedelta(minutes=offset_minutes)).isoformat(timespec="seconds")


def channel_from_target(target: str) -> str | None:
    """Pull the `s` channel tag out of a request target, if there is one."""
    parts = urlsplit(target)
    if ASSET.search(parts.path):
        return None
    code = parse_qs(parts.query).get("s", [""])[0]
    if not code.strip():
        return None
    # Normalised with the same function that mints the link and reads the
    # ?start= payload, so one campaign cannot split across two rows.
    return attribution.normalize_source(code)


def classify(line: str) -> tuple[str, tuple[str, str] | None]:
    """Sort one log line into ("click", (channel, ts)) / ("bot", None) / ("skip", None).

    "bot" is reported separately from "skip" so the run can say how much of the
    traffic on a campaign link was a preview fetcher rather than a person —
    that ratio is the reason to trust the number at all.
    """
    match = LINE.search(line)
    if not match:
        raise ParseError("does not match the combined log format")
    if match.group("method") != "GET" or match.group("status") not in {"200", "304"}:
        return "skip", None
    channel = channel_from_target(match.group("target"))
    if channel is None:
        return "skip", None
    if is_bot(match.group("ua")):
        return "bot", None
    return "click", (channel, parse_timestamp(match.group("ts")))


def open_log(path: str):
    if path.endswith(".gz"):
        return gzip.open(path, "rt", encoding="utf-8", errors="replace")
    return open(path, "r", encoding="utf-8", errors="replace")


def collect(paths: list[str], since: str | None) -> tuple[dict[str, Counter], dict]:
    """Read every log file into {day: Counter({channel: clicks})} plus counters."""
    by_day: dict[str, Counter] = defaultdict(Counter)
    stats = {"lines": 0, "clicks": 0, "bots": 0, "unparsed": 0, "files": 0}

    for path in paths:
        stats["files"] += 1
        with open_log(path) as handle:
            for line in handle:
                if not line.strip():
                    continue
                stats["lines"] += 1
                try:
                    kind, event = classify(line)
                except ParseError:
                    stats["unparsed"] += 1
                    continue
                if kind == "bot":
                    stats["bots"] += 1
                    continue
                if event is None:
                    continue
                channel, timestamp = event
                day = timestamp[:10]
                if since and day < since:
                    continue
                by_day[day][channel] += 1
                stats["clicks"] += 1
    return by_day, stats


MAGIC = re.compile(r"[*?\[]")


def expand(patterns: list[str]) -> tuple[list[str], list[str]]:
    """Resolve globs to real files. Returns (paths, literal paths that are missing)."""
    paths: list[str] = []
    missing: list[str] = []
    for pattern in patterns:
        matched = sorted(glob.glob(pattern))
        if matched:
            paths.extend(matched)
        elif MAGIC.search(pattern):
            continue  # a glob matching nothing is not an error
        elif not Path(pattern).exists():
            missing.append(pattern)
    return paths, missing


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "logs",
        nargs="*",
        help="access log paths or globs (default: ACCESS_LOG_PATH from .env)",
    )
    parser.add_argument(
        "--write",
        action="store_true",
        help="store the tallies; without it this only reports what it found",
    )
    parser.add_argument("--since", help="ignore entries before this ISO date")
    parser.add_argument(
        "--force",
        action="store_true",
        help="write a day even when it would lower that day's stored count",
    )
    args = parser.parse_args()

    config.load_env()
    patterns = [p for p in (args.logs or [config.access_log_pattern()]) if p]
    if not patterns:
        print("no log path configured; set ACCESS_LOG_PATH or pass one", file=sys.stderr)
        return 2

    paths, missing = expand(patterns)
    for path in missing:
        print(f"no such log file: {path}", file=sys.stderr)
    if missing:
        return 1
    if not paths:
        print(f"no log files matched {patterns}", file=sys.stderr)
        return 1

    by_day, stats = collect(paths, args.since)

    print(
        f"read {stats['lines']} line(s) from {stats['files']} file(s): "
        f"{stats['clicks']} click(s), {stats['bots']} bot/preview hit(s) dropped, "
        f"{stats['unparsed']} unparsed line(s)"
    )
    if not by_day:
        print("nothing to import")
        return 0

    conn = db.connect()
    db.init_schema(conn)

    print(f"\n{'day':<12} {'found':>7} {'stored':>7}  action")
    print("-" * 46)
    written = skipped = 0
    for day in sorted(by_day):
        tallies = dict(by_day[day])
        found = sum(tallies.values())
        existing = db.clicks_on_day(conn, day)

        # A log that has rotated away mid-day would otherwise replace a full
        # day's tally with a partial re-read. Losing counts silently is worse
        # than refusing to write, so a decrease needs --force to go through.
        if found < existing and not args.force:
            print(
                f"{day:<12} {found:>7} {existing:>7}  "
                f"SKIPPED — would lose {existing - found}, pass --force"
            )
            skipped += 1
            continue

        if not args.write:
            print(f"{day:<12} {found:>7} {existing:>7}  would replace")
            continue

        db.replace_clicks_for_day(conn, day, tallies)
        print(f"{day:<12} {found:>7} {existing:>7}  replaced")
        written += 1

    print("-" * 46)
    if args.write:
        print(f"{written} day(s) written, {skipped} skipped")
    else:
        print("dry run — nothing written. Re-run with --write.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
