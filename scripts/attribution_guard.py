#!/usr/bin/env python3
"""Fail if any subscriber has no usable source. Run before a launch.

    python scripts/attribution_guard.py
    python scripts/attribution_guard.py --strict           # warnings fail too
    python scripts/attribution_guard.py --max-direct-pct 40

Exit 0 means every subscriber row carries a source code that the funnel can
count. Non-zero means at least one row leaked through a path that did not
record where the person came from — which is unrecoverable after the fact, so
it is worth catching before a campaign rather than after.

`source` is the column referred to elsewhere as `source_channel`:
`subscribers.source` in the schema, and the `source` column of
`attribution_events`.

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

REQUIRED_TABLES = {"subscribers", "attribution_events"}

# Both the subscriber's source and an event's source are checked with this.
EMPTY = "source IS NULL OR trim(source) = ''"


class Findings:
    """Tallies results as checks run.

    A class rather than module-level counters so a caller — the test suite,
    or anything that wants to audit twice — gets a fresh count each time
    instead of inheriting the previous run's.
    """

    def __init__(self, echo: bool = True) -> None:
        self.failures = 0
        self.warnings = 0
        self.echo = echo

    def add(self, level: str, message: str) -> None:
        if level == FAIL:
            self.failures += 1
        elif level == WARN:
            self.warnings += 1
        if self.echo:
            print(f"[{level}] {message}")

    def exit_code(self, strict: bool = False) -> int:
        if self.failures:
            return 1
        return 1 if (strict and self.warnings) else 0


def listing(values: list[object], limit: int) -> str:
    shown = ", ".join(str(v) for v in values[:limit])
    if len(values) > limit:
        shown += f", ... (+{len(values) - limit} more)"
    return shown


# --------------------------------------------------------------------------
# Checks
# --------------------------------------------------------------------------


def check_default_source(found: Findings) -> None:
    configured = config.get("DEFAULT_SOURCE", "direct") or "direct"
    normalised = attribution.normalize_source(configured)
    if normalised != configured:
        found.add(
            FAIL,
            f"DEFAULT_SOURCE is {configured!r} but normalises to {normalised!r}; "
            "fallback rows will not match the configured name",
        )
    else:
        found.add(OK, f"DEFAULT_SOURCE ({configured!r}) is a valid source code")


def check_empty_sources(found: Findings, conn: sqlite3.Connection, limit: int) -> None:
    """The leak this whole script exists for.

    `subscribers.source` is NOT NULL with a default, so today a NULL can only
    arrive via a hand-written migration or an import that rebuilds the table —
    but an empty or whitespace-only string inserts happily, and reads as a
    source right up until the funnel groups on it. Both are checked; neither is
    repairable after the fact, because there is nothing recorded to repair from.
    """
    rows = conn.execute(
        f"SELECT chat_id FROM subscribers WHERE {EMPTY} ORDER BY joined_at"
    ).fetchall()
    if rows:
        found.add(
            FAIL,
            f"{len(rows)} subscriber(s) with an empty source — chat_id "
            f"{listing([r['chat_id'] for r in rows], limit)}",
        )
    else:
        found.add(OK, "every subscriber has a non-empty source")

    orphaned = conn.execute(
        f"SELECT COUNT(*) AS c FROM attribution_events WHERE {EMPTY}"
    ).fetchone()["c"]
    if orphaned:
        found.add(FAIL, f"{orphaned} attribution event(s) with an empty source")
    else:
        found.add(OK, "every attribution event has a non-empty source")


def check_normalised(found: Findings, conn: sqlite3.Connection, limit: int) -> None:
    """A source that does not round-trip got written past normalize_source.

    It splits the funnel silently: 'Reddit' and 'reddit' are two rows in the
    report and neither is the real number.
    """
    bad = [
        repr(row["source"])
        for row in conn.execute(
            f"SELECT DISTINCT source FROM subscribers WHERE NOT ({EMPTY})"
        )
        if attribution.normalize_source(row["source"]) != row["source"]
    ]
    if bad:
        found.add(
            FAIL,
            f"{len(bad)} source code(s) are not in normalised form, so the funnel "
            f"will double-count them — {listing(sorted(bad), limit)}",
        )
    else:
        found.add(OK, "every source code is in normalised form")


def check_event_trail(found: Findings, conn: sqlite3.Connection, limit: int) -> None:
    """Every subscriber should have the /start event that created them."""
    rows = conn.execute(
        "SELECT s.chat_id FROM subscribers s WHERE NOT EXISTS ("
        "  SELECT 1 FROM attribution_events e"
        "  WHERE e.chat_id = s.chat_id AND e.event = 'start'"
        ") ORDER BY s.joined_at"
    ).fetchall()
    if rows:
        found.add(
            WARN,
            f"{len(rows)} subscriber(s) have no 'start' event — chat_id "
            f"{listing([r['chat_id'] for r in rows], limit)}",
        )
    else:
        found.add(OK, "every subscriber has a matching 'start' event")

    mismatched = conn.execute(
        "SELECT COUNT(*) AS c FROM subscribers s"
        " JOIN attribution_events e ON e.id = ("
        "   SELECT id FROM attribution_events"
        "   WHERE chat_id = s.chat_id AND event = 'start'"
        "   ORDER BY created_at, id LIMIT 1"
        " ) WHERE e.source <> s.source"
    ).fetchone()["c"]
    if mismatched:
        found.add(
            FAIL,
            f"{mismatched} subscriber(s) have a source that disagrees with their "
            "first 'start' event — the recorded origin was overwritten",
        )
    else:
        found.add(OK, "each subscriber's source matches their first 'start' event")


def check_direct_share(
    found: Findings, conn: sqlite3.Connection, max_pct: float | None
) -> None:
    default = attribution.default_source()
    total = conn.execute("SELECT COUNT(*) AS c FROM subscribers").fetchone()["c"]
    if not total:
        found.add(WARN, "no subscribers yet — nothing to attribute")
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
        found.add(FAIL, f"{message}; ceiling is {max_pct:.0f}%")
    elif untracked:
        found.add(WARN, message)
    else:
        found.add(OK, f"no subscriber fell back to {default!r}")


# --------------------------------------------------------------------------


def audit(
    path: Path,
    limit: int = 20,
    max_direct_pct: float | None = None,
    echo: bool = True,
) -> Findings:
    """Run every check against `path`. Returns the tally; never raises."""
    found = Findings(echo=echo)

    if not path.exists():
        found.add(FAIL, f"{path} does not exist — nothing to check")
        return found

    try:
        conn = db.connect(path)
        tables = {
            row["name"]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
    except sqlite3.DatabaseError as exc:
        found.add(FAIL, f"cannot read {path}: {exc}")
        return found

    absent = REQUIRED_TABLES - tables
    if absent:
        found.add(
            FAIL,
            f"{path} is missing table(s) {', '.join(sorted(absent))}"
            " — the bot has never written to this file",
        )
        return found

    try:
        check_default_source(found)
        check_empty_sources(found, conn, limit)
        check_normalised(found, conn, limit)
        check_event_trail(found, conn, limit)
        check_direct_share(found, conn, max_direct_pct)
    finally:
        conn.close()
    return found


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

    print(f"== attribution guard: {path} ==\n")
    found = audit(path, limit=args.limit, max_direct_pct=args.max_direct_pct)
    print(f"\n{found.failures} failure(s), {found.warnings} warning(s)")
    return found.exit_code(strict=args.strict)


if __name__ == "__main__":
    sys.exit(main())
