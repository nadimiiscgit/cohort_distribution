"""Source-code normalisation, link building, and CTA tagging.

The important property: a code normalises to itself. The bot normalises the
inbound ?start= payload with the same function used to mint the link, so any
drift here silently splits one campaign across two rows in the report.
"""

from __future__ import annotations

import unittest
from urllib.parse import parse_qs, urlsplit

from bot import attribution, db


class TestNormalizeSource(unittest.TestCase):
    def test_lowercases_and_replaces_illegal_characters(self) -> None:
        self.assertEqual(
            attribution.normalize_source("Reddit /r/Medicine!!"), "reddit-r-medicine"
        )

    def test_collapses_runs_and_trims_edges(self) -> None:
        self.assertEqual(attribution.normalize_source("  a??  b  "), "a-b")
        self.assertEqual(attribution.normalize_source("--x--"), "x")

    def test_preserves_the_telegram_safe_alphabet(self) -> None:
        """The channel tags actually in use must survive untouched."""
        for code in ("tg_group1", "ig_drxyz", "batch_2024-b"):
            self.assertEqual(attribution.normalize_source(code), code)

    def test_is_idempotent(self) -> None:
        """Minting then reading back must not change the code."""
        for raw in ("Reddit /r/Medicine", "whatsapp batch 3", "A" * 200, "--x--"):
            once = attribution.normalize_source(raw)
            self.assertEqual(once, attribution.normalize_source(once), raw)

    def test_respects_the_telegram_payload_limit(self) -> None:
        self.assertEqual(attribution.MAX_PAYLOAD, 64)
        self.assertEqual(
            len(attribution.normalize_source("a" * 200)), attribution.MAX_PAYLOAD
        )

    def test_empty_and_unsalvageable_input_falls_back_to_direct(self) -> None:
        for raw in (None, "", "   ", "!!!", "---"):
            self.assertEqual(attribution.normalize_source(raw), "direct", repr(raw))

    def test_the_default_matches_the_database_default(self) -> None:
        """Three places spell 'direct'; this is what stops them drifting."""
        self.assertEqual(attribution.DEFAULT_SOURCE, db.DEFAULT_SOURCE_CHANNEL)
        self.assertIn(
            f"DEFAULT '{attribution.DEFAULT_SOURCE}'".lower(), db.SCHEMA.lower()
        )


class TestLinks(unittest.TestCase):
    def test_deep_link_uses_the_normalised_code(self) -> None:
        link = attribution.deep_link("Reddit /r/Medicine", bot_username="cohort_bot")
        self.assertEqual(link, "https://t.me/cohort_bot?start=reddit-r-medicine")

    def test_deep_link_tolerates_a_leading_at_sign(self) -> None:
        link = attribution.deep_link("x", bot_username="@cohort_bot")
        self.assertEqual(link, "https://t.me/cohort_bot?start=x")

    def test_landing_link_strips_a_trailing_slash(self) -> None:
        link = attribution.landing_link("x", base_url="https://example.com/")
        self.assertEqual(link, "https://example.com/?s=x")


class TestCtaLink(unittest.TestCase):
    def params(self, url: str) -> dict[str, list[str]]:
        return parse_qs(urlsplit(url).query)

    def test_stamps_user_source_and_question(self) -> None:
        link = attribution.cta_link(
            "https://app.example.com/join",
            user_id=7,
            source_channel="tg_group1",
            question_id="Q1",
        )
        self.assertEqual(
            self.params(link),
            {"src": ["tg_group1"], "uid": ["7"], "qid": ["Q1"]},
        )

    def test_keeps_query_parameters_the_operator_already_set(self) -> None:
        """CTA_URL may well arrive with utm tags on it already."""
        link = attribution.cta_link(
            "https://app.example.com/join?utm_source=telegram&utm_medium=bot",
            user_id=7,
            source_channel="tg_group1",
        )
        params = self.params(link)
        self.assertEqual(params["utm_source"], ["telegram"])
        self.assertEqual(params["utm_medium"], ["bot"])
        self.assertEqual(params["uid"], ["7"])

    def test_omits_the_question_when_there_is_none(self) -> None:
        link = attribution.cta_link(
            "https://app.example.com/join", user_id=7, source_channel="direct"
        )
        self.assertNotIn("qid", self.params(link))

    def test_normalises_the_source_it_stamps(self) -> None:
        link = attribution.cta_link(
            "https://app.example.com/join",
            user_id=7,
            source_channel="Reddit /r/Medicine",
        )
        self.assertEqual(self.params(link)["src"], ["reddit-r-medicine"])

    def test_preserves_path_and_fragment(self) -> None:
        link = attribution.cta_link(
            "https://app.example.com/join/now#pricing",
            user_id=7,
            source_channel="direct",
        )
        parts = urlsplit(link)
        self.assertEqual(parts.path, "/join/now")
        self.assertEqual(parts.fragment, "pricing")


if __name__ == "__main__":
    unittest.main()
