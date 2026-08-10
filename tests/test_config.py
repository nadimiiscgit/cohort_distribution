"""Environment parsing and validation.

validate() is what stands between a typo and a bot that starts, polls with a
dead token, and looks fine in `systemctl status`.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from bot import config

GOOD_ENV = {
    "TELEGRAM_BOT_TOKEN": "987654321:AAHrealisheenoughforaformatcheck1234",
    "TELEGRAM_BOT_USERNAME": "cohort_bot",
    "ADMIN_CHAT_IDS": "42",
}


class EnvTestCase(unittest.TestCase):
    """Runs each test against a controlled environment, ignoring any real .env."""

    def use_env(self, **overrides: str) -> None:
        env = {**GOOD_ENV, **overrides}
        patcher = mock.patch.dict(os.environ, env, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        # Stop _ensure_loaded from reading a developer's real .env into the test.
        loaded = mock.patch.object(config, "_loaded", True)
        loaded.start()
        self.addCleanup(loaded.stop)


class TestLoadEnv(EnvTestCase):
    def test_parses_comments_quotes_and_blank_lines(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env"
            path.write_text(
                "# a comment\n"
                "\n"
                'QUOTED="value"\n'
                "SINGLE='other'\n"
                "PLAIN=bare\n"
                "SPACED = padded \n"
                "WITH_EQUALS=a=b\n"
                "not a pair\n",
                encoding="utf-8",
            )
            with mock.patch.dict(os.environ, {}, clear=True):
                config.load_env(path)
                self.assertEqual(os.environ["QUOTED"], "value")
                self.assertEqual(os.environ["SINGLE"], "other")
                self.assertEqual(os.environ["PLAIN"], "bare")
                self.assertEqual(os.environ["SPACED"], "padded")
                self.assertEqual(os.environ["WITH_EQUALS"], "a=b")

    def test_real_environment_wins_over_the_file(self) -> None:
        """systemd's EnvironmentFile and a local .env must not fight."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env"
            path.write_text("LOG_LEVEL=DEBUG\n", encoding="utf-8")
            with mock.patch.dict(os.environ, {"LOG_LEVEL": "ERROR"}, clear=True):
                config.load_env(path)
                self.assertEqual(os.environ["LOG_LEVEL"], "ERROR")

    def test_missing_file_is_not_an_error(self) -> None:
        config.load_env(Path("/nonexistent/.env"))


class TestCasts(EnvTestCase):
    def test_int_and_float_and_bool(self) -> None:
        self.use_env(COUNT="7", RATE="0.25")
        self.assertEqual(config.get_int("COUNT", 1), 7)
        self.assertEqual(config.get_int("ABSENT", 3), 3)
        self.assertEqual(config.get_float("RATE", 1.0), 0.25)

    def test_bool_accepts_the_usual_spellings(self) -> None:
        for raw in ("1", "true", "TRUE", "yes", "on"):
            self.use_env(FLAG=raw)
            self.assertTrue(config.get_bool("FLAG"), raw)
        for raw in ("0", "false", "no", "off", "anything"):
            self.use_env(FLAG=raw)
            self.assertFalse(config.get_bool("FLAG"), raw)

    def test_empty_value_falls_back_to_the_default(self) -> None:
        self.use_env(EMPTY="")
        self.assertEqual(config.get("EMPTY", "fallback"), "fallback")

    def test_bad_int_raises_config_error(self) -> None:
        self.use_env(COUNT="ten")
        with self.assertRaises(config.ConfigError):
            config.get_int("COUNT", 1)

    def test_require_names_the_missing_variable(self) -> None:
        self.use_env()
        with self.assertRaises(config.ConfigError) as ctx:
            config.require("NOPE")
        self.assertIn("NOPE", str(ctx.exception))

    def test_relative_paths_resolve_from_the_repo_root(self) -> None:
        self.use_env(DATABASE_PATH="data/cohort.db")
        self.assertEqual(config.database_path(), config.ROOT / "data" / "cohort.db")

    def test_absolute_paths_are_left_alone(self) -> None:
        self.use_env(DATABASE_PATH="/var/lib/cohort/x.db")
        self.assertEqual(config.database_path(), Path("/var/lib/cohort/x.db"))

    def test_admin_ids_parse_and_reject_junk(self) -> None:
        self.use_env(ADMIN_CHAT_IDS="1, 2 ,3")
        self.assertEqual(config.admin_chat_ids(), {1, 2, 3})
        self.use_env(ADMIN_CHAT_IDS="")
        self.assertEqual(config.admin_chat_ids(), set())
        self.use_env(ADMIN_CHAT_IDS="1,notanid")
        with self.assertRaises(config.ConfigError):
            config.admin_chat_ids()


class TestValidate(EnvTestCase):
    def assert_problem_mentions(self, needle: str, **overrides: str) -> None:
        self.use_env(**overrides)
        problems = config.validate()
        self.assertTrue(
            any(needle in p for p in problems), f"expected {needle!r} in {problems}"
        )

    def test_a_good_environment_has_no_problems(self) -> None:
        self.use_env()
        self.assertEqual(config.validate(), [])

    def test_catches_the_example_placeholders(self) -> None:
        """The most likely real failure: shipping .env.example unedited."""
        self.assert_problem_mentions(
            "placeholder",
            TELEGRAM_BOT_TOKEN="123456789:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
        )
        self.assert_problem_mentions(
            "placeholder", TELEGRAM_BOT_USERNAME="your_bot_username"
        )

    def test_catches_a_malformed_token(self) -> None:
        self.assert_problem_mentions("BotFather", TELEGRAM_BOT_TOKEN="not-a-token")

    def test_catches_an_at_sign_in_the_username(self) -> None:
        self.assert_problem_mentions("@", TELEGRAM_BOT_USERNAME="@cohort_bot")

    def test_catches_an_out_of_range_hour(self) -> None:
        self.assert_problem_mentions("0-23", DAILY_QUESTION_HOUR="25")

    def test_catches_bad_admin_ids(self) -> None:
        self.assert_problem_mentions("ADMIN_CHAT_IDS", ADMIN_CHAT_IDS="me")


if __name__ == "__main__":
    unittest.main()
