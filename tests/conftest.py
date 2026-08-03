"""Shared pytest fixtures.

API/acceptance tests use the local-dev auth opt-out so they exercise ledger
behavior without configuring a key. Auth-required behavior is covered in
``tests/test_auth.py``.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _allow_unauthenticated_for_tests(monkeypatch):
    # Clear production key unless a test sets its own; enable explicit opt-out.
    monkeypatch.delenv("JARVIS_API_KEY", raising=False)
    monkeypatch.setenv("JARVIS_ALLOW_UNAUTHENTICATED", "1")
