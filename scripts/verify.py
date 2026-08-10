#!/usr/bin/env python3
"""Pre-flight and health check. Run before every deploy and nightly from cron.

    python scripts/verify.py
    python scripts/verify.py --check-telegram   # also calls getMe (needs network)

Exit code 0 means everything passed. Non-zero means something needs a human.
Checks are grouped so a fresh checkout with no database still gets useful
output rather than one crash.
"""

from __future__ import annotations

import _bootstrap  # noqa: F401  (sys.path side effect)

import argparse
import sqlite3
import sys
from pathlib import Path

from bot import config, db

OK = "  ok   "
WARN = " warn  "
FAIL = " FAIL  "

_failures = 0
_warnings = 0


def report(level: str, message: str) -> None:
    global _failures, _warnings
    if level == FAIL:
        _failures += 1
    elif level == WARN:
        _warnings += 1
    print(f"[{level}] {message}")


def check_env() -> None:
    print("\n== environment ==")
    if not (config.ROOT / ".env").exists():
        report(FAIL, ".env not found — copy .env.example and fill it in")
        return
    problems = config.validate()
    if problems:
        for problem in problems:
            report(FAIL, problem)
    else:
        report(OK, "all required variables present and well-formed")

    example = config.ROOT / ".env.example"
    if example.exists():
        documented = {
            line.split("=", 1)[0].strip()
            for line in example.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.strip().startswith("#") and "=" in line
        }
        actual = {
            line.split("=", 1)[0].strip()
            for line in (config.ROOT / ".env").read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.strip().startswith("#") and "=" in line
        }
        for missing in sorted(documented - actual):
            report(WARN, f"{missing} is documented in .env.example but absent from .env")
        for extra in sorted(actual - documented):
            report(WARN, f"{extra} is set in .env but undocumented in .env.example")


def check_secrets_not_tracked() -> None:
    """Cheap guard against the one mistake that actually matters."""
    print("\n== secret hygiene ==")
    import subprocess

    try:
        tracked = subprocess.run(
            ["git", "ls-files"],
            cwd=config.ROOT,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.split()
    except (OSError, subprocess.CalledProcessError):
        report(WARN, "could not run `git ls-files` — skipping tracked-file check")
        return

    bad = [
        f
        for f in tracked
        if f == ".env"
        or f.endswith((".db", ".sqlite", ".sqlite3"))
        or (f.startswith("data/") and f.endswith(".csv") and f != "data/sample.csv")
    ]
    if bad:
        for f in bad:
            report(FAIL, f"{f} is tracked by git and must not be")
    else:
        report(OK, "no secrets, databases, or real question data tracked")


def check_database() -> None:
    print("\n== database ==")
    path: Path = config.database_path()
    if not path.exists():
        report(WARN, f"{path} does not exist yet — it is created on first run")
        return

    try:
        conn = db.connect(path)
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
    except sqlite3.DatabaseError as exc:
        report(FAIL, f"cannot open {path}: {exc}")
        return

    if integrity != "ok":
        report(FAIL, f"integrity_check returned {integrity!r}")
    else:
        report(OK, f"{path} passes integrity_check")

    db.init_schema(conn)
    questions = db.question_count(conn)
    if questions == 0:
        report(WARN, "no questions loaded — run scripts/seed_questions.py")
    else:
        report(OK, f"{questions} question(s) loaded")

    subscribers = len(db.active_subscribers(conn))
    report(OK, f"{subscribers} active subscriber(s)")

    orphans = conn.execute(
        "SELECT COUNT(*) AS c FROM deliveries d"
        " WHERE NOT EXISTS (SELECT 1 FROM questions q WHERE q.id = d.question_id)"
    ).fetchone()["c"]
    if orphans:
        report(WARN, f"{orphans} delivery row(s) point at deleted questions")

    bad_answers = conn.execute(
        "SELECT COUNT(*) AS c FROM questions WHERE correct_option NOT IN ('A','B','C','D')"
    ).fetchone()["c"]
    if bad_answers:
        report(FAIL, f"{bad_answers} question(s) have an invalid correct_option")

    missing_expl = conn.execute(
        "SELECT COUNT(*) AS c FROM questions"
        " WHERE explanation IS NULL OR trim(explanation) = ''"
    ).fetchone()["c"]
    if missing_expl:
        report(WARN, f"{missing_expl} question(s) have no explanation")


def check_backups() -> None:
    print("\n== backups ==")
    backup_dir = config.backup_dir()
    if not backup_dir.exists():
        report(WARN, f"{backup_dir} does not exist — run scripts/backup.py")
        return
    snapshots = sorted(backup_dir.glob("cohort-*.db"))
    if not snapshots:
        report(WARN, "no snapshots found")
    else:
        report(OK, f"{len(snapshots)} snapshot(s), newest {snapshots[-1].name}")


def check_telegram() -> None:
    print("\n== telegram ==")
    import asyncio

    from telegram import Bot
    from telegram.error import TelegramError

    async def call() -> None:
        bot = Bot(config.require("TELEGRAM_BOT_TOKEN"))
        async with bot:
            me = await bot.get_me()
            report(OK, f"authenticated as @{me.username}")
            configured = (config.get("TELEGRAM_BOT_USERNAME") or "").lstrip("@")
            if configured and me.username != configured:
                report(
                    FAIL,
                    f"TELEGRAM_BOT_USERNAME is {configured!r} but the token belongs "
                    f"to @{me.username} — deep links will point at the wrong bot",
                )

    try:
        asyncio.run(call())
    except (TelegramError, config.ConfigError) as exc:
        report(FAIL, f"getMe failed: {exc}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--check-telegram",
        action="store_true",
        help="also verify the bot token against the Telegram API",
    )
    args = parser.parse_args()

    config.load_env()
    check_env()
    check_secrets_not_tracked()
    check_database()
    check_backups()
    if args.check_telegram:
        check_telegram()

    print(f"\n{_failures} failure(s), {_warnings} warning(s)")
    return 1 if _failures else 0


if __name__ == "__main__":
    sys.exit(main())
