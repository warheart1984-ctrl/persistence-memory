"""/health is liveness (the process is up); /ready is readiness (the ledger can be served safely)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import pg_store
from app.main import app
from app.pg_schema import migrate
from app.refusal import LEDGER_UNAVAILABLE
from app.store import reset_store_for_tests

pg_only = pytest.mark.postgres


@pytest.fixture
def client():
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


# --- liveness never touches the ledger ---------------------------------------------------------------


def test_health_is_liveness_only_and_leaks_no_ledger_state(client, monkeypatch):
    monkeypatch.setenv("JARVIS_DATABASE_URL", "postgresql://u:p@127.0.0.1:9/none")  # unreachable
    monkeypatch.setenv("JARVIS_PG_STORE", "rows")
    reset_store_for_tests()
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok" and body["live"] is True and body["schema"] == "continuity-ledger-v1"
    for leaked in ("memory_count", "board_id", "store_path"):
        assert leaked not in body
    assert "127.0.0.1" not in response.text and "postgresql" not in response.text


def test_ready_is_public_like_health(client, monkeypatch):
    monkeypatch.delenv("JARVIS_ALLOW_UNAUTHENTICATED", raising=False)
    monkeypatch.setenv("JARVIS_API_KEY", "some-key")
    assert client.get("/ready").status_code == 200  # no key needed, like /health
    assert client.get("/health").status_code == 200


# --- JSON store ------------------------------------------------------------------------------------------


@pytest.mark.json_store_only
def test_json_store_is_ready_when_it_loads(client):
    response = client.get("/ready")
    assert response.status_code == 200
    assert response.json() == {"status": "ready", "checks": {"store": "ok"}}


# --- PostgreSQL --------------------------------------------------------------------------------------------


@pytest.fixture
def pg(pg_schema, monkeypatch):
    migrate(pg_schema.admin_dsn, schema=pg_schema.schema, app_role="jarvis_app_test")
    monkeypatch.setenv("JARVIS_DATABASE_SCHEMA", pg_schema.schema)
    monkeypatch.setenv("JARVIS_PG_STORE", "rows")
    monkeypatch.setenv("JARVIS_DATABASE_URL", pg_schema.app_dsn)
    monkeypatch.setenv("JARVIS_DATABASE_POOL_TIMEOUT_MS", "300")
    reset_store_for_tests()
    yield pg_schema
    pg_store.close_pools()


_ALL_OK = {"database": "ok", "schema_version": "ok", "role": "ok", "history_write_denied": "ok", "legacy_data": "ok"}


@pg_only
def test_ready_when_every_check_passes(pg, client):
    response = client.get("/ready")
    assert response.status_code == 200
    assert response.json() == {"status": "ready", "checks": _ALL_OK}


def _not_ready(client, **expected):
    response = client.get("/ready")
    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "unavailable" and body["code"] == LEDGER_UNAVAILABLE
    assert response.headers["retry-after"] == "5"
    for name, state in expected.items():
        assert body["checks"][name] == state, body
    assert client.get("/health").status_code == 200  # liveness is unaffected
    return response


@pg_only
def test_not_ready_when_the_database_is_unreachable(client, monkeypatch):
    monkeypatch.setenv("JARVIS_DATABASE_URL", "postgresql://secretuser:secretpw@127.0.0.1:9/none")
    monkeypatch.setenv("JARVIS_PG_STORE", "rows")
    monkeypatch.setenv("JARVIS_DATABASE_POOL_TIMEOUT_MS", "300")
    reset_store_for_tests()
    try:
        response = _not_ready(client, database="failed")
        for secret in ("secretuser", "secretpw", "127.0.0.1", "postgresql://"):
            assert secret not in response.text
    finally:
        pg_store.close_pools()


@pg_only
def test_not_ready_when_the_schema_is_missing_or_mismatched(pg, client):
    with pg.admin_conn() as conn:
        conn.execute("INSERT INTO schema_version (version) VALUES (999)")
    _not_ready(client, schema_version="failed")


@pg_only
def test_not_ready_for_an_unmigrated_database(pg_schema, client, monkeypatch):
    monkeypatch.setenv("JARVIS_DATABASE_SCHEMA", pg_schema.schema)
    monkeypatch.setenv("JARVIS_PG_STORE", "rows")
    monkeypatch.setenv("JARVIS_DATABASE_URL", pg_schema.app_dsn)
    reset_store_for_tests()
    try:
        _not_ready(client, schema_version="failed")
    finally:
        pg_store.close_pools()


@pg_only
def test_not_ready_when_the_app_connects_as_a_superuser(pg, client, monkeypatch):
    monkeypatch.setenv("JARVIS_DATABASE_URL", pg.admin_dsn)
    reset_store_for_tests()
    _not_ready(client, role="failed")


@pg_only
def test_not_ready_when_the_role_bypasses_rls(pg, client, monkeypatch):
    from psycopg.conninfo import make_conninfo

    with pg.admin_conn() as conn:
        conn.execute("DROP ROLE IF EXISTS jarvis_ready_bypass")
        conn.execute("CREATE ROLE jarvis_ready_bypass LOGIN BYPASSRLS NOSUPERUSER PASSWORD 'bypass-pw'")
    try:
        migrate(pg.admin_dsn, schema=pg.schema, app_role="jarvis_ready_bypass")
        monkeypatch.setenv("JARVIS_DATABASE_URL", make_conninfo(pg.admin_dsn, user="jarvis_ready_bypass", password="bypass-pw"))
        reset_store_for_tests()
        _not_ready(client, role="failed")
    finally:
        pg_store.close_pools()
        with pg.admin_conn() as conn:
            conn.execute("DROP OWNED BY jarvis_ready_bypass")
            conn.execute("DROP ROLE jarvis_ready_bypass")


@pg_only
@pytest.mark.parametrize(
    "grant",
    [
        "GRANT INSERT ON record_history TO jarvis_app_test",
        "GRANT UPDATE ON record_history TO jarvis_app_test",
        "GRANT DELETE ON record_history TO jarvis_app_test",
        "GRANT TRUNCATE ON record_history TO jarvis_app_test",
        "GRANT INSERT ON chain_heads TO jarvis_app_test",
        "GRANT UPDATE ON history_counters TO jarvis_app_test",
    ],
)
def test_not_ready_when_the_role_could_write_the_history(pg, client, grant):
    assert client.get("/ready").status_code == 200
    with pg.admin_conn() as conn:
        conn.execute(grant)
    _not_ready(client, history_write_denied="failed")
    with pg.admin_conn() as conn:  # fixing the grant fixes readiness: it is checked live, never cached
        conn.execute("REVOKE ALL ON record_history, chain_heads, history_counters FROM jarvis_app_test")
        conn.execute("GRANT SELECT ON record_history, chain_heads, history_counters TO jarvis_app_test")
    assert client.get("/ready").status_code == 200


@pg_only
def test_the_readiness_probe_leaves_no_trace(pg, client):
    for _ in range(3):
        assert client.get("/ready").status_code == 200
    with pg.admin_conn() as conn:
        counts = [conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
                  for t in ("memories", "boards", "record_history", "chain_heads", "history_counters")]
        locks = conn.execute("SELECT count(*) FROM pg_locks l JOIN pg_class c ON c.oid = l.relation "
                             "WHERE c.relname = 'record_history' AND l.mode = 'AccessExclusiveLock'").fetchone()[0]
    assert counts == [0, 0, 0, 0, 0] and locks == 0


@pg_only
def test_not_ready_while_legacy_data_is_unimported(pg, client, monkeypatch):
    import json

    monkeypatch.setenv("JARVIS_LEGACY_BLOB_SCHEMA", pg.schema)
    with pg.admin_conn() as conn:
        conn.execute("CREATE TABLE jarvis_tenant_ledgers (tenant_key text PRIMARY KEY, payload jsonb NOT NULL)")
        conn.execute("INSERT INTO jarvis_tenant_ledgers VALUES ('operator', %s::jsonb)", (json.dumps({"memories": [{"id": "x"}]}),))
        conn.execute("GRANT SELECT ON jarvis_tenant_ledgers TO jarvis_app_test")
    _not_ready(client, legacy_data="failed")
