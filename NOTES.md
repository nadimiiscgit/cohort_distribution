# NOTES

A running log of things deliberately **not** built, and why. The point is to
stop re-litigating the same decisions every few weeks, and to make it obvious
when the reasoning has actually expired.

Format: what it is, why it was skipped, and what would change the answer.
Append at the bottom; don't delete entries — if something gets built, mark it
`BUILT` with the date and leave the reasoning in place.

---

### Webhooks instead of long polling
Would mean a public HTTPS endpoint, a certificate, a reverse proxy, and a
health check for all of it. Long polling has none of that and costs one idle
connection. At our subscriber count the latency difference is invisible.
**Revisit when:** we're rate-limited by polling, or the box moves behind a load
balancer that makes an inbound endpoint free.

### Daily automatic question push
Pull-based (`/question`) instead of a scheduled push. A push needs per-user
timezone handling, quiet hours, and a story for people who ignore it for three
weeks — and unsolicited daily messages are the fastest way to get a bot
blocked, which is unrecoverable. Pull means every message was asked for.
**Revisit when:** enough subscribers ask for a nudge, and only as opt-in with a
stored per-user send time.

> **BUILT 2026-08-10 — `scripts/daily.py`.** The product decision changed: the
> daily question *is* the experiment, so pull-only was measuring the wrong
> thing. The original objections were not wrong and are answered rather than
> dismissed: the send is cron-triggered at one fixed hour (no per-user
> timezone, no quiet hours — everyone gets it at the same UTC-anchored time),
> `/stop` is one tap away and honoured by `is_active`, and `Forbidden` is
> caught and the user deactivated, so blocking is absorbed rather than retried
> into. What is still unsolved is the three-weeks-of-silence case: a user who
> never opens a message keeps receiving them until they block. **Revisit when:**
> block rate is measurable — the fix is to stop sending after N consecutive
> unanswered days, not per-user scheduling.

### Postgres instead of SQLite
One process, one writer, low thousands of rows. SQLite in WAL mode handles
this with no daemon, no connection pool, and a backup that is one file.
**Revisit when:** we need a second writer process, or concurrent access from
somewhere off the box.

### Docker
The deploy is a venv, a systemd unit, and a git pull. A container adds a
registry, a build step, and a volume mount for the database without removing
anything we currently do by hand.
**Revisit when:** we deploy to more than one host, or the environment stops
being reproducible with `pip install -r requirements.txt`.

### Third-party analytics on the landing page
No Plausible, no GA, no pixel. The only funnel question that matters — which
channel produced subscribers who actually answer questions — is answered by a
SQL query against our own database. Adding a script tag means a cookie banner,
a privacy policy, and a third party holding data on medical students.
**Revisit when:** we need on-page behaviour (scroll depth, A/B), and even then
prefer server logs.

### `python-dotenv`
Twenty lines in `bot/config.py` cover `KEY=VALUE`, comments, and quotes, which
is the whole format we use. A dependency that saves twenty lines is not worth
the supply-chain surface.
**Revisit when:** we need interpolation or multiline values.

### Admin web dashboard
`/stats` in Telegram and `scripts/attribution.py report` cover everything a
dashboard would show, without an authenticated web surface to secure.
**Revisit when:** someone who doesn't use a terminal needs the numbers.

### User accounts / cross-device sync
Telegram chat ID *is* the identity. Accounts would mean passwords, resets, and
a login form for a service whose entire value is that it has no login form.
**Revisit when:** never, for this repo. That's the app's problem.

### Images and diagrams in questions
Text only. Images mean hosting, alt text, CDN bandwidth, and a much heavier
seeding pipeline, and a large share of questions don't need them.
**Revisit when:** we have a body of image-dependent questions worth the
pipeline, not before.

### LLM-generated explanations
Explanations ship in the CSV, written or reviewed by a human. Generating them
at send time means an API key on the box, a per-message cost, and the chance
of confidently telling a medical student something wrong.
**Revisit when:** there's a human review step between generation and delivery —
at which point it's a seeding-time tool, not a runtime one.

### CI pipeline
No test suite yet, so CI would only lint. `scripts/verify.py` is the check that
actually catches our failure modes (bad config, tracked secrets, broken data),
and it runs before every deploy and nightly from cron.
**Revisit when:** there are tests worth gating a merge on.

> **2026-08-10 — condition now met, still not built.** `tests/` exists and
> covers the invariants worth gating on. CI is a deliberate next step rather
> than an oversight: the suite needs no install and runs in ~0.2s, so
> `python3 -m unittest discover -s tests` before pushing is currently cheaper
> than a workflow file. Build it when more than one person is pushing and
> "before pushing" stops being reliable.

> **BUILT 2026-08-11.** Several branches were being pushed in parallel and
> "before pushing" had stopped being reliable — the stated trigger.
> `.github/workflows/ci.yml` runs three jobs on every pull request: the
> suite, the tracked-file check, and a seed of `data/sample.csv`.
>
> Two things are worth knowing. CI installs `requirements.txt` where a local
> run often does not: without `python-telegram-bot` the handler and render
> tests *skip*, and the suite reports `OK (skipped=29)` — green while
> silently testing 29 fewer things. CI installs it so all 144 run.
>
> And CI runs `verify.py --secrets-only`, not the whole thing. The full
> check needs a filled-in `.env`, which CI has no business holding; the
> tracked-file check needs nothing and is the half that guards rule 2. The
> other half stays where it was, before every deploy and nightly from cron.
> Linting is still not part of this — that would need a second dependency,
> and the rule does not accept convenience.

### pytest
The suite is standard-library `unittest`. pytest's fixtures and bare `assert`
are genuinely nicer to write, but that is convenience, and the dependency rule
in the README does not accept convenience. Staying stdlib also means the tests
run on a bare clone with nothing installed, which is worth more than the
ergonomics — a new contributor's first command works before `pip install`.
**Revisit when:** the suite gets big enough that fixtures and parametrisation
save real maintenance, not just typing.

### Payments / paid tier
Out of scope for distribution infrastructure entirely. If the product monetises,
that lives in the app.
**Revisit when:** never, here.

### Multi-language support
English only. i18n means translated question banks, not just translated UI
strings, and the question bank is the expensive part.
**Revisit when:** we have a translated question bank in hand.

### Referral codes for subscribers
Distinct from campaign attribution: this would be per-user share links plus
some reward. It needs anti-gaming rules and something to actually give people.
Campaign codes already tell us which *channels* work, which is the current
question.
**Revisit when:** growth is the bottleneck and there's a reward worth giving.

### Automated end-to-end click test
`scripts/CHECKLIST.md` is a manual walkthrough on purpose. Automating it means
driving a real Telegram *user* account, which is a second API (MTProto), a
second set of credentials, a phone number per test run, and a client library —
against a rule that says one runtime dependency. It would also test the wrong
half: the parsing is already covered by `tests/`, and the failures that
actually happen live in the redirect chain and the shortener, outside anything
we could drive. `attribution.py link` and `attribution.py report` automate the
two ends — printing the exact link, reading back what landed — and leave the
click, which is the part a human has to do anyway to prove the real path works.
**Revisit when:** we're minting enough links that clicking them by hand stops
happening, and even then automate the redirect chain (`curl -I` following
hops) rather than the Telegram side.
### First-party CTA click logging
`cta_clicked` exists in the `events` schema and `db.log_event` accepts it, but
nothing in the bot writes one. The CTA is a URL button, and **Telegram sends no
update when a URL button is tapped** — there is no callback to handle. The one
tap goes straight to the destination, which is the right trade for a
conversion experiment, so the click is measured at the destination instead:
the bot stamps `uid`, `src`, and `qid` onto `CTA_URL` and the landing side
joins them back to `users.user_id`.

The alternative is a `callback_data` button that logs the click and *then*
hands over the link — first-party click data at the cost of a second tap on
the highest-intent action in the funnel. (`answerCallbackQuery(url=...)` only
opens t.me links and games, so it does not rescue the web case.)

**Revisit when:** the destination cannot be instrumented, or CTA_URL becomes a
t.me link. It is a ~10-line change in `render.cta_markup` plus a callback
handler; the event type and the index are already in place so no schema change
is needed.

### Leaderboards, streaks, and spaced repetition
Three separate asks, one answer: they turn a distribution experiment into a
product. Each needs its own state, its own daily recompute, and its own
failure mode when a user drops out for a week, and none of them tell us
anything about which channel sends people who stick. The product app is where
retention mechanics belong.
**Revisit when:** never, here. If the experiment says the channel works, these
are the app's problem.

### Group posting, auto-DMs, and member scraping
Not built, and not a cost/benefit call: posting into groups the bot was not
invited to, DMing people who never started a chat, and enumerating a group's
members are the behaviours that get a bot permanently banned from Telegram,
and the last one is unlawful in several of the places our users live. Every
message this repo sends goes to someone who pressed Start.
**Revisit when:** never.

### Multiple questions a day, and subject filtering
Both existed in the pull-based design (`DAILY_QUESTION_LIMIT=5`,
`/question <subject>`) and were removed when the daily schedule landed. With
one dated question per day there is no queue to draw a second from and no
subject to filter within — `/question` now re-sends today's question, which is
the only coherent meaning it has left. The `subject` column is still stored
and still shown as the question's heading.
**Revisit when:** the schedule holds more than one question per date, which is
a `question_for_date` change and a rethink of what "the daily question" means.

### Within-N-days retention instead of exactly-day-N
`/stats` reports D1 and D7 as *exactly-day-N* return over cohorts whose day N
has fully elapsed. Within-N-days was rejected because it double-counts: every
D1 returner is also a D7 returner, so the two numbers stop being comparable
and both drift upward as the window grows. Exactly-day-N is noisier on small
cohorts — hence the `n/a` until a cohort is old enough — but it answers "did
they come back on day 7" rather than "have they ever come back".
**Revisit when:** cohorts are large enough that a rolling window is readable,
and then add it alongside rather than replacing.
