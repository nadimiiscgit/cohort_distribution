"""Message formatting, shared by the bot process and the cron scripts.

Kept separate from handlers.py so `scripts/daily.py` can build exactly the
message a user would get from /start without importing handler wiring. Pure
functions: they take rows and return text or markup, and touch neither the
database nor the network.

All text is Telegram's HTML subset, so everything interpolated from the
question bank goes through html.escape first.
"""

from __future__ import annotations

import html

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from . import attribution

OPTIONS = ("A", "B", "C", "D")

# Button copy lives here; the destination it points at is CTA_URL.
CTA_LABEL = "Open the full question bank →"

# Callback payload prefix for the four answer buttons: ans:<question_id>:<A-D>.
ANSWER_PREFIX = "ans"


def question_text(row) -> str:
    """The question as first shown: subject, stem, four lettered options."""
    lines = [f"<b>{html.escape(row['subject'])}</b>", ""]
    lines.append(html.escape(row["stem"]))
    lines.append("")
    for opt in OPTIONS:
        lines.append(f"<b>{opt}.</b> {html.escape(row[f'option_{opt.lower()}'])}")
    return "\n".join(lines)


def answer_keyboard(question_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    opt, callback_data=f"{ANSWER_PREFIX}:{question_id}:{opt}"
                )
                for opt in OPTIONS
            ]
        ]
    )


def answer_text(row, chosen: str, is_correct: bool) -> str:
    """The question, resolved: what they picked, what was right, and why."""
    if is_correct:
        verdict = f"✅ <b>Correct — {chosen}.</b>"
    else:
        verdict = (
            f"❌ <b>You picked {chosen}. The answer is "
            f"{row['correct_option']}.</b>"
        )
    lines = [question_text(row), "", verdict]
    if row["explanation"]:
        lines += ["", html.escape(row["explanation"])]
    return "\n".join(lines)


def cta_markup(
    cta_url: str | None,
    *,
    user_id: int,
    source_channel: str,
    question_id: str | None = None,
) -> InlineKeyboardMarkup | None:
    """The button under an answered question. None when CTA_URL is unset."""
    if not cta_url:
        return None
    link = attribution.cta_link(
        cta_url,
        user_id=user_id,
        source_channel=source_channel,
        question_id=question_id,
    )
    return InlineKeyboardMarkup([[InlineKeyboardButton(CTA_LABEL, url=link)]])


def stats_text(snapshot: dict) -> str:
    """Render the /stats snapshot. Monospaced so the columns line up."""
    d1_returned, d1_cohort = snapshot["d1"]
    d7_returned, d7_cohort = snapshot["d7"]

    def rate(returned: int, cohort: int) -> str:
        if not cohort:
            return "  n/a  (no cohort old enough yet)"
        return f"{returned:>4}/{cohort:<4} ({round(100 * returned / cohort):>3}%)"

    lines = [
        "users",
        f"  total       {snapshot['total_users']:>6}",
        f"  active      {snapshot['active_users']:>6}",
        f"  new today   {snapshot['new_today']:>6}",
        f"  DAU         {snapshot['dau']:>6}",
        f"  answers     {snapshot['answers_today']:>6}",
        "",
        "return rate",
        f"  D1  {rate(d1_returned, d1_cohort)}",
        f"  D7  {rate(d7_returned, d7_cohort)}",
        "",
        "by source_channel",
    ]

    rows = snapshot["by_source"]
    if not rows:
        lines.append("  (no users yet)")
    else:
        labels = [r["source_channel"] for r in rows] + ["channel"]
        width = max(len(label) for label in labels)
        lines.append(
            f"  {'channel':<{width}}  {'users':>6} {'active':>7}"
            f" {'served':>7} {'answered':>9}"
        )
        for r in rows:
            lines.append(
                f"  {r['source_channel']:<{width}}  {r['users']:>6}"
                f" {r['active']:>7} {r['served']:>7} {r['answered']:>9}"
            )

    body = html.escape("\n".join(lines))
    return f"<b>Cohort stats</b> · UTC\n<pre>{body}</pre>"
