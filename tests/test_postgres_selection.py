from __future__ import annotations

from app.identity import Principal, reset_principal, set_principal
from app.store import PostgresJarvisStore, get_store, reset_store_for_tests


def test_database_url_selects_postgres_tenant_store(monkeypatch):
    reset_store_for_tests()
    monkeypatch.setenv("JARVIS_DATABASE_URL", "postgresql://unused.example/test")
    token = set_principal(Principal(subject="user-a", scopes=frozenset({"memory.read"}), issuer="https://issuer.example"))
    try:
        first = get_store()
    finally:
        reset_principal(token)
    assert isinstance(first, PostgresJarvisStore)
    assert first._tenant_key != "user-a"


# The Postgres counterparts of these properties live in tests/test_pg_*.py (CHECK constraints,
# fail-closed on a database outage, generic MCP errors, history verification).
import pytest as _pytest_marker  # noqa: E402

pytestmark = _pytest_marker.mark.json_store_only
