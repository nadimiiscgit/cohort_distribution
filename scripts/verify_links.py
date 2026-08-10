#!/usr/bin/env python3
"""Prove a tracked link actually lands its source code, end to end.

Two phases, one script. Run it once to get the links, click them by hand, run
it again to see what the database recorded.

    python scripts/verify_links.py tg_group1 insta_bio
    python scripts/verify_links.py tg_group1 --landing
    python scripts/verify_links.py tg_group1 --report-only --since 2026-08-10
    python scripts/verify_links.py tg_group1 --report-only --require-all

The codes are normalised through the same function the bot uses on the way in
(`bot.attribution.normalize_source`), so the URL printed here is character-for-
character the one that has to show up in the table below it.

`starts` counts `start` events, not every event: a user who answers five
questions generates five more rows in `events`, and none of them mean the link
was clicked again. Events reach a source only through `users.source_channel` —
the events table carries no source of its own — so an event whose user row is
missing has no attribution at all and is bucketed separately rather than
dropped from the join.

Sources found in the database that were not asked about are listed too, marked
with `*`. That is the part that catches mistakes — a typo'd tag, a link that
lost its payload, or traffic quietly piling up under `direct`.

Output aggregates rather than naming users, but source codes are campaign
names; treat a redirected log the way you'd treat the report.
"""

from __future__ import annotations

import _bootstrap  # noqa: F401  (sys.path side effect)

import argparse
import sqlite3
import sys
from pathlib import Path

from bot import attribution, config, db

NO_DATA = "-"

# Events whose user row is gone. No source code can normalise to this — the
# alphabet has no spaces or parentheses — so it cannot collide with a real one.
ORPHAN = "(no user row)"


def resolve_db(explicit: str | None) -> Path:
    return Path(explicit).expanduser() if explicit else config.database_path()


def print_links(codes: list[str], bot_username: str | None, landing: bool) -> None:
    print("== links to test ==\n")
    width = max(len(c) for c in codes)
    for code in codes:
        print(f"{code:<{width}}  {attribution.deep_link(code, bot_username)}")
        if landing:
            print(f"{'':<{width}}  {attribution.landing_link(code)}")
    print(
        "\nOpen each link from a Telegram account that has never messaged this"
        "\nbot, press START, then re-run with --report-only. Pressing /start"
        "\nagain from an account that already exists records an event but will"
        "\nNOT change that user's source_channel — first touch wins."
    )


def collect(conn: sqlite3.Connection, since: str | None) -> dict[str, dict[str, object]]:
    """Per-source user counts, `start` counts, and the newest `start`."""
    stats: dict[str, dict[str, object]] = {}

    def slot(source: str | None) -> dict[str, object]:
        key = source if source is not None else ""
        return stats.setdefault(key, {"users": 0, "starts": 0, "last_start": None})

    where, params = ("WHERE first_seen >= ?", (since,)) if since else ("", ())
    for row in conn.execute(
        f"SELECT source_channel, COUNT(*) AS n FROM users {where}"
        " GROUP BY source_channel",
        params,
    ):
        slot(row["source_channel"])["users"] = row["n"]

    # LEFT JOIN, not JOIN: an event whose user row was deleted would otherwise
    # vanish silently, and silently losing rows is the exact failure this
    # script exists to make visible.
    clause, params = ("AND e.created_at >= ?", (since,)) if since else ("", ())
    for row in conn.execute(
        "SELECT COALESCE(u.source_channel, ?) AS source_channel,"
        "       COUNT(*) AS n, MAX(e.created_at) AS last_start"
        " FROM events e LEFT JOIN users u ON u.user_id = e.user_id"
        f" WHERE e.event_type = 'start' {clause}"
        " GROUP BY COALESCE(u.source_channel, ?)",
        (ORPHAN, *params, ORPHAN),
    ):
        entry = slot(row["source_channel"])
        entry["starts"] = row["n"]
        entry["last_start"] = row["last_start"]

    return stats


def _display(source: str) -> str:
    """Make an unusable source visible rather than printing blank space."""
    if not source:
        return "(empty)"
    if not source.strip():
        return "(whitespace)"
    return source


def print_table(codes: list[str], stats: dict[str, dict[str, object]]) -> None:
    empty = {"users": 0, "starts": 0, "last_start": None}
    rows = [(code, stats.get(code, empty), False) for code in codes]

    extras = sorted(
        (s for s in stats if s not in codes),
        key=lambda s: (-int(stats[s]["users"] or 0), s),
    )
    rows += [(_display(source), stats[source], True) for source in extras]

    label = lambda name, extra: f"{name} *" if extra else name  # noqa: E731
    width = max([len(label(n, e)) for n, _, e in rows] + [len("source_channel")])

    print("== recorded ==\n")
    print(f"{'source_channel':<{width}}  {'users':>5} {'starts':>6}  last_start")
    print("-" * (width + 32))
    for name, entry, extra in rows:
        print(
            f"{label(name, extra):<{width}}  {entry['users']:>5} {entry['starts']:>6}  "
            f"{entry['last_start'] or NO_DATA}"
        )
    if extras:
        print("\n* present in the database but not on the command line")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("tags", nargs="+", help="channel tags, e.g. tg_group1")
    parser.add_argument("--landing", action="store_true", help="also print landing URLs")
    phase = parser.add_mutually_exclusive_group()
    phase.add_argument("--links-only", action="store_true", help="skip the database")
    phase.add_argument("--report-only", action="store_true", help="skip the links")
    parser.add_argument("--since", help="only count rows at or after this ISO timestamp")
    parser.add_argument(
        "--require-all",
        action="store_true",
        help="exit non-zero unless every listed tag has at least one user",
    )
    parser.add_argument("--db", help="database path, overriding DB_PATH")
    parser.add_argument(
        "--bot-username", help="bot username, overriding TELEGRAM_BOT_USERNAME"
    )
    args = parser.parse_args()

    config.load_env()

    codes = []
    for tag in args.tags:
        code = attribution.normalize_source(tag)
        if code != tag:
            print(f"# normalised {tag!r} -> {code!r}", file=sys.stderr)
        if code not in codes:
            codes.append(code)

    if not args.report_only:
        print_links(codes, args.bot_username, args.landing)

    if args.links_only:
        return 0

    path = resolve_db(args.db)
    if not path.exists():
        print(f"\n{path} does not exist yet — nothing has been recorded.")
        return 1 if args.require_all else 0

    if not args.report_only:
        print()

    conn = db.connect(path)
    db.init_schema(conn)
    stats = collect(conn, args.since)
    print_table(codes, stats)

    missing = [c for c in codes if not stats.get(c, {}).get("users")]
    if missing:
        print(f"\nno user recorded yet for: {', '.join(missing)}")
        if args.require_all:
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
