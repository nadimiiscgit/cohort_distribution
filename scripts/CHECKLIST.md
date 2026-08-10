# Attribution walkthrough

A manual end-to-end test: mint a tracked link, put a real redirect in front of
it, click it from an account the bot has never seen, and confirm the source
code reached `users.source_channel`.

Do this once before any campaign that spends money or goodwill on a link. The
failure it catches — people arriving with no source, or the wrong one — cannot
be repaired afterwards, because there is nothing in the database to repair
from.

Budget fifteen minutes. You need a second Telegram account.

> `users.source_channel` is the only place a source is stored. `events` has no
> source column of its own, so every event reaches a channel by joining back to
> its user — and an event whose user row is gone is unattributable outright.

---

## 0. Before you start

```bash
python scripts/verify.py            # 0 failures
systemctl status cohort-bot         # active (running)
```

The bot has to be **running** while you click. Long polling means a stopped
bot records nothing at click time — Telegram queues the update and delivers it
when the process comes back, so a bot that was down mid-test produces a
confusing delay rather than a clean failure.

Test against production if the point is to verify the production link. If you
would rather not put a fake user in the real database, do the whole
walkthrough against a throwaway bot and a local database first — step 7 exists
either way.

---

## 1. Mint the link

Pick a tag that names the channel, not the campaign copy: `tg_group1`,
`insta_bio`, `poster-lecture-hall`.

```bash
python scripts/verify_links.py tg_group1
```

- [ ] The printed `t.me` URL is the one you will shorten. Copy it exactly.
- [ ] If the script printed a `# normalised ...` line, the tag you typed is not
      the tag that will be recorded. Use the normalised one everywhere from
      here on, including in the shortener's own label.

Telegram's `start` payload accepts `A-Za-z0-9_-` and at most 64 characters.
Anything else is folded before the link is built, which is why the tag you type
and the tag you get can differ.

---

## 2. Baseline the database

```bash
python scripts/verify_links.py tg_group1 --report-only
```

- [ ] Note the current `users` and `starts` for the tag. Usually `0 0`.

Anything already there means the tag has been used before — pick a fresh one
for the test, or you will not be able to tell your own click apart from
history.

---

## 3. Put a tracked short link in front of it

Create the redirect in whatever shortener the campaign will actually use. A
test against a different shortener than the campaign proves nothing about the
campaign.

- [ ] Destination is the **full** deep link from step 1, `?start=` and all.
- [ ] The hop is a plain `301`/`302` to `t.me`. Check it without a browser:

```bash
curl -sSI https://your.short/link | grep -i '^location:'
# location: https://t.me/your_bot?start=tg_group1
```

- [ ] `start=` survives the hop, spelled exactly as in step 1. Shorteners that
      append their own tracking parameters are fine — `t.me` ignores everything
      but `start`. Shorteners that percent-encode the `?`, drop the query
      string, or land on an interstitial page are not.
- [ ] No interstitial, no preview page, no "you are leaving" splash. Every one
      of those is a place the payload gets lost, and the ones that keep it
      still cost you clicks.

If the shortener cannot preserve a query string, do not work around it. Use the
landing page instead (step 6) — that path is designed for it.

---

## 4. Click it from a fresh account

**Fresh means the bot has never seen this user ID.** Not "I deleted the chat",
not "I sent /stop first". `ensure_user` never overwrites `source_channel` — first
touch wins, by design, so a user's origin stays stable when they later
click a different link (`bot/db.py`). An account that has ever pressed start is
permanently useless for this test until you delete its row.

- [ ] Open the **short link** — not the deep link — on a phone, in the account
      the bot has not seen.
- [ ] Telegram opens the bot's chat.
- [ ] **Press START.** Opening the chat sends nothing. This is the single most
      common reason a link "doesn't work": the tester looked at the chat and
      closed it.
- [ ] The welcome message arrives, followed by a question.

---

## 5. Confirm it landed

```bash
python scripts/verify_links.py tg_group1 --report-only
```

- [ ] `users` went up by exactly one against the step-2 baseline.
- [ ] `starts` went up by exactly one.
- [ ] `last_start` is within a minute of your click.
- [ ] No new row appeared under `direct`, `(empty)`, `(no user row)`, or a
      near-miss spelling of your tag. Rows marked `*` are channels in the
      database you did not ask about — that is where a typo shows up.

Then check nothing leaked in the process:

```bash
python scripts/attribution_guard.py
```

- [ ] Exit 0.

If `users` did not move, work backwards:

```bash
journalctl -u cohort-bot | grep 'start user_id='
# start user_id=123456789 source_channel=tg_group1 new=True payload='tg_group1'
```

That line separates the two failures. `payload=None` means the deep link never
carried the code and the problem is in steps 3–4, not the database.
`payload='tg_group1'` with `source_channel=` something else means the account
was not fresh — the payload arrived and first-touch correctly refused it.

---

## 6. The landing-page variant

Only if the campaign points at the landing page rather than straight at
Telegram.

```bash
python scripts/verify_links.py tg_group1 --landing --links-only
```

- [ ] Short link redirects to the printed `https://…/?s=tg_group1`.
- [ ] Tap "Open in Telegram" and check the chat you land in is the right bot.
- [ ] Repeat steps 4 and 5 with a second fresh account.

**Do not verify this step by inspecting the button's href before you tap it.**
The forwarding is an inline script in `landing/index.html`, and depending on
the version of that page it either rewrites the href on page load or builds it
in a click handler at tap time. In the second case the href reads as a bare
`https://t.me/<bot>` right up until the moment it is followed, so long-pressing
and copying the link — or reading it in devtools — shows a missing payload on a
page that is working perfectly. Step 5's report is the ground truth here, and
it is the only check that holds across both versions.

The page's normalisation is its own, separate from `normalize_source`, and the
two have drifted apart before. That is survivable by design: the bot normalises
the inbound payload again, so the recorded code is correct even when the page's
is not byte-identical. It also means a divergence here is invisible until it
truncates something — one more reason to trust the report over the href.

---

## 7. Remove the test user

Test rows inflate signup counts and distort the engagement percentage for the
channel you are about to spend on. Delete them.

Get the user ID from the log line in step 5, confirm it is the one you think it
is, then:

```bash
sqlite3 data/cohort.db
```
```sql
SELECT user_id, username, source_channel, first_seen FROM users WHERE user_id = 123456789;
-- read that back before continuing

DELETE FROM events WHERE user_id = 123456789;
DELETE FROM users  WHERE user_id = 123456789;
```

- [ ] `SELECT` first, `DELETE` second, one specific `user_id` in every statement.
- [ ] **Events first, user second.** There is no foreign key to cascade for you.
      Drop the user row while their events remain and those events lose the only
      copy of their source — `attribution_guard.py` fails on exactly this, which
      is how you find out you did it.
- [ ] Re-run `python scripts/verify_links.py <tag> --report-only`; the counts
      are back to the step-2 baseline, with nothing under `(no user row)`.

Deleting the user row also makes that account fresh again, which is how
you re-test without burning a new phone number.

---

## 8. Pre-launch gate

Immediately before the links go out:

```bash
python scripts/verify_links.py tg_group1 insta_bio poster-lecture-hall
python scripts/attribution_guard.py --max-direct-pct 40
```

- [ ] Every tag in the campaign appears in the links block, spelled the way it
      is spelled in the scheduled posts.
- [ ] The guard exits 0.

`attribution_guard.py` exits non-zero on any leak, so it can gate a deploy or
run from cron next to `verify.py`. `--max-direct-pct` additionally fails when
too large a share of users arrived with no tracked link at all — the
symptom of an untagged link in circulation.

---

## What the symptoms mean

| What you see | What it usually is |
| --- | --- |
| Subscriber landed on `direct` | The payload never reached Telegram: the shortener dropped the query string, or the tester opened the plain `t.me` link |
| Nothing recorded at all | START was never pressed, or the bot was not running |
| Source is a near-miss of the tag | The shortener's destination was typed by hand instead of pasted |
| Counts did not change on a re-test | The account had subscribed before; source is first-touch-wins, so re-clicking cannot change it |
| Guard reports an empty source | A write path bypassed `normalize_source` — a leak, and the rows it names cannot be attributed retroactively |
| Guard reports a non-normalised source | Same cause, milder symptom: the funnel will report that channel as two separate rows |
