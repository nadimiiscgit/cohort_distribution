"""Command and callback handlers.

Handlers stay thin: parse the update, call into db.py, format a reply with
render.py. The SQLite connection lives in `application.bot_data["conn"]` (see
main.py) — one connection for the process, which is fine because
python-telegram-bot runs handlers on a single event loop.

Every handler starts with db.ensure_user, which refreshes `last_active`, so
the DAU number in /stats counts anyone who did anything — not just anyone who
answered.
"""

from __future__ import annotations

import logging

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from . import attribution, config, db, render

log = logging.getLogger(__name__)

WELCOME = (
    "<b>One NEET PG question a day.</b>\n\n"
    "Tap an option and you get the answer, the reasoning, and a way to keep "
    "going. Here's today's:"
)

HELP = (
    "/question — today's question\n"
    "/score — how you're doing\n"
    "/stop — stop the daily question\n"
    "/help — this list"
)

NO_QUESTION = "No question is scheduled yet. Check back tomorrow."


def _conn(context: ContextTypes.DEFAULT_TYPE):
    return context.application.bot_data["conn"]


async def _serve_current_question(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Send whatever question is live today, or say there isn't one."""
    conn = _conn(context)
    user_id = update.effective_user.id
    row = db.current_question(conn)
    if row is None:
        await update.effective_message.reply_text(NO_QUESTION)
        return

    await update.effective_message.reply_text(
        render.question_text(row),
        parse_mode=ParseMode.HTML,
        reply_markup=render.answer_keyboard(row["question_id"]),
    )
    # Deduped per (user, question): a second /start on the same day re-sends the
    # message but does not inflate the served count.
    db.log_event(conn, user_id, "question_served", question_id=row["question_id"])


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/start, optionally carrying an attribution payload from a deep link.

    `t.me/<bot>?start=tg_group1` arrives as context.args == ["tg_group1"]. The
    payload is only consulted when the user row is created; see db.upsert_user.
    """
    user = update.effective_user
    if user is None:
        return

    payload = context.args[0] if context.args else None
    source_channel = attribution.normalize_source(payload)
    conn = _conn(context)

    created = db.upsert_user(conn, user.id, user.username, source_channel)
    db.log_event(conn, user.id, "start")
    log.info(
        "start user_id=%s source_channel=%s new=%s payload=%r",
        user.id,
        source_channel if created else db.get_user(conn, user.id)["source_channel"],
        created,
        payload,
    )

    await update.effective_message.reply_text(WELCOME, parse_mode=ParseMode.HTML)
    await _serve_current_question(update, context)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    db.ensure_user(_conn(context), user.id, user.username)
    await update.effective_message.reply_text(HELP)


async def question(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/question — re-send today's question, for anyone who lost the message."""
    user = update.effective_user
    db.ensure_user(_conn(context), user.id, user.username)
    await _serve_current_question(update, context)


async def answer(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Inline-keyboard callback: `ans:<question_id>:<option>`."""
    query = update.callback_query
    if query is None or not query.data:
        return

    try:
        _, question_id, chosen = query.data.split(":", 2)
    except ValueError:
        log.warning("malformed callback data: %r", query.data)
        await query.answer()
        return

    conn = _conn(context)
    user_id = update.effective_user.id
    db.ensure_user(conn, user_id, update.effective_user.username)
    row = db.get_question(conn, question_id)
    if row is None:
        # The question was deleted from the bank after being sent.
        await query.answer("That question is no longer available.", show_alert=True)
        await query.edit_message_reply_markup(reply_markup=None)
        return

    is_correct = chosen == row["correct_option"]
    first_time = db.log_event(
        conn,
        user_id,
        "answer_submitted",
        question_id=question_id,
        is_correct=is_correct,
    )
    if not first_time:
        # The keyboard is gone after the first tap, but an old message still in
        # someone's scrollback can be tapped again. Say so; change nothing.
        await query.answer("You already answered this one.")
        return

    await query.answer()
    user = db.get_user(conn, user_id)
    await query.edit_message_text(
        render.answer_text(row, chosen, is_correct),
        parse_mode=ParseMode.HTML,
        reply_markup=render.cta_markup(
            config.cta_url(),
            user_id=user_id,
            source_channel=(
                user["source_channel"] if user else attribution.DEFAULT_SOURCE
            ),
            question_id=question_id,
        ),
    )


async def score(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    conn = _conn(context)
    user_id = update.effective_user.id
    db.ensure_user(conn, user_id, update.effective_user.username)

    correct, total = db.user_score(conn, user_id)
    if total == 0:
        await update.effective_message.reply_text("No answers yet. /question to start.")
        return
    await update.effective_message.reply_text(
        f"{correct}/{total} correct ({round(100 * correct / total)}%)."
    )


async def stop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    conn = _conn(context)
    user = update.effective_user
    db.ensure_user(conn, user.id, user.username)
    db.deactivate_user(conn, user.id)
    await update.effective_message.reply_text(
        "Stopped — no more daily questions. Your answers are kept;"
        " /start brings you back."
    )


async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Admin-only. Silent for everyone else: no hint that the command exists."""
    user_id = update.effective_user.id if update.effective_user else None
    if not config.is_admin(user_id):
        return
    db.ensure_user(_conn(context), user_id, update.effective_user.username)
    await update.effective_message.reply_text(
        render.stats_text(db.stats(_conn(context))), parse_mode=ParseMode.HTML
    )


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.exception("handler error", exc_info=context.error)
