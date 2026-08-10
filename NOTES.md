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
