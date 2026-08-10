"""CSV validation.

The contract: a file with any bad row is rejected whole. Half-loading a
question bank is worse than loading none, because the gap is invisible.
"""

from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# seed_questions.py is a script, not a package member: load it by path, with
# scripts/ on sys.path so its `import _bootstrap` resolves.
sys.path.insert(0, str(ROOT / "scripts"))
_spec = importlib.util.spec_from_file_location(
    "seed_questions", ROOT / "scripts" / "seed_questions.py"
)
seed_questions = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(seed_questions)

HEADER = (
    "id,subject,year,stem,option_a,option_b,option_c,option_d,"
    "correct_option,explanation,source_tag"
)
GOOD_ROW = "Q1,Anatomy,2024,Stem,a,b,c,d,A,Because,test"


class SeedTestCase(unittest.TestCase):
    def parse(self, body: str):
        tmp = tempfile.NamedTemporaryFile(
            "w", suffix=".csv", delete=False, encoding="utf-8"
        )
        tmp.write(body)
        tmp.close()
        self.addCleanup(Path(tmp.name).unlink)
        return seed_questions.parse_rows(Path(tmp.name))


class TestValidRows(SeedTestCase):
    def test_parses_a_good_row(self) -> None:
        rows, errors = self.parse(f"{HEADER}\n{GOOD_ROW}\n")
        self.assertEqual(errors, [])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], "Q1")
        self.assertEqual(rows[0]["year"], 2024)
        self.assertEqual(rows[0]["correct_option"], "A")

    def test_the_committed_sample_is_valid(self) -> None:
        """data/sample.csv is the schema of record — it must always parse."""
        rows, errors = seed_questions.parse_rows(ROOT / "data" / "sample.csv")
        self.assertEqual(errors, [])
        self.assertGreater(len(rows), 0)

    def test_lowercase_answer_is_accepted_and_upcased(self) -> None:
        rows, errors = self.parse(
            f"{HEADER}\nQ1,Anatomy,2024,Stem,a,b,c,d,c,Because,test\n"
        )
        self.assertEqual(errors, [])
        self.assertEqual(rows[0]["correct_option"], "C")

    def test_blank_optional_fields_become_none(self) -> None:
        rows, errors = self.parse(f"{HEADER}\nQ1,Anatomy,,Stem,a,b,c,d,A,,\n")
        self.assertEqual(errors, [])
        self.assertIsNone(rows[0]["year"])
        self.assertIsNone(rows[0]["explanation"])
        self.assertIsNone(rows[0]["source_tag"])


class TestRejectedRows(SeedTestCase):
    def assert_error_mentions(self, body: str, needle: str) -> None:
        _, errors = self.parse(body)
        self.assertTrue(
            any(needle in e for e in errors), f"expected {needle!r} in {errors}"
        )

    def test_missing_required_columns_are_named(self) -> None:
        _, errors = self.parse("id,stem\nQ1,x\n")
        self.assertEqual(len(errors), 1)
        for column in ("subject", "option_a", "correct_option"):
            self.assertIn(column, errors[0])

    def test_answer_outside_a_to_d(self) -> None:
        self.assert_error_mentions(
            f"{HEADER}\nQ1,Anatomy,2024,Stem,a,b,c,d,E,x,test\n", "correct_option"
        )

    def test_duplicate_id(self) -> None:
        self.assert_error_mentions(f"{HEADER}\n{GOOD_ROW}\n{GOOD_ROW}\n", "duplicate")

    def test_empty_id(self) -> None:
        self.assert_error_mentions(
            f"{HEADER}\n,Anatomy,2024,Stem,a,b,c,d,A,x,test\n", "empty id"
        )

    def test_empty_required_field(self) -> None:
        self.assert_error_mentions(
            f"{HEADER}\nQ1,,2024,Stem,a,b,c,d,A,x,test\n", "empty subject"
        )

    def test_non_numeric_year(self) -> None:
        self.assert_error_mentions(
            f"{HEADER}\nQ1,Anatomy,soon,Stem,a,b,c,d,A,x,test\n", "year"
        )

    def test_errors_report_the_source_line_number(self) -> None:
        _, errors = self.parse(f"{HEADER}\n{GOOD_ROW}\nQ2,Anatomy,2024,S,a,b,c,d,Z,x,t\n")
        self.assertIn("line 3", errors[0])


if __name__ == "__main__":
    unittest.main()
