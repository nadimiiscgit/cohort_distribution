"""Telegram distribution bot for the cohort.

This package contains only distribution logic: user management, question
delivery, and attribution capture. The product application lives in a separate
repository and is never imported from here — the only thing that crosses the
boundary is a CSV of questions and the CTA_URL the bot links out to.
"""

__version__ = "0.1.0"
