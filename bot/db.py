"""SQLite storage for users, questions, and the event log.

One file, no ORM. Every function takes an explicit connection so scripts can
share a transaction and tests can point at a temp file.

Three tables and nothing else. `events` is the only append-only table and the
only thing /stats reads: every number the experiment reports is a query over
it, so a metric can never drift from what actually happened.

All timestamps are UTC ISO-8601 strings. SQLite's `date('now')` is also UTC,
so day-boundary comparisons stay consistent without a timezone library.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

from . import config

# The four things that can happen to a user, in the order they happen.
EVENT_TYPES = ("start", "question_served", "answer_submitted", "cta_clicked")

# What source_channel says when there was no deep-link payload. Matches both
# the column default below and attribution.DEFAULT_SOURCE; a test pins them
# together so the three cannot drift apart.
DEFAULT_SOURCE_CHANNEL = "direct"

SCHEMA = """
PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS users (
    user_id        INTEGER PRIMARY KEY,
    username       TEXT,
    first_seen     TEXT NOT NULL,
    source_channel TEXT NOT NULL DEFAULT 'direct',
    last_active    TEXT,
    is_active      INTEGER NOT NULL DEFAULT 1
);

CREATE INDEX IF NOT EXISTS idx_users_active ON users(is_active);
CREATE INDEX IF NOT EXISTS idx_users_source ON users(source_channel);

CREATE TABLE IF NOT EXISTS questions (
    question_id    TEXT PRIMARY KEY,
    subject        TEXT NOT NULL,
    stem           TEXT NOT NULL,
    option_a       TEXT NOT NULL,
    option_b       TEXT NOT NULL,
    option_c       TEXT NOT NULL,
    option_d       TEXT NOT NULL,
    correct_option TEXT NOT NULL CHECK (correct_option IN ('A','B','C','D')),
    explanation    TEXT,
    scheduled_date TEXT
);

CREATE INDEX IF NOT EXISTS idx_questions_scheduled ON questions(scheduled_date);

CREATE TABLE IF NOT EXISTS events (
    event_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL,
    event_type  TEXT NOT NULL CHECK (event_type IN
                    ('start','question_served','answer_submitted','cta_clicked')),
    question_id TEXT,
    is_correct  INTEGER,
    created_at  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_events_user ON events(user_id, created_at);
CREATE INDEX IF NOT EXISTS idx_events_type ON events(event_type, created_at);

-- One answer per user per question, enforced by storage rather than by handler
-- convention: an inline keyboard stays tappable after the first tap, and a
-- double-tap must not double-count in the accuracy numbers.
CREATE UNIQUE INDEX IF NOT EXISTS idx_events_one_answer_per_question
    ON events(user_id, question_id) WHERE event_type = 'answer_submitted';

-- The same question is never served to the same user twice, so a cron rerun
-- is a no-op instead of a second copy in everyone's chat.
CREATE UNIQUE INDEX IF NOT EXISTS idx_events_one_serve_per_question
    ON events(user_id, question_id) WHERE event_type = 'question_served';
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def connect(path: Path | None = None) -> sqlite3.Connection:
    db_path = path or config.database_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=15)
    conn.row_factory = sqlite3.Row
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


# --------------------------------------------------------------------------
# Users
# --------------------------------------------------------------------------


def ensure_user(
    conn: sqlite3.Connection,
    user_id: int,
    username: str | None = None,
    source_channel: str = DEFAULT_SOURCE_CHANNEL,
) -> bool:
    """Make sure a user row exists and refresh last_active. True if newly created.

    `source_channel` is written on first touch and never again. Someone who
    finds us through a Telegram group, blocks the bot, then comes back through
    an Instagram link six weeks later still counts for the group — otherwise
    the last campaign to touch a user would quietly claim every earlier one.

    Deliberately does not change `is_active`: this runs on every interaction,
    and checking your /score must not undo your /stop. Only /start does that.
    """
    now = utcnow()
    row = conn.execute(
        "SELECT user_id FROM users WHERE user_id = ?", (user_id,)
    ).fetchone()

    if row is None:
        conn.execute(
            "INSERT INTO users (user_id, username, first_seen, source_channel,"
            " last_active, is_active) VALUES (?, ?, ?, ?, ?, 1)",
            (user_id, username, now, source_channel, now),
        )
        conn.commit()
        return True

    # Note the absent source_channel: username and activity refresh, origin does not.
    conn.execute(
        "UPDATE users SET username = COALESCE(?, username), last_active = ?"
        " WHERE user_id = ?",
        (username, now, user_id),
    )
    conn.commit()
    return False


def upsert_user(
    conn: sqlite3.Connection,
    user_id: int,
    username: str | None,
    source_channel: str,
) -> bool:
    """/start: ensure_user, plus resubscribe anyone who had stopped."""
    created = ensure_user(conn, user_id, username, source_channel)
    if not created:
        conn.execute("UPDATE users SET is_active = 1 WHERE user_id = ?", (user_id,))
        conn.commit()
    return created


def deactivate_user(conn: sqlite3.Connection, user_id: int) -> None:
    conn.execute("UPDATE users SET is_active = 0 WHERE user_id = ?", (user_id,))
    conn.commit()


def get_user(conn: sqlite3.Connection, user_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone()


def active_users(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM users WHERE is_active = 1 ORDER BY first_seen"
    ).fetchall()


def users_awaiting(conn: sqlite3.Connection, question_id: str) -> list[sqlite3.Row]:
    """Active users who have not been served this question yet."""
    return conn.execute(
        "SELECT u.* FROM users u"
        " WHERE u.is_active = 1"
        "   AND NOT EXISTS (SELECT 1 FROM events e"
        "                   WHERE e.user_id = u.user_id"
        "                     AND e.event_type = 'question_served'"
        "                     AND e.question_id = ?)"
        " ORDER BY u.first_seen",
        (question_id,),
    ).fetchall()


# --------------------------------------------------------------------------
# Events
# --------------------------------------------------------------------------


def log_event(
    conn: sqlite3.Connection,
    user_id: int,
    event_type: str,
    question_id: str | None = None,
    is_correct: bool | None = None,
) -> bool:
    """Append one event. Returns False if a uniqueness rule swallowed it.

    `answer_submitted` and `question_served` are unique per (user, question);
    everything else always appends. Callers use the return value to decide
    whether to reply — a repeat tap logs nothing and should say nothing.
    """
    if event_type not in EVENT_TYPES:
        raise ValueError(f"unknown event_type {event_type!r}")
    cur = conn.execute(
        "INSERT OR IGNORE INTO events (user_id, event_type, question_id, is_correct,"
        " created_at) VALUES (?, ?, ?, ?, ?)",
        (
            user_id,
            event_type,
            question_id,
            None if is_correct is None else int(is_correct),
            utcnow(),
        ),
    )
    conn.commit()
    return cur.rowcount > 0


def user_score(conn: sqlite3.Connection, user_id: int) -> tuple[int, int]:
    """(correct, answered) for one user."""
    row = conn.execute(
        "SELECT COUNT(*) AS total, COALESCE(SUM(is_correct), 0) AS correct"
        " FROM events WHERE user_id = ? AND event_type = 'answer_submitted'",
        (user_id,),
    ).fetchone()
    return row["correct"], row["total"]


# --------------------------------------------------------------------------
# Questions
# --------------------------------------------------------------------------

QUESTION_COLUMNS: Sequence[str] = (
    "question_id",
    "subject",
    "stem",
    "option_a",
    "option_b",
    "option_c",
    "option_d",
    "correct_option",
    "explanation",
    "scheduled_date",
)


def upsert_questions(
    conn: sqlite3.Connection, rows: Iterable[dict[str, Any]]
) -> tuple[int, int]:
    """Insert or update questions keyed on question_id. Returns (inserted, updated)."""
    inserted = updated = 0
    for row in rows:
        exists = conn.execute(
            "SELECT 1 FROM questions WHERE question_id = ?", (row["question_id"],)
        ).fetchone()
        conn.execute(
            "INSERT INTO questions"
            " (question_id, subject, stem, option_a, option_b, option_c, option_d,"
            "  correct_option, explanation, scheduled_date)"
            " VALUES (:question_id, :subject, :stem, :option_a, :option_b, :option_c,"
            "         :option_d, :correct_option, :explanation, :scheduled_date)"
            " ON CONFLICT(question_id) DO UPDATE SET"
            "  subject=excluded.subject, stem=excluded.stem,"
            "  option_a=excluded.option_a, option_b=excluded.option_b,"
            "  option_c=excluded.option_c, option_d=excluded.option_d,"
            "  correct_option=excluded.correct_option,"
            "  explanation=excluded.explanation,"
            "  scheduled_date=excluded.scheduled_date",
            row,
        )
        if exists:
            updated += 1
        else:
            inserted += 1
    conn.commit()
    return inserted, updated


def get_question(conn: sqlite3.Connection, question_id: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM questions WHERE question_id = ?", (question_id,)
    ).fetchone()


def question_for_date(
    conn: sqlite3.Connection, on_date: str | None = None
) -> sqlite3.Row | None:
    """The question scheduled for exactly this date, or None.

    Used by the daily send: no row for today means nobody gets messaged, which
    is the right failure mode for an empty schedule.
    """
    return conn.execute(
        "SELECT * FROM questions WHERE scheduled_date = ?"
        " ORDER BY question_id LIMIT 1",
        (on_date or today(),),
    ).fetchone()


def current_question(
    conn: sqlite3.Connection, on_date: str | None = None
) -> sqlite3.Row | None:
    """The most recent question scheduled on or before this date.

    Used by /start and /question: someone who joins on a Wednesday with nothing
    scheduled until Friday should still get a question, not an apology.
    Unscheduled questions are ignored — a NULL scheduled_date means "loaded but
    not in the rotation".
    """
    return conn.execute(
        "SELECT * FROM questions WHERE scheduled_date IS NOT NULL"
        " AND scheduled_date <= ? ORDER BY scheduled_date DESC, question_id LIMIT 1",
        (on_date or today(),),
    ).fetchone()


def question_count(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) AS c FROM questions").fetchone()["c"]


# --------------------------------------------------------------------------
# Stats — everything /stats prints
# --------------------------------------------------------------------------


def return_rate(conn: sqlite3.Connection, day: int) -> tuple[int, int]:
    """Classic DN retention: (returned, cohort) for users whose day N has passed.

    Cohort = users signed up long enough ago that their day N is fully over, so
    a user who joined this morning never drags D1 down. Returned = had any event
    on the calendar day exactly N days after they first appeared. Exactly-day-N,
    not within-N-days: within-N conflates D1 and D7 into the same number.
    """
    row = conn.execute(
        "SELECT COUNT(*) AS cohort,"
        "       COALESCE(SUM(CASE WHEN EXISTS ("
        "           SELECT 1 FROM events e WHERE e.user_id = u.user_id"
        "             AND date(e.created_at) = date(u.first_seen, ?)"
        "       ) THEN 1 ELSE 0 END), 0) AS returned"
        " FROM users u WHERE date(u.first_seen) <= date('now', ?)",
        (f"+{day} day", f"-{day + 1} day"),
    ).fetchone()
    return row["returned"], row["cohort"]


def stats(conn: sqlite3.Connection) -> dict[str, Any]:
    """One snapshot of the experiment, as /stats prints it."""
    day = today()

    def scalar(sql: str, params: tuple[Any, ...] = ()) -> int:
        return conn.execute(sql, params).fetchone()[0]

    d1_returned, d1_cohort = return_rate(conn, 1)
    d7_returned, d7_cohort = return_rate(conn, 7)

    return {
        "total_users": scalar("SELECT COUNT(*) FROM users"),
        "active_users": scalar("SELECT COUNT(*) FROM users WHERE is_active = 1"),
        "new_today": scalar(
            "SELECT COUNT(*) FROM users WHERE date(first_seen) = ?", (day,)
        ),
        "dau": scalar(
            "SELECT COUNT(DISTINCT user_id) FROM events WHERE date(created_at) = ?",
            (day,),
        ),
        "answers_today": scalar(
            "SELECT COUNT(*) FROM events"
            " WHERE event_type = 'answer_submitted' AND date(created_at) = ?",
            (day,),
        ),
        "d1": (d1_returned, d1_cohort),
        "d7": (d7_returned, d7_cohort),
        "by_source": source_funnel(conn),
    }


def source_funnel(
    conn: sqlite3.Connection, since: str | None = None
) -> list[sqlite3.Row]:
    """Users per source_channel, and how far down the funnel each cohort got.

    `since` is an ISO date filtering on first_seen, so a campaign can be read
    without the pre-launch users muddying the rates.
    """
    where = "WHERE date(u.first_seen) >= ?" if since else ""
    params = (since,) if since else ()
    return conn.execute(
        "SELECT u.source_channel AS source_channel,"
        "       COUNT(*) AS users,"
        "       COALESCE(SUM(u.is_active), 0) AS active,"
        "       COALESCE(SUM(CASE WHEN EXISTS ("
        "           SELECT 1 FROM events e WHERE e.user_id = u.user_id"
        "             AND e.event_type = 'question_served'"
        "       ) THEN 1 ELSE 0 END), 0) AS served,"
        "       COALESCE(SUM(CASE WHEN EXISTS ("
        "           SELECT 1 FROM events e WHERE e.user_id = u.user_id"
        "             AND e.event_type = 'answer_submitted'"
        "       ) THEN 1 ELSE 0 END), 0) AS answered"
        " FROM users u"
        f" {where}"
        " GROUP BY u.source_channel"
        " ORDER BY users DESC, u.source_channel",
        params,
    ).fetchall()
