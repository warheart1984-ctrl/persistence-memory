"""/health is liveness (the process is up); /ready is readiness (the ledger can be served safely)."""

from __future__ import annotations

import time

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


_ALL_OK = {"database": "ok", "schema_version": "ok", "role": "ok", "rls_tables": "ok", "history_write_denied": "ok", "legacy_data": "ok"}


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
@pytest.mark.parametrize(
    "weaken",
    [
        "ALTER TABLE memories NO FORCE ROW LEVEL SECURITY",                      # the owner (and so a role that becomes the owner) is exempt again
        "ALTER TABLE memories DISABLE ROW LEVEL SECURITY",                       # no row-level security at all on a tenant table
        "ALTER TABLE record_history NO FORCE ROW LEVEL SECURITY",
        "DROP POLICY IF EXISTS tenant_isolation ON boards",                      # RLS is on, but there is no policy: nothing is isolated by it
    ],
)
def test_not_ready_when_a_tenant_table_loses_forced_row_level_security(pg, client, weaken):
    """The role check proves the app role cannot bypass RLS; this proves there is RLS, forced, with a policy, on every table that holds a tenant's rows."""
    assert client.get("/ready").status_code == 200
    with pg.admin_conn() as conn:
        policies = [r[0] for r in conn.execute("SELECT polname FROM pg_policy p JOIN pg_class c ON c.oid = p.polrelid WHERE c.relname = 'boards'").fetchall()]
        if weaken.startswith("DROP POLICY"):
            weaken = f"DROP POLICY IF EXISTS {policies[0]} ON boards"
        conn.execute(weaken)
    r = _not_ready(client, rls_tables="failed")
    assert "memories" not in r.text and "record_history" not in r.text and "boards" not in r.text      # the offenders are named in the log, never in the response


@pg_only
def test_not_ready_when_a_new_tenant_table_is_added_without_row_level_security(pg, client):
    """A migration that adds a table with a tenant_key and forgets RLS is the drift this check exists for."""
    assert client.get("/ready").status_code == 200
    with pg.admin_conn() as conn:
        conn.execute("CREATE TABLE forgotten (tenant_key text NOT NULL, id text NOT NULL, note text)")
    _not_ready(client, rls_tables="failed")
    with pg.admin_conn() as conn:                                                # fixed live, never cached
        conn.execute("ALTER TABLE forgotten ENABLE ROW LEVEL SECURITY")
        conn.execute("ALTER TABLE forgotten FORCE ROW LEVEL SECURITY")
        conn.execute("CREATE POLICY isolate ON forgotten USING (tenant_key = current_setting('jarvis.tenant_key', true))")
    assert client.get("/ready").status_code == 200


@pg_only
def test_the_offending_tables_are_named_by_the_helper_and_the_real_schema_has_none(pg):
    with pg.admin_conn() as conn:
        conn.row_factory = __import__("psycopg").rows.dict_row
        assert pg_store.tenant_tables_without_forced_rls(conn, pg.schema) == []
        conn.execute("ALTER TABLE boards NO FORCE ROW LEVEL SECURITY")
        assert pg_store.tenant_tables_without_forced_rls(conn, pg.schema) == ["boards"]


@pg_only
def test_the_retained_legacy_blob_table_is_not_a_tenant_table_for_the_rls_check(pg, client):
    """The importer leaves jarvis_tenant_ledgers (a tenant_key, no RLS) in place as a rollback copy; it must not hold /ready at 503 after a successful import.
    Only that exact table is exempt: a look-alike with a tenant_key and no RLS is still drift."""
    with pg.admin_conn() as conn:
        conn.execute("CREATE TABLE jarvis_tenant_ledgers (tenant_key text PRIMARY KEY, payload jsonb NOT NULL DEFAULT '{}')")
    assert client.get("/ready").status_code == 200
    with pg.admin_conn() as conn:
        conn.row_factory = __import__("psycopg").rows.dict_row
        assert pg_store.tenant_tables_without_forced_rls(conn, pg.schema) == []
        conn.execute("CREATE TABLE jarvis_tenant_ledgers_copy (tenant_key text PRIMARY KEY)")
        assert pg_store.tenant_tables_without_forced_rls(conn, pg.schema) == ["jarvis_tenant_ledgers_copy"]


@pg_only
def test_a_schema_with_no_tenant_tables_is_not_ready(pg):
    with pg.admin_conn() as conn:
        conn.row_factory = __import__("psycopg").rows.dict_row
        assert pg_store.tenant_tables_without_forced_rls(conn, "no_such_schema_here") == ["(no tenant table found in the schema)"]


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


# ---------------------------------------------------------------------------------------------------- strict-mode STARTUP guard (JARVIS_MULTI_TENANT)
def _strict(monkeypatch):
    monkeypatch.setenv("JARVIS_MULTI_TENANT", "1")
    monkeypatch.setenv("JARVIS_API_KEY", "startup-guard-key-0123456789")
    monkeypatch.delenv("JARVIS_ALLOW_UNAUTHENTICATED", raising=False)
    monkeypatch.delenv("JARVIS_AUTH_MODE", raising=False)


@pg_only
def test_the_startup_guard_passes_on_a_healthy_database_and_the_app_starts_in_strict_mode(pg, monkeypatch):
    _strict(monkeypatch)
    assert pg_store.assert_tenant_isolation_ready() is None
    with TestClient(app, raise_server_exceptions=False) as c:                         # the lifespan runs the guard
        assert c.get("/ready").status_code == 200


@pg_only
@pytest.mark.parametrize("weaken", [
    "ALTER TABLE memories NO FORCE ROW LEVEL SECURITY",
    "ALTER TABLE memories DISABLE ROW LEVEL SECURITY",
    "DROP POLICY IF EXISTS tenant_isolation ON boards",
    "CREATE TABLE forgotten (tenant_key text NOT NULL, id text NOT NULL)",
])
def test_the_startup_guard_refuses_at_once_when_a_tenant_table_is_not_protected_and_the_app_does_not_start(pg, monkeypatch, weaken):
    _strict(monkeypatch)
    monkeypatch.setenv("JARVIS_STARTUP_DB_WAIT_SECONDS", "30")                  # a definitive failure must not wait this out
    with pg.admin_conn() as conn:
        if weaken.startswith("DROP POLICY"):
            name = conn.execute("SELECT polname FROM pg_policy p JOIN pg_class c ON c.oid = p.polrelid WHERE c.relname = 'boards'").fetchone()[0]
            weaken = f"DROP POLICY IF EXISTS {name} ON boards"
        conn.execute(weaken)
    started = time.monotonic()
    with pytest.raises(RuntimeError, match="refuses to start"):
        pg_store.assert_tenant_isolation_ready()
    with pytest.raises(RuntimeError, match="refuses to start"):
        with TestClient(app, raise_server_exceptions=False):
            pass
    assert time.monotonic() - started < 10


@pg_only
def test_with_the_flag_off_the_same_unprotected_database_still_starts_as_today(pg, monkeypatch):
    monkeypatch.delenv("JARVIS_MULTI_TENANT", raising=False)
    with pg.admin_conn() as conn:
        conn.execute("ALTER TABLE memories NO FORCE ROW LEVEL SECURITY")
    with TestClient(app, raise_server_exceptions=False) as c:                         # (/ready says 503; nothing changed about starting)
        assert c.get("/ready").status_code == 503


@pg_only
def test_the_startup_guard_refuses_a_role_that_bypasses_row_level_security(pg, monkeypatch):
    _strict(monkeypatch)
    monkeypatch.setenv("JARVIS_DATABASE_URL", pg.admin_dsn)                           # the superuser DSN: RLS would not apply to it
    pg_store.close_pools()
    with pytest.raises(RuntimeError, match="refuses to start"):
        pg_store.assert_tenant_isolation_ready()


@pg_only
def test_the_startup_guard_still_accepts_the_retained_legacy_blob_table(pg, monkeypatch):
    _strict(monkeypatch)
    with pg.admin_conn() as conn:
        conn.execute("CREATE TABLE jarvis_tenant_ledgers (tenant_key text PRIMARY KEY, payload jsonb NOT NULL DEFAULT '{}')")
    assert pg_store.assert_tenant_isolation_ready() is None


def test_an_unreachable_database_is_retried_for_a_bounded_time_and_then_refused(monkeypatch):
    _strict(monkeypatch)
    monkeypatch.setenv("JARVIS_DATABASE_URL", "postgresql://u:p@127.0.0.1:9/none")
    monkeypatch.setenv("JARVIS_PG_STORE", "rows")
    monkeypatch.setenv("JARVIS_STARTUP_DB_WAIT_SECONDS", "2")
    monkeypatch.setenv("JARVIS_DATABASE_POOL_TIMEOUT_MS", "300")
    started = time.monotonic()
    with pytest.raises(RuntimeError, match="could not be reached and verified within 2s"):
        pg_store.assert_tenant_isolation_ready()
    assert 1.5 <= time.monotonic() - started < 20
    pg_store.close_pools()


def test_no_database_configured_is_refused_in_strict_mode(monkeypatch):
    _strict(monkeypatch)
    monkeypatch.delenv("JARVIS_DATABASE_URL", raising=False)
    with pytest.raises(RuntimeError, match="no PostgreSQL store"):
        pg_store.assert_tenant_isolation_ready()


# ------------------------------------------------------------- review findings on the startup guard (policies must isolate; wrapped connection errors are transient)
def _policy_name(conn, table):
    return conn.execute("SELECT polname FROM pg_policy p JOIN pg_class c ON c.oid = p.polrelid WHERE c.relname = %s LIMIT 1", (table,)).fetchone()[0]


@pg_only
def test_a_permissive_extra_policy_or_a_replaced_policy_is_not_isolation(pg, monkeypatch):
    """Policies are OR-ed: `USING (true)` added beside the real one, or in its place, leaves RLS 'on' and isolating nothing. The guard and /ready must both say so."""
    _strict(monkeypatch)
    with pg.admin_conn() as conn:
        conn.execute("CREATE POLICY open_to_all ON boards USING (true)")
    with pg.admin_conn() as conn:
        conn.row_factory = __import__("psycopg").rows.dict_row
        assert pg_store.tenant_tables_without_forced_rls(conn, pg.schema) == ["boards"]
    with pytest.raises(RuntimeError, match="refuses to start"):
        pg_store.assert_tenant_isolation_ready()
    with pg.admin_conn() as conn:
        conn.execute("DROP POLICY open_to_all ON boards")
    assert pg_store.assert_tenant_isolation_ready() is None                               # positive control: back to the real policy
    with pg.admin_conn() as conn:
        name = _policy_name(conn, "memories")
        conn.execute(f"DROP POLICY {name} ON memories")
        conn.execute("CREATE POLICY replaced ON memories USING (true) WITH CHECK (true)")
    with pytest.raises(RuntimeError, match="refuses to start"):
        pg_store.assert_tenant_isolation_ready()


@pg_only
def test_the_isolation_probe_catches_a_policy_that_mentions_the_setting_but_is_always_true(pg, monkeypatch):
    """The text check passes `tenant_key = current_setting(...) OR true`; only behaviour shows it: a tenant that owns nothing can read another tenant's row."""
    from app.models import EvidenceLink, MemoryCreate
    from app.pg_store import PostgresRowStore

    _strict(monkeypatch)
    PostgresRowStore(pg.app_dsn, "someone", schema=pg.schema).create_memory(MemoryCreate(
        content="a row that must stay invisible to others", source_agent="t", session_id="s", type="fact", evidence=[EvidenceLink(kind="doc", ref="x", note="y")]))
    assert pg_store.assert_tenant_isolation_ready() is None                               # positive control, with data present
    with pg.admin_conn() as conn:
        name = _policy_name(conn, "memories")
        conn.execute(f"DROP POLICY {name} ON memories")
        conn.execute("CREATE POLICY sneaky ON memories USING (tenant_key = current_setting('jarvis.tenant_key', true) OR true) "
                     "WITH CHECK (tenant_key = current_setting('jarvis.tenant_key', true))")
    with pg.admin_conn() as conn:
        conn.row_factory = __import__("psycopg").rows.dict_row
        assert pg_store.tenant_tables_without_forced_rls(conn, pg.schema) == []          # the text check alone is fooled (that is why the probe exists)
    with pytest.raises(RuntimeError, match="does not isolate tenants"):
        pg_store.assert_tenant_isolation_ready()


@pg_only
def test_a_dropped_connection_inside_the_schema_check_is_retried_but_a_missing_schema_is_not(pg, monkeypatch):
    import psycopg

    from app.store_errors import StoreUnavailableError

    _strict(monkeypatch)
    monkeypatch.setenv("JARVIS_STARTUP_DB_WAIT_SECONDS", "10")
    real = pg_store.check_schema_version
    calls = {"n": 0}

    def flaky(conn):
        calls["n"] += 1
        if calls["n"] <= 2:
            raise StoreUnavailableError("Ledger schema is missing; run the migration") from psycopg.OperationalError("server closed the connection unexpectedly")
        return real(conn)

    monkeypatch.setattr(pg_store, "check_schema_version", flaky)
    assert pg_store.assert_tenant_isolation_ready() is None and calls["n"] == 3         # retried twice, then verified

    def missing(conn):
        raise StoreUnavailableError("Ledger schema is missing; run the migration") from psycopg.errors.UndefinedTable("relation schema_version does not exist")

    monkeypatch.setattr(pg_store, "check_schema_version", missing)
    started = time.monotonic()
    with pytest.raises(RuntimeError, match="refuses to start"):
        pg_store.assert_tenant_isolation_ready()
    assert time.monotonic() - started < 5                                              # a missing schema is definitive: no waiting
