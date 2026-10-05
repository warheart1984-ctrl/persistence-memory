"""Cutover safety: the row store is the default for JARVIS_DATABASE_URL, and it will not quietly
serve an empty ledger while a tenant's legacy JSONB blob ledger still holds data."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from app import pg_import, pg_store
from app.main import app
from app.models import MemoryCreate
from app.pg_schema import migrate
from app.pg_store import PostgresRowStore
from app.store import PostgresJarvisStore, StoreUnavailableError, get_store, reset_store_for_tests

# --- selection (needs no database) ---------------------------------------------------------------


def test_row_store_is_the_default_for_a_database_url(monkeypatch):
    reset_store_for_tests()
    monkeypatch.setenv("JARVIS_DATABASE_URL", "postgresql://unused.example/test")
    monkeypatch.delenv("JARVIS_PG_STORE", raising=False)
    assert isinstance(get_store(), PostgresRowStore)


def test_the_blob_store_stays_available_explicitly(monkeypatch):
    reset_store_for_tests()
    monkeypatch.setenv("JARVIS_DATABASE_URL", "postgresql://unused.example/test")
    monkeypatch.setenv("JARVIS_PG_STORE", "blob")
    assert isinstance(get_store(), PostgresJarvisStore)
    monkeypatch.setenv("JARVIS_PG_STORE", "rows")
    reset_store_for_tests()
    assert isinstance(get_store(), PostgresRowStore)


def test_an_unknown_mode_fails_closed(monkeypatch):
    reset_store_for_tests()
    monkeypatch.setenv("JARVIS_DATABASE_URL", "postgresql://unused.example/test")
    monkeypatch.setenv("JARVIS_PG_STORE", "bogus")
    with pytest.raises(StoreUnavailableError):
        get_store()


def test_without_a_database_url_the_json_store_is_still_used(monkeypatch):
    from app.store import JarvisStore

    reset_store_for_tests()
    monkeypatch.delenv("JARVIS_DATABASE_URL", raising=False)
    monkeypatch.delenv("JARVIS_PG_STORE", raising=False)
    assert isinstance(get_store(), JarvisStore)


# --- the legacy-blob guard (throwaway Postgres) ---------------------------------------------------

pg_only = pytest.mark.postgres

_REC = {
    "id": "mem-legacy-1", "content": "held only in the legacy blob ledger", "created_at": "2026-07-01T00:00:00+00:00",
    "updated_at": "2026-07-01T00:00:00+00:00", "source_agent": "a", "session_id": "s", "type": "fact",
    "confidence": 0.5, "status": "draft", "tags": [], "evidence": [],
}


@pytest.fixture
def pg(pg_schema, monkeypatch):
    migrate(pg_schema.admin_dsn, schema=pg_schema.schema, app_role="jarvis_app_test")
    monkeypatch.setenv("JARVIS_LEGACY_BLOB_SCHEMA", pg_schema.schema)  # keep the throwaway DB's public schema clean
    yield pg_schema
    pg_store.close_pools()


def _blob(pg, tenant, memories, *, grant=True):
    with pg.admin_conn() as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS jarvis_tenant_ledgers (tenant_key text PRIMARY KEY, "
            "payload jsonb NOT NULL, updated_at timestamptz DEFAULT now())"
        )
        conn.execute(
            "INSERT INTO jarvis_tenant_ledgers (tenant_key, payload) VALUES (%s, %s::jsonb)",
            (tenant, json.dumps({"board": {}, "memories": memories})),
        )
        if grant:
            conn.execute("GRANT SELECT ON jarvis_tenant_ledgers TO jarvis_app_test")


def _store(pg, tenant="operator"):
    return PostgresRowStore(pg.app_dsn, tenant, schema=pg.schema)


@pg_only
def test_empty_row_store_with_legacy_data_refuses_to_serve(pg):
    _blob(pg, "operator", [_REC])
    s = _store(pg)
    for call in (
        lambda: s.list_memories(),
        lambda: s.get_memory("mem-legacy-1"),
        lambda: s.create_memory(MemoryCreate(content="must not start a second ledger", source_agent="t", session_id="s", type="fact")),
    ):
        with pytest.raises(StoreUnavailableError, match="(?i)legacy blob"):
            call()
    with pg.admin_conn() as conn:  # and nothing was written while refusing
        assert conn.execute("SELECT count(*) FROM memories").fetchone()[0] == 0


@pg_only
def test_importing_the_blob_clears_the_guard(pg, monkeypatch):
    _blob(pg, "operator", [_REC])
    with pytest.raises(StoreUnavailableError):
        _store(pg).list_memories()
    monkeypatch.setenv("JARVIS_DATABASE_MIGRATE_URL", pg.admin_dsn)
    monkeypatch.setenv("JARVIS_DATABASE_SCHEMA", pg.schema)
    assert pg_import.main(["--source-blob", "operator", "--blob-schema", pg.schema, "--apply"]) == 0
    s = _store(pg)  # a fresh store object re-checks
    assert [m.id for m in s.list_memories()] == ["mem-legacy-1"]


@pg_only
def test_ready_is_503_while_legacy_data_is_unimported(pg, monkeypatch):
    _blob(pg, "operator", [_REC])
    monkeypatch.setenv("JARVIS_DATABASE_URL", pg.app_dsn)
    monkeypatch.setenv("JARVIS_DATABASE_SCHEMA", pg.schema)
    monkeypatch.delenv("JARVIS_PG_STORE", raising=False)  # the new default
    reset_store_for_tests()
    with TestClient(app, raise_server_exceptions=False) as client:
        health = client.get("/ready")
        assert health.status_code == 503 and health.json()["status"] == "unavailable"
        for hint in ("jarvis_tenant_ledgers", "pg_import", "JARVIS_PG_STORE"):
            assert hint not in health.text  # remediation hints are for the log, not the response


@pg_only
def test_an_empty_legacy_ledger_does_not_block(pg):
    _blob(pg, "operator", [])
    assert _store(pg).list_memories() == []


@pg_only
def test_no_legacy_table_does_not_block(pg):
    assert _store(pg).list_memories() == []


@pg_only
def test_another_tenants_legacy_data_does_not_block_this_tenant(pg):
    _blob(pg, "someone-else", [_REC])
    assert _store(pg, "operator").list_memories() == []


@pg_only
def test_a_populated_row_store_is_not_blocked_by_the_old_table(pg):
    s = _store(pg)
    s.create_memory(MemoryCreate(content="already in the row store", source_agent="t", session_id="s", type="fact"))
    _blob(pg, "operator", [_REC])
    pg_store.close_pools()
    assert len(_store(pg).list_memories()) == 1


@pg_only
def test_the_guard_can_be_overridden_explicitly(pg, monkeypatch):
    _blob(pg, "operator", [_REC])
    monkeypatch.setenv("JARVIS_PG_IGNORE_LEGACY_BLOB", "1")
    assert _store(pg).list_memories() == []


@pg_only
def test_a_role_that_cannot_read_the_old_table_is_not_blocked(pg):
    _blob(pg, "operator", [_REC], grant=False)
    assert _store(pg).list_memories() == []  # nothing the guard could (or should) check
