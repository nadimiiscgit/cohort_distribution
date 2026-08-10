#!/usr/bin/env python3
"""Send one ad-hoc message to every active user.

    python scripts/broadcast.py --text "New questions are up."      # dry run
    python scripts/broadcast.py --file notes/launch.md --send       # for real
    python scripts/broadcast.py --text "hi" --send --only-source tg_group1

Dry run is the default and `--send` is the only way past it: an ad-hoc message
is irreversible and goes to every human we have. DRY_RUN=true in .env vetoes
`--send` as a second safety catch, and this script is deliberately absent from
cron.

For the daily question use `scripts/daily.py`, which is cron-driven and can
only send the scheduled question. This one exists for the rare announcement.

Users who have blocked the bot are deactivated when their send comes back
Forbidden, so the next run doesn't retry them.
"""

from __future__ import annotations

import _bootstrap  # noqa: F401  (sys.path side effect)

import argparse
import asyncio
import sys
from pathlib import Path

from telegram import Bot
from telegram.constants import ParseMode
from telegram.error import Forbidden, RetryAfter, TelegramError

from bot import config, db


async def run(text: str, send: bool, only_source: str | None) -> int:
    conn = db.connect()
    db.init_schema(conn)

    targets = [
        row
        for row in db.active_users(conn)
        if only_source is None or row["source_channel"] == only_source
    ]
    if not targets:
        print("no matching active users")
        return 0

    print(f"{len(targets)} recipient(s)")
    if not send:
        print("\n--- DRY RUN, nothing sent ---")
        print(text)
        print("--- pass --send to deliver ---")
        return 0

    bot = Bot(config.bot_token())
    delay = config.get_float("BROADCAST_RATE_LIMIT", 0.05)

    sent = failed = 0
    async with bot:
        for row in targets:
            user_id = row["user_id"]
            try:
                await bot.send_message(
                    chat_id=user_id, text=text, parse_mode=ParseMode.HTML
                )
                sent += 1
            except RetryAfter as exc:
                # Telegram tells us exactly how long to back off; obey and retry once.
                await asyncio.sleep(exc.retry_after + 1)
                try:
                    await bot.send_message(
                        chat_id=user_id, text=text, parse_mode=ParseMode.HTML
                    )
                    sent += 1
                except TelegramError as retry_exc:
                    failed += 1
                    print(f"  {user_id}: {retry_exc}", file=sys.stderr)
            except Forbidden:
                db.deactivate_user(conn, user_id)
                failed += 1
                print(f"  {user_id}: blocked the bot, deactivated", file=sys.stderr)
            except TelegramError as exc:
                failed += 1
                print(f"  {user_id}: {exc}", file=sys.stderr)
            await asyncio.sleep(delay)

    print(f"sent {sent}, failed {failed}")
    return 0 if failed == 0 else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--text", help="message body (Telegram HTML subset)")
    source.add_argument("--file", type=Path, help="read the body from a file")
    parser.add_argument(
        "--send", action="store_true", help="actually deliver; omit for a dry run"
    )
    parser.add_argument(
        "--only-source", help="limit to users with this source_channel"
    )
    args = parser.parse_args()

    config.load_env()
    text = args.text if args.text else args.file.read_text(encoding="utf-8").strip()
    if not text:
        print("error: empty message body", file=sys.stderr)
        return 1

    send = args.send
    if send and config.get_bool("DRY_RUN", False):
        print("DRY_RUN=true in .env — refusing to send. Unset it first.", file=sys.stderr)
        return 1

    return asyncio.run(run(text, send, args.only_source))


if __name__ == "__main__":
    sys.exit(main())
