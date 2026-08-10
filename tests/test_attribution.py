"""Source-code normalisation and link building.

The important property: a code normalises to itself. The bot normalises the
inbound ?start= payload with the same function used to mint the link, so any
drift here silently splits one campaign across two rows in the report.
"""

from __future__ import annotations

import os
import unittest
from unittest import mock

from bot import attribution


class TestNormalizeSource(unittest.TestCase):
    def test_lowercases_and_replaces_illegal_characters(self) -> None:
        self.assertEqual(
            attribution.normalize_source("Reddit /r/Medicine!!"), "reddit-r-medicine"
        )

    def test_collapses_runs_and_trims_edges(self) -> None:
        self.assertEqual(attribution.normalize_source("  a??  b  "), "a-b")
        self.assertEqual(attribution.normalize_source("--x--"), "x")

    def test_preserves_the_telegram_safe_alphabet(self) -> None:
        self.assertEqual(
            attribution.normalize_source("Batch_2024-B"), "batch_2024-b"
        )

    def test_is_idempotent(self) -> None:
        """Minting then reading back must not change the code."""
        for raw in ("Reddit /r/Medicine", "whatsapp batch 3", "A" * 200, "--x--"):
            once = attribution.normalize_source(raw)
            self.assertEqual(once, attribution.normalize_source(once), raw)

    def test_respects_the_telegram_payload_limit(self) -> None:
        self.assertEqual(len(attribution.normalize_source("a" * 200)), 64)

    def test_empty_and_unsalvageable_input_falls_back_to_default(self) -> None:
        with mock.patch.dict(os.environ, {"DEFAULT_SOURCE": "direct"}):
            for raw in (None, "", "   ", "!!!", "---"):
                self.assertEqual(attribution.normalize_source(raw), "direct", repr(raw))


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


if __name__ == "__main__":
    unittest.main()
