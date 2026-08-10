"""Snapshot and retention.

The snapshot must be a real, openable database — not a truncated copy — and
pruning must only ever delete files it recognises as its own.
"""

from __future__ import annotations

import importlib.util
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from bot import db

ROOT = Path(__file__).resolve().parent.parent

sys.path.insert(0, str(ROOT / "scripts"))
_spec = importlib.util.spec_from_file_location(
    "backup", ROOT / "scripts" / "backup.py"
)
backup = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(backup)


class BackupTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.backup_dir = self.tmp / "backups"

    def make_source(self) -> Path:
        path = self.tmp / "source.db"
        conn = db.connect(path)
        db.init_schema(conn)
        db.upsert_user(conn, 1, "u", "tg_group1")
        conn.close()
        return path

    def stamped(self, days_ago: int) -> Path:
        when = datetime.now(timezone.utc) - timedelta(days=days_ago)
        path = self.backup_dir / f"cohort-{when.strftime(backup.STAMP_FORMAT)}.db"
        path.write_bytes(b"")
        return path


class TestSnapshot(BackupTestCase):
    def test_snapshot_is_a_readable_database_with_the_data(self) -> None:
        target = backup.take_snapshot(self.make_source(), self.backup_dir)
        self.assertTrue(target.exists())

        conn = sqlite3.connect(target)
        row = conn.execute(
            "SELECT source_channel FROM users WHERE user_id = 1"
        ).fetchone()
        conn.close()
        self.assertEqual(row[0], "tg_group1")

    def test_snapshot_creates_the_directory(self) -> None:
        self.assertFalse(self.backup_dir.exists())
        backup.take_snapshot(self.make_source(), self.backup_dir)
        self.assertTrue(self.backup_dir.is_dir())


class TestPrune(BackupTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.backup_dir.mkdir(parents=True)

    def test_removes_only_snapshots_past_the_window(self) -> None:
        old = self.stamped(30)
        fresh = self.stamped(1)

        removed = backup.prune(self.backup_dir, retention_days=14)

        self.assertEqual(removed, [old])
        self.assertFalse(old.exists())
        self.assertTrue(fresh.exists())

    def test_leaves_unrecognised_files_alone(self) -> None:
        """Never delete something we did not name."""
        stray = self.backup_dir / "cohort-notatimestamp.db"
        stray.write_bytes(b"")
        unrelated = self.backup_dir / "important.db"
        unrelated.write_bytes(b"")

        backup.prune(self.backup_dir, retention_days=0)

        self.assertTrue(stray.exists())
        self.assertTrue(unrelated.exists())

    def test_snapshots_lists_in_chronological_order(self) -> None:
        newest = self.stamped(1)
        oldest = self.stamped(10)
        self.assertEqual(backup.snapshots(self.backup_dir), [oldest, newest])


if __name__ == "__main__":
    unittest.main()
