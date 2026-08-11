# cohort_distribution

Distribution infrastructure for the cohort: a Telegram bot that delivers
questions, the attribution tooling that tells us where subscribers came from,
and the landing page that points at the bot.

**Scope boundary.** This repo is distribution only. The product application
lives in its own repository and is never imported, vendored, or referenced
here. If a change requires reaching into the app, it belongs in the app repo,
not this one. The only thing crossing the boundary is a CSV of questions,
handed over as data.

## Layout

```
bot/          Telegram bot — commands, question delivery, attribution capture
scripts/      Operational scripts — seed, broadcast, verify, backup, attribution,
              click import
deploy/       systemd unit, cron entries, deploy script
landing/      Static landing page (no build step)
data/         Question CSVs and the SQLite database — gitignored except sample.csv
tests/        Standard-library unittest suite, no dependencies
```

`CLAUDE.md` is the short version for agents: the hard rules, the invariants
the tests protect, and how to get set up.

## Running locally

Requires Python 3.11+ and a bot token from [@BotFather](https://t.me/BotFather).

```bash
git clone https://github.com/nadimiiscgit/cohort_distribution.git
cd cohort_distribution

python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
$EDITOR .env                       # at minimum: token and bot username

python scripts/seed_questions.py data/sample.csv
python scripts/verify.py           # should report 0 failures
python -m bot.main
```

Message your bot on Telegram and send `/start`. Long polling means no tunnel,
no public URL, and no webhook setup — a laptop behind NAT works fine.

Talk to a throwaway bot locally, not the production one: two processes polling
the same token will steal updates from each other.

### Commands

| Command | Who | What |
| --- | --- | --- |
| `/start [source]` | anyone | Subscribe; records the deep-link source |
| `/question [subject]` | anyone | Next unseen question, optionally by subject |
| `/score` | anyone | Correct / attempted |
| `/stop` | anyone | Stop messages; answers are kept |
| `/stats` | admins | Subscriber counts and the attribution funnel |

Admins are the chat IDs listed in `ADMIN_CHAT_IDS`. Non-admins get silence
from `/stats`, not an error — no reason to advertise that it exists.

## Attribution

Every acquisition channel gets its own code. Mint a link:

```bash
python scripts/attribution.py link reddit-r-medicine --landing
# https://t.me/your_bot?start=reddit-r-medicine
# https://example.com/?s=reddit-r-medicine
```

Either link works. The Telegram one sends people straight to the bot; the
landing one gives them a page first and forwards the code into the deep link.
The bot records the code on first `/start` and never overwrites it, so a
subscriber's origin stays stable even if they later click a different link.

Read it back:

```bash
python scripts/attribution.py report
```

`clicks` counts landing-page hits, `opens` counts `/start` presses (including
returning users), `signups` counts distinct new subscribers, and `engaged`
counts those who answered at least one question. `conv%` — signups per click —
is the column that separates a channel with a small, well-matched audience
from one with a large, indifferent one.

Codes that were clicked but produced no subscriber still get a row. A link with
five hundred clicks and nothing to show for it is the most useful line in the
table, and it has no subscriber to be found by.

### Counting clicks

`clicks` and `conv%` stay empty until the click importer has run. It reads the
`?s=<code>` hits out of the web server's own access log — there is no script on
the landing page and no third party involved:

```bash
python scripts/import_clicks.py                          # dry run
python scripts/import_clicks.py --write                  # store
python scripts/import_clicks.py '/var/log/nginx/access.log*' --write
```

Set `ACCESS_LOG_PATH` in `.env` and it runs hourly from cron. It replaces each
date's rows rather than appending, so re-running is harmless and a missed hour
catches itself up. If a rotated log would make a day's count go *down*, it
refuses and tells you, because losing counts quietly is worse than not writing;
`--force` overrides that.

Link-preview fetchers are excluded. Telegram, WhatsApp and Slack fetch every
URL that passes through them, so without filtering, pasting a campaign link
into a channel registers a click before any human has seen it — and it would
skew hardest in exactly the channel we most want to measure. The run prints how
many hits it dropped, which is worth glancing at: an implausible ratio means
the filter needs a new user-agent token.

There is no third-party analytics anywhere in this repo, and the landing page
loads nothing from an external origin. Attribution is a query against our own
database and a log we already write.

## Loading questions

Real question data is **never** committed. `data/sample.csv` documents the
format; see `data/README.md` for the column reference.

```bash
python scripts/seed_questions.py data/questions.csv --check   # validate only
python scripts/seed_questions.py data/questions.csv           # load
```

Loading is idempotent and keyed on `id`: re-running an edited CSV updates rows
in place. Validation runs over the whole file first, so a malformed row fails
the batch rather than half-loading it.

## Broadcasting

```bash
python scripts/broadcast.py --text "New questions are up."          # dry run
python scripts/broadcast.py --text "New questions are up." --send   # delivers
```

Dry run is the default. A broadcast reaches every active subscriber and cannot
be recalled, so `--send` is required, `DRY_RUN=true` in `.env` overrides it,
and it is deliberately absent from cron. Subscribers who have blocked the bot
are deactivated automatically when their send returns `Forbidden`.

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

The landing page is served as static files from `landing/dist/`. It is not
built from `landing/` directly because `index.html` carries a
`__BOT_USERNAME__` placeholder that the deploy script substitutes from `.env`;
that keeps a deploy-specific value out of the repo.

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

Snapshots contain subscriber chat IDs. `backups/` is gitignored; if you copy
snapshots off the box, treat them as personal data.

To restore, stop the service, move the snapshot over `DATABASE_PATH`, delete
any stale `-wal`/`-shm` siblings, and start it again.

## Testing

```bash
python3 -m unittest discover -s tests
```

No install, no plugins, no config — the suite is standard-library `unittest`
and runs on a bare clone in about two tenths of a second. It covers the
invariants that are easy to break by accident: first-touch attribution never
being overwritten, a subscriber never seeing the same question twice, an
answer being recorded once, a malformed CSV loading nothing at all, pruning
only ever deleting files it named itself, and re-importing a log never
inflating a click count.

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
- `*.db` — the database holds subscriber chat IDs
- Real question CSVs (`data/*.csv` other than `sample.csv`)
- Backup snapshots

`.gitignore` covers all four and `scripts/verify.py` checks `git ls-files` for
violations on every run, so cron catches a mistake the morning after.

## Contributing

Single branch, `main`. Small commits, present-tense messages that say what
changed and why. Run `python scripts/verify.py` before pushing.
