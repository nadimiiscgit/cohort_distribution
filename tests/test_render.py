"""Message rendering.

Skipped on a bare clone: render.py is the only non-script module that imports
python-telegram-bot, and the rest of the suite is meant to run with nothing
installed. Everything here is a pure function over rows.
"""

from __future__ import annotations

import unittest

try:
    from bot import render
except ImportError:  # pragma: no cover - depends on the environment
    render = None


def question_row(**overrides) -> dict:
    row = {
        "question_id": "Q1",
        "subject": "Physiology",
        "stem": "Which node sets the pace?",
        "option_a": "Sinoatrial",
        "option_b": "Atrioventricular",
        "option_c": "Bundle of His",
        "option_d": "Purkinje",
        "correct_option": "A",
        "explanation": "The SA node depolarises fastest.",
        "scheduled_date": "2026-08-10",
    }
    row.update(overrides)
    return row


@unittest.skipIf(render is None, "python-telegram-bot is not installed")
class TestQuestionRendering(unittest.TestCase):
    def test_shows_every_option(self) -> None:
        text = render.question_text(question_row())
        for label in ("A.", "B.", "C.", "D."):
            self.assertIn(label, text)
        self.assertIn("Sinoatrial", text)

    def test_escapes_html_from_the_question_bank(self) -> None:
        """A stem containing < or & must not break the message or inject markup."""
        text = render.question_text(
            question_row(stem="A <b>bold</b> claim & a caveat")
        )
        self.assertIn("&lt;b&gt;bold&lt;/b&gt;", text)
        self.assertIn("&amp;", text)

    def test_keyboard_encodes_the_question_and_option(self) -> None:
        markup = render.answer_keyboard("Q1")
        data = [b.callback_data for row in markup.inline_keyboard for b in row]
        self.assertEqual(
            data, ["ans:Q1:A", "ans:Q1:B", "ans:Q1:C", "ans:Q1:D"]
        )

    def test_answer_text_names_the_right_option_when_wrong(self) -> None:
        text = render.answer_text(question_row(), "C", is_correct=False)
        self.assertIn("You picked C", text)
        self.assertIn("answer is A", text)
        self.assertIn("SA node", text)

    def test_answer_text_congratulates_when_right(self) -> None:
        text = render.answer_text(question_row(), "A", is_correct=True)
        self.assertIn("Correct", text)

    def test_answer_text_survives_a_question_with_no_explanation(self) -> None:
        text = render.answer_text(question_row(explanation=None), "A", True)
        self.assertIn("Correct", text)


@unittest.skipIf(render is None, "python-telegram-bot is not installed")
class TestCtaMarkup(unittest.TestCase):
    def test_no_button_without_a_cta_url(self) -> None:
        """The bot must still work when CTA_URL is unset."""
        self.assertIsNone(
            render.cta_markup(None, user_id=1, source_channel="direct")
        )

    def test_button_carries_the_tracking_parameters(self) -> None:
        markup = render.cta_markup(
            "https://app.example.com/join",
            user_id=7,
            source_channel="tg_group1",
            question_id="Q1",
        )
        url = markup.inline_keyboard[0][0].url
        self.assertIn("uid=7", url)
        self.assertIn("src=tg_group1", url)
        self.assertIn("qid=Q1", url)


@unittest.skipIf(render is None, "python-telegram-bot is not installed")
class TestStatsRendering(unittest.TestCase):
    def snapshot(self, **overrides) -> dict:
        base = {
            "total_users": 128,
            "active_users": 119,
            "new_today": 7,
            "dau": 23,
            "answers_today": 31,
            "d1": (14, 40),
            "d7": (6, 31),
            "by_source": [
                {
                    "source_channel": "tg_group1",
                    "users": 64,
                    "active": 61,
                    "served": 60,
                    "answered": 48,
                }
            ],
        }
        base.update(overrides)
        return base

    def test_reports_every_headline_number(self) -> None:
        text = render.stats_text(self.snapshot())
        for expected in ("128", "119", "23", "tg_group1", "35%", "19%"):
            self.assertIn(expected, text)

    def test_says_n_a_rather_than_dividing_by_zero(self) -> None:
        """Day one of the experiment: no cohort is old enough yet."""
        text = render.stats_text(self.snapshot(d1=(0, 0), d7=(0, 0)))
        self.assertIn("n/a", text)

    def test_handles_an_empty_database(self) -> None:
        text = render.stats_text(
            self.snapshot(total_users=0, by_source=[], d1=(0, 0), d7=(0, 0))
        )
        self.assertIn("no users yet", text)


if __name__ == "__main__":
    unittest.main()
