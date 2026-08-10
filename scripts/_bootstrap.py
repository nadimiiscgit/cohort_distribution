"""Put the repo root on sys.path so scripts can be run directly.

    python scripts/seed_questions.py      # works
    python -m scripts.seed_questions      # also works

Cron and systemd both invoke scripts by path, so the direct form has to work
without an installed package or a PYTHONPATH set in three places.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
