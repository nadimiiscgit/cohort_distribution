# cohort_distribution

Distribution infrastructure for the cohort: a Telegram bot that delivers
questions, the attribution tooling that tells us where users came from,
and the landing page that points at the bot.

**Scope boundary.** This repo is distribution only. The product application
lives in its own repository and is never imported, vendored, or referenced
here. If a change requires reaching into the app, it belongs in the app repo,
not this one. The only thing crossing the boundary is a CSV of questions,
handed over as data.

## Layout

```
bot/          Telegram bot — commands, question delivery, attribution capture
scripts/      Operational scripts — seed, daily, broadcast, verify, backup, attribution
deploy/       systemd unit, cron entries, deploy script
landing/      Static landing page (no build step)
data/         Question CSVs and the SQLite database — gitignored except sample.csv
tests/        Standard-library unittest suite, no dependencies
```

`CLAUDE.md` is the short version for agents: the hard rules, the invariants
the tests protect, and how to get set up.

## Data model

Four tables in one SQLite file at `DB_PATH`. No ORM, no migrations — the
schema lives in `bot/db.py` and is applied with `CREATE TABLE IF NOT EXISTS`
on every start.

```
users        user_id, username, first_seen, source_channel, last_active, is_active
events       event_id, user_id, event_type, question_id, is_correct, created_at
questions    question_id, subject, stem, option_a..option_d, correct_option,
             explanation, scheduled_date
link_clicks  day, source_channel, clicks
```

`event_type` is one of `start`, `question_served`, `answer_submitted`,
`cta_clicked`. `events` is append-only and is the only thing `/stats` reads,
so a reported number can never drift from what actually happened.

`link_clicks` is the one table outside that rule, and it is separate for a
reason: a landing-page click has no user — it happens before Telegram is
involved — so it cannot be an event without making `events.user_id` nullable
and weakening the only thing that table guarantees. It is a daily tally rather
than a log, one row per (day, channel), which is also what makes re-importing
a log idempotent and means no per-visitor row is ever stored.

Two partial unique indexes do the idempotency work: one answer per
(user, question), and one serve per (user, question). Both are enforced by
storage rather than by handler convention, because the first is what stops a
double-tap double-counting and the second is what makes a cron retry safe.

All timestamps are UTC, and so is SQLite's `date('now')`, so day boundaries
compare without a timezone library. `TIMEZONE` in `.env` is for humans reading
logs.

## Running locally

Requires Python 3.11+ and a bot token from [@BotFather](https://t.me/BotFather).

```bash
git clone https://github.com/nadimiiscgit/cohort_distribution.git
cd cohort_distribution

python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
$EDITOR .env                       # at minimum: token and bot username

# --schedule-from puts the sample questions on consecutive days starting today,
# so there is something live to answer immediately.
python scripts/seed.py data/sample.csv --schedule-from "$(date -u +%F)"
python scripts/verify.py           # should report 0 failures
python -m bot.main
```

Message your bot on Telegram and send `/start`. Long polling means no tunnel,
no public URL, and no webhook setup — a laptop behind NAT works fine.

Talk to a throwaway bot locally, not the production one: two processes polling
the same token will steal updates from each other.

To exercise the daily send without waiting for cron:

```bash
python scripts/daily.py --dry-run       # who would get what, nothing sent
python scripts/daily.py                 # sends today's question
```

Only `BOT_TOKEN` and `TELEGRAM_BOT_USERNAME` are needed to get a bot talking.
Set `CTA_URL` too or answered questions come back without a button, which is
the one thing this experiment exists to measure. Set `ADMIN_ID` to your own
Telegram user id or `/stats` answers nobody — the bot logs the id on `/start`.

### Commands

| Command | Who | What |
| --- | --- | --- |
| `/start [payload]` | anyone | Subscribe, record the deep-link channel, send today's question |
| `/question` | anyone | Re-send today's question |
| `/score` | anyone | Correct / answered |
| `/stop` | anyone | Stop the daily question; answers are kept |
| `/stats` | admin | Users, retention, and the per-channel funnel |

The admin is the single Telegram user id in `ADMIN_ID`. Non-admins get silence
from `/stats`, not an error — no reason to advertise that it exists.

### What a user sees

`/start` creates the user row, stamps the channel they arrived from, and sends
the question scheduled for today (falling back to the most recent scheduled
one, so someone joining mid-week is not told to come back later). The question
arrives with four inline buttons. Tapping one records the answer, replaces the
message with the verdict and the explanation, and puts the CTA button
underneath.

Answers are recorded once per question: the keyboard disappears after the
first tap, but an old message further up the chat can still be tapped, and
that second tap changes nothing.

## Attribution

Every acquisition channel gets its own code. Mint a link:

```bash
python scripts/attribution.py link tg_group1 --landing
# https://t.me/your_bot?start=tg_group1
# https://example.com/?s=tg_group1
```

Either link works. The Telegram one sends people straight to the bot; the
landing one gives them a page first and forwards the code into the deep link.
The bot records the code as `source_channel` on the **first** `/start` and
never overwrites it, so a user's origin stays stable even if they later arrive
through a different campaign. No payload at all records `direct`.

Read it back:

```bash
python scripts/attribution.py report
```

`clicks` counts landing-page hits, `users` counts distinct people first seen on
that channel, `served` those sent at least one question, and `answered` those
who tapped an option. `join%` — users per click — is the column that separates
a channel with a small, well-matched audience from one with a large,
indifferent one, and `ans%` says whether the people it sent actually wanted
this.

Channels that were clicked but produced no user still get a row. A link with
five hundred clicks and nothing to show for it is the most useful line in the
table, and it has no user to be found by.

### Counting clicks

`clicks` and `join%` stay empty until the click importer has run. It reads the
`?s=<code>` hits out of the web server's own access log — there is no script on
the landing page and no third party involved:

```bash
python scripts/import_clicks.py                          # dry run
python scripts/import_clicks.py --write                  # store
python scripts/import_clicks.py '/var/log/nginx/access.log*' --write
```

Set `ACCESS_LOG_PATH` in `.env` and it runs hourly from cron. Clicks are stored
in `link_clicks` as one tally per (day, channel) rather than one row per
visitor: re-running replaces a day's tally instead of adding to it, so a missed
hour catches itself up and a double run is harmless — and nothing
visitor-identifying is kept at all. If a rotated log would make a day's count
go *down*, it refuses and says so, because losing counts quietly is worse than
not writing; `--force` overrides that.

Link-preview fetchers are excluded. Telegram, WhatsApp and Slack fetch every
URL that passes through them, so without filtering, pasting a campaign link
into a channel registers a click before any human has seen it — and it would
skew hardest in exactly the channel we most want to measure. The run prints how
many hits it dropped, which is worth glancing at: an implausible ratio means
the filter needs a new user-agent token.

Click-through on the CTA is measured at the destination, not here: Telegram
sends no update when a URL button is tapped, so the bot stamps `uid`, `src`,
and `qid` onto `CTA_URL` and the landing side joins them back. `NOTES.md`
records what it would take to log clicks first-party instead.

There is no third-party analytics anywhere in this repo, and the landing page
loads nothing from an external origin. Attribution is a query against our own
database and a log we already write.

### Checking a link before you spend on it

Attribution is write-once. A user who arrives with no source code can never be
attributed afterwards, because nothing was recorded to work back from — so the
time to find a broken link is before the campaign, not after.

```bash
python scripts/attribution.py link tg_group1
python scripts/attribution.py link insta_bio
# ... click each link from a fresh Telegram account, then:
python scripts/attribution.py report
```

`link` prints the exact deep link to test; `report` shows what landed, per
channel. Codes go through the same normalisation the bot applies to the inbound
payload, so the URL printed is character-for-character the one that has to
appear in the report. Every channel in the database is listed, so a tag that
never shows up — or one you did not expect — is where a typo'd tag or a
redirect that ate the `?start=` payload shows up.

```bash
python scripts/attribution_guard.py
```

Exits non-zero if any user's `source_channel` is empty or skipped normalisation
(which splits one channel across two rows in the report), or if an event points
at a user row that is gone — `events` holds no source of its own, so that
activity can never be attributed again. A missing database is a failure rather
than a pass. Run it before a launch, or from cron next to `verify.py`.

`scripts/CHECKLIST.md` is the manual walkthrough behind all of this: mint a
link, put a real shortener in front of it, click from a fresh account, confirm
the code landed, and delete the test rows afterwards.

## Loading questions

Real question data is **never** committed. `data/sample.csv` documents the
format; see `data/README.md` for the column reference.

```bash
python scripts/seed.py data/questions.csv --check   # validate only
python scripts/seed.py data/questions.csv           # load
python scripts/seed.py data/questions.csv --schedule-from 2026-08-11
```

Loading is idempotent and keyed on `question_id`: re-running an edited CSV
updates rows in place. Validation runs over the whole file first, so a
malformed row fails the batch rather than half-loading it.

`scheduled_date` is what makes a question the question of the day — one per
date, and the loader refuses a file that puts two on the same day. Leave the
column blank and pass `--schedule-from` to lay rows out on consecutive days in
file order.

## The daily question

```bash
python scripts/daily.py --dry-run
python scripts/daily.py
```

Cron-triggered, from `deploy/crontab.example` at `BROADCAST_HOUR`. It can only
ever send the question whose `scheduled_date` is today — there is no free-text
mode — and nothing scheduled for today means nothing is sent.

Safe to run twice. Recipients are the active users with no `question_served`
event for that question, so a retry after a partial failure reaches exactly
the people who were missed and a double-fire reaches nobody. Users who have
blocked or deleted the bot come back as `Forbidden`, are marked inactive, and
the run carries on.

## Broadcasting

```bash
python scripts/broadcast.py --text "New questions are up."          # dry run
python scripts/broadcast.py --text "New questions are up." --send   # delivers
```

This is the ad-hoc announcement path, separate from the daily question. Dry run
is the default. It reaches every active user with arbitrary text and cannot be
recalled, so `--send` is required, `DRY_RUN=true` in `.env` overrides it, and it
is deliberately absent from cron. Users who have blocked the bot are
deactivated automatically when their send returns `Forbidden`.

## Stats

`/stats` in the chat, admin only. It prints, all UTC:

- **total / active users** — active means they have not sent `/stop` and have
  not blocked the bot
- **new today** — user rows whose `first_seen` is today
- **DAU** — distinct users with any event today, not just answers
- **answers** — answers submitted today
- **D1 / D7 return rate** — of the users whose day N has *fully elapsed*, the
  share who had any event on the calendar day exactly N days after they first
  appeared. Exactly-day-N, not within-N-days: a within-N figure conflates D1
  into D7 and both drift upward forever. Someone who signed up this morning is
  not in the D1 cohort at all, so a good launch day cannot depress the number.
  Small cohorts make these numbers noisy — `n/a` means no cohort is old enough
  yet.
- **by source_channel** — users, active, served, and answered per channel

The same funnel, with a `--since` filter and CSV output, is available outside
Telegram:

```bash
python scripts/attribution.py report --since 2026-08-01 --csv
```

## Deploying

Target: one small Linux box, systemd for the process, cron for the chores.

```bash
# once, as root
useradd --system --home /srv/cohort_distribution cohort
git clone https://github.com/nadimiiscgit/cohort_distribution.git /srv/cohort_distribution
chown -R cohort:cohort /srv/cohort_distribution
mkdir -p /var/log/cohort && chown cohort:cohort /var/log/cohort

# so the hourly click import can read the web server's access log
usermod -aG adm cohort

cp /srv/cohort_distribution/deploy/cohort-bot.service /etc/systemd/system/
systemctl daemon-reload

# as the service user
sudo -u cohort cp /srv/cohort_distribution/.env.example /srv/cohort_distribution/.env
sudo -u cohort $EDITOR /srv/cohort_distribution/.env
sudo -u cohort chmod 600 /srv/cohort_distribution/.env
sudo -u cohort crontab /srv/cohort_distribution/deploy/crontab.example

systemctl enable --now cohort-bot
```

Every deploy after that:

```bash
sudo -u cohort /srv/cohort_distribution/deploy/deploy.sh
```

which backs up the database, fast-forwards the branch, syncs the venv, runs
`verify.py`, renders `landing/dist/`, and restarts the service — failing before
the restart if verification does not pass.

The landing page is a single self-contained `index.html` — CSS inline, no
images, no external requests — served from `landing/dist/`. It is not served
from `landing/` directly because the file carries a `__BOT_USERNAME__`
placeholder that the deploy script substitutes from `.env`; that keeps a
deploy-specific value out of the repo.

```bash
journalctl -u cohort-bot -f          # logs
systemctl status cohort-bot          # state
python scripts/verify.py --check-telegram
```

## Backups

`scripts/backup.py` runs nightly from cron. It uses SQLite's online backup API
rather than copying the file, so snapshots are consistent without stopping the
bot, and it prunes anything older than `BACKUP_RETENTION_DAYS`.

```bash
python scripts/backup.py --list
```

Snapshots contain subscriber user IDs. `backups/` is gitignored; if you copy
snapshots off the box, treat them as personal data.

To restore, stop the service, move the snapshot over `DB_PATH`, delete
any stale `-wal`/`-shm` siblings, and start it again.

## Testing

```bash
python3 -m unittest discover -s tests
```

No install, no plugins, no config — the suite is standard-library `unittest`
and runs on a bare clone in about two tenths of a second. It covers the
invariants that are easy to break by accident: first-touch attribution never
being overwritten, a question being served once per user, an answer being
recorded once, `/score` never undoing a `/stop`, the D1/D7 cohort maths, a
malformed CSV loading nothing at all, pruning only ever deleting files it
named itself, and re-importing a log never inflating a click tally.

The eleven `bot/render.py` tests skip on a bare clone, because `render.py` is
the only non-script module that imports `python-telegram-bot`. Everything else
runs with nothing installed.

`unittest` over pytest is the same call as everywhere else here — pytest is
nicer to write, but nicer is convenience, and the rule below does not accept
convenience as a justification.

This complements `scripts/verify.py` rather than replacing it: the tests check
that the code behaves, `verify.py` checks that a given deploy is configured and
populated correctly. Run both before pushing.

## Dependencies

**One runtime dependency**, pinned in `requirements.txt`:

- **`python-telegram-bot==21.6`** — the Bot API is a plain HTTPS interface and
  the polling loop is short enough to hand-roll, but retry-after handling,
  inline-keyboard callbacks, update deduplication, and graceful shutdown are
  not. This library is the well-maintained implementation of exactly that
  surface, and writing it ourselves would be more code to own than the bot.

Everything else is standard library: `sqlite3` for storage, `csv` for seeding,
`argparse` for the scripts, `urllib` for links, `shutil`/`pathlib` for backups.
Environment loading is a twenty-line parser in `bot/config.py` rather than
`python-dotenv`, and the landing page has no build step, no framework, and no
external requests.

**Adding a dependency:** justify it in this section — what it does, and why
the standard library is not enough. If the answer is convenience rather than
correctness, the answer is no. Things previously considered and rejected are
logged in `NOTES.md`.

## Things that must never be committed

- `.env` or any real token
- `*.db` — the database holds subscriber user IDs
- Real question CSVs (`data/*.csv` other than `sample.csv`)
- Backup snapshots

`.gitignore` covers all four and `scripts/verify.py` checks `git ls-files` for
violations on every run, so cron catches a mistake the morning after.

## Contributing

Single branch, `main`. Small commits, present-tense messages that say what
changed and why. Run `python scripts/verify.py` before pushing.
