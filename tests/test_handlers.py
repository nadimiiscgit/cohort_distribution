"""Handler behaviour, with Telegram mocked out.

Skipped on a bare clone alongside test_render.py — handlers.py imports
python-telegram-bot. What is worth testing here is not the wire format but the
two things a wrong handler breaks silently: the attribution payload being read
on the first /start only, and a repeat tap on an old keyboard being a no-op.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

try:
    from bot import db, handlers
except ImportError:  # pragma: no cover - depends on the environment
    handlers = None
    from bot import db


def fake_update(user_id: int = 1, username: str = "alice", callback_data: str = None):
    """An Update with just the attributes the handlers actually reach for."""
    update = mock.MagicMock()
    update.effective_user.id = user_id
    update.effective_user.username = username
    update.effective_message.reply_text = mock.AsyncMock()
    if callback_data is None:
        update.callback_query = None
    else:
        update.callback_query.data = callback_data
        update.callback_query.answer = mock.AsyncMock()
        update.callback_query.edit_message_text = mock.AsyncMock()
        update.callback_query.edit_message_reply_markup = mock.AsyncMock()
    return update


@unittest.skipIf(handlers is None, "python-telegram-bot is not installed")
class HandlerTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.conn = db.connect(Path(self._tmp.name) / "test.db")
        self.addCleanup(self.conn.close)
        db.init_schema(self.conn)
        db.upsert_questions(
            self.conn,
            [
                {
                    "question_id": "Q1",
                    "subject": "Physiology",
                    "stem": "Which node?",
                    "option_a": "SA",
                    "option_b": "AV",
                    "option_c": "His",
                    "option_d": "Purkinje",
                    "correct_option": "A",
                    "explanation": "The SA node is fastest.",
                    "scheduled_date": db.today(),
                }
            ],
        )

        self.context = mock.MagicMock()
        self.context.application.bot_data = {"conn": self.conn}
        self.context.args = []

        # Keep every test off the developer's real .env.
        patcher = mock.patch.dict(
            os.environ,
            {"CTA_URL": "https://app.example.com/join", "ADMIN_ID": "999"},
            clear=True,
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        loaded = mock.patch("bot.config._loaded", True)
        loaded.start()
        self.addCleanup(loaded.stop)


class TestStart(HandlerTestCase):
    async def test_records_the_deep_link_payload_as_source_channel(self) -> None:
        self.context.args = ["tg_group1"]
        await handlers.start(fake_update(), self.context)

        self.assertEqual(db.get_user(self.conn, 1)["source_channel"], "tg_group1")

    async def test_no_payload_records_direct(self) -> None:
        await handlers.start(fake_update(), self.context)

        self.assertEqual(db.get_user(self.conn, 1)["source_channel"], "direct")

    async def test_a_second_start_from_another_channel_does_not_overwrite(self) -> None:
        """The whole attribution model rests on this."""
        self.context.args = ["tg_group1"]
        await handlers.start(fake_update(), self.context)

        self.context.args = ["ig_drxyz"]
        await handlers.start(fake_update(), self.context)

        self.assertEqual(db.get_user(self.conn, 1)["source_channel"], "tg_group1")

    async def test_sends_a_greeting_and_todays_question(self) -> None:
        update = fake_update()
        await handlers.start(update, self.context)

        self.assertEqual(update.effective_message.reply_text.await_count, 2)
        question_call = update.effective_message.reply_text.await_args_list[1]
        self.assertIn("Which node?", question_call.args[0])
        self.assertIsNotNone(question_call.kwargs["reply_markup"])

    async def test_logs_a_start_event_and_a_serve(self) -> None:
        await handlers.start(fake_update(), self.context)

        kinds = [
            r["event_type"]
            for r in self.conn.execute(
                "SELECT event_type FROM events ORDER BY event_id"
            )
        ]
        self.assertEqual(kinds, ["start", "question_served"])

    async def test_says_so_when_nothing_is_scheduled(self) -> None:
        self.conn.execute("UPDATE questions SET scheduled_date = NULL")
        self.conn.commit()

        update = fake_update()
        await handlers.start(update, self.context)

        self.assertIn(
            handlers.NO_QUESTION,
            update.effective_message.reply_text.await_args_list[1].args[0],
        )


class TestAnswer(HandlerTestCase):
    async def setUp_user(self) -> None:
        db.upsert_user(self.conn, 1, "alice", "tg_group1")

    async def test_correct_answer_is_recorded_and_acknowledged(self) -> None:
        await self.setUp_user()
        update = fake_update(callback_data="ans:Q1:A")

        await handlers.answer(update, self.context)

        self.assertEqual(db.user_score(self.conn, 1), (1, 1))
        text = update.callback_query.edit_message_text.await_args.args[0]
        self.assertIn("Correct", text)
        self.assertIn("The SA node is fastest.", text)

    async def test_wrong_answer_names_the_right_one(self) -> None:
        await self.setUp_user()
        update = fake_update(callback_data="ans:Q1:C")

        await handlers.answer(update, self.context)

        self.assertEqual(db.user_score(self.conn, 1), (0, 1))
        self.assertIn(
            "answer is A", update.callback_query.edit_message_text.await_args.args[0]
        )

    async def test_the_reply_carries_a_cta_button_with_tracking(self) -> None:
        await self.setUp_user()
        update = fake_update(callback_data="ans:Q1:A")

        await handlers.answer(update, self.context)

        kwargs = update.callback_query.edit_message_text.await_args.kwargs
        markup = kwargs["reply_markup"]
        url = markup.inline_keyboard[0][0].url
        self.assertIn("uid=1", url)
        self.assertIn("src=tg_group1", url)
        self.assertIn("qid=Q1", url)

    async def test_no_cta_button_when_cta_url_is_unset(self) -> None:
        await self.setUp_user()
        with mock.patch.dict(os.environ, {}, clear=True):
            update = fake_update(callback_data="ans:Q1:A")
            await handlers.answer(update, self.context)

        self.assertIsNone(
            update.callback_query.edit_message_text.await_args.kwargs["reply_markup"]
        )

    async def test_a_repeat_tap_changes_nothing(self) -> None:
        """An old message stays tappable; the second tap must be inert."""
        await self.setUp_user()
        await handlers.answer(fake_update(callback_data="ans:Q1:A"), self.context)

        second = fake_update(callback_data="ans:Q1:B")
        await handlers.answer(second, self.context)

        second.callback_query.edit_message_text.assert_not_awaited()
        self.assertEqual(db.user_score(self.conn, 1), (1, 1))

    async def test_a_deleted_question_does_not_crash(self) -> None:
        await self.setUp_user()
        update = fake_update(callback_data="ans:GONE:A")

        await handlers.answer(update, self.context)

        update.callback_query.edit_message_reply_markup.assert_awaited()
        self.assertEqual(db.user_score(self.conn, 1), (0, 0))

    async def test_malformed_callback_data_is_ignored(self) -> None:
        await self.setUp_user()
        update = fake_update(callback_data="garbage")

        with self.assertLogs("bot.handlers", level="WARNING"):
            await handlers.answer(update, self.context)

        update.callback_query.edit_message_text.assert_not_awaited()


class TestStopAndScore(HandlerTestCase):
    async def test_stop_deactivates_without_deleting_answers(self) -> None:
        db.upsert_user(self.conn, 1, "alice", "tg_group1")
        await handlers.answer(fake_update(callback_data="ans:Q1:A"), self.context)

        await handlers.stop(fake_update(), self.context)

        self.assertEqual(db.get_user(self.conn, 1)["is_active"], 0)
        self.assertEqual(db.user_score(self.conn, 1), (1, 1))

    async def test_score_does_not_resubscribe(self) -> None:
        db.upsert_user(self.conn, 1, "alice", "tg_group1")
        await handlers.stop(fake_update(), self.context)

        await handlers.score(fake_update(), self.context)

        self.assertEqual(db.get_user(self.conn, 1)["is_active"], 0)


class TestStats(HandlerTestCase):
    async def test_silent_for_non_admins(self) -> None:
        update = fake_update(user_id=1)
        await handlers.stats(update, self.context)
        update.effective_message.reply_text.assert_not_awaited()

    async def test_answers_the_admin(self) -> None:
        update = fake_update(user_id=999)
        await handlers.stats(update, self.context)
        update.effective_message.reply_text.assert_awaited()
        self.assertIn(
            "by source_channel",
            update.effective_message.reply_text.await_args.args[0],
        )

    async def test_silent_when_no_admin_is_configured(self) -> None:
        """An unset ADMIN_ID must not mean everybody is an admin."""
        with mock.patch.dict(os.environ, {}, clear=True):
            update = fake_update(user_id=999)
            await handlers.stats(update, self.context)
        update.effective_message.reply_text.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
