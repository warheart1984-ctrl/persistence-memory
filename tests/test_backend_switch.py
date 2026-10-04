"""JARVIS_TEST_BACKEND must really change the backend, or 'passing on Postgres' means nothing."""

from __future__ import annotations

import os

from app.pg_store import PostgresRowStore
from app.store import JarvisStore, get_store


def test_get_store_matches_the_selected_test_backend():
    if os.environ.get("JARVIS_TEST_BACKEND", "").strip().lower() == "postgres":
        store = get_store()
        assert isinstance(store, PostgresRowStore)
        assert store.list_memories() == []  # a fresh, migrated, empty schema per test
    else:
        assert isinstance(get_store(), JarvisStore)
