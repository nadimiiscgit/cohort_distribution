# CLAUDE.md

Guidance for agents working in this repo. Read this before changing anything.

## What this repo is

Distribution infrastructure for the cohort: a Telegram bot that delivers
questions, attribution tooling, and a landing page. That is the whole remit.

```
bot/       Telegram bot — config, storage, handlers, attribution capture
scripts/   seed, broadcast, verify, backup, attribution report + link checks
deploy/    systemd unit, cron entries, deploy script
landing/   static landing page, no build step
data/      question CSVs + SQLite db — gitignored except sample.csv
tests/     stdlib unittest, no dependencies
```

## Hard rules

These are not style preferences. Breaking one is a defect.

1. **Never reference the product app.** It lives in a separate repository. Do
   not import it, vendor it, link to its internals, or add code that assumes
   it is present. The only thing that crosses the boundary is a CSV of
   questions, handed over as data. If a change needs the app, it belongs in
   the app repo.

2. **Never commit secrets or real data.** Not `.env`, not a token, not `*.db`
   (it holds subscriber chat IDs), not backups, not any `data/*.csv` other
   than `sample.csv`. `.gitignore` covers these and `scripts/verify.py` greps
   `git ls-files` for violations — run it before you commit.

3. **One runtime dependency.** `python-telegram-bot`, pinned in
   `requirements.txt`. Everything else is standard library, including the
   dotenv parser (`bot/config.py`) and the test suite (`unittest`, not
   pytest). To add a second, justify it in the README's Dependencies section:
   what it does and why the stdlib is insufficient. *Convenience is not
   sufficient.* Check `NOTES.md` first — it may already have been rejected.

4. **Broadcasts stay manual.** `scripts/broadcast.py` defaults to a dry run,
   requires `--send`, is vetoed by `DRY_RUN=true`, and is deliberately absent
   from cron. Do not add a scheduled or automatic broadcast path. A broadcast
   reaches every subscriber and cannot be recalled.

5. **Attribution stays first-party.** No analytics scripts, pixels, or
   external requests on the landing page. The funnel is a SQL query against
   our own database.

## Invariants the tests protect

Run `python3 -m unittest discover -s tests` — no install needed, ~0.2s.

- **First-touch attribution is frozen.** `upsert_subscriber` never overwrites
  `source`. A subscriber who clicks a second campaign link keeps their
  original origin; only profile fields refresh.
- **A subscriber never sees the same question twice.** `next_question_for`
  excludes anything in `deliveries` for that chat.
- **An answer is recorded once.** `record_attempt` returns `False` on a repeat
  tap and does not overwrite the first answer.
- **A bad CSV loads nothing.** `seed_questions.py` validates the entire file
  before writing. Half a question bank is worse than none.
- **Pruning only deletes files it named.** `backup.prune` skips anything not
  matching its own timestamp format.
- **An unattributable subscriber blocks a launch.** `attribution_guard.py`
  exits non-zero on a source that is empty, unnormalised, or disagrees with
  its own first `start` event. A missing or schema-less database fails too —
  a pre-launch check that passes against nothing is worse than no check.

If you change behaviour these describe, update the test in the same commit —
don't delete it.

## Working here

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env && $EDITOR .env
python scripts/seed_questions.py data/sample.csv
python scripts/verify.py            # must exit 0
python -m unittest discover -s tests
```

Most work needs no bot token: seeding, validation, attribution tooling,
backups, the landing page, and the whole test suite run without one. You only
need a token from [@BotFather](https://t.me/BotFather) to exercise live
Telegram traffic — and use **your own throwaway bot**, never the production
token. Two processes polling the same token steal each other's updates.

Before pushing:

```bash
python scripts/verify.py && python -m unittest discover -s tests
```

## Conventions

- Branch per task; `main` is the trunk. Several agents editing `bot/` at once
  will collide — `landing/`, `bot/`, and `deploy/` are safely disjoint.
- Small commits. Present-tense subject saying what changed, body saying why.
- Match the surrounding style: comments explain *why*, not *what*. Docstrings
  on modules and non-obvious functions; skip them on the obvious ones.
- Every script is argparse-driven with a docstring showing real invocations,
  and destructive or outbound actions default to safe.

## When you decide not to build something

Append it to `NOTES.md` — what it is, why it was skipped, and what would
change the answer. Don't delete existing entries; if something gets built,
mark it `BUILT` with the date and leave the reasoning. That file exists so the
same decisions stop being re-argued.
