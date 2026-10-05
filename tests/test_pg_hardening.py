"""Gapless per-tenant history sequence, chain heads, forge-proof history, no RLS-bypassing roles."""

from __future__ import annotations

import threading

import psycopg
import pytest
from fastapi.testclient import TestClient

from app import pg_store
from app.main import app
from app.models import MemoryCreate, MemoryUpdate
from app.pg_schema import EXPECTED_SCHEMA_VERSION, migrate
from app.pg_store import PostgresRowStore
from app.store import StoreUnavailableError

pytestmark = pytest.mark.postgres


@pytest.fixture
def pg(pg_schema):
    migrate(pg_schema.admin_dsn, schema=pg_schema.schema, app_role="jarvis_app_test")
    yield pg_schema
    pg_store.close_pools()


@pytest.fixture
def store(pg):
    return PostgresRowStore(pg.app_dsn, "alice", schema=pg.schema)


def _new(store, content="a ledger record", **kw):
    base = dict(content=content, source_agent="t", session_id="s", type="fact")
    base.update(kw)
    return store.create_memory(MemoryCreate(**base))


def _admin(pg, *statements):
    """Run statements as the superuser with the append-only guards switched off (a tamper)."""
    with pg.admin_conn() as conn:
        conn.execute("ALTER TABLE record_history DISABLE TRIGGER record_history_no_update")
        conn.execute("ALTER TABLE record_history DISABLE TRIGGER record_history_no_delete")
        try:
            for stmt in statements:
                conn.execute(stmt)
        finally:
            conn.execute("ALTER TABLE record_history ENABLE TRIGGER record_history_no_update")
            conn.execute("ALTER TABLE record_history ENABLE TRIGGER record_history_no_delete")


def _problems(store, rec_id=None):
    return [p["problem"] for p in store.verify_history(rec_id)]


def _lifecycle(store):
    rec = _new(store, "lifecycle record text")
    store.update_memory(rec.id, MemoryUpdate(subject="one"))
    store.delete_memory(rec.id)
    return rec


# --- sequence + heads ---------------------------------------------------------------------


def test_history_sequence_is_gapless_per_tenant(pg):
    alice = PostgresRowStore(pg.app_dsn, "alice", schema=pg.schema)
    bob = PostgresRowStore(pg.app_dsn, "bob", schema=pg.schema)
    a = _new(alice)
    b = _new(bob)
    alice.update_memory(a.id, MemoryUpdate(subject="x"))
    bob.update_memory(b.id, MemoryUpdate(subject="y"))
    alice.delete_memory(a.id)
    assert [e["seq"] for e in alice.history(a.id)] == [1, 2, 3]
    assert [e["seq"] for e in bob.history(b.id)] == [1, 2]


def test_rolled_back_and_conflicting_writes_consume_no_sequence_number(store):
    from app.store import StoreVersionConflict

    rec = _new(store)
    with pytest.raises(ValueError):
        store.update_memory(rec.id, MemoryUpdate(supersedes="mem-missing"))
    with pytest.raises(StoreVersionConflict):
        store.update_memory(rec.id, MemoryUpdate(subject="x", expected_version=99))
    store.update_memory(rec.id, MemoryUpdate(subject="real"))
    assert [e["seq"] for e in store.history(rec.id)] == [1, 2]
    assert store.verify_history() == []


def test_chain_heads_track_the_latest_entry_including_deleted_records(pg, store):
    rec = _new(store)
    store.update_memory(rec.id, MemoryUpdate(subject="x"))
    with pg.admin_conn() as conn:
        head = conn.execute("SELECT last_seq, last_hash, deleted FROM chain_heads WHERE id = %s", (rec.id,)).fetchone()
    last = store.history(rec.id)[-1]
    assert head == (last["seq"], last["row_hash"], False)
    store.delete_memory(rec.id)
    with pg.admin_conn() as conn:
        head = conn.execute("SELECT last_seq, last_hash, deleted FROM chain_heads WHERE id = %s", (rec.id,)).fetchone()
    last = store.history(rec.id)[-1]
    assert last["op"] == "delete" and head == (last["seq"], last["row_hash"], True)  # head survives deletion
    assert store.verify_history() == []


def test_removing_the_final_delete_entry_is_detected_even_though_the_record_is_gone(pg, store):
    rec = _lifecycle(store)
    assert store.verify_history() == []
    _admin(pg, "DELETE FROM record_history WHERE op = 'delete'")
    problems = _problems(store)
    assert any("missing history sequence number" in p for p in problems)
    assert any("chain head points at a missing history entry" in p for p in problems)
    assert store.get_memory(rec.id) is None


def test_removing_a_middle_entry_is_a_sequence_gap(pg, store):
    _lifecycle(store)
    _admin(pg, "DELETE FROM record_history WHERE seq = 2")
    assert any("missing history sequence number 2" in p for p in _problems(store))


def test_removing_every_entry_of_a_deleted_record_is_detected(pg, store):
    rec = _lifecycle(store)
    other = _new(store, "an unrelated record")
    _admin(pg, f"DELETE FROM record_history WHERE memory_id = '{rec.id}'")
    problems = _problems(store)
    assert any("missing history sequence number" in p for p in problems)
    assert any("chain head points at a missing history entry" in p for p in problems)
    assert store.get_memory(other.id) is not None


def test_tampered_chain_heads_and_counter_are_detected(pg, store):
    rec = _new(store, "head tamper target")
    store.update_memory(rec.id, MemoryUpdate(subject="x"))
    with pg.admin_conn() as conn:
        conn.execute("UPDATE chain_heads SET last_hash = repeat('f', 64) WHERE id = %s", (rec.id,))
    assert any("chain head hash does not match" in p for p in _problems(store))
    with pg.admin_conn() as conn:
        conn.execute("UPDATE chain_heads SET last_hash = (SELECT row_hash FROM record_history WHERE memory_id = %s ORDER BY seq DESC LIMIT 1), deleted = true WHERE id = %s", (rec.id, rec.id))
    assert any("deleted flag" in p or "live record differs" in p for p in _problems(store))
    with pg.admin_conn() as conn:
        conn.execute("UPDATE chain_heads SET deleted = false WHERE id = %s", (rec.id,))
        assert store.verify_history() == []
        conn.execute("DELETE FROM chain_heads WHERE id = %s", (rec.id,))
    assert any("no chain head" in p for p in _problems(store))


def test_lowered_counter_is_detected(pg, store):
    rec = _new(store)
    store.update_memory(rec.id, MemoryUpdate(subject="x"))
    with pg.admin_conn() as conn:
        conn.execute("UPDATE history_counters SET last_seq = 1")
    assert any("counter is behind" in p for p in _problems(store))


def test_concurrent_writers_in_two_tenants_keep_each_sequence_gapless(pg):
    stores = {t: [PostgresRowStore(pg.app_dsn, t, schema=pg.schema) for _ in range(2)] for t in ("alice", "bob")}
    recs = {t: _new(s[0], f"{t} record") for t, s in stores.items()}
    errors: list[BaseException] = []

    def worker(tenant, s, idx):
        from app.store import StoreVersionConflict

        for i in range(8):
            try:
                s.update_memory(recs[tenant].id, MemoryUpdate(subject=f"{idx}-{i}"))
            except StoreVersionConflict:
                pass
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

    threads = [threading.Thread(target=worker, args=(t, s, i)) for t, ss in stores.items() for i, s in enumerate(ss)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert errors == []
    for tenant, ss in stores.items():
        seqs = [e["seq"] for e in ss[0].history(recs[tenant].id)]
        assert seqs == list(range(1, len(seqs) + 1))
        assert ss[0].verify_history() == []


# --- the app role cannot forge or edit history ---------------------------------------------


@pytest.mark.parametrize(
    "stmt",
    [
        "INSERT INTO record_history (tenant_key,memory_id,version,op,actor,after,prev_hash,row_hash,seq) "
        "VALUES ('alice','mem-x',1,'create','mallory','{}',repeat('0',64),repeat('a',64),99)",
        "INSERT INTO chain_heads (tenant_key,id,last_seq,last_hash,deleted) VALUES ('alice','mem-x',1,repeat('a',64),false)",
        "INSERT INTO history_counters (tenant_key,last_seq) VALUES ('alice',1)",
        "UPDATE chain_heads SET deleted = true",
        "UPDATE history_counters SET last_seq = 0",
        "DELETE FROM chain_heads",
        "DELETE FROM history_counters",
    ],
)
def test_app_role_cannot_write_history_tables_directly(pg, store, stmt):
    _new(store)
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        with pg.app_conn("alice") as conn:
            conn.execute(stmt)


def test_app_role_can_read_only_its_own_tenants_heads(pg):
    alice = PostgresRowStore(pg.app_dsn, "alice", schema=pg.schema)
    bob = PostgresRowStore(pg.app_dsn, "bob", schema=pg.schema)
    a, b = _new(alice), _new(bob)
    with pg.app_conn("alice") as conn:
        assert [r[0] for r in conn.execute("SELECT id FROM chain_heads").fetchall()] == [a.id]
        assert conn.execute("SELECT count(*) FROM history_counters").fetchone()[0] == 1
    assert b.id != a.id


# --- upgrades -------------------------------------------------------------------------------


def _row_sql(rec_id, tenant="alice"):
    return (
        "INSERT INTO memories (tenant_key,id,content,content_sha256,created_at,updated_at,source_agent,"
        f"session_id,type,status,confidence) VALUES ('{tenant}','{rec_id}','legacy row text','{'a' * 64}',"
        "now(),now(),'t','s','fact','draft',0.5)"
    )


def test_upgrade_from_v2_backfills_sequence_and_heads_for_real_history(pg_schema):
    migrate(pg_schema.admin_dsn, schema=pg_schema.schema, app_role="jarvis_app_test", up_to=2)
    with pg_schema.app_conn("alice") as conn:
        conn.execute(_row_sql("mem-1"))
        conn.execute("UPDATE memories SET content = 'edited under v2' WHERE id = 'mem-1'")
        conn.execute(_row_sql("mem-2"))
    with pg_schema.app_conn("bob") as conn:
        conn.execute(_row_sql("mem-1", "bob"))
    assert migrate(pg_schema.admin_dsn, schema=pg_schema.schema, app_role="jarvis_app_test") == EXPECTED_SCHEMA_VERSION
    try:
        alice = PostgresRowStore(pg_schema.app_dsn, "alice", schema=pg_schema.schema)
        assert [(e["seq"], e["op"]) for e in alice.history("mem-1")] == [(1, "create"), (2, "update")]
        assert [e["seq"] for e in alice.history("mem-2")] == [3]
        assert alice.verify_history() == []
        alice.update_memory("mem-2", MemoryUpdate(subject="after upgrade"))
        assert [e["seq"] for e in alice.history("mem-2")] == [3, 4]  # numbering continues
        assert alice.verify_history() == []
        bob = PostgresRowStore(pg_schema.app_dsn, "bob", schema=pg_schema.schema)
        assert [e["seq"] for e in bob.history("mem-1")] == [1] and bob.verify_history() == []
    finally:
        pg_store.close_pools()


def test_upgrade_from_v1_still_works_end_to_end(pg_schema):
    migrate(pg_schema.admin_dsn, schema=pg_schema.schema, app_role="jarvis_app_test", up_to=1)
    with pg_schema.app_conn("alice") as conn:
        conn.execute(_row_sql("mem-old"))
    migrate(pg_schema.admin_dsn, schema=pg_schema.schema, app_role="jarvis_app_test")
    try:
        s = PostgresRowStore(pg_schema.app_dsn, "alice", schema=pg_schema.schema)
        assert [(e["seq"], e["op"]) for e in s.history("mem-old")] == [(1, "backfill")]
        assert s.verify_history() == []
    finally:
        pg_store.close_pools()


# --- the app must not run as a role that bypasses RLS ----------------------------------------


def test_store_refuses_a_superuser_connection(pg):
    s = PostgresRowStore(pg.admin_dsn, "alice", schema=pg.schema)
    for call in (lambda: s.list_memories(), lambda: s.get_memory("mem-1"), lambda: _new(s)):
        with pytest.raises(StoreUnavailableError):
            call()


def test_store_refuses_a_bypassrls_role(pg):
    from psycopg.conninfo import make_conninfo

    with pg.admin_conn() as conn:
        conn.execute("DROP ROLE IF EXISTS jarvis_bypass_test")
        conn.execute("CREATE ROLE jarvis_bypass_test LOGIN BYPASSRLS NOSUPERUSER PASSWORD 'bypass-pw'")
    try:
        migrate(pg.admin_dsn, schema=pg.schema, app_role="jarvis_bypass_test")
        s = PostgresRowStore(make_conninfo(pg.admin_dsn, user="jarvis_bypass_test", password="bypass-pw"), "alice", schema=pg.schema)
        with pytest.raises(StoreUnavailableError):
            s.list_memories()
    finally:
        pg_store.close_pools()
        with pg.admin_conn() as conn:
            conn.execute(f'DROP OWNED BY jarvis_bypass_test')
            conn.execute("DROP ROLE jarvis_bypass_test")


def test_ready_is_503_when_the_app_connects_as_superuser(pg, monkeypatch):
    monkeypatch.setenv("JARVIS_DATABASE_URL", pg.admin_dsn)
    monkeypatch.setenv("JARVIS_DATABASE_SCHEMA", pg.schema)
    monkeypatch.setenv("JARVIS_PG_STORE", "rows")
    with TestClient(app, raise_server_exceptions=False) as client:
        health = client.get("/ready")
        assert health.status_code == 503 and health.json()["status"] == "unavailable"
        assert client.get("/api/jarvis/memory").status_code == 503
    assert pg.admin_dsn.split("@")[0] not in health.text


def test_an_ordinary_role_is_accepted(pg, monkeypatch):
    monkeypatch.setenv("JARVIS_DATABASE_URL", pg.app_dsn)
    monkeypatch.setenv("JARVIS_DATABASE_SCHEMA", pg.schema)
    monkeypatch.setenv("JARVIS_PG_STORE", "rows")
    with TestClient(app, raise_server_exceptions=False) as client:
        assert client.get("/ready").status_code == 200
