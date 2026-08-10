"""Storage behaviour — the rules that are easy to break by accident."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from bot import db


def sample_question(qid: str, subject: str = "Physiology") -> dict:
    return {
        "id": qid,
        "subject": subject,
        "year": 2024,
        "stem": f"Stem {qid}",
        "option_a": "a",
        "option_b": "b",
        "option_c": "c",
        "option_d": "d",
        "correct_option": "A",
        "explanation": "because",
        "source_tag": "test",
    }


class DbTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.conn = db.connect(Path(self._tmp.name) / "test.db")
        self.addCleanup(self.conn.close)
        db.init_schema(self.conn)


class TestSubscribers(DbTestCase):
    def test_first_insert_reports_new(self) -> None:
        self.assertTrue(db.upsert_subscriber(self.conn, 1, "u", "U", "reddit"))
        self.assertFalse(db.upsert_subscriber(self.conn, 1, "u", "U", "reddit"))

    def test_source_is_first_touch_and_never_overwritten(self) -> None:
        """A later /start from a different link must not rewrite the origin."""
        db.upsert_subscriber(self.conn, 1, "u", "U", "reddit")
        db.upsert_subscriber(self.conn, 1, "u2", "U2", "twitter")
        row = self.conn.execute(
            "SELECT source, username FROM subscribers WHERE chat_id = 1"
        ).fetchone()
        self.assertEqual(row["source"], "reddit")
        # Profile fields do refresh, though — only source is frozen.
        self.assertEqual(row["username"], "u2")

    def test_stop_then_start_reactivates(self) -> None:
        db.upsert_subscriber(self.conn, 1, "u", "U", "reddit")
        db.deactivate_subscriber(self.conn, 1)
        self.assertEqual(db.active_subscribers(self.conn), [])

        db.upsert_subscriber(self.conn, 1, "u", "U", "reddit")
        row = self.conn.execute(
            "SELECT active, left_at FROM subscribers WHERE chat_id = 1"
        ).fetchone()
        self.assertEqual(row["active"], 1)
        self.assertIsNone(row["left_at"], "left_at must clear on rejoin")

    def test_active_subscribers_excludes_stopped(self) -> None:
        db.upsert_subscriber(self.conn, 1, "a", "A", "x")
        db.upsert_subscriber(self.conn, 2, "b", "B", "x")
        db.deactivate_subscriber(self.conn, 2)
        self.assertEqual([r["chat_id"] for r in db.active_subscribers(self.conn)], [1])


class TestQuestions(DbTestCase):
    def test_upsert_is_idempotent_on_id(self) -> None:
        inserted, updated = db.upsert_questions(self.conn, [sample_question("Q1")])
        self.assertEqual((inserted, updated), (1, 0))

        edited = sample_question("Q1")
        edited["stem"] = "Edited stem"
        inserted, updated = db.upsert_questions(self.conn, [edited])
        self.assertEqual((inserted, updated), (0, 1))
        self.assertEqual(db.question_count(self.conn), 1)
        self.assertEqual(db.get_question(self.conn, "Q1")["stem"], "Edited stem")

    def test_next_question_excludes_already_delivered(self) -> None:
        db.upsert_questions(
            self.conn, [sample_question(f"Q{i}") for i in range(1, 4)]
        )
        db.record_delivery(self.conn, 1, "Q1")
        db.record_delivery(self.conn, 1, "Q2")

        for _ in range(10):
            self.assertEqual(db.next_question_for(self.conn, 1)["id"], "Q3")

    def test_next_question_returns_none_when_exhausted(self) -> None:
        db.upsert_questions(self.conn, [sample_question("Q1")])
        db.record_delivery(self.conn, 1, "Q1")
        self.assertIsNone(db.next_question_for(self.conn, 1))

    def test_deliveries_are_per_subscriber(self) -> None:
        db.upsert_questions(self.conn, [sample_question("Q1")])
        db.record_delivery(self.conn, 1, "Q1")
        self.assertIsNotNone(db.next_question_for(self.conn, 2))

    def test_subject_filter_is_case_insensitive(self) -> None:
        db.upsert_questions(
            self.conn,
            [
                sample_question("Q1", "Pharmacology"),
                sample_question("Q2", "Anatomy"),
            ],
        )
        row = db.next_question_for(self.conn, 1, subject="pHaRmAcOlOgY")
        self.assertEqual(row["id"], "Q1")

    def test_subject_filter_with_no_match_returns_none(self) -> None:
        db.upsert_questions(self.conn, [sample_question("Q1", "Anatomy")])
        self.assertIsNone(db.next_question_for(self.conn, 1, subject="Radiology"))

    def test_record_delivery_is_idempotent(self) -> None:
        db.upsert_questions(self.conn, [sample_question("Q1")])
        db.record_delivery(self.conn, 1, "Q1")
        db.record_delivery(self.conn, 1, "Q1")
        self.assertEqual(db.deliveries_today(self.conn, 1), 1)


class TestAttempts(DbTestCase):
    def setUp(self) -> None:
        super().setUp()
        db.upsert_questions(self.conn, [sample_question("Q1")])

    def test_first_answer_counts_and_repeats_do_not(self) -> None:
        self.assertTrue(db.record_attempt(self.conn, 1, "Q1", "A", True))
        self.assertFalse(
            db.record_attempt(self.conn, 1, "Q1", "B", False),
            "a second tap must not overwrite the first answer",
        )
        self.assertEqual(db.subscriber_score(self.conn, 1), (1, 1))

    def test_score_with_no_attempts(self) -> None:
        self.assertEqual(db.subscriber_score(self.conn, 99), (0, 0))

    def test_score_counts_correct_and_total(self) -> None:
        db.upsert_questions(self.conn, [sample_question("Q2"), sample_question("Q3")])
        db.record_attempt(self.conn, 1, "Q1", "A", True)
        db.record_attempt(self.conn, 1, "Q2", "B", False)
        db.record_attempt(self.conn, 1, "Q3", "A", True)
        self.assertEqual(db.subscriber_score(self.conn, 1), (2, 3))


class TestAttribution(DbTestCase):
    def test_summary_counts_signups_active_and_engaged(self) -> None:
        db.upsert_questions(self.conn, [sample_question("Q1")])
        for chat_id in (1, 2, 3):
            db.upsert_subscriber(self.conn, chat_id, None, None, "reddit")
        db.upsert_subscriber(self.conn, 4, None, None, "direct")

        db.deactivate_subscriber(self.conn, 3)
        db.record_attempt(self.conn, 1, "Q1", "A", True)

        summary = {r["source"]: r for r in db.attribution_summary(self.conn)}
        self.assertEqual(summary["reddit"]["signups"], 3)
        self.assertEqual(summary["reddit"]["active"], 2)
        self.assertEqual(summary["reddit"]["engaged"], 1)
        self.assertEqual(summary["direct"]["engaged"], 0)

    def test_opens_count_every_start_not_just_new_users(self) -> None:
        db.record_attribution(self.conn, "reddit", "start", 1)
        db.record_attribution(self.conn, "reddit", "start", 1)
        db.record_attribution(self.conn, "direct", "start", 2)
        self.assertEqual(db.attribution_opens(self.conn), {"reddit": 2, "direct": 1})


if __name__ == "__main__":
    unittest.main()
