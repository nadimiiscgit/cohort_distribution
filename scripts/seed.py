#!/usr/bin/env python3
"""Load a question CSV into the SQLite database.

    python scripts/seed.py                    # uses QUESTIONS_CSV
    python scripts/seed.py data/sample.csv
    python scripts/seed.py --check            # validate only, no writes
    python scripts/seed.py --schedule-from 2026-08-11   # fill blank dates

Idempotent: rows are keyed on `question_id`, so re-running an edited CSV
updates in place rather than duplicating. Validation runs over the whole file
before anything is written — a bad row fails the batch instead of half-loading
it, because half a question bank is worse than none.

`scheduled_date` (YYYY-MM-DD) is what makes a question the question of the day.
Leave it blank in the CSV and pass --schedule-from to lay the rows out on
consecutive days in file order.
"""

from __future__ import annotations

import _bootstrap  # noqa: F401  (sys.path side effect)

import argparse
import csv
import sys
from datetime import date, timedelta
from pathlib import Path

from bot import config, db

REQUIRED = (
    "question_id",
    "subject",
    "stem",
    "option_a",
    "option_b",
    "option_c",
    "option_d",
    "correct_option",
)
OPTIONAL = ("explanation", "scheduled_date")
VALID_OPTIONS = {"A", "B", "C", "D"}


def parse_date(raw: str) -> date:
    return date.fromisoformat(raw)


def parse_rows(path: Path) -> tuple[list[dict], list[str]]:
    """Read and validate the whole file. Returns (rows, errors)."""
    rows: list[dict] = []
    errors: list[str] = []
    seen: set[str] = set()
    scheduled: dict[str, str] = {}

    with path.open(newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        missing = [c for c in REQUIRED if c not in (reader.fieldnames or [])]
        if missing:
            return [], [f"missing required column(s): {', '.join(missing)}"]

        for lineno, raw in enumerate(reader, start=2):
            row = {k: (v or "").strip() for k, v in raw.items() if k}
            qid = row.get("question_id", "")
            if not qid:
                errors.append(f"line {lineno}: empty question_id")
                continue
            if qid in seen:
                errors.append(f"line {lineno}: duplicate question_id {qid!r}")
                continue
            seen.add(qid)

            for col in REQUIRED:
                if not row.get(col):
                    errors.append(f"line {lineno} ({qid}): empty {col}")

            answer = row.get("correct_option", "").upper()
            if answer not in VALID_OPTIONS:
                errors.append(
                    f"line {lineno} ({qid}): correct_option must be A-D, got {answer!r}"
                )

            when = row.get("scheduled_date") or None
            if when is not None:
                try:
                    when = parse_date(when).isoformat()
                except ValueError:
                    errors.append(
                        f"line {lineno} ({qid}): scheduled_date must be YYYY-MM-DD,"
                        f" got {row['scheduled_date']!r}"
                    )
                    when = None
                else:
                    # Two questions on one date means one of them silently never
                    # ships: question_for_date takes a single row.
                    if when in scheduled:
                        errors.append(
                            f"line {lineno} ({qid}): {when} is already taken by"
                            f" {scheduled[when]!r}"
                        )
                    else:
                        scheduled[when] = qid

            rows.append(
                {
                    "question_id": qid,
                    "subject": row.get("subject", ""),
                    "stem": row.get("stem", ""),
                    "option_a": row.get("option_a", ""),
                    "option_b": row.get("option_b", ""),
                    "option_c": row.get("option_c", ""),
                    "option_d": row.get("option_d", ""),
                    "correct_option": answer,
                    "explanation": row.get("explanation") or None,
                    "scheduled_date": when,
                }
            )
    return rows, errors


def apply_schedule(rows: list[dict], start: date) -> int:
    """Give every undated row the next free consecutive day. Returns how many."""
    taken = {r["scheduled_date"] for r in rows if r["scheduled_date"]}
    cursor = start
    filled = 0
    for row in rows:
        if row["scheduled_date"]:
            continue
        while cursor.isoformat() in taken:
            cursor += timedelta(days=1)
        row["scheduled_date"] = cursor.isoformat()
        taken.add(row["scheduled_date"])
        filled += 1
    return filled


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "csv_path", nargs="?", type=Path, help="CSV to load (default: $QUESTIONS_CSV)"
    )
    parser.add_argument(
        "--check", action="store_true", help="validate the file without writing"
    )
    parser.add_argument(
        "--schedule-from",
        metavar="YYYY-MM-DD",
        help="assign consecutive dates to rows with no scheduled_date",
    )
    args = parser.parse_args()

    config.load_env()
    path = args.csv_path or config.questions_csv()
    if not path.exists():
        print(f"error: {path} does not exist", file=sys.stderr)
        return 1

    rows, errors = parse_rows(path)
    for err in errors:
        print(f"error: {err}", file=sys.stderr)
    if errors:
        print(f"\n{len(errors)} problem(s) found — nothing written.", file=sys.stderr)
        return 1

    if args.schedule_from:
        try:
            start = parse_date(args.schedule_from)
        except ValueError:
            print(
                f"error: --schedule-from must be YYYY-MM-DD, got"
                f" {args.schedule_from!r}",
                file=sys.stderr,
            )
            return 1
        filled = apply_schedule(rows, start)
        print(f"scheduled {filled} undated row(s) from {start.isoformat()}")

    undated = sum(1 for r in rows if not r["scheduled_date"])
    suffix = f" ({undated} with no scheduled_date)" if undated else ""
    print(f"{len(rows)} valid row(s) in {path}{suffix}")
    if args.check:
        return 0

    conn = db.connect()
    db.init_schema(conn)
    inserted, updated = db.upsert_questions(conn, rows)
    print(f"inserted {inserted}, updated {updated}; {db.question_count(conn)} total")

    upcoming = conn.execute(
        "SELECT scheduled_date, question_id FROM questions"
        " WHERE scheduled_date >= ? ORDER BY scheduled_date LIMIT 5",
        (db.today(),),
    ).fetchall()
    if upcoming:
        print("\nnext up:")
        for row in upcoming:
            print(f"  {row['scheduled_date']}  {row['question_id']}")
    else:
        print("\nnothing scheduled from today onward — the daily send will do nothing")
    return 0


if __name__ == "__main__":
    sys.exit(main())
