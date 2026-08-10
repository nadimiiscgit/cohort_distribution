"""CSV validation and scheduling.

The load is all-or-nothing on purpose: a half-loaded question bank is worse
than an empty one, because it looks like it worked.
"""

from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

sys.path.insert(0, str(ROOT / "scripts"))
_spec = importlib.util.spec_from_file_location("seed", ROOT / "scripts" / "seed.py")
seed = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(seed)

HEADER = (
    "question_id,subject,stem,option_a,option_b,option_c,option_d,"
    "correct_option,explanation,scheduled_date"
)
GOOD_ROW = "Q1,Physiology,Stem?,a,b,c,d,A,Because.,2026-08-10"


class SeedTestCase(unittest.TestCase):
    def write(self, *lines: str) -> Path:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "questions.csv"
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return path

    def parse(self, *lines: str):
        return seed.parse_rows(self.write(*lines))


class TestValidation(SeedTestCase):
    def test_a_good_file_parses(self) -> None:
        rows, errors = self.parse(HEADER, GOOD_ROW)
        self.assertEqual(errors, [])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["question_id"], "Q1")
        self.assertEqual(rows[0]["scheduled_date"], "2026-08-10")

    def test_a_missing_column_fails_the_whole_file(self) -> None:
        rows, errors = self.parse(
            "question_id,subject,stem,option_a,option_b,option_c,option_d",
            "Q1,Physiology,Stem?,a,b,c,d",
        )
        self.assertEqual(rows, [])
        self.assertTrue(any("correct_option" in e for e in errors), errors)

    def test_rejects_an_answer_outside_a_to_d(self) -> None:
        _, errors = self.parse(HEADER, "Q1,Physiology,Stem?,a,b,c,d,E,Because.,")
        self.assertTrue(any("correct_option" in e for e in errors), errors)

    def test_accepts_a_lowercase_answer_and_upcases_it(self) -> None:
        rows, errors = self.parse(HEADER, "Q1,Physiology,Stem?,a,b,c,d,b,Because.,")
        self.assertEqual(errors, [])
        self.assertEqual(rows[0]["correct_option"], "B")

    def test_rejects_duplicate_ids(self) -> None:
        _, errors = self.parse(HEADER, GOOD_ROW, GOOD_ROW)
        self.assertTrue(any("duplicate" in e for e in errors), errors)

    def test_rejects_an_empty_required_field(self) -> None:
        _, errors = self.parse(HEADER, "Q1,,Stem?,a,b,c,d,A,Because.,")
        self.assertTrue(any("empty subject" in e for e in errors), errors)

    def test_rejects_a_row_with_no_id(self) -> None:
        _, errors = self.parse(HEADER, ",Physiology,Stem?,a,b,c,d,A,Because.,")
        self.assertTrue(any("empty question_id" in e for e in errors), errors)

    def test_errors_name_the_line_number(self) -> None:
        """A 900-row CSV is unfixable without one."""
        _, errors = self.parse(HEADER, GOOD_ROW, "Q2,Physiology,Stem?,a,b,c,d,E,x,")
        self.assertTrue(any("line 3" in e for e in errors), errors)

    def test_explanation_and_schedule_are_optional(self) -> None:
        rows, errors = self.parse(HEADER, "Q1,Physiology,Stem?,a,b,c,d,A,,")
        self.assertEqual(errors, [])
        self.assertIsNone(rows[0]["explanation"])
        self.assertIsNone(rows[0]["scheduled_date"])


class TestScheduling(SeedTestCase):
    def test_rejects_a_malformed_date(self) -> None:
        _, errors = self.parse(HEADER, "Q1,Physiology,Stem?,a,b,c,d,A,x,10-08-2026")
        self.assertTrue(any("YYYY-MM-DD" in e for e in errors), errors)

    def test_rejects_two_questions_on_the_same_day(self) -> None:
        """Only one can ever send; the other would vanish silently."""
        _, errors = self.parse(
            HEADER,
            "Q1,Physiology,Stem?,a,b,c,d,A,x,2026-08-10",
            "Q2,Physiology,Stem?,a,b,c,d,A,x,2026-08-10",
        )
        self.assertTrue(any("already taken" in e for e in errors), errors)

    def test_schedule_from_fills_consecutive_days(self) -> None:
        rows = [
            {"question_id": "Q1", "scheduled_date": None},
            {"question_id": "Q2", "scheduled_date": None},
        ]
        filled = seed.apply_schedule(rows, date(2026, 8, 10))
        self.assertEqual(filled, 2)
        self.assertEqual(
            [r["scheduled_date"] for r in rows], ["2026-08-10", "2026-08-11"]
        )

    def test_schedule_from_skips_days_already_taken(self) -> None:
        rows = [
            {"question_id": "Q1", "scheduled_date": "2026-08-11"},
            {"question_id": "Q2", "scheduled_date": None},
            {"question_id": "Q3", "scheduled_date": None},
        ]
        seed.apply_schedule(rows, date(2026, 8, 10))
        self.assertEqual(
            [r["scheduled_date"] for r in rows],
            ["2026-08-11", "2026-08-10", "2026-08-12"],
        )

    def test_schedule_from_leaves_dated_rows_alone(self) -> None:
        rows = [{"question_id": "Q1", "scheduled_date": "2026-01-01"}]
        self.assertEqual(seed.apply_schedule(rows, date(2026, 8, 10)), 0)
        self.assertEqual(rows[0]["scheduled_date"], "2026-01-01")


class TestSampleCsv(unittest.TestCase):
    def test_the_committed_sample_is_valid(self) -> None:
        """It is the schema documentation; it must load."""
        rows, errors = seed.parse_rows(ROOT / "data" / "sample.csv")
        self.assertEqual(errors, [])
        self.assertTrue(rows)

    def test_the_sample_header_matches_what_the_loader_requires(self) -> None:
        text = (ROOT / "data" / "sample.csv").read_text(encoding="utf-8")
        header = text.splitlines()[0]
        columns = header.lstrip("﻿").split(",")
        self.assertEqual(columns, list(seed.REQUIRED) + list(seed.OPTIONAL))


if __name__ == "__main__":
    unittest.main()
