#!/usr/bin/env python3
"""Fail if any user has no usable source_channel. Run before a launch.

    python scripts/attribution_guard.py
    python scripts/attribution_guard.py --strict           # warnings fail too
    python scripts/attribution_guard.py --max-direct-pct 40

Exit 0 means every user row carries a source code the funnel can count.
Non-zero means at least one row leaked through a path that did not record
where the person came from — which is unrecoverable after the fact, so it is
worth catching before a campaign rather than after.

`users.source_channel` is the only place a source is stored. `events` carries
no source of its own, so an event whose user row is missing is unattributable
outright: it cannot be joined to a channel, and nothing else remembers.

A missing database is a failure, not a pass. A pre-launch check that reports
"all clear" against a file that does not exist is worse than no check.

Offending rows are printed by user ID, which is personal data — the same care
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

REQUIRED_TABLES = {"users", "events"}

EMPTY = "source_channel IS NULL OR trim(source_channel) = ''"


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
    """The fallback has to be a valid code, or every untracked row is a leak."""
    default = attribution.DEFAULT_SOURCE
    normalised = attribution.normalize_source(default)
    if normalised != default:
        found.add(
            FAIL,
            f"attribution.DEFAULT_SOURCE is {default!r} but normalises to "
            f"{normalised!r}; fallback rows will not match the configured name",
        )
    else:
        found.add(OK, f"DEFAULT_SOURCE ({default!r}) is a valid source code")


def check_empty_sources(found: Findings, conn: sqlite3.Connection, limit: int) -> None:
    """The leak this whole script exists for.

    `users.source_channel` is NOT NULL with a default and `normalize_source`
    never returns an empty string, so today a blank can only arrive via a
    hand-written migration or an import that rebuilds the table. It still
    inserts happily, and reads as a source right up until the funnel groups
    on it — at which point there is nothing recorded to repair from.
    """
    rows = conn.execute(
        f"SELECT user_id FROM users WHERE {EMPTY} ORDER BY first_seen"
    ).fetchall()
    if rows:
        found.add(
            FAIL,
            f"{len(rows)} user(s) with an empty source_channel — user_id "
            f"{listing([r['user_id'] for r in rows], limit)}",
        )
    else:
        found.add(OK, "every user has a non-empty source_channel")


def check_normalised(found: Findings, conn: sqlite3.Connection, limit: int) -> None:
    """A source that does not round-trip got written past normalize_source.

    It splits the funnel silently: 'Reddit' and 'reddit' are two rows in the
    report and neither is the real number.
    """
    bad = [
        repr(row["source_channel"])
        for row in conn.execute(
            f"SELECT DISTINCT source_channel FROM users WHERE NOT ({EMPTY})"
        )
        if attribution.normalize_source(row["source_channel"]) != row["source_channel"]
    ]
    if bad:
        found.add(
            FAIL,
            f"{len(bad)} source code(s) are not in normalised form, so the funnel "
            f"will double-count them — {listing(sorted(bad), limit)}",
        )
    else:
        found.add(OK, "every source_channel is in normalised form")


def check_orphan_events(found: Findings, conn: sqlite3.Connection, limit: int) -> None:
    """Events pointing at a user row that isn't there.

    `events` has no foreign key and no source column of its own, so an event
    whose user is gone can never be attributed to a channel again. It also
    drops out of any inner join, which is how this stays invisible until a
    report quietly under-counts.
    """
    rows = conn.execute(
        "SELECT DISTINCT e.user_id FROM events e"
        " WHERE NOT EXISTS (SELECT 1 FROM users u WHERE u.user_id = e.user_id)"
        " ORDER BY e.user_id"
    ).fetchall()
    if rows:
        found.add(
            FAIL,
            f"{len(rows)} user_id(s) appear in events with no users row, so their "
            f"activity cannot be attributed — {listing([r['user_id'] for r in rows], limit)}",
        )
    else:
        found.add(OK, "every event belongs to a user that exists")


def check_start_events(found: Findings, conn: sqlite3.Connection, limit: int) -> None:
    """Every user should carry the `start` event that created them.

    Only a warning: `ensure_user` creates a row on any interaction, so a user
    with no start is odd rather than broken. Their source_channel is still
    recorded, which is what the funnel actually reads.
    """
    rows = conn.execute(
        "SELECT u.user_id FROM users u WHERE NOT EXISTS ("
        "  SELECT 1 FROM events e"
        "  WHERE e.user_id = u.user_id AND e.event_type = 'start'"
        ") ORDER BY u.first_seen"
    ).fetchall()
    if rows:
        found.add(
            WARN,
            f"{len(rows)} user(s) have no 'start' event — user_id "
            f"{listing([r['user_id'] for r in rows], limit)}",
        )
    else:
        found.add(OK, "every user has a matching 'start' event")


def check_direct_share(
    found: Findings, conn: sqlite3.Connection, max_pct: float | None
) -> None:
    default = db.DEFAULT_SOURCE_CHANNEL
    total = conn.execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"]
    if not total:
        found.add(WARN, "no users yet — nothing to attribute")
        return

    untracked = conn.execute(
        "SELECT COUNT(*) AS c FROM users WHERE source_channel = ?", (default,)
    ).fetchone()["c"]
    pct = 100 * untracked / total
    message = (
        f"{untracked}/{total} user(s) ({pct:.0f}%) are on the default source "
        f"{default!r} — they arrived without a tracked link"
    )
    if max_pct is not None and pct > max_pct:
        found.add(FAIL, f"{message}; ceiling is {max_pct:.0f}%")
    elif untracked:
        found.add(WARN, message)
    else:
        found.add(OK, f"no user fell back to {default!r}")


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
        check_orphan_events(found, conn, limit)
        check_start_events(found, conn, limit)
        check_direct_share(found, conn, max_direct_pct)
    finally:
        conn.close()
    return found


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", help="database path, overriding DB_PATH")
    parser.add_argument(
        "--strict", action="store_true", help="treat warnings as failures too"
    )
    parser.add_argument(
        "--max-direct-pct",
        type=float,
        help="fail if more than this share of users is on the default source",
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
