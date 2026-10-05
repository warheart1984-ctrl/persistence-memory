"""Row-level Postgres schema: constraints, FK, version bump, RLS, migration runner, guard."""

from __future__ import annotations

import psycopg
import pytest

from app.pg_schema import EXPECTED_SCHEMA_VERSION, check_schema_version, migrate
from app.store_errors import StoreUnavailableError

pytestmark = pytest.mark.postgres

_SHA = "a" * 64


@pytest.fixture
def pg(pg_schema):
    migrate(pg_schema.admin_dsn, schema=pg_schema.schema, app_role="jarvis_app_test")
    return pg_schema


def _row(**over):
    base = dict(
        tenant_key="alice", id="mem-1", content="hello", content_sha256=_SHA,
        created_at="2026-07-01T00:00:00+00:00", updated_at="2026-07-01T00:00:00+00:00",
        source_agent="t", session_id="s", type="fact", status="draft", confidence=0.5,
        subject=None, supersedes=None, tags=[], evidence="[]",
    )
    base.update(over)
    return base


_INSERT = """INSERT INTO memories (tenant_key,id,content,content_sha256,created_at,updated_at,source_agent,
 session_id,type,status,confidence,subject,supersedes,tags,evidence)
 VALUES (%(tenant_key)s,%(id)s,%(content)s,%(content_sha256)s,%(created_at)s,%(updated_at)s,%(source_agent)s,
 %(session_id)s,%(type)s,%(status)s,%(confidence)s,%(subject)s,%(supersedes)s,%(tags)s,%(evidence)s::jsonb)"""


def _insert(pg, **over):
    row = _row(**over)
    with pg.app_conn(row["tenant_key"]) as conn:
        conn.execute(_INSERT, row)


def test_migrate_applies_and_is_idempotent(pg_schema):
    assert migrate(pg_schema.admin_dsn, schema=pg_schema.schema) == EXPECTED_SCHEMA_VERSION
    assert migrate(pg_schema.admin_dsn, schema=pg_schema.schema) == EXPECTED_SCHEMA_VERSION
    with pg_schema.admin_conn() as conn:
        assert conn.execute("SELECT count(*) FROM schema_version").fetchone()[0] == EXPECTED_SCHEMA_VERSION


@pytest.mark.parametrize(
    "over",
    [
        {"confidence": 5}, {"confidence": -0.1}, {"confidence": 1.0001},
        {"type": "BOGUS"}, {"status": "BOGUS"}, {"content": ""},
        {"content_sha256": "xyz"}, {"tags": ["t"] * 33}, {"evidence": "{}"},
        {"evidence": "[" + ",".join(["{}"] * 33) + "]"}, {"id": ""}, {"session_id": ""},
    ],
    ids=lambda o: next(iter(o)) + "=" + str(next(iter(o.values())))[:12],
)
def test_check_constraints_reject_bad_rows(pg, over):
    with pytest.raises(psycopg.errors.CheckViolation):
        _insert(pg, **over)


def test_boundary_values_accepted(pg):
    for i, c in enumerate((0, 1, 0.5)):
        _insert(pg, id=f"mem-{i}", confidence=c)


def test_same_id_allowed_in_different_tenants_but_not_twice(pg):
    _insert(pg, tenant_key="alice")
    _insert(pg, tenant_key="bob")
    with pytest.raises(psycopg.errors.UniqueViolation):
        _insert(pg, tenant_key="alice")


def test_supersedes_fk_rejects_missing_target(pg):
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        _insert(pg, id="mem-2", supersedes="mem-missing")


def test_supersedes_fk_is_per_tenant(pg):
    _insert(pg, tenant_key="bob", id="mem-1")
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        _insert(pg, tenant_key="alice", id="mem-2", supersedes="mem-1")  # target exists only for bob


def test_deleting_target_nulls_only_the_pointer(pg):
    _insert(pg, id="mem-1")
    _insert(pg, id="mem-2", supersedes="mem-1")
    with pg.app_conn("alice") as conn:
        conn.execute("DELETE FROM memories WHERE tenant_key='alice' AND id='mem-1'")
    with pg.app_conn("alice") as conn:
        row = conn.execute("SELECT tenant_key, supersedes FROM memories WHERE id='mem-2'").fetchone()
    assert row == ("alice", None)


def test_update_bumps_version_and_identity_is_immutable(pg):
    _insert(pg)
    with pg.app_conn("alice") as conn:
        conn.execute("UPDATE memories SET content='changed' WHERE id='mem-1'")
        conn.execute("UPDATE memories SET content='changed again' WHERE id='mem-1'")
        assert conn.execute("SELECT version FROM memories WHERE id='mem-1'").fetchone()[0] == 3
    with pytest.raises(psycopg.errors.RaiseException):
        with pg.app_conn("alice") as conn:
            conn.execute("UPDATE memories SET id='other' WHERE id='mem-1'")


def test_client_cannot_set_version_directly(pg):
    _insert(pg)
    with pg.app_conn("alice") as conn:
        conn.execute("UPDATE memories SET version=99 WHERE id='mem-1'")  # trigger overrides it
        assert conn.execute("SELECT version FROM memories WHERE id='mem-1'").fetchone()[0] == 2


def test_rls_isolates_tenants(pg):
    _insert(pg, tenant_key="alice", id="mem-a")
    _insert(pg, tenant_key="bob", id="mem-b")
    with pg.app_conn("alice") as conn:
        assert [r[0] for r in conn.execute("SELECT id FROM memories").fetchall()] == ["mem-a"]
        assert conn.execute("DELETE FROM memories WHERE id='mem-b'").rowcount == 0
        assert conn.execute("UPDATE memories SET content='x' WHERE id='mem-b'").rowcount == 0
    with pg.app_conn("bob") as conn:
        assert [r[0] for r in conn.execute("SELECT id FROM memories").fetchall()] == ["mem-b"]


def test_rls_denies_everything_without_a_tenant_setting(pg):
    _insert(pg)
    with pg.app_conn(None) as conn:
        assert conn.execute("SELECT count(*) FROM memories").fetchone()[0] == 0
    with pytest.raises(psycopg.errors.InsufficientPrivilege):  # WITH CHECK fails
        with pg.app_conn(None) as conn:
            conn.execute(_INSERT, _row(id="mem-9"))


def test_rls_with_check_blocks_writing_into_another_tenant(pg):
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        with pg.app_conn("alice") as conn:
            conn.execute(_INSERT, _row(tenant_key="bob"))


def test_app_role_cannot_run_ddl_or_disable_rls(pg):
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        with pg.app_conn("alice") as conn:
            conn.execute("CREATE TABLE sneaky (x int)")
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        with pg.app_conn("alice") as conn:
            conn.execute("ALTER TABLE memories DISABLE ROW LEVEL SECURITY")


def test_boards_table_is_tenant_isolated_and_versioned(pg):
    with pg.app_conn("alice") as conn:
        conn.execute("""INSERT INTO boards (tenant_key, board) VALUES ('alice', '{"summary":"a"}')""")
        conn.execute("""UPDATE boards SET board='{"summary":"b"}' WHERE tenant_key='alice'""")
        assert conn.execute("SELECT version FROM boards").fetchone()[0] == 2
    with pg.app_conn("bob") as conn:
        assert conn.execute("SELECT count(*) FROM boards").fetchone()[0] == 0
    with pytest.raises(psycopg.errors.CheckViolation):
        with pg.app_conn("bob") as conn:
            conn.execute("INSERT INTO boards (tenant_key, board) VALUES ('bob', '[]')")


def test_schema_guard_passes_when_migrated(pg):
    with pg.app_conn("alice") as conn:
        check_schema_version(conn)


def test_schema_guard_fails_closed_when_not_migrated(pg_schema):
    with pg_schema.admin_conn() as conn:
        with pytest.raises(StoreUnavailableError):
            check_schema_version(conn)


def test_schema_guard_fails_closed_on_version_mismatch(pg):
    with pg.admin_conn() as conn:
        conn.execute("INSERT INTO schema_version (version) VALUES (999)")
        with pytest.raises(StoreUnavailableError):
            check_schema_version(conn)


def test_migrate_rejects_bad_schema_identifier(pg_schema):
    with pytest.raises(ValueError):
        migrate(pg_schema.admin_dsn, schema='x"; DROP SCHEMA public; --')


def test_cli_migrates_from_env_and_never_prints_the_dsn(pg_schema, monkeypatch, capsys):
    from app import pg_migrate

    monkeypatch.setenv("JARVIS_DATABASE_MIGRATE_URL", pg_schema.admin_dsn)
    monkeypatch.setenv("JARVIS_DATABASE_SCHEMA", pg_schema.schema)
    monkeypatch.setenv("JARVIS_DATABASE_APP_ROLE", "jarvis_app_test")
    assert pg_migrate.main() == 0
    out = capsys.readouterr()
    assert f"version {EXPECTED_SCHEMA_VERSION}" in out.out
    assert "postgresql://" not in out.out + out.err
    with pg_schema.app_conn("alice") as conn:
        check_schema_version(conn)


def test_cli_requires_a_dsn(monkeypatch):
    from app import pg_migrate

    monkeypatch.delenv("JARVIS_DATABASE_MIGRATE_URL", raising=False)
    monkeypatch.delenv("JARVIS_DATABASE_URL", raising=False)
    assert pg_migrate.main() == 2
