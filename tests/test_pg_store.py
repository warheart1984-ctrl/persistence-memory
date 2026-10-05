"""PostgresRowStore: behaviour, optimistic locking, tenant isolation, fail-closed."""

from __future__ import annotations

import threading
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from app import pg_store
from app.main import app
from app.models import BoardSlot, EvidenceLink, MemoryBoard, MemoryCreate, MemoryUpdate
from app.pg_schema import migrate
from app.pg_store import PostgresRowStore
from app.store import StoreUnavailableError, StoreVersionConflict

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


def test_create_get_roundtrip(store):
    rec = _new(
        store, subject="topic", tags=["a", "b"], confidence=0.9, status="draft",
        evidence=[EvidenceLink(kind="ref", ref="doc#1", note="n")],
    )
    got = store.get_memory(rec.id)
    assert got == rec
    assert rec.version == 1 and rec.id.startswith("mem-")
    assert rec.tags == ["a", "b"] and rec.evidence[0].ref == "doc#1"
    assert len(rec.content_sha256) == 64
    assert datetime.fromisoformat(rec.created_at).utcoffset().total_seconds() == 0
    assert store.get_memory("mem-missing") is None


def test_update_merges_fields_bumps_version_and_keeps_the_rest(store):
    rec = _new(store, subject="s1", tags=["x"])
    upd = store.update_memory(rec.id, MemoryUpdate(confidence=0.2, status="verified"))
    assert upd.version == 2 and upd.confidence == 0.2 and upd.status == "verified"
    assert upd.subject == "s1" and upd.tags == ["x"] and upd.created_at == rec.created_at
    assert upd.updated_at >= rec.updated_at
    changed = store.update_memory(rec.id, MemoryUpdate(content="new content here"))
    assert changed.content_sha256 != rec.content_sha256 and changed.version == 3
    assert store.update_memory("mem-missing", MemoryUpdate(subject="x")) is None


def test_supersedes_requires_a_known_target_and_empty_string_clears(store):
    first = _new(store, "first")
    with pytest.raises(ValueError, match="supersedes target not found"):
        _new(store, "second", supersedes="mem-missing")
    second = _new(store, "second", supersedes=first.id)
    assert second.supersedes == first.id
    with pytest.raises(ValueError, match="supersedes target not found"):
        store.update_memory(second.id, MemoryUpdate(supersedes="mem-missing"))
    assert store.update_memory(second.id, MemoryUpdate(supersedes="")).supersedes is None


def test_delete_returns_bool_and_nulls_dangling_pointer(store):
    first = _new(store, "first")
    second = _new(store, "second", supersedes=first.id)
    assert store.delete_memory(first.id) is True
    assert store.delete_memory(first.id) is False
    assert store.get_memory(first.id) is None
    assert store.get_memory(second.id).supersedes is None


def test_expected_version_match_and_mismatch(store):
    rec = _new(store)
    ok = store.update_memory(rec.id, MemoryUpdate(subject="one", expected_version=1))
    assert ok.version == 2
    with pytest.raises(StoreVersionConflict):
        store.update_memory(rec.id, MemoryUpdate(subject="stale", expected_version=1))
    now = store.get_memory(rec.id)
    assert now.subject == "one" and now.version == 2


def test_two_stale_writers_exactly_one_wins(pg):
    one = PostgresRowStore(pg.app_dsn, "alice", schema=pg.schema)
    two = PostgresRowStore(pg.app_dsn, "alice", schema=pg.schema)
    rec = _new(one)
    barrier = threading.Barrier(2)
    results: list[str] = []

    def writer(s, label):
        barrier.wait()
        try:
            s.update_memory(rec.id, MemoryUpdate(subject=label, expected_version=1))
            results.append("ok")
        except StoreVersionConflict:
            results.append("conflict")

    threads = [threading.Thread(target=writer, args=(s, n)) for s, n in ((one, "one"), (two, "two"))]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert sorted(results) == ["conflict", "ok"]
    assert one.get_memory(rec.id).version == 2


def test_concurrent_updates_to_different_fields_are_never_clobbered(pg):
    stores = [PostgresRowStore(pg.app_dsn, "alice", schema=pg.schema) for _ in range(2)]
    rec = _new(stores[0], subject="start", confidence=0.5)
    n = 25

    ok = {"subject": 0, "confidence": 0}
    unexpected: list[BaseException] = []
    guard = threading.Lock()

    def attempt(kind, store_, data):
        try:
            store_.update_memory(rec.id, data)
            with guard:
                ok[kind] += 1
        except StoreVersionConflict:
            pass  # allowed under contention (HTTP 409)
        except BaseException as exc:  # noqa: BLE001
            with guard:
                unexpected.append(exc)

    def set_subject():
        for i in range(n):
            attempt("subject", stores[0], MemoryUpdate(subject=f"subject-{i}"))

    def set_confidence():
        for i in range(n):
            attempt("confidence", stores[1], MemoryUpdate(confidence=(i + 1) / 100))

    threads = [threading.Thread(target=f) for f in (set_subject, set_confidence)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert unexpected == []
    final = stores[0].get_memory(rec.id)
    assert final.version == 1 + ok["subject"] + ok["confidence"]  # every success counted exactly once
    updates = [e for e in stores[0].history(rec.id) if e["op"] == "update"]
    assert len(updates) == ok["subject"] + ok["confidence"]
    for e in updates:  # no stale snapshot ever clobbered the other writer's field
        changed = [k for k in ("subject", "confidence") if e["before"][k] != e["after"][k]]
        assert len(changed) == 1
    assert stores[0].verify_history() == []


def test_internal_retry_recovers_from_a_concurrent_update(pg, store):
    rec = _new(store, subject="start")
    other = PostgresRowStore(pg.app_dsn, "alice", schema=pg.schema)  # the competing worker
    fired = []

    def competing_write():
        if not fired:
            fired.append(True)
            other.update_memory(rec.id, MemoryUpdate(confidence=0.9))

    store._after_read_hook = competing_write
    result = store.update_memory(rec.id, MemoryUpdate(subject="mine"))
    store._after_read_hook = None
    assert result.subject == "mine" and result.confidence == 0.9 and result.version == 3


def test_retry_exhaustion_raises_conflict_and_changes_nothing(pg, store):
    rec = _new(store, subject="start")
    other = PostgresRowStore(pg.app_dsn, "alice", schema=pg.schema)
    store._after_read_hook = lambda: other.update_memory(rec.id, MemoryUpdate(confidence=0.1))
    with pytest.raises(StoreVersionConflict):
        store.update_memory(rec.id, MemoryUpdate(subject="never lands"))
    store._after_read_hook = None
    assert store.get_memory(rec.id).subject == "start"


def test_tenants_are_isolated_even_with_equal_ids(pg):
    alice = PostgresRowStore(pg.app_dsn, "alice", schema=pg.schema)
    bob = PostgresRowStore(pg.app_dsn, "bob", schema=pg.schema)
    rec = _new(alice, "alice private")
    assert bob.get_memory(rec.id) is None
    assert bob.list_memories() == []
    assert bob.delete_memory(rec.id) is False
    assert bob.update_memory(rec.id, MemoryUpdate(subject="x")) is None
    with pytest.raises(ValueError):
        _new(bob, "bob tries alice pointer", supersedes=rec.id)
    assert alice.get_memory(rec.id).subject is None


def test_rls_backstop_hides_other_tenants_even_without_a_where_clause(pg):
    alice = PostgresRowStore(pg.app_dsn, "alice", schema=pg.schema)
    bob = PostgresRowStore(pg.app_dsn, "bob", schema=pg.schema)
    _new(alice)
    _new(bob)
    with alice._tx() as conn:  # a buggy query that forgot the tenant filter
        assert conn.execute("SELECT count(*) AS n FROM memories").fetchone()["n"] == 1


def test_list_memories_filters_order_and_limit(store):
    a = _new(store, "alpha note", type="decision", status="verified", subject="s1", session_id="x", tags=["red"])
    b = _new(store, "beta note", type="fact", subject="s1", session_id="y")
    c = _new(store, "gamma note", type="fact", status="archived", session_id="y", source_agent="Archivist")
    assert [m.id for m in store.list_memories()] == [c.id, b.id, a.id]  # newest first
    assert [m.id for m in store.list_memories(truth_scope="live")] == [b.id, a.id]
    assert [m.id for m in store.list_memories(truth_scope="archived")] == [c.id]
    assert [m.id for m in store.list_memories(memory_type="decision")] == [a.id]
    assert [m.id for m in store.list_memories(status="verified")] == [a.id]
    assert [m.id for m in store.list_memories(session_id="y")] == [c.id, b.id]
    assert [m.id for m in store.list_memories(subject="s1")] == [b.id, a.id]
    assert [m.id for m in store.list_memories(query="RED")] == [a.id]  # tags, case-insensitive
    assert [m.id for m in store.list_memories(query="note", limit=2)] == [c.id, b.id]


def test_retrieve_and_conflicts_match_json_semantics(store):
    one = _new(store, "the sky is blue today", subject="sky")
    two = _new(store, "the sky is green today", subject="sky")
    memories, selections, conflicts = store.retrieve(query="sky")
    assert {m.id for m in memories} == {one.id, two.id}
    assert len(selections) == 2
    assert len(conflicts) == 1 and conflicts[0].subject == "sky"
    assert len(store.conflicts(subject="sky")) == 1


def test_board_default_set_patch_and_isolation(pg):
    alice = PostgresRowStore(pg.app_dsn, "alice", schema=pg.schema)
    bob = PostgresRowStore(pg.app_dsn, "bob", schema=pg.schema)
    assert alice.get_board().board_id == "default_board"
    alice.set_board(MemoryBoard(board_id="a-board", summary="one"))
    patched = alice.patch_board({"summary": "two", "slots": [BoardSlot(slot_name="s", accepted_class="foundation").model_dump()]})
    assert patched.board_id == "a-board" and patched.summary == "two" and patched.slots[0].slot_name == "s"
    assert alice.get_board().summary == "two"
    assert bob.get_board().board_id == "default_board"
    assert bob.patch_board({"summary": "bob"}).summary == "bob"
    assert alice.get_board().summary == "two"


def test_unreachable_database_fails_closed(monkeypatch):
    monkeypatch.setenv("JARVIS_DATABASE_CONNECT_TIMEOUT", "1")
    dead = PostgresRowStore("postgresql://u:p@127.0.0.1:9/none", "alice")
    try:
        with pytest.raises(StoreUnavailableError):
            dead.get_memory("mem-1")
        with pytest.raises(StoreUnavailableError):
            dead.list_memories()
        with pytest.raises(StoreUnavailableError):
            dead.create_memory(MemoryCreate(content="x" * 10, source_agent="t", session_id="s", type="fact"))
    finally:
        pg_store.close_pools()


def test_unmigrated_schema_fails_closed(pg_schema):
    try:
        s = PostgresRowStore(pg_schema.app_dsn, "alice", schema=pg_schema.schema)
        with pytest.raises(StoreUnavailableError):
            s.list_memories()
    finally:
        pg_store.close_pools()


def test_schema_version_mismatch_fails_closed(pg):
    with pg.admin_conn() as conn:
        conn.execute("INSERT INTO schema_version (version) VALUES (999)")
    s = PostgresRowStore(pg.app_dsn, "alice", schema=pg.schema)
    with pytest.raises(StoreUnavailableError):
        s.get_memory("mem-1")


# --- through the HTTP API ---------------------------------------------------------------


@pytest.fixture
def api(pg, monkeypatch):
    monkeypatch.setenv("JARVIS_DATABASE_URL", pg.app_dsn)
    monkeypatch.setenv("JARVIS_DATABASE_SCHEMA", pg.schema)
    monkeypatch.setenv("JARVIS_PG_STORE", "rows")
    monkeypatch.setenv("JARVIS_DATABASE_CONNECT_TIMEOUT", "1")
    with TestClient(app, raise_server_exceptions=False) as client:
        yield client


_BODY = {"content": "stored in a postgres row", "source_agent": "t", "session_id": "s", "type": "fact"}


def test_http_crud_with_expected_version(api):
    created = api.post("/api/jarvis/memory", json=_BODY)
    assert created.status_code == 200, created.text
    mem = created.json()["memory"]
    assert mem["version"] == 1
    assert api.get(f"/api/jarvis/memory/{mem['id']}").json()["memory"]["content"] == _BODY["content"]
    ok = api.patch(f"/api/jarvis/memory/{mem['id']}", json={"subject": "a", "expected_version": 1})
    assert ok.status_code == 200 and ok.json()["memory"]["version"] == 2
    stale = api.patch(f"/api/jarvis/memory/{mem['id']}", json={"subject": "b", "expected_version": 1})
    assert stale.status_code == 409
    assert api.patch(f"/api/jarvis/memory/{mem['id']}", json={"subject": "c"}).json()["memory"]["version"] == 3
    assert api.delete(f"/api/jarvis/memory/{mem['id']}").status_code == 200
    assert api.get(f"/api/jarvis/memory/{mem['id']}").status_code == 404
    assert api.get("/ready").json()["status"] == "ready"


def test_http_database_outage_is_503_and_not_ready(pg, monkeypatch):
    monkeypatch.setenv("JARVIS_DATABASE_URL", "postgresql://u:p@127.0.0.1:9/none")
    monkeypatch.setenv("JARVIS_PG_STORE", "rows")
    monkeypatch.setenv("JARVIS_DATABASE_CONNECT_TIMEOUT", "1")
    with TestClient(app, raise_server_exceptions=False) as client:
        assert client.get("/api/jarvis/memory").status_code == 503
        assert client.post("/api/jarvis/memory", json=_BODY).status_code == 503
        health = client.get("/ready")
    assert health.status_code == 503 and health.json()["status"] == "unavailable"
    assert "127.0.0.1" not in health.text and "postgresql://" not in health.text


# --- the red-team properties, restated for the database ---------------------------------------


def test_mcp_tool_error_is_generic_when_the_database_is_down(monkeypatch, caplog):
    """MCP clients see a fixed message; the host, port and driver detail stay in the server log."""
    import logging

    monkeypatch.setenv("JARVIS_DATABASE_URL", "postgresql://secretuser:secretpw@127.0.0.1:9/none")
    monkeypatch.setenv("JARVIS_PG_STORE", "rows")
    monkeypatch.setenv("JARVIS_DATABASE_CONNECT_TIMEOUT", "1")
    rpc = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
           "params": {"name": "emr_fetch", "arguments": {"id": "mem-1"}}}
    try:
        with caplog.at_level(logging.ERROR), TestClient(app, raise_server_exceptions=False) as client:
            response = client.post("/mcp", json=rpc)
        result = response.json()["result"]
        assert result["isError"] is True and result["content"][0]["text"] == "Ledger store unavailable"
        for secret in ("secretuser", "secretpw", "127.0.0.1", ":9/", "postgresql://"):
            assert secret not in response.text
        assert "Ledger database error" in caplog.text or "ledger database error" in caplog.text
    finally:
        pg_store.close_pools()


def test_a_write_that_fails_midway_leaves_the_original_and_its_history_intact(pg):
    """A database error inside the transaction rolls everything back: row, history, sequence."""
    s = PostgresRowStore(pg.app_dsn, "alice", schema=pg.schema)
    rec = _new(s, "original content text", subject="before")
    before_history = s.history(rec.id)
    with pg.admin_conn() as conn:
        conn.execute(
            "CREATE FUNCTION boom() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'injected failure'; END $$"
        )
        # fires after the history trigger ran, so the rollback has real work to undo
        conn.execute(
            "CREATE TRIGGER zz_boom AFTER UPDATE ON memories FOR EACH ROW WHEN (NEW.content = 'boom boom boom') EXECUTE FUNCTION boom()"
        )
    with pytest.raises(StoreUnavailableError):
        s.update_memory(rec.id, MemoryUpdate(content="boom boom boom", subject="after"))
    current = s.get_memory(rec.id)
    assert current.content == "original content text" and current.subject == "before" and current.version == 1
    assert s.history(rec.id) == before_history
    with pg.admin_conn() as conn:
        conn.execute("DROP TRIGGER zz_boom ON memories")
    s.update_memory(rec.id, MemoryUpdate(subject="later"))  # the store is still healthy
    assert [e["seq"] for e in s.history(rec.id)] == [1, 2]  # the failed attempt consumed no sequence number
    assert s.verify_history() == []
