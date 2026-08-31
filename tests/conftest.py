"""Pytest configuration.

We don't require live Supabase / OpenAI / Drive credentials in CI. Tests
that need external services are marked `integration` and skipped by
default; run them with `pytest -m integration`.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pytest  # noqa: E402


def pytest_collection_modifyitems(config, items):
    skip_integration = pytest.mark.skip(reason="integration test (needs live creds)")
    for item in items:
        if "integration" in item.keywords:
            item.add_marker(skip_integration)
