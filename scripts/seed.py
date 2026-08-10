#!/usr/bin/env python3
"""Load a question CSV into the SQLite database.

    python scripts/seed_questions.py                  # uses QUESTIONS_CSV
    python scripts/seed_questions.py data/sample.csv
    python scripts/seed_questions.py --check          # validate only, no writes

Idempotent: rows are keyed on `id`, so re-running an edited CSV updates in
place rather than duplicating. Validation runs over the whole file before
anything is written — a bad row fails the batch instead of half-loading it.
"""

from __future__ import annotations

import _bootstrap  # noqa: F401  (sys.path side effect)

import argparse
import csv
import sys
from pathlib import Path

from bot import config, db

REQUIRED = (
    "id",
    "subject",
    "stem",
    "option_a",
    "option_b",
    "option_c",
    "option_d",
    "correct_option",
)
OPTIONAL = ("year", "explanation", "source_tag")
VALID_OPTIONS = {"A", "B", "C", "D"}


def parse_rows(path: Path) -> tuple[list[dict], list[str]]:
    rows: list[dict] = []
    errors: list[str] = []
    seen: set[str] = set()

    with path.open(newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        missing = [c for c in REQUIRED if c not in (reader.fieldnames or [])]
        if missing:
            return [], [f"missing required column(s): {', '.join(missing)}"]

        for lineno, raw in enumerate(reader, start=2):
            row = {k: (v or "").strip() for k, v in raw.items() if k}
            qid = row.get("id", "")
            if not qid:
                errors.append(f"line {lineno}: empty id")
                continue
            if qid in seen:
                errors.append(f"line {lineno}: duplicate id {qid!r}")
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

            year = row.get("year") or None
            if year is not None:
                try:
                    year = int(year)
                except ValueError:
                    errors.append(f"line {lineno} ({qid}): year must be a number")
                    year = None

            rows.append(
                {
                    "id": qid,
                    "subject": row.get("subject", ""),
                    "year": year,
                    "stem": row.get("stem", ""),
                    "option_a": row.get("option_a", ""),
                    "option_b": row.get("option_b", ""),
                    "option_c": row.get("option_c", ""),
                    "option_d": row.get("option_d", ""),
                    "correct_option": answer,
                    "explanation": row.get("explanation") or None,
                    "source_tag": row.get("source_tag") or None,
                }
            )
    return rows, errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "csv_path", nargs="?", type=Path, help="CSV to load (default: $QUESTIONS_CSV)"
    )
    parser.add_argument(
        "--check", action="store_true", help="validate the file without writing"
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

    print(f"{len(rows)} valid row(s) in {path}")
    if args.check:
        return 0

    conn = db.connect()
    db.init_schema(conn)
    inserted, updated = db.upsert_questions(conn, rows)
    print(f"inserted {inserted}, updated {updated}; {db.question_count(conn)} total")
    return 0


if __name__ == "__main__":
    sys.exit(main())
