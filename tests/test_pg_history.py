"""record_history: capture of every create/update/delete, append-only, hash chain, verifier."""

from __future__ import annotations

import threading

import psycopg
import pytest
from fastapi.testclient import TestClient

from app import pg_store
from app.main import app
from app.models import MemoryCreate, MemoryUpdate
from app.pg_schema import migrate
from app.pg_store import PostgresRowStore

pytestmark = pytest.mark.postgres

GENESIS = "0" * 64


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


def test_create_update_delete_are_all_recorded(store):
    rec = _new(store, "first version of the text", subject="a")
    store.update_memory(rec.id, MemoryUpdate(content="second version of the text", confidence=0.9))
    store.delete_memory(rec.id)
    entries = store.history(rec.id)
    assert [e["op"] for e in entries] == ["create", "update", "delete"]
    create, update, delete = entries
    assert create["before"] is None and create["after"]["content"] == "first version of the text"
    assert update["before"]["content"] == "first version of the text"
    assert update["after"]["content"] == "second version of the text" and update["after"]["confidence"] == 0.9
    assert delete["before"]["content"] == "second version of the text" and delete["after"] is None
    assert [e["version"] for e in entries] == [1, 2, 2]
    assert all(e["actor"] == "alice" for e in entries)
    assert store.get_memory(rec.id) is None  # the record is gone, its history is not


def test_chain_links_each_entry_to_the_previous_one(store):
    rec = _new(store)
    for i in range(3):
        store.update_memory(rec.id, MemoryUpdate(subject=f"s{i}"))
    entries = store.history(rec.id)
    assert entries[0]["prev_hash"] == GENESIS
    for earlier, later in zip(entries, entries[1:]):
        assert later["prev_hash"] == earlier["row_hash"]
    assert len({e["row_hash"] for e in entries}) == len(entries)
    assert all(len(e["row_hash"]) == 64 for e in entries)


def test_failed_and_conflicting_writes_leave_no_history(store):
    rec = _new(store)
    before = len(store.history(rec.id))
    with pytest.raises(ValueError):
        store.update_memory(rec.id, MemoryUpdate(supersedes="mem-missing"))
    from app.store import StoreVersionConflict

    with pytest.raises(StoreVersionConflict):
        store.update_memory(rec.id, MemoryUpdate(subject="x", expected_version=99))
    with pytest.raises(ValueError):
        _new(store, "dangling", supersedes="mem-missing")
    assert len(store.history(rec.id)) == before


def test_deleting_a_target_records_the_old_pointer_on_the_referencing_row(store):
    first = _new(store, "the first record")
    second = _new(store, "the second record", supersedes=first.id)
    store.delete_memory(first.id)
    entries = store.history(second.id)
    assert [e["op"] for e in entries] == ["create", "update"]
    assert entries[1]["before"]["supersedes"] == first.id
    assert entries[1]["after"]["supersedes"] is None
    assert store.verify_history() == []


def test_history_is_tenant_isolated(pg):
    alice = PostgresRowStore(pg.app_dsn, "alice", schema=pg.schema)
    bob = PostgresRowStore(pg.app_dsn, "bob", schema=pg.schema)
    rec = _new(alice)
    assert bob.history(rec.id) == []
    assert bob.verify_history() == []
    assert len(alice.history(rec.id)) == 1


def test_history_is_append_only_for_the_app_role_and_even_the_superuser(pg, store):
    rec = _new(store)
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        with pg.app_conn("alice") as conn:
            conn.execute("UPDATE record_history SET actor='mallory'")
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        with pg.app_conn("alice") as conn:
            conn.execute("DELETE FROM record_history")
    with pg.admin_conn() as conn:
        with pytest.raises(psycopg.errors.RaiseException):
            conn.execute("UPDATE record_history SET actor='mallory'")
        with pytest.raises(psycopg.errors.RaiseException):
            conn.execute("DELETE FROM record_history")
        with pytest.raises(psycopg.errors.RaiseException):
            conn.execute("TRUNCATE record_history")
    assert len(store.history(rec.id)) == 1


def test_verifier_is_clean_after_normal_activity(store):
    a = _new(store, "record number one")
    b = _new(store, "record number two", supersedes=a.id)
    store.update_memory(a.id, MemoryUpdate(confidence=0.7))
    store.update_memory(b.id, MemoryUpdate(subject="x"))
    store.delete_memory(a.id)
    assert store.verify_history() == []
    assert store.verify_history(b.id) == []


def _tamper(pg, *statements):
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


def _three_entries(store):
    rec = _new(store, "tamper target text")
    store.update_memory(rec.id, MemoryUpdate(subject="one"))
    store.update_memory(rec.id, MemoryUpdate(subject="two"))
    return rec


def test_verifier_detects_an_altered_entry(pg, store):
    rec = _three_entries(store)
    _tamper(pg, "UPDATE record_history SET after = jsonb_set(after, '{content}', '\"forged\"') WHERE op='update' AND version=2")
    assert any("altered" in p for p in _problems(store, rec.id))


def test_verifier_detects_a_removed_middle_entry(pg, store):
    rec = _three_entries(store)
    _tamper(pg, "DELETE FROM record_history WHERE version = 2")
    assert any("previous entry" in p for p in _problems(store, rec.id))


def test_verifier_detects_a_removed_first_entry(pg, store):
    rec = _three_entries(store)
    _tamper(pg, "DELETE FROM record_history WHERE op = 'create'")
    assert any("previous entry" in p for p in _problems(store, rec.id))


def test_verifier_detects_a_truncated_tail(pg, store):
    rec = _three_entries(store)
    _tamper(pg, "DELETE FROM record_history WHERE version = 3")
    assert any("live record" in p for p in _problems(store, rec.id))


def test_verifier_detects_a_write_that_bypassed_the_triggers(pg, store):
    rec = _new(store, "bypass target text")
    with pg.admin_conn() as conn:
        conn.execute("SET session_replication_role = replica")  # skips triggers
        conn.execute("UPDATE memories SET content = 'silently rewritten' WHERE id = %s", (rec.id,))
    assert any("live record" in p for p in _problems(store, rec.id))


def test_verifier_detects_a_live_row_with_no_history(pg, store):
    rec = _new(store, "no history target")
    _tamper(pg, "DELETE FROM record_history")
    assert any("live record" in p or "no history" in p for p in _problems(store, rec.id))


def test_concurrent_updates_are_accounted_for_exactly(pg):
    """Every attempt either succeeded or got a 409; every success is in history exactly once."""
    from app.store import StoreVersionConflict

    stores = [PostgresRowStore(pg.app_dsn, "alice", schema=pg.schema) for _ in range(4)]
    rec = _new(stores[0], subject="start")
    start_version = rec.version
    n = 10
    successes: list[str] = []
    conflicts: list[str] = []
    unexpected: list[BaseException] = []
    guard = threading.Lock()

    def worker(s, idx):
        for i in range(n):
            subject = f"w{idx}-{i}"
            try:
                s.update_memory(rec.id, MemoryUpdate(subject=subject))
                with guard:
                    successes.append(subject)
            except StoreVersionConflict:
                with guard:
                    conflicts.append(subject)
            except BaseException as exc:  # noqa: BLE001 - anything else is a bug
                with guard:
                    unexpected.append(exc)

    threads = [threading.Thread(target=worker, args=(s, i)) for i, s in enumerate(stores)]
    [t.start() for t in threads]
    [t.join() for t in threads]

    assert unexpected == []
    assert len(successes) + len(conflicts) == 4 * n
    final = stores[0].get_memory(rec.id)
    assert final.version == start_version + len(successes)

    entries = stores[0].history(rec.id)
    assert len(entries) == 1 + len(successes)  # the create, plus exactly one per success
    assert [e["version"] for e in entries] == list(range(start_version, final.version + 1))
    updates = [e for e in entries if e["op"] == "update"]
    assert sorted(e["after"]["subject"] for e in updates) == sorted(successes)  # none lost, none doubled
    assert all(e["before"]["version"] + 1 == e["after"]["version"] for e in updates)
    assert final.subject == updates[-1]["after"]["subject"]
    assert stores[0].verify_history() == []


def test_upgrading_a_v1_database_backfills_history(pg_schema):
    migrate(pg_schema.admin_dsn, schema=pg_schema.schema, app_role="jarvis_app_test", up_to=1)
    with pg_schema.app_conn("alice") as conn:
        conn.execute(
            "INSERT INTO memories (tenant_key,id,content,content_sha256,created_at,updated_at,source_agent,"
            "session_id,type,status,confidence) VALUES ('alice','mem-old','legacy row text',%s,now(),now(),'t','s','fact','draft',0.5)",
            ("a" * 64,),
        )
    migrate(pg_schema.admin_dsn, schema=pg_schema.schema, app_role="jarvis_app_test")
    try:
        s = PostgresRowStore(pg_schema.app_dsn, "alice", schema=pg_schema.schema)
        entries = s.history("mem-old")
        # clearly a backfill snapshot, not a claimed past edit
        assert [(e["op"], e["actor"]) for e in entries] == [("backfill", "migration:backfill")]
        assert entries[0]["before"] is None and entries[0]["after"]["content"] == "legacy row text"
        assert entries[0]["after"]["created_at"] < entries[0]["changed_at"]  # real creation time is preserved
        assert s.verify_history() == []
        s.update_memory("mem-old", MemoryUpdate(subject="after upgrade"))
        assert [e["op"] for e in s.history("mem-old")] == ["backfill", "update"]
        assert s.verify_history() == []
        assert s.verify_history() == []
    finally:
        pg_store.close_pools()


def test_history_endpoints_on_the_row_store(pg, monkeypatch):
    monkeypatch.setenv("JARVIS_DATABASE_URL", pg.app_dsn)
    monkeypatch.setenv("JARVIS_DATABASE_SCHEMA", pg.schema)
    monkeypatch.setenv("JARVIS_PG_STORE", "rows")
    body = {"content": "history over http", "source_agent": "t", "session_id": "s", "type": "fact"}
    with TestClient(app, raise_server_exceptions=False) as client:
        mem = client.post("/api/jarvis/memory", json=body).json()["memory"]
        client.patch(f"/api/jarvis/memory/{mem['id']}", json={"subject": "x"})
        hist = client.get(f"/api/jarvis/memory/{mem['id']}/history")
        assert hist.status_code == 200
        assert [e["op"] for e in hist.json()["history"]] == ["create", "update"]
        verify = client.get("/api/jarvis/memory/history/verify")
        assert verify.status_code == 200 and verify.json() == {"ok": True, "problems": []}
        assert client.get("/api/jarvis/memory/mem-nope/history").json()["history"] == []


def test_history_endpoints_are_501_on_the_json_store():
    with TestClient(app, raise_server_exceptions=False) as client:
        assert client.get("/api/jarvis/memory/mem-x/history").status_code == 501
        assert client.get("/api/jarvis/memory/history/verify").status_code == 501


def test_verify_cli_reports_and_exits_nonzero_on_tamper(pg, store, monkeypatch, capsys):
    from app import pg_verify

    rec = _three_entries(store)
    monkeypatch.setenv("JARVIS_DATABASE_MIGRATE_URL", pg.admin_dsn)
    monkeypatch.setenv("JARVIS_DATABASE_SCHEMA", pg.schema)
    assert pg_verify.main(["--tenant", "alice"]) == 0
    assert "ok" in capsys.readouterr().out
    _tamper(pg, "DELETE FROM record_history WHERE version = 2")
    assert pg_verify.main(["--tenant", "alice"]) == 1
    out = capsys.readouterr()
    assert rec.id in out.out and "postgresql://" not in out.out + out.err
