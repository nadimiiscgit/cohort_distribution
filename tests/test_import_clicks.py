"""Click ingestion from web server logs.

Three properties matter here, and all three are silent when broken:

- Re-running the importer must not inflate the count. It runs hourly over a
  log that still contains the hours it already read.
- Link-preview fetchers must not be counted. Telegram fetches every URL that
  passes through it, so pasting a campaign link registers a hit before any
  human sees it — in exactly the channel we most want to measure.
- A malformed line must not abort the run. One bad line in a log is normal;
  losing the other 40,000 because of it is not.
"""

from __future__ import annotations

import gzip
import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path

from bot import db

ROOT = Path(__file__).resolve().parent.parent

sys.path.insert(0, str(ROOT / "scripts"))
_spec = importlib.util.spec_from_file_location(
    "import_clicks", ROOT / "scripts" / "import_clicks.py"
)
import_clicks = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(import_clicks)

BROWSER = (
    "Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0 Mobile Safari/537.36"
)


def line(
    target: str = "/?s=reddit",
    ts: str = "10/Aug/2026:13:55:36 +0000",
    status: str = "200",
    ua: str = BROWSER,
    method: str = "GET",
) -> str:
    return (
        f'203.0.113.7 - - [{ts}] "{method} {target} HTTP/1.1" {status} 512 '
        f'"https://example.com/" "{ua}"\n'
    )


class ClassifyTestCase(unittest.TestCase):
    def classify(self, raw: str):
        return import_clicks.classify(raw)

    def test_counts_a_plain_browser_hit_carrying_a_code(self) -> None:
        kind, event = self.classify(line())
        self.assertEqual(kind, "click")
        self.assertEqual(event[0], "reddit")

    def test_normalises_the_code_the_same_way_the_bot_does(self) -> None:
        """A code must not split into two rows depending on which side saw it."""
        _, event = self.classify(line(target="/?s=Reddit%20%2Fr%2FMedicine"))
        self.assertEqual(event[0], "reddit-r-medicine")

    def test_converts_the_timestamp_to_utc(self) -> None:
        _, event = self.classify(line(ts="10/Aug/2026:13:55:36 +0530"))
        self.assertEqual(event[1], "2026-08-10T08:25:36+00:00")

    def test_handles_a_negative_utc_offset(self) -> None:
        _, event = self.classify(line(ts="10/Aug/2026:13:55:36 -0400"))
        self.assertEqual(event[1], "2026-08-10T17:55:36+00:00")

    def test_ignores_hits_with_no_campaign_code(self) -> None:
        self.assertEqual(self.classify(line(target="/"))[0], "skip")
        self.assertEqual(self.classify(line(target="/?s="))[0], "skip")

    def test_ignores_non_get_and_unsuccessful_requests(self) -> None:
        self.assertEqual(self.classify(line(method="POST"))[0], "skip")
        self.assertEqual(self.classify(line(status="404"))[0], "skip")

    def test_ignores_assets_that_inherited_the_query_string(self) -> None:
        self.assertEqual(self.classify(line(target="/styles.css?s=reddit"))[0], "skip")

    def test_raises_on_a_line_it_cannot_parse(self) -> None:
        with self.assertRaises(import_clicks.ParseError):
            self.classify("this is not a log line\n")


class BotFilterTestCase(unittest.TestCase):
    def test_drops_the_link_preview_fetchers(self) -> None:
        for ua in (
            "TelegramBot (like TwitterBot)",
            "WhatsApp/2.23.20.0 A",
            "facebookexternalhit/1.1",
            "Slackbot-LinkExpanding 1.0",
            "Twitterbot/1.0",
            "Mozilla/5.0 (compatible; Discordbot/2.0)",
        ):
            self.assertTrue(import_clicks.is_bot(ua), ua)
            self.assertEqual(import_clicks.classify(line(ua=ua))[0], "bot", ua)

    def test_drops_crawlers_and_scripted_clients(self) -> None:
        for ua in ("Googlebot/2.1", "curl/8.4.0", "python-requests/2.31.0", "-", ""):
            self.assertTrue(import_clicks.is_bot(ua), ua)

    def test_keeps_real_browsers(self) -> None:
        for ua in (
            BROWSER,
            "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) Safari/605.1",
            # CUBOT is a phone brand: a naive "bot" substring match drops a
            # real user here, which is the failure nobody would ever notice.
            "Mozilla/5.0 (Linux; Android 11; CUBOT_NOTE_20) Chrome/120.0",
        ):
            self.assertFalse(import_clicks.is_bot(ua), ua)


class CollectTestCase(unittest.TestCase):
    def write_log(self, body: str, suffix: str = ".log") -> str:
        tmp = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
        tmp.close()
        path = Path(tmp.name)
        self.addCleanup(path.unlink)
        if suffix.endswith(".gz"):
            with gzip.open(path, "wt", encoding="utf-8") as handle:
                handle.write(body)
        else:
            path.write_text(body, encoding="utf-8")
        return str(path)

    def test_groups_clicks_by_utc_date(self) -> None:
        log = self.write_log(
            line(ts="10/Aug/2026:13:00:00 +0000")
            + line(ts="10/Aug/2026:14:00:00 +0000")
            + line(ts="11/Aug/2026:01:00:00 +0000")
        )
        by_date, stats = import_clicks.collect([log], since=None)
        self.assertEqual(sorted(by_date), ["2026-08-10", "2026-08-11"])
        self.assertEqual(len(by_date["2026-08-10"]), 2)
        self.assertEqual(stats["clicks"], 3)

    def test_a_malformed_line_does_not_abort_the_run(self) -> None:
        log = self.write_log(line() + "garbage\n" + line(target="/?s=other"))
        by_date, stats = import_clicks.collect([log], since=None)
        self.assertEqual(stats["clicks"], 2)
        self.assertEqual(stats["unparsed"], 1)

    def test_counts_dropped_preview_hits_separately(self) -> None:
        log = self.write_log(line() + line(ua="TelegramBot (like TwitterBot)"))
        _, stats = import_clicks.collect([log], since=None)
        self.assertEqual(stats["clicks"], 1)
        self.assertEqual(stats["bots"], 1)

    def test_reads_rotated_gzipped_logs(self) -> None:
        log = self.write_log(line(), suffix=".log.gz")
        _, stats = import_clicks.collect([log], since=None)
        self.assertEqual(stats["clicks"], 1)

    def test_since_excludes_earlier_dates(self) -> None:
        log = self.write_log(
            line(ts="01/Aug/2026:13:00:00 +0000") + line(ts="10/Aug/2026:13:00:00 +0000")
        )
        by_date, _ = import_clicks.collect([log], since="2026-08-05")
        self.assertEqual(sorted(by_date), ["2026-08-10"])


class ReplaceClicksTestCase(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        self.path = Path(tmp.name)
        self.addCleanup(self.path.unlink)
        self.conn = db.connect(self.path)
        db.init_schema(self.conn)
        self.addCleanup(self.conn.close)

    def events(self, n: int, day: str = "2026-08-10", source: str = "reddit"):
        return [(source, f"{day}T{h:02d}:00:00+00:00") for h in range(n)]

    def test_reimporting_a_day_does_not_inflate_the_count(self) -> None:
        """The importer runs hourly over a log it has already partly read."""
        db.replace_clicks_for_date(self.conn, "2026-08-10", self.events(3))
        db.replace_clicks_for_date(self.conn, "2026-08-10", self.events(3))
        self.assertEqual(db.clicks_on_date(self.conn, "2026-08-10"), 3)
        self.assertEqual(db.attribution_clicks(self.conn), {"reddit": 3})

    def test_a_later_import_picks_up_the_rest_of_the_day(self) -> None:
        db.replace_clicks_for_date(self.conn, "2026-08-10", self.events(3))
        removed, added = db.replace_clicks_for_date(
            self.conn, "2026-08-10", self.events(5)
        )
        self.assertEqual((removed, added), (3, 5))
        self.assertEqual(db.clicks_on_date(self.conn, "2026-08-10"), 5)

    def test_replacing_one_day_leaves_other_days_alone(self) -> None:
        db.replace_clicks_for_date(
            self.conn, "2026-08-09", self.events(4, day="2026-08-09")
        )
        db.replace_clicks_for_date(self.conn, "2026-08-10", self.events(2))
        db.replace_clicks_for_date(self.conn, "2026-08-10", self.events(1))
        self.assertEqual(db.clicks_on_date(self.conn, "2026-08-09"), 4)
        self.assertEqual(db.clicks_on_date(self.conn, "2026-08-10"), 1)

    def test_rejects_rows_that_are_not_on_the_named_day(self) -> None:
        """A mismatch would delete one day and insert into another, silently."""
        with self.assertRaises(ValueError):
            db.replace_clicks_for_date(
                self.conn, "2026-08-09", self.events(2, day="2026-08-10")
            )

    def test_clicks_do_not_disturb_the_start_events_in_the_same_table(self) -> None:
        db.record_attribution(self.conn, source="reddit", event="start", chat_id=1)
        db.replace_clicks_for_date(self.conn, "2026-08-10", self.events(2))
        db.replace_clicks_for_date(self.conn, "2026-08-10", self.events(2))
        self.assertEqual(db.attribution_opens(self.conn), {"reddit": 1})
        self.assertEqual(db.attribution_clicks(self.conn), {"reddit": 2})


if __name__ == "__main__":
    unittest.main()
