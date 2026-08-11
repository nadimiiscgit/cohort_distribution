"""Bot entry point.

    python -m bot.main

Long polling, not webhooks: no inbound port, no TLS termination, no reverse
proxy to keep alive. See NOTES.md before changing that.

Nothing here schedules anything. The daily question is sent by
`scripts/daily.py` from cron, so a restart can never miss or double-fire a
send — see NOTES.md.
"""

from __future__ import annotations

import logging
import sys

from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
)

from . import config, db, handlers, render


def setup_logging() -> None:
    level = (config.get("LOG_LEVEL", "INFO") or "INFO").upper()
    logging.basicConfig(
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        level=getattr(logging, level, logging.INFO),
    )
    # httpx logs every getUpdates poll at INFO, which drowns everything else.
    logging.getLogger("httpx").setLevel(logging.WARNING)


def build_application() -> Application:
    token = config.bot_token()
    conn = db.connect()
    db.init_schema(conn)

    app = ApplicationBuilder().token(token).build()
    app.bot_data["conn"] = conn

    app.add_handler(CommandHandler("start", handlers.start))
    app.add_handler(CommandHandler("help", handlers.help_command))
    app.add_handler(CommandHandler("question", handlers.question))
    app.add_handler(CommandHandler("score", handlers.score))
    app.add_handler(CommandHandler("stop", handlers.stop))
    app.add_handler(CommandHandler("stats", handlers.stats))
    app.add_handler(
        CallbackQueryHandler(handlers.answer, pattern=rf"^{render.ANSWER_PREFIX}:")
    )
    app.add_error_handler(handlers.on_error)
    return app


def main() -> int:
    config.load_env()
    setup_logging()

    problems = config.validate()
    if problems:
        for problem in problems:
            logging.error("config: %s", problem)
        return 1
    for note in config.advisories():
        logging.warning("config: %s", note)

    app = build_application()
    logging.info("starting long polling")
    app.run_polling(drop_pending_updates=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
