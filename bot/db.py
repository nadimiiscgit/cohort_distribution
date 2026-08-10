"""SQLite storage for subscribers, questions, deliveries, and attribution.

One file, no ORM. Every function takes an explicit connection so scripts can
share a transaction and tests can point at a temp file.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

from . import config

SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS subscribers (
    chat_id      INTEGER PRIMARY KEY,
    username     TEXT,
    first_name   TEXT,
    source       TEXT NOT NULL DEFAULT 'direct',
    active       INTEGER NOT NULL DEFAULT 1,
    joined_at    TEXT NOT NULL,
    left_at      TEXT,
    last_sent_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_subscribers_active ON subscribers(active);
CREATE INDEX IF NOT EXISTS idx_subscribers_source ON subscribers(source);

CREATE TABLE IF NOT EXISTS questions (
    id             TEXT PRIMARY KEY,
    subject        TEXT NOT NULL,
    year           INTEGER,
    stem           TEXT NOT NULL,
    option_a       TEXT NOT NULL,
    option_b       TEXT NOT NULL,
    option_c       TEXT NOT NULL,
    option_d       TEXT NOT NULL,
    correct_option TEXT NOT NULL CHECK (correct_option IN ('A','B','C','D')),
    explanation    TEXT,
    source_tag     TEXT,
    created_at     TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_questions_subject ON questions(subject);

CREATE TABLE IF NOT EXISTS deliveries (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id     INTEGER NOT NULL,
    question_id TEXT NOT NULL,
    sent_at     TEXT NOT NULL,
    channel     TEXT NOT NULL DEFAULT 'on_demand',
    UNIQUE (chat_id, question_id)
);

CREATE INDEX IF NOT EXISTS idx_deliveries_chat ON deliveries(chat_id, sent_at);

CREATE TABLE IF NOT EXISTS attempts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id     INTEGER NOT NULL,
    question_id TEXT NOT NULL,
    chosen      TEXT NOT NULL,
    is_correct  INTEGER NOT NULL,
    answered_at TEXT NOT NULL,
    UNIQUE (chat_id, question_id)
);

CREATE TABLE IF NOT EXISTS attribution_events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id    INTEGER,
    source     TEXT NOT NULL,
    event      TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_attribution_source ON attribution_events(source);

CREATE TABLE IF NOT EXISTS broadcasts (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    body       TEXT NOT NULL,
    sent_count INTEGER NOT NULL DEFAULT 0,
    fail_count INTEGER NOT NULL DEFAULT 0,
    started_at TEXT NOT NULL,
    finished_at TEXT
);
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(path: Path | None = None) -> sqlite3.Connection:
    db_path = path or config.database_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


# --------------------------------------------------------------------------
# Subscribers
# --------------------------------------------------------------------------


def upsert_subscriber(
    conn: sqlite3.Connection,
    chat_id: int,
    username: str | None,
    first_name: str | None,
    source: str,
) -> bool:
    """Register or reactivate a subscriber. Returns True if newly created.

    The original `source` is never overwritten: first touch wins, so a user who
    re-runs /start from a different link does not rewrite their own history.
    """
    row = conn.execute(
        "SELECT chat_id, active FROM subscribers WHERE chat_id = ?", (chat_id,)
    ).fetchone()
    now = utcnow()
    if row is None:
        conn.execute(
            "INSERT INTO subscribers (chat_id, username, first_name, source, active, joined_at)"
            " VALUES (?, ?, ?, ?, 1, ?)",
            (chat_id, username, first_name, source, now),
        )
        conn.commit()
        return True

    conn.execute(
        "UPDATE subscribers SET username = ?, first_name = ?, active = 1, left_at = NULL"
        " WHERE chat_id = ?",
        (username, first_name, chat_id),
    )
    conn.commit()
    return False


def deactivate_subscriber(conn: sqlite3.Connection, chat_id: int) -> None:
    conn.execute(
        "UPDATE subscribers SET active = 0, left_at = ? WHERE chat_id = ?",
        (utcnow(), chat_id),
    )
    conn.commit()


def active_subscribers(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM subscribers WHERE active = 1 ORDER BY joined_at"
    ).fetchall()


def touch_last_sent(conn: sqlite3.Connection, chat_id: int) -> None:
    conn.execute(
        "UPDATE subscribers SET last_sent_at = ? WHERE chat_id = ?", (utcnow(), chat_id)
    )
    conn.commit()


# --------------------------------------------------------------------------
# Questions
# --------------------------------------------------------------------------

QUESTION_COLUMNS: Sequence[str] = (
    "id",
    "subject",
    "year",
    "stem",
    "option_a",
    "option_b",
    "option_c",
    "option_d",
    "correct_option",
    "explanation",
    "source_tag",
)


def upsert_questions(conn: sqlite3.Connection, rows: Iterable[dict[str, Any]]) -> tuple[int, int]:
    """Insert or replace questions. Returns (inserted, updated)."""
    inserted = updated = 0
    now = utcnow()
    for row in rows:
        exists = conn.execute(
            "SELECT 1 FROM questions WHERE id = ?", (row["id"],)
        ).fetchone()
        conn.execute(
            "INSERT INTO questions"
            " (id, subject, year, stem, option_a, option_b, option_c, option_d,"
            "  correct_option, explanation, source_tag, created_at)"
            " VALUES (:id, :subject, :year, :stem, :option_a, :option_b, :option_c,"
            "         :option_d, :correct_option, :explanation, :source_tag, :created_at)"
            " ON CONFLICT(id) DO UPDATE SET"
            "  subject=excluded.subject, year=excluded.year, stem=excluded.stem,"
            "  option_a=excluded.option_a, option_b=excluded.option_b,"
            "  option_c=excluded.option_c, option_d=excluded.option_d,"
            "  correct_option=excluded.correct_option,"
            "  explanation=excluded.explanation, source_tag=excluded.source_tag",
            {**row, "created_at": now},
        )
        if exists:
            updated += 1
        else:
            inserted += 1
    conn.commit()
    return inserted, updated


def get_question(conn: sqlite3.Connection, question_id: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM questions WHERE id = ?", (question_id,)
    ).fetchone()


def next_question_for(
    conn: sqlite3.Connection, chat_id: int, subject: str | None = None
) -> sqlite3.Row | None:
    """Pick a random question this subscriber has not been sent yet."""
    params: list[Any] = [chat_id]
    subject_clause = ""
    if subject:
        subject_clause = " AND lower(q.subject) = lower(?)"
        params.append(subject)
    return conn.execute(
        "SELECT q.* FROM questions q"
        " WHERE q.id NOT IN (SELECT question_id FROM deliveries WHERE chat_id = ?)"
        f"{subject_clause}"
        " ORDER BY RANDOM() LIMIT 1",
        params,
    ).fetchone()


def question_count(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) AS c FROM questions").fetchone()["c"]


# --------------------------------------------------------------------------
# Deliveries & attempts
# --------------------------------------------------------------------------


def record_delivery(
    conn: sqlite3.Connection, chat_id: int, question_id: str, channel: str = "on_demand"
) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO deliveries (chat_id, question_id, sent_at, channel)"
        " VALUES (?, ?, ?, ?)",
        (chat_id, question_id, utcnow(), channel),
    )
    conn.commit()


def deliveries_today(conn: sqlite3.Connection, chat_id: int) -> int:
    return conn.execute(
        "SELECT COUNT(*) AS c FROM deliveries"
        " WHERE chat_id = ? AND date(sent_at) = date('now')",
        (chat_id,),
    ).fetchone()["c"]


def record_attempt(
    conn: sqlite3.Connection, chat_id: int, question_id: str, chosen: str, is_correct: bool
) -> bool:
    """Store an answer. Returns False if this question was already answered."""
    cur = conn.execute(
        "INSERT OR IGNORE INTO attempts (chat_id, question_id, chosen, is_correct, answered_at)"
        " VALUES (?, ?, ?, ?, ?)",
        (chat_id, question_id, chosen, 1 if is_correct else 0, utcnow()),
    )
    conn.commit()
    return cur.rowcount > 0


def subscriber_score(conn: sqlite3.Connection, chat_id: int) -> tuple[int, int]:
    row = conn.execute(
        "SELECT COUNT(*) AS total, COALESCE(SUM(is_correct), 0) AS correct"
        " FROM attempts WHERE chat_id = ?",
        (chat_id,),
    ).fetchone()
    return row["correct"], row["total"]


# --------------------------------------------------------------------------
# Attribution
# --------------------------------------------------------------------------


def record_attribution(
    conn: sqlite3.Connection, source: str, event: str, chat_id: int | None = None
) -> None:
    conn.execute(
        "INSERT INTO attribution_events (chat_id, source, event, created_at)"
        " VALUES (?, ?, ?, ?)",
        (chat_id, source, event, utcnow()),
    )
    conn.commit()


def attribution_summary(conn: sqlite3.Connection, since: str | None = None) -> list[sqlite3.Row]:
    """Per-source funnel: link opens, signups, still-active, answered at least once."""
    where = "WHERE s.joined_at >= ?" if since else ""
    params = (since,) if since else ()
    return conn.execute(
        "SELECT s.source AS source,"
        "       COUNT(*) AS signups,"
        "       SUM(s.active) AS active,"
        "       SUM(CASE WHEN a.n > 0 THEN 1 ELSE 0 END) AS engaged"
        " FROM subscribers s"
        " LEFT JOIN (SELECT chat_id, COUNT(*) AS n FROM attempts GROUP BY chat_id) a"
        "   ON a.chat_id = s.chat_id"
        f" {where}"
        " GROUP BY s.source ORDER BY signups DESC",
        params,
    ).fetchall()


def attribution_opens(conn: sqlite3.Connection) -> dict[str, int]:
    rows = conn.execute(
        "SELECT source, COUNT(*) AS c FROM attribution_events"
        " WHERE event = 'start' GROUP BY source"
    ).fetchall()
    return {r["source"]: r["c"] for r in rows}
