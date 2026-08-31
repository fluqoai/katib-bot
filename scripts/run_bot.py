"""Convenience runner for the Telegram bot (adds the project root to sys.path
so `python scripts/run_bot.py` works without installing the package)."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from bot.main import main  # noqa: E402


if __name__ == "__main__":
    main()
