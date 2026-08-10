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

        env = mock.patch.dict(os.environ, {"TELEGRAM_BOT_USERNAME": "cohort_bot"})
        env.start()
        self.addCleanup(env.stop)

    def join(self, user_id: int, source: str) -> None:
        db.upsert_user(self.conn, user_id, f"u{user_id}", source)
        db.log_event(self.conn, user_id, "start")

    def run_main(self, *argv: str) -> tuple[int, str]:
        """Returns (exit code, stdout). Normalisation notices go to stderr and
        are swallowed here so they don't clutter the suite's own output."""
        buffer = io.StringIO()
        with mock.patch.object(sys, "argv", ["verify_links.py", *argv]):
            with redirect_stdout(buffer), redirect_stderr(io.StringIO()):
                code = verify_links.main()
        return code, buffer.getvalue()


class TestCollect(LinksTestCase):
    def test_counts_users_and_starts_per_source(self) -> None:
        self.join(1, "tg_group1")
        self.join(2, "tg_group1")
        self.join(3, "insta_bio")
        stats = verify_links.collect(self.conn, since=None)
        self.assertEqual(stats["tg_group1"]["users"], 2)
        self.assertEqual(stats["tg_group1"]["starts"], 2)
        self.assertEqual(stats["insta_bio"]["users"], 1)

    def test_a_returning_user_adds_a_start_but_not_a_user(self) -> None:
        """The difference between opens and signups, which the table shows."""
        self.join(1, "tg_group1")
        db.log_event(self.conn, 1, "start")
        stats = verify_links.collect(self.conn, since=None)
        self.assertEqual(stats["tg_group1"]["users"], 1)
        self.assertEqual(stats["tg_group1"]["starts"], 2)

    def test_answers_do_not_inflate_the_start_count(self) -> None:
        """Every event lands in one table now; only 'start' means a click."""
        self.join(1, "tg_group1")
        db.log_event(self.conn, 1, "question_served", question_id="Q1")
        db.log_event(self.conn, 1, "answer_submitted", question_id="Q1", is_correct=True)
        self.assertEqual(verify_links.collect(self.conn, since=None)["tg_group1"]["starts"], 1)

    def test_an_event_whose_user_is_gone_is_bucketed_not_dropped(self) -> None:
        """An inner join would hide these, and hiding rows is the bug."""
        db.log_event(self.conn, 999, "start")
        stats = verify_links.collect(self.conn, since=None)
        self.assertEqual(stats[verify_links.ORPHAN]["starts"], 1)
        self.assertEqual(stats[verify_links.ORPHAN]["users"], 0)

    def test_since_excludes_older_rows(self) -> None:
        """A fully filtered-out source drops out of the dict rather than zeroing.

        The table fills it back in for any code that was asked about, so the
        row still prints — it just does not come from here.
        """
        self.join(1, "tg_group1")
        self.assertNotIn("tg_group1", verify_links.collect(self.conn, since="2099-01-01"))
        self.assertEqual(
            verify_links.collect(self.conn, since="2000-01-01")["tg_group1"]["users"], 1
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
        self.assertEqual(verify_links._display("tg_group1"), "tg_group1")


class TestLinkPhase(LinksTestCase):
    def test_prints_the_deep_link_for_each_tag(self) -> None:
        _, out = self.run_main("tg_group1", "insta_bio", "--links-only")
        self.assertIn("https://t.me/cohort_bot?start=tg_group1", out)
        self.assertIn("https://t.me/cohort_bot?start=insta_bio", out)

    def test_the_printed_link_carries_the_normalised_code(self) -> None:
        """What is printed must equal what the bot will record."""
        _, out = self.run_main("Reddit /r/Medicine", "--links-only")
        self.assertIn("?start=reddit-r-medicine", out)
        self.assertNotIn("Reddit /r/Medicine", out)

    def test_landing_url_is_printed_only_when_asked(self) -> None:
        with mock.patch.dict(os.environ, {"LANDING_BASE_URL": "https://example.com"}):
            _, plain = self.run_main("tg_group1", "--links-only")
            _, with_landing = self.run_main("tg_group1", "--links-only", "--landing")
        self.assertNotIn("?s=tg_group1", plain)
        self.assertIn("https://example.com/?s=tg_group1", with_landing)

    def test_duplicate_tags_collapse_after_normalisation(self) -> None:
        _, out = self.run_main("Reddit", "reddit", "--links-only")
        self.assertEqual(out.count("?start=reddit"), 1)


class TestReportPhase(LinksTestCase):
    def test_a_clicked_tag_shows_its_counts(self) -> None:
        self.join(1, "tg_group1")
        _, out = self.run_main("tg_group1", "--report-only", "--db", str(self.path))
        self.assertRegex(out, r"tg_group1\s+1\s+1")

    def test_an_unclicked_tag_is_listed_with_zeroes(self) -> None:
        _, out = self.run_main("tg_group1", "--report-only", "--db", str(self.path))
        self.assertIn("tg_group1", out)
        self.assertIn("no user recorded yet for: tg_group1", out)

    def test_unrequested_sources_are_surfaced_and_marked(self) -> None:
        """This is what catches a typo'd tag or a payload-stripping redirect."""
        self.join(1, "tg-group1")
        _, out = self.run_main("tg_group1", "--report-only", "--db", str(self.path))
        self.assertIn("tg-group1 *", out)

    def test_an_empty_source_is_visible_in_the_table(self) -> None:
        self.conn.execute(
            "INSERT INTO users (user_id, first_seen, source_channel) VALUES (1, ?, '')",
            (db.utcnow(),),
        )
        self.conn.commit()
        _, out = self.run_main("tg_group1", "--report-only", "--db", str(self.path))
        self.assertIn("(empty)", out)


class TestExitCodes(LinksTestCase):
    def test_zero_when_not_asserting_anything(self) -> None:
        code, _ = self.run_main("tg_group1", "--report-only", "--db", str(self.path))
        self.assertEqual(code, 0)

    def test_require_all_fails_until_every_tag_has_a_user(self) -> None:
        self.join(1, "tg_group1")
        code, _ = self.run_main(
            "tg_group1", "insta_bio", "--report-only", "--require-all", "--db", str(self.path)
        )
        self.assertEqual(code, 1)

    def test_require_all_passes_once_they_all_have_one(self) -> None:
        self.join(1, "tg_group1")
        self.join(2, "insta_bio")
        code, _ = self.run_main(
            "tg_group1", "insta_bio", "--report-only", "--require-all", "--db", str(self.path)
        )
        self.assertEqual(code, 0)

    def test_a_missing_database_is_not_a_silent_pass_under_require_all(self) -> None:
        missing = Path(self._tmp.name) / "nope.db"
        code, out = self.run_main(
            "tg_group1", "--report-only", "--require-all", "--db", str(missing)
        )
        self.assertEqual(code, 1)
        self.assertIn("does not exist", out)

    def test_links_only_never_touches_the_database(self) -> None:
        missing = Path(self._tmp.name) / "nope.db"
        code, _ = self.run_main("tg_group1", "--links-only", "--db", str(missing))
        self.assertEqual(code, 0)
        self.assertFalse(missing.exists())


if __name__ == "__main__":
    unittest.main()
