#!/usr/bin/env python3
"""Prove a tracked link actually lands its source code, end to end.

Two phases, one script. Run it once to get the links, click them by hand, run
it again to see what the database recorded.

    python scripts/verify_links.py reddit-r-medicine whatsapp-batch-2024
    python scripts/verify_links.py reddit-r-medicine --landing
    python scripts/verify_links.py reddit-r-medicine --report-only --since 2026-08-10
    python scripts/verify_links.py reddit-r-medicine --report-only --require-all

The codes are normalised through the same function the bot uses on the way in
(`bot.attribution.normalize_source`), so the URL printed here is character-for-
character the one that has to show up in the table below it.

`source` is the column the request calls `source_channel`: `subscribers.source`
in the schema, and the `source` column of `attribution_events`.

Sources found in the database that were not asked about are listed too, marked
with `*`. That is the part that catches mistakes — a typo'd tag, a link that
lost its payload, or traffic quietly piling up under the default source.

Output includes subscriber chat IDs only in aggregate, but the source codes are
campaign names; treat a redirected log the same way you'd treat the report.
"""

from __future__ import annotations

import _bootstrap  # noqa: F401  (sys.path side effect)

import argparse
import sqlite3
import sys
from pathlib import Path

from bot import attribution, config, db

NO_DATA = "-"


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
        "\nagain from an account that already subscribed records an event but"
        "\nwill NOT change that subscriber's source — first touch wins."
    )


def collect(conn: sqlite3.Connection, since: str | None) -> dict[str, dict[str, object]]:
    """Per-source subscriber counts, event counts, and the newest event."""
    stats: dict[str, dict[str, object]] = {}

    def slot(source: str | None) -> dict[str, object]:
        key = source if source is not None else ""
        return stats.setdefault(key, {"users": 0, "events": 0, "last_event": None})

    where, params = ("WHERE joined_at >= ?", (since,)) if since else ("", ())
    for row in conn.execute(
        f"SELECT source, COUNT(*) AS n FROM subscribers {where} GROUP BY source", params
    ):
        slot(row["source"])["users"] = row["n"]

    where, params = ("WHERE created_at >= ?", (since,)) if since else ("", ())
    for row in conn.execute(
        "SELECT source, COUNT(*) AS n, MAX(created_at) AS last_event"
        f" FROM attribution_events {where} GROUP BY source",
        params,
    ):
        entry = slot(row["source"])
        entry["events"] = row["n"]
        entry["last_event"] = row["last_event"]

    return stats


def _display(source: str) -> str:
    """Make an unusable source visible rather than printing blank space."""
    if not source:
        return "(empty)"
    if not source.strip():
        return "(whitespace)"
    return source


def print_table(codes: list[str], stats: dict[str, dict[str, object]]) -> None:
    empty = {"users": 0, "events": 0, "last_event": None}
    rows = [(code, stats.get(code, empty), False) for code in codes]

    extras = sorted(
        (s for s in stats if s not in codes),
        key=lambda s: (-int(stats[s]["users"] or 0), s),
    )
    rows += [(_display(source), stats[source], True) for source in extras]

    label = lambda name, extra: f"{name} *" if extra else name  # noqa: E731
    width = max([len(label(n, e)) for n, _, e in rows] + [len("source")])

    print("== recorded ==\n")
    print(f"{'source':<{width}}  {'users':>5} {'events':>6}  last_event")
    print("-" * (width + 32))
    for name, entry, extra in rows:
        print(
            f"{label(name, extra):<{width}}  {entry['users']:>5} {entry['events']:>6}  "
            f"{entry['last_event'] or NO_DATA}"
        )
    if extras:
        print("\n* present in the database but not on the command line")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("tags", nargs="+", help="channel tags, e.g. reddit-r-medicine")
    parser.add_argument("--landing", action="store_true", help="also print landing URLs")
    phase = parser.add_mutually_exclusive_group()
    phase.add_argument("--links-only", action="store_true", help="skip the database")
    phase.add_argument("--report-only", action="store_true", help="skip the links")
    parser.add_argument("--since", help="only count rows at or after this ISO timestamp")
    parser.add_argument(
        "--require-all",
        action="store_true",
        help="exit non-zero unless every listed tag has at least one subscriber",
    )
    parser.add_argument("--db", help="database path, overriding DATABASE_PATH")
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
        print(f"\nno subscriber recorded yet for: {', '.join(missing)}")
        if args.require_all:
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
