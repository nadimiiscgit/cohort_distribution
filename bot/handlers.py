"""Command and callback handlers.

Handlers stay thin: parse the update, call into db.py, format a reply. The
SQLite connection lives in `application.bot_data["conn"]` (see main.py) — one
connection for the process, which is fine because python-telegram-bot runs
handlers on a single event loop.
"""

from __future__ import annotations

import html
import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from . import attribution, config, db

log = logging.getLogger(__name__)

OPTIONS = ("A", "B", "C", "D")

WELCOME = (
    "<b>Welcome to the cohort.</b>\n\n"
    "One question at a time, with the reasoning spelled out.\n\n"
    "/question — get a question\n"
    "/score — how you're doing\n"
    "/stop — stop receiving messages\n"
    "/help — this list"
)


def _conn(context: ContextTypes.DEFAULT_TYPE):
    return context.application.bot_data["conn"]


def _is_admin(chat_id: int) -> bool:
    return chat_id in config.admin_chat_ids()


def _question_markup(question_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(opt, callback_data=f"ans:{question_id}:{opt}")
                for opt in OPTIONS
            ]
        ]
    )


def _format_question(row) -> str:
    parts = [f"<b>{html.escape(row['subject'])}</b>"]
    if row["year"]:
        parts[0] += f" · {row['year']}"
    parts.append("")
    parts.append(html.escape(row["stem"]))
    parts.append("")
    for opt in OPTIONS:
        parts.append(f"<b>{opt}.</b> {html.escape(row[f'option_{opt.lower()}'])}")
    return "\n".join(parts)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/start, optionally carrying an attribution payload from a deep link."""
    chat = update.effective_chat
    user = update.effective_user
    if chat is None or user is None:
        return

    payload = context.args[0] if context.args else None
    source = attribution.normalize_source(payload)
    conn = _conn(context)

    db.record_attribution(conn, source=source, event="start", chat_id=chat.id)
    created = db.upsert_subscriber(
        conn,
        chat_id=chat.id,
        username=user.username,
        first_name=user.first_name,
        source=source,
    )
    log.info(
        "start chat_id=%s source=%s new=%s", chat.id, source, created
    )

    await update.effective_message.reply_text(WELCOME, parse_mode=ParseMode.HTML)
    if created:
        await send_question(update, context)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(WELCOME, parse_mode=ParseMode.HTML)


async def send_question(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/question — deliver one unseen question, respecting the daily limit."""
    chat = update.effective_chat
    if chat is None:
        return
    conn = _conn(context)

    limit = config.get_int("DAILY_QUESTION_LIMIT", 5)
    if db.deliveries_today(conn, chat.id) >= limit:
        await update.effective_message.reply_text(
            f"That's {limit} for today — come back tomorrow."
        )
        return

    subject = " ".join(context.args) if context.args else None
    row = db.next_question_for(conn, chat.id, subject=subject)
    if row is None and subject:
        await update.effective_message.reply_text(
            f"Nothing left in {subject}. Try /question with no subject."
        )
        return
    if row is None:
        await update.effective_message.reply_text(
            "You've seen every question we have. More are on the way."
        )
        return

    await update.effective_message.reply_text(
        _format_question(row),
        parse_mode=ParseMode.HTML,
        reply_markup=_question_markup(row["id"]),
    )
    db.record_delivery(conn, chat.id, row["id"])
    db.touch_last_sent(conn, chat.id)


async def answer(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Inline-keyboard callback: `ans:<question_id>:<option>`."""
    query = update.callback_query
    if query is None or not query.data:
        return
    await query.answer()

    try:
        _, question_id, chosen = query.data.split(":", 2)
    except ValueError:
        log.warning("malformed callback data: %r", query.data)
        return

    conn = _conn(context)
    row = db.get_question(conn, question_id)
    if row is None:
        await query.edit_message_reply_markup(reply_markup=None)
        return

    correct = row["correct_option"]
    is_correct = chosen == correct
    first_time = db.record_attempt(
        conn, query.message.chat_id, question_id, chosen, is_correct
    )
    if not first_time:
        return

    verdict = "✅ Correct." if is_correct else f"❌ Not quite — the answer is {correct}."
    body = [_format_question(row), "", verdict]
    if row["explanation"]:
        body += ["", html.escape(row["explanation"])]

    await query.edit_message_text(
        "\n".join(body), parse_mode=ParseMode.HTML, reply_markup=None
    )


async def score(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    if chat is None:
        return
    correct, total = db.subscriber_score(_conn(context), chat.id)
    if total == 0:
        await update.effective_message.reply_text(
            "No answers yet. /question to start."
        )
        return
    pct = round(100 * correct / total)
    await update.effective_message.reply_text(
        f"{correct}/{total} correct ({pct}%)."
    )


async def stop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    if chat is None:
        return
    db.deactivate_subscriber(_conn(context), chat.id)
    await update.effective_message.reply_text(
        "Stopped. Your answers are kept; /start brings you back."
    )


async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Admin-only: subscriber and question counts, plus the attribution funnel."""
    chat = update.effective_chat
    if chat is None or not _is_admin(chat.id):
        return
    conn = _conn(context)
    active = len(db.active_subscribers(conn))
    questions = db.question_count(conn)
    lines = [f"Active subscribers: {active}", f"Questions loaded: {questions}", ""]
    for row in db.attribution_summary(conn):
        lines.append(
            f"{row['source']}: {row['signups']} signups, "
            f"{row['active']} active, {row['engaged']} engaged"
        )
    await update.effective_message.reply_text("\n".join(lines))


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.exception("handler error", exc_info=context.error)
