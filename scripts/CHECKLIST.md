# Attribution walkthrough

A manual end-to-end test: mint a tracked link, put a real redirect in front of
it, click it from an account the bot has never seen, and confirm the source
code reached `subscribers.source`.

Do this once before any campaign that spends money or goodwill on a link. The
failure it catches — people arriving with no source, or the wrong one — cannot
be repaired afterwards, because there is nothing in the database to repair
from.

Budget fifteen minutes. You need a second Telegram account.

> Throughout, **source** is the column referred to elsewhere as
> `source_channel`: `subscribers.source`, and the `source` column of
> `attribution_events`.

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
would rather not put a fake subscriber in the real database, do the whole
walkthrough against a throwaway bot and a local database first — step 7 exists
either way.

---

## 1. Mint the link

Pick a tag that names the channel, not the campaign copy: `reddit-r-medicine`,
`whatsapp-batch-2024`, `poster-lecture-hall`.

```bash
python scripts/verify_links.py reddit-r-medicine
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
python scripts/verify_links.py reddit-r-medicine --report-only
```

- [ ] Note the current `users` and `events` for the tag. Usually `0 0`.

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
# location: https://t.me/your_bot?start=reddit-r-medicine
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

**Fresh means the bot has never seen this chat ID.** Not "I deleted the chat",
not "I sent /stop first". `upsert_subscriber` never overwrites `source` — first
touch wins, by design, so a subscriber's origin stays stable when they later
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
python scripts/verify_links.py reddit-r-medicine --report-only
```

- [ ] `users` went up by exactly one against the step-2 baseline.
- [ ] `events` went up by exactly one.
- [ ] `last_event` is within a minute of your click.
- [ ] No new row appeared under `direct`, `(empty)`, or a near-miss spelling of
      your tag. Rows marked `*` are sources in the database you did not ask
      about — that is where a typo shows up.

Then check nothing leaked in the process:

```bash
python scripts/attribution_guard.py
```

- [ ] Exit 0.

If `users` did not move, work backwards: `journalctl -u cohort-bot | grep start`
shows the `source=` the bot actually parsed. A `source=direct` there means the
payload never arrived and the problem is in steps 3–4, not the database.

---

## 6. The landing-page variant

Only if the campaign points at the landing page rather than straight at
Telegram.

```bash
python scripts/verify_links.py reddit-r-medicine --landing --links-only
```

- [ ] Short link redirects to the printed `https://…/?s=reddit-r-medicine`.
- [ ] On the rendered page, the "Open in Telegram" button's href ends in
      `?start=reddit-r-medicine` — inspect it, or long-press and copy.
- [ ] Repeat steps 4 and 5 with a second fresh account.

The forwarding is an inline script in `landing/index.html`. Its normalisation
is looser than `normalize_source` — it folds disallowed characters, lowercases,
and truncates, but does not trim leading or trailing separators — so the
button's payload can differ from what you passed in `?s=`. That is harmless:
the bot normalises the payload again on the way in, and the two paths land on
the same code for anything short of the 64-character limit. What actually
matters here is that the button carries a `start` payload at all; a bare
`https://t.me/<bot>` href means the `?s=` value never reached the script.

---

## 7. Remove the test subscriber

Test rows inflate signup counts and distort the engagement percentage for the
channel you are about to spend on. Delete them.

Get the chat ID from the log line in step 5, confirm it is the one you think it
is, then:

```bash
sqlite3 data/cohort.db
```
```sql
SELECT chat_id, username, source, joined_at FROM subscribers WHERE chat_id = 123456789;
-- read that back before continuing

DELETE FROM attempts           WHERE chat_id = 123456789;
DELETE FROM deliveries         WHERE chat_id = 123456789;
DELETE FROM attribution_events WHERE chat_id = 123456789;
DELETE FROM subscribers        WHERE chat_id = 123456789;
```

- [ ] `SELECT` first, `DELETE` second, one specific `chat_id` in every statement.
- [ ] Re-run `python scripts/verify_links.py <tag> --report-only`; the counts
      are back to the step-2 baseline.

Deleting the subscriber row also makes that account fresh again, which is how
you re-test without burning a new phone number.

---

## 8. Pre-launch gate

Immediately before the links go out:

```bash
python scripts/verify_links.py reddit-r-medicine whatsapp-batch-2024 poster-lecture-hall
python scripts/attribution_guard.py --max-direct-pct 40
```

- [ ] Every tag in the campaign appears in the links block, spelled the way it
      is spelled in the scheduled posts.
- [ ] The guard exits 0.

`attribution_guard.py` exits non-zero on any leak, so it can gate a deploy or
run from cron next to `verify.py`. `--max-direct-pct` additionally fails when
too large a share of subscribers arrived with no tracked link at all — the
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
