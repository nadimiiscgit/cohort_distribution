#!/usr/bin/env python3
"""Fail if any subscriber has no usable source. Run before a launch.

    python scripts/attribution_guard.py
    python scripts/attribution_guard.py --strict           # warnings fail too
    python scripts/attribution_guard.py --max-direct-pct 40

Exit 0 means every subscriber row carries a source code that the funnel can
count. Non-zero means at least one row leaked through a path that did not
record where the person came from — which is unrecoverable after the fact, so
it is worth catching before a campaign rather than after.

`source` is the column the request calls `source_channel`: `subscribers.source`
in the schema, and the `source` column of `attribution_events`.

A missing database is a failure, not a pass. A pre-launch check that reports
"all clear" against a file that does not exist is worse than no check.

Offending rows are printed by chat ID, which is personal data — the same care
that applies to a backup snapshot applies to this output.
"""

from __future__ import annotations

import _bootstrap  # noqa: F401  (sys.path side effect)

import argparse
import sqlite3
import sys
from pathlib import Path

from bot import attribution, config, db

OK = "  ok   "
WARN = " warn  "
FAIL = " FAIL  "

_failures = 0
_warnings = 0


def report(level: str, message: str) -> None:
    global _failures, _warnings
    if level == FAIL:
        _failures += 1
    elif level == WARN:
        _warnings += 1
    print(f"[{level}] {message}")


def listing(values: list[object], limit: int) -> str:
    shown = ", ".join(str(v) for v in values[:limit])
    if len(values) > limit:
        shown += f", ... (+{len(values) - limit} more)"
    return shown


# --------------------------------------------------------------------------
# Checks
# --------------------------------------------------------------------------

EMPTY = "source IS NULL OR trim(source) = ''"


def check_empty_sources(conn: sqlite3.Connection, limit: int) -> None:
    """The leak this whole script exists for.

    `subscribers.source` is NOT NULL with a default, so today a NULL can only
    arrive via a hand-written migration or an import that rebuilds the table —
    but an empty or whitespace-only string inserts happily, and reads as a
    source right up until the funnel groups on it. Both are checked; neither is
    repairable after the fact, because there is nothing recorded to repair from.
    """
    rows = conn.execute(
        f"SELECT chat_id, source FROM subscribers WHERE {EMPTY} ORDER BY joined_at"
    ).fetchall()
    if rows:
        report(
            FAIL,
            f"{len(rows)} subscriber(s) with an empty source — chat_id "
            f"{listing([r['chat_id'] for r in rows], limit)}",
        )
    else:
        report(OK, "every subscriber has a non-empty source")

    orphaned = conn.execute(
        f"SELECT COUNT(*) AS c FROM attribution_events WHERE {EMPTY}"
    ).fetchone()["c"]
    if orphaned:
        report(FAIL, f"{orphaned} attribution event(s) with an empty source")
    else:
        report(OK, "every attribution event has a non-empty source")


def check_normalised(conn: sqlite3.Connection, limit: int) -> None:
    """A source that does not round-trip got written past normalize_source.

    It splits the funnel silently: 'Reddit' and 'reddit' are two rows in the
    report and neither is the real number.
    """
    bad = []
    for row in conn.execute(
        f"SELECT DISTINCT source FROM subscribers WHERE NOT ({EMPTY})"
    ):
        source = row["source"]
        if attribution.normalize_source(source) != source:
            bad.append(f"{source!r}")
    if bad:
        report(
            FAIL,
            f"{len(bad)} source code(s) are not in normalised form, so the funnel "
            f"will double-count them — {listing(bad, limit)}",
        )
    else:
        report(OK, "every source code is in normalised form")


def check_event_trail(conn: sqlite3.Connection, limit: int) -> None:
    """Every subscriber should have the /start event that created them."""
    rows = conn.execute(
        "SELECT s.chat_id FROM subscribers s WHERE NOT EXISTS ("
        "  SELECT 1 FROM attribution_events e"
        "  WHERE e.chat_id = s.chat_id AND e.event = 'start'"
        ") ORDER BY s.joined_at"
    ).fetchall()
    if rows:
        report(
            WARN,
            f"{len(rows)} subscriber(s) have no 'start' event — chat_id "
            f"{listing([r['chat_id'] for r in rows], limit)}",
        )
    else:
        report(OK, "every subscriber has a matching 'start' event")

    mismatched = conn.execute(
        "SELECT COUNT(*) AS c FROM subscribers s"
        " JOIN attribution_events e ON e.id = ("
        "   SELECT id FROM attribution_events"
        "   WHERE chat_id = s.chat_id AND event = 'start'"
        "   ORDER BY created_at, id LIMIT 1"
        " ) WHERE e.source <> s.source"
    ).fetchone()["c"]
    if mismatched:
        report(
            FAIL,
            f"{mismatched} subscriber(s) have a source that disagrees with their "
            "first 'start' event — the recorded origin was overwritten",
        )
    else:
        report(OK, "each subscriber's source matches their first 'start' event")


def check_direct_share(conn: sqlite3.Connection, max_pct: float | None) -> None:
    default = attribution.default_source()
    total = conn.execute("SELECT COUNT(*) AS c FROM subscribers").fetchone()["c"]
    if not total:
        report(WARN, "no subscribers yet — nothing to attribute")
        return

    untracked = conn.execute(
        "SELECT COUNT(*) AS c FROM subscribers WHERE source = ?", (default,)
    ).fetchone()["c"]
    pct = 100 * untracked / total
    message = (
        f"{untracked}/{total} subscriber(s) ({pct:.0f}%) are on the default "
        f"source {default!r} — they arrived without a tracked link"
    )
    if max_pct is not None and pct > max_pct:
        report(FAIL, f"{message}; ceiling is {max_pct:.0f}%")
    elif untracked:
        report(WARN, message)
    else:
        report(OK, f"no subscriber fell back to {default!r}")


def check_default_source() -> None:
    configured = config.get("DEFAULT_SOURCE", "direct") or "direct"
    normalised = attribution.normalize_source(configured)
    if normalised != configured:
        report(
            FAIL,
            f"DEFAULT_SOURCE is {configured!r} but normalises to {normalised!r}; "
            "fallback rows will not match the configured name",
        )
    else:
        report(OK, f"DEFAULT_SOURCE ({configured!r}) is a valid source code")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", help="database path, overriding DATABASE_PATH")
    parser.add_argument(
        "--strict", action="store_true", help="treat warnings as failures too"
    )
    parser.add_argument(
        "--max-direct-pct",
        type=float,
        help="fail if more than this share of subscribers is on DEFAULT_SOURCE",
    )
    parser.add_argument(
        "--limit", type=int, default=20, help="offending rows to name per check"
    )
    args = parser.parse_args()

    config.load_env()

    path = Path(args.db).expanduser() if args.db else config.database_path()
    if not path.exists():
        print(f"[{FAIL}] {path} does not exist — nothing to check")
        print("\n1 failure(s), 0 warning(s)")
        return 1

    try:
        conn = db.connect(path)
        tables = {
            row["name"]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
    except sqlite3.DatabaseError as exc:
        print(f"[{FAIL}] cannot read {path}: {exc}")
        print("\n1 failure(s), 0 warning(s)")
        return 1

    absent = {"subscribers", "attribution_events"} - tables
    if absent:
        print(
            f"[{FAIL}] {path} is missing table(s) {', '.join(sorted(absent))}"
            " — the bot has never written to this file"
        )
        print("\n1 failure(s), 0 warning(s)")
        return 1

    print(f"== attribution guard: {path} ==\n")
    check_default_source()
    check_empty_sources(conn, args.limit)
    check_normalised(conn, args.limit)
    check_event_trail(conn, args.limit)
    check_direct_share(conn, args.max_direct_pct)

    print(f"\n{_failures} failure(s), {_warnings} warning(s)")
    if _failures:
        return 1
    return 1 if (args.strict and _warnings) else 0


if __name__ == "__main__":
    sys.exit(main())
