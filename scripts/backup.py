#!/usr/bin/env python3
"""Snapshot the SQLite database and prune old snapshots.

    python scripts/backup.py
    python scripts/backup.py --retention-days 30
    python scripts/backup.py --list

Uses sqlite3's online backup API rather than copying the file, so a snapshot
taken while the bot is mid-write is still consistent — no need to stop the
service. Snapshots land in BACKUP_DIR (gitignored) as cohort-<UTC stamp>.db.
"""

from __future__ import annotations

import _bootstrap  # noqa: F401  (sys.path side effect)

import argparse
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from bot import config

STAMP_FORMAT = "%Y%m%dT%H%M%SZ"
PREFIX = "cohort-"


def snapshots(backup_dir: Path) -> list[Path]:
    return sorted(backup_dir.glob(f"{PREFIX}*.db"))


def take_snapshot(source: Path, backup_dir: Path) -> Path:
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime(STAMP_FORMAT)
    target = backup_dir / f"{PREFIX}{stamp}.db"

    src = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    dst = sqlite3.connect(target)
    try:
        with dst:
            src.backup(dst)
    finally:
        dst.close()
        src.close()
    return target


def prune(backup_dir: Path, retention_days: int) -> list[Path]:
    cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
    removed: list[Path] = []
    for path in snapshots(backup_dir):
        stamp = path.stem[len(PREFIX) :]
        try:
            taken = datetime.strptime(stamp, STAMP_FORMAT).replace(tzinfo=timezone.utc)
        except ValueError:
            continue  # not one of ours; leave it alone
        if taken < cutoff:
            path.unlink()
            removed.append(path)
    return removed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--retention-days", type=int, help="override BACKUP_RETENTION_DAYS")
    parser.add_argument("--list", action="store_true", help="list snapshots and exit")
    args = parser.parse_args()

    config.load_env()
    source = config.database_path()
    backup_dir = config.backup_dir()

    if args.list:
        found = snapshots(backup_dir)
        if not found:
            print(f"no snapshots in {backup_dir}")
        for path in found:
            print(f"{path.name}\t{path.stat().st_size:>12,} bytes")
        return 0

    if not source.exists():
        print(f"error: {source} does not exist — nothing to back up", file=sys.stderr)
        return 1

    target = take_snapshot(source, backup_dir)
    print(f"wrote {target} ({target.stat().st_size:,} bytes)")

    retention = args.retention_days or config.get_int("BACKUP_RETENTION_DAYS", 14)
    removed = prune(backup_dir, retention)
    for path in removed:
        print(f"pruned {path.name}")
    print(f"{len(snapshots(backup_dir))} snapshot(s) retained ({retention}-day window)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
