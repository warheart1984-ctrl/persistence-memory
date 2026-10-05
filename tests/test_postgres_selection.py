from __future__ import annotations

from app.identity import Principal, reset_principal, set_principal
from app.pg_store import PostgresRowStore
from app.store import get_store, reset_store_for_tests


def test_database_url_selects_a_per_tenant_row_store(monkeypatch):
    reset_store_for_tests()
    monkeypatch.setenv("JARVIS_DATABASE_URL", "postgresql://unused.example/test")
    monkeypatch.delenv("JARVIS_PG_STORE", raising=False)
    token = set_principal(Principal(subject="user-a", scopes=frozenset({"memory.read"}), issuer="https://issuer.example"))
    try:
        first = get_store()
    finally:
        reset_principal(token)
    assert isinstance(first, PostgresRowStore)
    assert first._tenant_key != "user-a"  # opaque, hashed from issuer + subject
