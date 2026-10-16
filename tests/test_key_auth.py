"""Per-tenant API keys authenticate requests (RFC phase 1, slice 2), behind JARVIS_KEY_TENANTS.

What these pin: a key reaches exactly its own tenant, in a deployment whose OTHER credential is the shared operator key (so "not the operator" must come from the principal, not the deployment
mode), every failure is one indistinguishable 401, a lost control schema is a 503 and never an admission, a tenant that is not active is a 503 for that tenant only, and with the flag off nothing
changes. The whole isolation matrix is also run over key tenants in tests/test_tenant_isolation_keys.py.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg.rows import dict_row

from app import pg_store, tenant_keys
from app.main import app
from app.pg_schema import control_schema_name, migrate
from app.store import reset_store_for_tests

pg_only = pytest.mark.postgres
PEPPER = "key-auth-test-pepper-0123456789-0123456789"
OPERATOR_KEY = "operator-key-for-key-auth-tests-0123456789"


@pytest.fixture
def world(pg_schema, monkeypatch, tmp_path):
    migrate(pg_schema.admin_dsn, schema=pg_schema.schema, app_role="jarvis_app_test")
    control = control_schema_name(pg_schema.schema)
    for k, v in {"JARVIS_DATABASE_SCHEMA": pg_schema.schema, "JARVIS_PG_STORE": "rows", "JARVIS_DATABASE_URL": pg_schema.app_dsn, "JARVIS_DATABASE_POOL_TIMEOUT_MS": "500",
                 "JARVIS_KEY_TENANTS": "1", "JARVIS_KEY_PEPPER": PEPPER, "JARVIS_API_KEY": OPERATOR_KEY, "JARVIS_MEMORY_WRITE_ENABLED": "true",
                 "JARVIS_CURSOR_HMAC_KEY": "key-auth-cursor-key-0123456789", "JARVIS_CALL_LOG_ENABLED": "1", "JARVIS_CALL_LOG_DIR": str(tmp_path / "calls")}.items():
        monkeypatch.setenv(k, v)
    for k in ("JARVIS_AUTH_MODE", "JARVIS_ALLOW_UNAUTHENTICATED", "JARVIS_MULTI_TENANT", "EMR_RECALL_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    reset_store_for_tests()

    class World:
        schema, admin_dsn = pg_schema, pg_schema.admin_dsn

        def issue(self, label="t", **kw):
            with psycopg.connect(pg_schema.admin_dsn, autocommit=True, row_factory=dict_row) as c:
                tenant = tenant_keys.create_tenant(c, control, label)
                return tenant, tenant_keys.create_key(c, control, tenant, PEPPER.encode(), **kw)

        def more_keys(self, tenant, **kw):
            with psycopg.connect(pg_schema.admin_dsn, autocommit=True, row_factory=dict_row) as c:
                return tenant_keys.create_key(c, control, tenant, PEPPER.encode(), **kw)

        def admin(self):
            return psycopg.connect(pg_schema.admin_dsn, autocommit=True, row_factory=dict_row)

        control_name = control

    w = World()
    with TestClient(app, raise_server_exceptions=False) as client:
        w.client = client
        yield w
    pg_store.close_pools()


def bearer(token):
    return {"Authorization": f"Bearer {token}"}


def op():
    return {"X-API-Key": OPERATOR_KEY}


def write(client, headers, content):
    r = client.post("/api/jarvis/memory", headers=headers, json={"content": content, "source_agent": "t", "session_id": "s", "type": "fact"})
    assert r.status_code == 200, (r.status_code, r.text[:200])
    return r.json()["memory"]


def ids(client, headers):
    r = client.get("/api/jarvis/memory", headers=headers)
    assert r.status_code == 200, (r.status_code, r.text[:200])
    return {m["id"] for m in r.json()["memories"]}


# --------------------------------------------------------------------------------------------------------------------- tenant separation
@pg_only
def test_each_key_sees_only_its_own_tenants_records_and_the_operator_sees_neither(world):
    c = world.client
    _, alice = world.issue("alice")
    _, bob = world.issue("bob")
    a1 = write(c, bearer(alice), "alice private note")
    b1 = write(c, bearer(bob), "bob private note")
    o1 = write(c, op(), "operator note")
    assert ids(c, bearer(alice)) == {a1["id"]} and ids(c, bearer(bob)) == {b1["id"]} and ids(c, op()) == {o1["id"]}
    for who, other in ((bearer(bob), a1["id"]), (bearer(alice), b1["id"]), (bearer(alice), o1["id"])):
        assert c.get(f"/api/jarvis/memory/{other}", headers=who).status_code == 404               # an id that exists for someone else looks like one that exists for nobody
    assert c.get(f"/api/jarvis/memory/{a1['id']}", headers=bearer(alice)).status_code == 200      # positive control
    assert c.get(f"/api/jarvis/memory/{a1['id']}", headers=op()).status_code == 404               # the operator key is the operator's namespace, not a master key


@pg_only
def test_two_keys_of_one_tenant_share_that_tenants_ledger_and_the_x_api_key_header_works_too(world):
    c = world.client
    tenant, k1 = world.issue("two keys")
    k2 = world.more_keys(tenant, label="second")
    m = write(c, bearer(k1), "written with the first key")
    assert ids(c, bearer(k2)) == {m["id"]} and ids(c, {"X-API-Key": k2}) == {m["id"]}


# ----------------------------------------------------------------------------------------- a key tenant is never the operator (non-strict)
@pg_only
def test_a_key_tenant_is_refused_every_operator_only_surface_even_though_the_deployment_has_a_shared_key(world):
    """In a shared-key deployment `caller_is_operator()` used to be True for anyone who got past the key middleware; the principal must decide."""
    c = world.client
    _, key = world.issue("tenant")
    for method, path in (("GET", "/api/jarvis/blocks/head"), ("GET", "/api/jarvis/attestations/head"), ("GET", "/api/jarvis/tools/calls"), ("GET", "/api/jarvis/tools/calls/verify"),
                         ("GET", "/api/jarvis/llm/status"), ("GET", "/api/jarvis/replay/state"), ("GET", "/api/jarvis/drift/log"), ("POST", "/api/jarvis/blocks/seal")):
        r = c.request(method, path, headers=bearer(key), json={} if method == "POST" else None)
        assert r.status_code in (401, 403), (method, path, r.status_code)
        assert c.request(method, path, headers=op(), json={} if method == "POST" else None).status_code not in (401, 403), (method, path)     # positive control: the operator key reaches them


# ------------------------------------------------------------------------------------------------------------------------ failures
@pg_only
def test_every_way_a_token_can_fail_is_the_same_401_as_a_wrong_operator_key(world):
    c = world.client
    tenant, good = world.issue("x")
    key_id = tenant_keys.parse_token(good)[0]
    _, other = world.issue("y")
    unknown = tenant_keys.make_token(tenant_keys.new_key()[0], tenant_keys.new_key()[1])
    wrong_secret = tenant_keys.make_token(key_id, tenant_keys.new_key()[1])
    with world.admin() as a:
        revoked = world.more_keys(tenant)
        tenant_keys.revoke_key(a, world.control_name, tenant_keys.parse_token(revoked)[0])
    expired = world.more_keys(tenant, expires_at=datetime.now(timezone.utc) - timedelta(seconds=1))
    assert c.get("/api/jarvis/memory", headers=bearer(good)).status_code == 200                      # positive control
    wrong_operator = c.get("/api/jarvis/memory", headers={"X-API-Key": "wrong"})
    bodies = set()
    for bad in (unknown, wrong_secret, revoked, expired, "jlk_notakey", "jlk_" + "0" * 16 + "_" + "x" * 43):
        r = c.get("/api/jarvis/memory", headers=bearer(bad))
        assert r.status_code == 401 == wrong_operator.status_code, bad[:12]
        assert bad not in r.text                                                                     # a presented credential is never echoed
        bodies.add(json.dumps(r.json(), sort_keys=True))
    assert len(bodies) == 1 and json.dumps(wrong_operator.json(), sort_keys=True) in bodies          # one answer, and it is the wrong-ledger-key answer
    assert c.get("/api/jarvis/memory").status_code == 401                                          # no credential at all


@pg_only
def test_a_suspended_tenant_is_503_for_its_own_keys_only(world):
    c = world.client
    t1, k1 = world.issue("suspend me")
    _, k2 = world.issue("untouched")
    assert c.get("/api/jarvis/memory", headers=bearer(k1)).status_code == 200
    for status in ("suspended", "quiesced", "restoring", "deleting"):
        with world.admin() as a:
            tenant_keys.set_tenant_status(a, world.control_name, t1, status)
        r = c.get("/api/jarvis/memory", headers=bearer(k1))
        assert r.status_code == 503 and "tenant_unavailable" in r.text, status
        assert c.get("/api/jarvis/memory", headers=bearer(k2)).status_code == 200                      # another tenant is unaffected
    with world.admin() as a:
        tenant_keys.set_tenant_status(a, world.control_name, t1, "active")
    assert c.get("/api/jarvis/memory", headers=bearer(k1)).status_code == 200


@pg_only
def test_scopes_are_enforced_a_read_only_key_cannot_write_and_a_key_must_be_able_to_read(world):
    c = world.client
    _, reader = world.issue("r", scopes=("memory.read",))
    _, writer_only = world.issue("w", scopes=("memory.write",))
    _, both = world.issue("rw")
    assert c.get("/api/jarvis/memory", headers=bearer(reader)).status_code == 200
    assert c.post("/api/jarvis/memory", headers=bearer(reader), json={"content": "x", "source_agent": "t", "session_id": "s", "type": "fact"}).status_code == 403
    assert c.get("/api/jarvis/memory", headers=bearer(writer_only)).status_code == 403
    write(c, bearer(both), "allowed")


@pg_only
def test_the_operator_key_still_works_exactly_as_before_and_a_key_in_the_wrong_header_form_is_not_a_second_credential(world):
    c = world.client
    write(c, op(), "operator write")
    assert c.get("/api/jarvis/blocks/head", headers=op()).status_code != 403
    assert c.get("/api/jarvis/memory", headers={"X-API-Key": OPERATOR_KEY, "Authorization": "Bearer jlk_" + "0" * 16 + "_" + "y" * 43}).status_code == 200   # operator credential wins when present


# --------------------------------------------------------------------------------------------------------------------------- the flag
@pg_only
def test_with_the_flag_off_a_valid_tenant_key_is_just_a_wrong_key_and_nothing_else_changed(world, monkeypatch):
    c = world.client
    _, key = world.issue("flag off")
    assert c.get("/api/jarvis/memory", headers=bearer(key)).status_code == 200
    monkeypatch.delenv("JARVIS_KEY_TENANTS")
    r = c.get("/api/jarvis/memory", headers=bearer(key))
    assert r.status_code == 401 and key not in r.text
    assert c.get("/api/jarvis/memory", headers=op()).status_code == 200
    assert c.post("/api/jarvis/tools/emr_latest", headers=bearer(key), json={}).status_code in (401, 403)


@pg_only
def test_a_database_that_cannot_be_asked_is_a_503_never_an_admission(world, monkeypatch):
    c = world.client
    _, key = world.issue("db trouble")
    assert c.get("/api/jarvis/memory", headers=bearer(key)).status_code == 200
    monkeypatch.setenv("JARVIS_KEY_PEPPER", "short")                                               # pepper unusable
    r = c.get("/api/jarvis/memory", headers=bearer(key))
    assert r.status_code == 503 and "Ledger unavailable" in r.text
    monkeypatch.setenv("JARVIS_KEY_PEPPER", PEPPER)
    with world.admin() as a:
        a.execute(psycopg.sql.SQL("REVOKE EXECUTE ON FUNCTION {}.resolve_key(text) FROM jarvis_app_test").format(psycopg.sql.Identifier(world.control_name)))
    r = c.get("/api/jarvis/memory", headers=bearer(key))
    assert r.status_code == 503, r.status_code
    assert c.get("/api/jarvis/memory", headers=op()).status_code == 200                           # the operator key does not depend on the control schema


# ------------------------------------------------------------------------------------------------------------------------------ MCP, call log
@pg_only
def test_over_mcp_a_key_tenant_recalls_only_its_own_records(world):
    c = world.client
    _, alice = world.issue("a")
    _, bob = world.issue("b")
    write(c, bearer(alice), "the alice-only marker zebra")
    def recall(token):
        r = c.post("/mcp", headers=bearer(token), json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "emr_search_ledger", "arguments": {"query": "zebra"}}})
        assert r.status_code == 200, r.text[:200]
        return r.json()["result"]
    got_alice, got_bob = recall(alice), recall(bob)
    assert len(got_alice["structuredContent"]["records"]) == 1 and got_bob["structuredContent"]["records"] == []         # (the query text is echoed, so look at the records)
    assert got_alice["structuredContent"]["tenant"] != got_bob["structuredContent"]["tenant"]
    r = c.post("/mcp", headers=bearer(alice), json={"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "emr_drift_check", "arguments": {"summary": "x", "records": []}}})
    assert r.status_code == 200 and r.json()["result"]["isError"] is True                           # the drift check is the operator's


@pg_only
def test_the_call_log_files_a_key_tenants_calls_under_its_own_tenant_key(world, tmp_path):
    c = world.client
    tenant, key = world.issue("logged")
    write(c, bearer(key), "a logged write")
    c.get("/api/jarvis/memory", headers=bearer(key))
    from app import call_log
    entries = [json.loads(line) for f in sorted((tmp_path / "calls").glob("calls-*.jsonl")) for line in f.read_text().splitlines() if line.startswith("{")]
    mine = [e for e in entries if e.get("tenant") == tenant]
    assert mine and all(e.get("tenant") != "operator" for e in mine)
    assert json.dumps(entries).count(key) == 0                                                      # the token is not in the log


# ------------------------------------------------------------------------------------------------------------------------ strict mode
@pg_only
def test_key_tenants_work_in_strict_mode_and_unauthenticated_is_still_refused(world, monkeypatch):
    monkeypatch.setenv("JARVIS_MULTI_TENANT", "1")
    with TestClient(app, raise_server_exceptions=False) as c:                                       # the lifespan runs both startup guards
        _, key = world.issue("strict")
        m = write(c, bearer(key), "strict mode note")
        assert ids(c, bearer(key)) == {m["id"]}
        assert c.get("/api/jarvis/memory").status_code == 401
        assert c.get("/ready").status_code == 200


# ----------------------------------------------------------------------------------------------------------------------------- startup
@pg_only
def test_startup_refuses_when_the_pepper_is_missing_or_weak(world, monkeypatch):
    monkeypatch.delenv("JARVIS_KEY_PEPPER")
    with pytest.raises(RuntimeError, match="JARVIS_KEY_TENANTS refuses to start"):
        with TestClient(app):
            pass
    monkeypatch.setenv("JARVIS_KEY_PEPPER", "short")
    with pytest.raises(RuntimeError, match="refuses to start"):
        with TestClient(app):
            pass


@pg_only
def test_startup_refuses_when_the_control_schema_is_missing_or_not_callable(world, monkeypatch):
    with world.admin() as a:
        a.execute(psycopg.sql.SQL("REVOKE EXECUTE ON FUNCTION {}.resolve_key(text) FROM jarvis_app_test").format(psycopg.sql.Identifier(world.control_name)))
    with pytest.raises(RuntimeError, match="may not call the control schema"):
        with TestClient(app):
            pass
    with world.admin() as a:
        a.execute(psycopg.sql.SQL("GRANT EXECUTE ON FUNCTION {}.resolve_key(text) TO jarvis_app_test").format(psycopg.sql.Identifier(world.control_name)))
        a.execute(psycopg.sql.SQL("DROP SCHEMA {} CASCADE").format(psycopg.sql.Identifier(world.control_name)))
    with pytest.raises(RuntimeError, match="control schema or its lookup function is missing"):
        with TestClient(app):
            pass


def test_startup_refuses_without_a_postgres_store(monkeypatch):
    monkeypatch.setenv("JARVIS_KEY_TENANTS", "1")
    monkeypatch.setenv("JARVIS_KEY_PEPPER", PEPPER)
    monkeypatch.setenv("JARVIS_API_KEY", OPERATOR_KEY)
    monkeypatch.delenv("JARVIS_DATABASE_URL", raising=False)
    monkeypatch.delenv("JARVIS_AUTH_MODE", raising=False)
    with pytest.raises(RuntimeError, match="no PostgreSQL store"):
        with TestClient(app):
            pass
