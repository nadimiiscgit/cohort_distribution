#!/usr/bin/env python3
"""Send the day's scheduled question to every active user. Cron-triggered.

    python scripts/daily.py                 # send today's question
    python scripts/daily.py --dry-run       # show who would get what
    python scripts/daily.py --date 2026-08-11
    python scripts/daily.py --resend        # ignore the already-served check

This is the one automated outbound path in the repo, and it is narrow on
purpose: it can only ever send the question row whose `scheduled_date` matches
the day. There is no free-text mode — that is `scripts/broadcast.py`, which
stays manual. See NOTES.md.

Safe to run twice. Recipients are the active users with no `question_served`
event for that question, so a cron retry after a partial failure delivers to
exactly the people who were missed, and a double-fire delivers to nobody.

Users who have blocked or deleted the bot come back as Forbidden; they are
marked is_active = 0 and the run carries on.
"""

from __future__ import annotations

import _bootstrap  # noqa: F401  (sys.path side effect)

import argparse
import asyncio
import logging
import sys

from telegram import Bot
from telegram.constants import ParseMode
from telegram.error import Forbidden, RetryAfter, TelegramError

from bot import config, db, render

log = logging.getLogger("daily")


async def send_to_all(
    question, targets, *, conn, bot: Bot, delay: float
) -> tuple[int, int]:
    """Deliver to each target, logging the serve only when Telegram accepted it."""
    text = render.question_text(question)
    markup = render.answer_keyboard(question["question_id"])
    sent = failed = 0

    async with bot:
        for user in targets:
            user_id = user["user_id"]
            try:
                await bot.send_message(
                    chat_id=user_id,
                    text=text,
                    parse_mode=ParseMode.HTML,
                    reply_markup=markup,
                )
            except RetryAfter as exc:
                # Telegram tells us exactly how long to back off; obey and retry once.
                log.warning("rate limited, sleeping %ss", exc.retry_after)
                await asyncio.sleep(exc.retry_after + 1)
                try:
                    await bot.send_message(
                        chat_id=user_id,
                        text=text,
                        parse_mode=ParseMode.HTML,
                        reply_markup=markup,
                    )
                except TelegramError as retry_exc:
                    failed += 1
                    log.error("%s: %s", user_id, retry_exc)
                    continue
            except Forbidden:
                # Blocked us or deleted the account. Stop trying, keep the row.
                db.deactivate_user(conn, user_id)
                failed += 1
                log.info("%s: forbidden, deactivated", user_id)
                continue
            except TelegramError as exc:
                failed += 1
                log.error("%s: %s", user_id, exc)
                continue

            db.log_event(
                conn, user_id, "question_served", question_id=question["question_id"]
            )
            sent += 1
            await asyncio.sleep(delay)

    return sent, failed


async def run(on_date: str, dry_run: bool, resend: bool) -> int:
    conn = db.connect()
    db.init_schema(conn)

    question = db.question_for_date(conn, on_date)
    if question is None:
        log.warning("no question scheduled for %s — nothing to send", on_date)
        return 0

    qid = question["question_id"]
    targets = db.active_users(conn) if resend else db.users_awaiting(conn, qid)
    log.info(
        "%s: question %s (%s), %d recipient(s)",
        on_date,
        qid,
        question["subject"],
        len(targets),
    )

    if dry_run:
        print(f"--- DRY RUN, nothing sent: {qid} to {len(targets)} user(s) ---")
        print(render.question_text(question))
        return 0
    if not targets:
        log.info("everyone already has %s", qid)
        return 0

    bot = Bot(config.bot_token())
    sent, failed = await send_to_all(
        question,
        targets,
        conn=conn,
        bot=bot,
        delay=config.get_float("BROADCAST_RATE_LIMIT", 0.05),
    )
    log.info("sent %d, failed %d", sent, failed)
    return 0 if failed == 0 else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--date",
        metavar="YYYY-MM-DD",
        help="the schedule date to send (default: today UTC)",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="print the message and recipient count"
    )
    parser.add_argument(
        "--resend",
        action="store_true",
        help="send to every active user, even those already served",
    )
    args = parser.parse_args()

    config.load_env()
    level = (config.get("LOG_LEVEL", "INFO") or "INFO").upper()
    logging.basicConfig(
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        level=getattr(logging, level, logging.INFO),
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)

    dry_run = args.dry_run or config.get_bool("DRY_RUN", False)
    if dry_run and not args.dry_run:
        log.warning("DRY_RUN=true in .env — not sending")

    return asyncio.run(run(args.date or db.today(), dry_run, args.resend))


if __name__ == "__main__":
    sys.exit(main())
