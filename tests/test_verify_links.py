"""Link verification.

The claim this script makes is that the URL it prints is the one that has to
show up in the table underneath it. These tests hold the two halves together:
the code is normalised exactly as the bot normalises it, and the counts are
read back per source without silently dropping a row.
"""

from __future__ import annotations

import importlib.util
import io
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from bot import db

ROOT = Path(__file__).resolve().parent.parent

sys.path.insert(0, str(ROOT / "scripts"))
_spec = importlib.util.spec_from_file_location(
    "verify_links", ROOT / "scripts" / "verify_links.py"
)
verify_links = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(verify_links)


class LinksTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "test.db"
        self.conn = db.connect(self.path)
        self.addCleanup(self.conn.close)
        db.init_schema(self.conn)

        env = mock.patch.dict(
            os.environ,
            {"DEFAULT_SOURCE": "direct", "TELEGRAM_BOT_USERNAME": "cohort_bot"},
        )
        env.start()
        self.addCleanup(env.stop)

    def join(self, chat_id: int, source: str) -> None:
        db.record_attribution(self.conn, source=source, event="start", chat_id=chat_id)
        db.upsert_subscriber(self.conn, chat_id, f"u{chat_id}", f"U{chat_id}", source)

    def run_main(self, *argv: str) -> tuple[int, str]:
        """Returns (exit code, stdout). Normalisation notices go to stderr and
        are swallowed here so they don't clutter the suite's own output."""
        buffer = io.StringIO()
        with mock.patch.object(sys, "argv", ["verify_links.py", *argv]):
            with redirect_stdout(buffer), redirect_stderr(io.StringIO()):
                code = verify_links.main()
        return code, buffer.getvalue()


class TestCollect(LinksTestCase):
    def test_counts_users_and_events_per_source(self) -> None:
        self.join(1, "reddit")
        self.join(2, "reddit")
        self.join(3, "twitter")
        stats = verify_links.collect(self.conn, since=None)
        self.assertEqual(stats["reddit"]["users"], 2)
        self.assertEqual(stats["reddit"]["events"], 2)
        self.assertEqual(stats["twitter"]["users"], 1)

    def test_a_returning_user_adds_an_event_but_not_a_user(self) -> None:
        """The difference between opens and signups, which the table shows."""
        self.join(1, "reddit")
        db.record_attribution(self.conn, source="reddit", event="start", chat_id=1)
        stats = verify_links.collect(self.conn, since=None)
        self.assertEqual(stats["reddit"]["users"], 1)
        self.assertEqual(stats["reddit"]["events"], 2)

    def test_a_source_with_events_but_no_subscriber_still_appears(self) -> None:
        """Someone pressed the link but never subscribed — worth seeing."""
        db.record_attribution(self.conn, source="poster", event="start", chat_id=9)
        stats = verify_links.collect(self.conn, since=None)
        self.assertEqual(stats["poster"]["users"], 0)
        self.assertEqual(stats["poster"]["events"], 1)

    def test_since_excludes_older_rows(self) -> None:
        """A fully filtered-out source drops out of the dict rather than zeroing.

        The table fills it back in for any code that was asked about, so the
        row still prints — it just does not come from here.
        """
        self.join(1, "reddit")
        self.assertNotIn("reddit", verify_links.collect(self.conn, since="2099-01-01"))
        self.assertEqual(
            verify_links.collect(self.conn, since="2000-01-01")["reddit"]["users"], 1
        )

    def test_an_empty_database_yields_nothing(self) -> None:
        self.assertEqual(verify_links.collect(self.conn, since=None), {})


class TestDisplay(unittest.TestCase):
    """An unusable source must not render as blank space in the table."""

    def test_empty_string_is_labelled(self) -> None:
        self.assertEqual(verify_links._display(""), "(empty)")

    def test_whitespace_is_labelled_distinctly(self) -> None:
        self.assertEqual(verify_links._display("   "), "(whitespace)")

    def test_a_real_code_is_left_alone(self) -> None:
        self.assertEqual(verify_links._display("reddit-r-medicine"), "reddit-r-medicine")


class TestLinkPhase(LinksTestCase):
    def test_prints_the_deep_link_for_each_tag(self) -> None:
        _, out = self.run_main("reddit", "twitter", "--links-only")
        self.assertIn("https://t.me/cohort_bot?start=reddit", out)
        self.assertIn("https://t.me/cohort_bot?start=twitter", out)

    def test_the_printed_link_carries_the_normalised_code(self) -> None:
        """What is printed must equal what the bot will record."""
        _, out = self.run_main("Reddit /r/Medicine", "--links-only")
        self.assertIn("?start=reddit-r-medicine", out)
        self.assertNotIn("Reddit /r/Medicine", out)

    def test_landing_url_is_printed_only_when_asked(self) -> None:
        with mock.patch.dict(os.environ, {"LANDING_BASE_URL": "https://example.com"}):
            _, plain = self.run_main("reddit", "--links-only")
            _, with_landing = self.run_main("reddit", "--links-only", "--landing")
        self.assertNotIn("?s=reddit", plain)
        self.assertIn("https://example.com/?s=reddit", with_landing)

    def test_duplicate_tags_collapse_after_normalisation(self) -> None:
        _, out = self.run_main("Reddit", "reddit", "--links-only")
        self.assertEqual(out.count("?start=reddit"), 1)


class TestReportPhase(LinksTestCase):
    def test_a_clicked_tag_shows_its_counts(self) -> None:
        self.join(1, "reddit")
        _, out = self.run_main("reddit", "--report-only", "--db", str(self.path))
        self.assertRegex(out, r"reddit\s+1\s+1")

    def test_an_unclicked_tag_is_listed_with_zeroes(self) -> None:
        _, out = self.run_main("reddit", "--report-only", "--db", str(self.path))
        self.assertIn("reddit", out)
        self.assertIn("no subscriber recorded yet for: reddit", out)

    def test_unrequested_sources_are_surfaced_and_marked(self) -> None:
        """This is what catches a typo'd tag or a payload-stripping redirect."""
        self.join(1, "reddit-typo")
        _, out = self.run_main("reddit", "--report-only", "--db", str(self.path))
        self.assertIn("reddit-typo *", out)

    def test_an_empty_source_is_visible_in_the_table(self) -> None:
        self.conn.execute(
            "INSERT INTO subscribers (chat_id, source, joined_at) VALUES (1, '', ?)",
            (db.utcnow(),),
        )
        self.conn.commit()
        _, out = self.run_main("reddit", "--report-only", "--db", str(self.path))
        self.assertIn("(empty)", out)


class TestExitCodes(LinksTestCase):
    def test_zero_when_not_asserting_anything(self) -> None:
        code, _ = self.run_main("reddit", "--report-only", "--db", str(self.path))
        self.assertEqual(code, 0)

    def test_require_all_fails_until_every_tag_has_a_subscriber(self) -> None:
        self.join(1, "reddit")
        code, _ = self.run_main(
            "reddit", "twitter", "--report-only", "--require-all", "--db", str(self.path)
        )
        self.assertEqual(code, 1)

    def test_require_all_passes_once_they_all_have_one(self) -> None:
        self.join(1, "reddit")
        self.join(2, "twitter")
        code, _ = self.run_main(
            "reddit", "twitter", "--report-only", "--require-all", "--db", str(self.path)
        )
        self.assertEqual(code, 0)

    def test_a_missing_database_is_not_a_silent_pass_under_require_all(self) -> None:
        missing = Path(self._tmp.name) / "nope.db"
        code, out = self.run_main(
            "reddit", "--report-only", "--require-all", "--db", str(missing)
        )
        self.assertEqual(code, 1)
        self.assertIn("does not exist", out)

    def test_links_only_never_touches_the_database(self) -> None:
        missing = Path(self._tmp.name) / "nope.db"
        code, _ = self.run_main("reddit", "--links-only", "--db", str(missing))
        self.assertEqual(code, 0)
        self.assertFalse(missing.exists())


if __name__ == "__main__":
    unittest.main()
