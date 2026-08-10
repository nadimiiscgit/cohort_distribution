"""The pre-launch leak check.

A subscriber recorded without a usable source can never be attributed later —
there is nothing stored to work back from. These tests pin the shapes that
count as a leak, and the exit code that stops a launch.
"""

from __future__ import annotations

import importlib.util
import io
import os
import sqlite3
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from bot import db

ROOT = Path(__file__).resolve().parent.parent

sys.path.insert(0, str(ROOT / "scripts"))
_spec = importlib.util.spec_from_file_location(
    "attribution_guard", ROOT / "scripts" / "attribution_guard.py"
)
guard = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(guard)


class GuardTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "test.db"
        self.conn = db.connect(self.path)
        self.addCleanup(self.conn.close)
        db.init_schema(self.conn)

        env = mock.patch.dict(os.environ, {"DEFAULT_SOURCE": "direct"})
        env.start()
        self.addCleanup(env.stop)

    def subscriber(self, chat_id: int, source: str, with_event: bool = True) -> None:
        """Insert directly: the point is to plant rows upsert_subscriber can't."""
        self.conn.execute(
            "INSERT INTO subscribers (chat_id, username, first_name, source,"
            " active, joined_at) VALUES (?, ?, ?, ?, 1, ?)",
            (chat_id, f"u{chat_id}", f"U{chat_id}", source, db.utcnow()),
        )
        if with_event:
            db.record_attribution(self.conn, source=source, event="start", chat_id=chat_id)
        self.conn.commit()

    def audit(self, **kwargs) -> guard.Findings:
        return guard.audit(self.path, echo=False, **kwargs)


class TestCleanDatabase(GuardTestCase):
    def test_a_fully_attributed_database_passes(self) -> None:
        self.subscriber(1, "reddit-r-medicine")
        self.subscriber(2, "whatsapp-batch-2024")
        found = self.audit()
        self.assertEqual(found.failures, 0)
        self.assertEqual(found.exit_code(), 0)

    def test_default_source_alone_warns_but_does_not_fail(self) -> None:
        """Untracked arrivals are expected; they are not a leak by themselves."""
        self.subscriber(1, "direct")
        found = self.audit()
        self.assertEqual(found.failures, 0)
        self.assertEqual(found.warnings, 1)


class TestEmptySources(GuardTestCase):
    def test_empty_string_source_is_a_failure(self) -> None:
        self.subscriber(1, "")
        self.assertTrue(self.audit().failures)

    def test_whitespace_only_source_is_a_failure(self) -> None:
        """It is not empty to SQLite, but it is empty to the funnel."""
        self.subscriber(1, "   ")
        self.assertTrue(self.audit().failures)

    def test_an_event_with_an_empty_source_is_a_failure(self) -> None:
        self.subscriber(1, "reddit")
        self.conn.execute(
            "INSERT INTO attribution_events (chat_id, source, event, created_at)"
            " VALUES (2, '', 'start', ?)",
            (db.utcnow(),),
        )
        self.conn.commit()
        self.assertTrue(self.audit().failures)


class TestNormalisation(GuardTestCase):
    def test_a_source_that_skipped_normalisation_is_a_failure(self) -> None:
        """'Reddit' and 'reddit' would be two rows in the report."""
        self.subscriber(1, "Reddit")
        self.assertTrue(self.audit().failures)

    def test_a_normalised_source_passes(self) -> None:
        self.subscriber(1, "reddit")
        self.assertEqual(self.audit().failures, 0)


class TestEventTrail(GuardTestCase):
    def test_a_subscriber_with_no_start_event_warns(self) -> None:
        self.subscriber(1, "reddit", with_event=False)
        found = self.audit()
        self.assertEqual(found.failures, 0)
        self.assertTrue(found.warnings)

    def test_a_source_disagreeing_with_the_first_start_event_is_a_failure(self) -> None:
        """First touch is frozen, so these two can only diverge by a bad write."""
        self.subscriber(1, "twitter", with_event=False)
        db.record_attribution(self.conn, source="instagram", event="start", chat_id=1)
        self.assertTrue(self.audit().failures)

    def test_a_later_start_on_a_different_code_is_not_a_failure(self) -> None:
        """Clicking a second campaign link is normal and must stay quiet."""
        self.subscriber(1, "reddit")
        db.record_attribution(self.conn, source="twitter", event="start", chat_id=1)
        self.assertEqual(self.audit().failures, 0)


class TestDirectShare(GuardTestCase):
    def test_exceeding_the_ceiling_fails(self) -> None:
        self.subscriber(1, "direct")
        self.subscriber(2, "direct")
        self.subscriber(3, "reddit")
        self.assertTrue(self.audit(max_direct_pct=50).failures)

    def test_staying_under_the_ceiling_passes(self) -> None:
        self.subscriber(1, "direct")
        self.subscriber(2, "reddit")
        self.subscriber(3, "reddit")
        self.assertEqual(self.audit(max_direct_pct=50).failures, 0)

    def test_an_empty_database_warns_rather_than_passing_silently(self) -> None:
        found = self.audit()
        self.assertTrue(found.warnings)


class TestUnusableDatabase(unittest.TestCase):
    """A check that cannot read anything must not report all-clear."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)

    def test_missing_file_is_a_failure(self) -> None:
        self.assertTrue(guard.audit(self.dir / "nope.db", echo=False).failures)

    def test_file_without_the_schema_is_a_failure(self) -> None:
        path = self.dir / "empty.db"
        sqlite3.connect(path).close()
        self.assertTrue(guard.audit(path, echo=False).failures)

    def test_a_file_that_is_not_a_database_is_a_failure(self) -> None:
        path = self.dir / "garbage.db"
        path.write_bytes(b"not a database, just bytes")
        self.assertTrue(guard.audit(path, echo=False).failures)


class TestFindings(unittest.TestCase):
    def test_counts_do_not_leak_between_audits(self) -> None:
        """The reason this is a class and not module-level counters."""
        missing = Path(tempfile.gettempdir()) / "definitely-not-here.db"
        first = guard.audit(missing, echo=False)
        second = guard.audit(missing, echo=False)
        self.assertEqual(first.failures, second.failures)

    def test_strict_promotes_warnings_to_a_non_zero_exit(self) -> None:
        found = guard.Findings(echo=False)
        found.add(guard.WARN, "something worth knowing")
        self.assertEqual(found.exit_code(), 0)
        self.assertEqual(found.exit_code(strict=True), 1)

    def test_ok_lines_change_neither_count(self) -> None:
        found = guard.Findings(echo=False)
        found.add(guard.OK, "fine")
        self.assertEqual((found.failures, found.warnings), (0, 0))


class TestListing(unittest.TestCase):
    def test_truncates_and_says_how_many_it_hid(self) -> None:
        self.assertEqual(guard.listing([1, 2, 3, 4], 2), "1, 2, ... (+2 more)")

    def test_shows_everything_when_it_fits(self) -> None:
        self.assertEqual(guard.listing([1, 2], 5), "1, 2")


class TestCommandLine(GuardTestCase):
    def run_main(self, *argv: str) -> int:
        with mock.patch.object(sys, "argv", ["attribution_guard.py", *argv]):
            with redirect_stdout(io.StringIO()):
                return guard.main()

    def test_exit_code_is_zero_on_a_clean_database(self) -> None:
        self.subscriber(1, "reddit")
        self.assertEqual(self.run_main("--db", str(self.path)), 0)

    def test_exit_code_is_non_zero_on_a_leak(self) -> None:
        self.subscriber(1, "")
        self.assertEqual(self.run_main("--db", str(self.path)), 1)

    def test_strict_flag_reaches_the_exit_code(self) -> None:
        self.subscriber(1, "reddit", with_event=False)
        self.assertEqual(self.run_main("--db", str(self.path)), 0)
        self.assertEqual(self.run_main("--db", str(self.path), "--strict"), 1)


if __name__ == "__main__":
    unittest.main()
