"""Storage behaviour — the rules that are easy to break by accident."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from bot import db


def sample_question(qid: str, scheduled_date: str | None = None, **overrides) -> dict:
    row = {
        "question_id": qid,
        "subject": "Physiology",
        "stem": f"Stem {qid}",
        "option_a": "a",
        "option_b": "b",
        "option_c": "c",
        "option_d": "d",
        "correct_option": "A",
        "explanation": "because",
        "scheduled_date": scheduled_date,
    }
    row.update(overrides)
    return row


class DbTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.conn = db.connect(Path(self._tmp.name) / "test.db")
        self.addCleanup(self.conn.close)
        db.init_schema(self.conn)

    def backdate_user(self, user_id: int, days_ago: int, source: str = "direct") -> None:
        """Insert a user whose first_seen is in the past, for retention maths."""
        self.conn.execute(
            "INSERT INTO users (user_id, username, first_seen, source_channel,"
            " last_active, is_active)"
            f" VALUES (?, ?, date('now', '-{days_ago} day'), ?,"
            f"         date('now', '-{days_ago} day'), 1)",
            (user_id, f"u{user_id}", source),
        )
        self.conn.commit()

    def backdate_event(self, user_id: int, days_ago: int, kind: str = "start") -> None:
        self.conn.execute(
            "INSERT INTO events (user_id, event_type, created_at)"
            f" VALUES (?, ?, date('now', '-{days_ago} day'))",
            (user_id, kind),
        )
        self.conn.commit()


class TestUsers(DbTestCase):
    def test_first_insert_reports_new(self) -> None:
        self.assertTrue(db.upsert_user(self.conn, 1, "u", "tg_group1"))
        self.assertFalse(db.upsert_user(self.conn, 1, "u", "tg_group1"))

    def test_source_channel_is_first_touch_and_never_overwritten(self) -> None:
        """A later /start from a different link must not rewrite the origin."""
        db.upsert_user(self.conn, 1, "u", "tg_group1")
        db.upsert_user(self.conn, 1, "u2", "ig_drxyz")

        row = db.get_user(self.conn, 1)
        self.assertEqual(row["source_channel"], "tg_group1")
        # Profile fields do refresh, though — only source_channel is frozen.
        self.assertEqual(row["username"], "u2")

    def test_no_payload_records_direct(self) -> None:
        db.ensure_user(self.conn, 1)
        self.assertEqual(db.get_user(self.conn, 1)["source_channel"], "direct")

    def test_ensure_user_does_not_undo_stop(self) -> None:
        """Checking your /score must not resubscribe you."""
        db.upsert_user(self.conn, 1, "u", "tg_group1")
        db.deactivate_user(self.conn, 1)

        db.ensure_user(self.conn, 1, "u")

        self.assertEqual(db.get_user(self.conn, 1)["is_active"], 0)
        self.assertEqual(db.active_users(self.conn), [])

    def test_start_after_stop_resubscribes(self) -> None:
        db.upsert_user(self.conn, 1, "u", "tg_group1")
        db.deactivate_user(self.conn, 1)

        db.upsert_user(self.conn, 1, "u", "tg_group1")

        self.assertEqual(db.get_user(self.conn, 1)["is_active"], 1)

    def test_last_active_advances_but_first_seen_does_not(self) -> None:
        self.backdate_user(1, days_ago=5)
        original = db.get_user(self.conn, 1)

        db.ensure_user(self.conn, 1, "u1")

        row = db.get_user(self.conn, 1)
        self.assertEqual(row["first_seen"], original["first_seen"])
        self.assertGreater(row["last_active"], original["last_active"])

    def test_username_is_not_erased_when_absent(self) -> None:
        db.upsert_user(self.conn, 1, "alice", "tg_group1")
        db.ensure_user(self.conn, 1)  # a Telegram user with no @username set
        self.assertEqual(db.get_user(self.conn, 1)["username"], "alice")


class TestEvents(DbTestCase):
    def setUp(self) -> None:
        super().setUp()
        db.upsert_questions(self.conn, [sample_question("Q1")])
        db.upsert_user(self.conn, 1, "u", "tg_group1")

    def test_an_answer_is_recorded_once(self) -> None:
        """A second tap on an old message must not double-count."""
        self.assertTrue(
            db.log_event(self.conn, 1, "answer_submitted", "Q1", is_correct=True)
        )
        self.assertFalse(
            db.log_event(self.conn, 1, "answer_submitted", "Q1", is_correct=False)
        )
        self.assertEqual(db.user_score(self.conn, 1), (1, 1))

    def test_a_question_is_served_once_per_user(self) -> None:
        """This is what makes a cron rerun a no-op."""
        self.assertTrue(db.log_event(self.conn, 1, "question_served", "Q1"))
        self.assertFalse(db.log_event(self.conn, 1, "question_served", "Q1"))

    def test_the_same_question_can_be_answered_by_different_users(self) -> None:
        db.upsert_user(self.conn, 2, "v", "direct")
        self.assertTrue(db.log_event(self.conn, 1, "answer_submitted", "Q1", True))
        self.assertTrue(db.log_event(self.conn, 2, "answer_submitted", "Q1", True))

    def test_cta_clicks_are_not_deduped(self) -> None:
        """Unlike answers, a repeat click is a real signal and always appends."""
        self.assertTrue(db.log_event(self.conn, 1, "cta_clicked", "Q1"))
        self.assertTrue(db.log_event(self.conn, 1, "cta_clicked", "Q1"))

    def test_unknown_event_types_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            db.log_event(self.conn, 1, "clicked_something")

    def test_users_awaiting_excludes_the_already_served_and_the_stopped(self) -> None:
        db.upsert_user(self.conn, 2, "v", "direct")
        db.upsert_user(self.conn, 3, "w", "direct")
        db.log_event(self.conn, 1, "question_served", "Q1")
        db.deactivate_user(self.conn, 3)

        self.assertEqual(
            [r["user_id"] for r in db.users_awaiting(self.conn, "Q1")], [2]
        )


class TestQuestions(DbTestCase):
    def test_reseeding_updates_in_place(self) -> None:
        db.upsert_questions(self.conn, [sample_question("Q1")])
        inserted, updated = db.upsert_questions(
            self.conn, [sample_question("Q1", stem="corrected")]
        )
        self.assertEqual((inserted, updated), (0, 1))
        self.assertEqual(db.question_count(self.conn), 1)
        self.assertEqual(db.get_question(self.conn, "Q1")["stem"], "corrected")

    def test_question_for_date_is_exact(self) -> None:
        db.upsert_questions(self.conn, [sample_question("Q1", "2026-08-10")])
        self.assertEqual(
            db.question_for_date(self.conn, "2026-08-10")["question_id"], "Q1"
        )
        self.assertIsNone(db.question_for_date(self.conn, "2026-08-11"))

    def test_current_question_falls_back_to_the_most_recent(self) -> None:
        """Someone joining mid-week gets a question, not an apology."""
        db.upsert_questions(
            self.conn,
            [
                sample_question("Q1", "2026-08-08"),
                sample_question("Q2", "2026-08-10"),
                sample_question("Q3", "2026-08-20"),
            ],
        )
        self.assertEqual(
            db.current_question(self.conn, "2026-08-12")["question_id"], "Q2"
        )

    def test_unscheduled_questions_are_never_current(self) -> None:
        """A NULL scheduled_date means loaded but not in the rotation."""
        db.upsert_questions(self.conn, [sample_question("Q1", None)])
        self.assertIsNone(db.current_question(self.conn, "2026-08-12"))
        self.assertIsNone(db.question_for_date(self.conn, "2026-08-12"))

    def test_correct_option_is_constrained(self) -> None:
        import sqlite3

        with self.assertRaises(sqlite3.IntegrityError):
            db.upsert_questions(
                self.conn, [sample_question("Q1", correct_option="E")]
            )


class TestRetention(DbTestCase):
    def test_cohort_excludes_users_whose_day_has_not_finished(self) -> None:
        """A user who joined this morning must not drag D1 down."""
        self.backdate_user(1, days_ago=0)
        self.assertEqual(db.return_rate(self.conn, 1), (0, 0))

    def test_d1_counts_activity_on_the_day_after_signup(self) -> None:
        self.backdate_user(1, days_ago=10)
        self.backdate_event(1, days_ago=9)  # their day 1
        self.backdate_user(2, days_ago=10)  # never came back

        self.assertEqual(db.return_rate(self.conn, 1), (1, 2))

    def test_d7_is_exactly_day_seven_not_within_seven(self) -> None:
        self.backdate_user(1, days_ago=10)
        self.backdate_event(1, days_ago=9)  # day 1 only

        self.assertEqual(db.return_rate(self.conn, 1), (1, 1))
        self.assertEqual(db.return_rate(self.conn, 7), (0, 1))

    def test_d7_cohort_excludes_users_younger_than_eight_days(self) -> None:
        self.backdate_user(1, days_ago=10)
        self.backdate_user(2, days_ago=3)

        self.assertEqual(db.return_rate(self.conn, 7)[1], 1)


class TestStats(DbTestCase):
    def test_counts_and_grouping(self) -> None:
        db.upsert_questions(self.conn, [sample_question("Q1")])
        db.upsert_user(self.conn, 1, "a", "tg_group1")
        db.upsert_user(self.conn, 2, "b", "tg_group1")
        db.upsert_user(self.conn, 3, "c", "ig_drxyz")
        db.deactivate_user(self.conn, 3)
        self.backdate_user(4, days_ago=30, source="direct")

        db.log_event(self.conn, 1, "start")
        db.log_event(self.conn, 1, "question_served", "Q1")
        db.log_event(self.conn, 1, "answer_submitted", "Q1", is_correct=True)
        db.log_event(self.conn, 2, "start")

        snapshot = db.stats(self.conn)
        self.assertEqual(snapshot["total_users"], 4)
        self.assertEqual(snapshot["active_users"], 3)
        self.assertEqual(snapshot["new_today"], 3)  # user 4 signed up 30 days ago
        self.assertEqual(snapshot["dau"], 2)  # users 1 and 2 did something today
        self.assertEqual(snapshot["answers_today"], 1)

        by_source = {r["source_channel"]: r for r in snapshot["by_source"]}
        self.assertEqual(by_source["tg_group1"]["users"], 2)
        self.assertEqual(by_source["tg_group1"]["answered"], 1)
        self.assertEqual(by_source["tg_group1"]["served"], 1)
        self.assertEqual(by_source["ig_drxyz"]["active"], 0)

    def test_source_funnel_since_filters_by_signup_date(self) -> None:
        db.upsert_user(self.conn, 1, "a", "tg_group1")
        self.backdate_user(2, days_ago=30, source="old_campaign")

        rows = db.source_funnel(self.conn, since=db.today())
        self.assertEqual([r["source_channel"] for r in rows], ["tg_group1"])

    def test_empty_database_reports_zeroes_not_crashes(self) -> None:
        snapshot = db.stats(self.conn)
        self.assertEqual(snapshot["total_users"], 0)
        self.assertEqual(snapshot["d1"], (0, 0))
        self.assertEqual(list(snapshot["by_source"]), [])


if __name__ == "__main__":
    unittest.main()
