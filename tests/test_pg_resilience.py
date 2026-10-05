"""Timeouts roll back whole transactions and map to 503; a saturated pool fails fast; readiness
proves the role is safe.  All against a throwaway Postgres."""

from __future__ import annotations

import threading
import time

import pytest
from fastapi.testclient import TestClient

from app import pg_store
from app.main import app
from app.models import MemoryCreate, MemoryUpdate
from app.pg_schema import migrate
from app.pg_store import PostgresRowStore
from app.refusal import LEDGER_UNAVAILABLE
from app.store import StoreUnavailableError, reset_store_for_tests

pytestmark = pytest.mark.postgres


@pytest.fixture
def pg(pg_schema):
    migrate(pg_schema.admin_dsn, schema=pg_schema.schema, app_role="jarvis_app_test")
    yield pg_schema
    pg_store.close_pools()


def _store(pg, tenant="alice"):
    return PostgresRowStore(pg.app_dsn, tenant, schema=pg.schema)


def _new(store, content="resilience record text", **kw):
    base = dict(content=content, source_agent="t", session_id="s", type="fact")
    base.update(kw)
    return store.create_memory(MemoryCreate(**base))


def _api(pg, monkeypatch):
    monkeypatch.setenv("JARVIS_DATABASE_URL", pg.app_dsn)
    monkeypatch.setenv("JARVIS_DATABASE_SCHEMA", pg.schema)
    monkeypatch.setenv("JARVIS_PG_STORE", "rows")
    reset_store_for_tests()
    return TestClient(app, raise_server_exceptions=False)


# --- timeouts: whole transaction rolled back, mapped to 503 -------------------------------------------


def test_statement_timeout_rolls_back_the_whole_transaction_and_is_503(pg, monkeypatch):
    monkeypatch.setenv("JARVIS_DATABASE_STATEMENT_TIMEOUT_MS", "300")
    pg_store.close_pools()
    s = _store(pg)
    rec = _new(s, "original content text", subject="before")
    history_before = s.history(rec.id)
    with pg.admin_conn() as conn:
        conn.execute("CREATE FUNCTION slow() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN PERFORM pg_sleep(3); RETURN NULL; END $$")
        # runs after the history trigger, so the rollback has real work to undo
        conn.execute("CREATE TRIGGER zz_slow AFTER UPDATE ON memories FOR EACH ROW WHEN (NEW.content = 'slow slow slow') EXECUTE FUNCTION slow()")
    started = time.monotonic()
    with pytest.raises(StoreUnavailableError):
        s.update_memory(rec.id, MemoryUpdate(content="slow slow slow", subject="after"))
    assert time.monotonic() - started < 2.5  # cancelled by the timeout, not retried to exhaustion
    current = s.get_memory(rec.id)
    assert (current.content, current.subject, current.version) == ("original content text", "before", 1)
    assert s.history(rec.id) == history_before
    with pg.admin_conn() as conn:
        assert conn.execute("SELECT last_seq FROM history_counters").fetchone()[0] == 1  # no number consumed
        assert conn.execute("SELECT count(*) FROM pg_stat_activity WHERE state = 'idle in transaction'").fetchone()[0] == 0
    assert s.verify_history() == []


def test_a_timeout_over_http_is_503_with_retry_after_and_no_partial_write(pg, monkeypatch):
    monkeypatch.setenv("JARVIS_DATABASE_STATEMENT_TIMEOUT_MS", "300")
    pg_store.close_pools()
    s = _store(pg, "operator")
    rec = _new(s, "http timeout original", subject="before")
    with pg.admin_conn() as conn:
        conn.execute("CREATE FUNCTION slow() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN PERFORM pg_sleep(3); RETURN NULL; END $$")
        conn.execute("CREATE TRIGGER zz_slow AFTER UPDATE ON memories FOR EACH ROW WHEN (NEW.content = 'slow slow slow') EXECUTE FUNCTION slow()")
    with _api(pg, monkeypatch) as client:
        response = client.patch(f"/api/jarvis/memory/{rec.id}", json={"content": "slow slow slow"})
    assert response.status_code == 503
    assert response.json() == {"detail": "Ledger store unavailable", "code": LEDGER_UNAVAILABLE}
    assert response.headers["retry-after"] == "5"
    assert "timeout" not in response.text.lower() and "cancel" not in response.text.lower()
    assert _store(pg, "operator").get_memory(rec.id).content == "http timeout original"


def test_lock_timeout_is_503_not_a_version_conflict_and_changes_nothing(pg, monkeypatch):
    import psycopg

    monkeypatch.setenv("JARVIS_DATABASE_LOCK_TIMEOUT_MS", "300")
    pg_store.close_pools()
    s = _store(pg)
    rec = _new(s, "lock timeout original", subject="before")
    blocker = psycopg.connect(pg.admin_dsn, options=f"-c search_path={pg.schema}")
    try:
        blocker.execute("SELECT id FROM memories WHERE id = %s FOR UPDATE", (rec.id,))  # held until closed
        started = time.monotonic()
        with pytest.raises(StoreUnavailableError):  # NOT StoreVersionConflict: nothing raced, the row is busy
            s.update_memory(rec.id, MemoryUpdate(subject="blocked"))
        assert time.monotonic() - started < 2.5
    finally:
        blocker.rollback()
        blocker.close()
    assert s.get_memory(rec.id).subject == "before" and len(s.history(rec.id)) == 1
    assert s.update_memory(rec.id, MemoryUpdate(subject="after release")).version == 2  # healthy again
    assert [e["seq"] for e in s.history(rec.id)] == [1, 2] and s.verify_history() == []


# --- the pool: fixed size, short checkout, no queue --------------------------------------------------------


def test_pool_defaults_are_fixed_and_short(pg):
    s = _store(pg)
    s.list_memories()
    pool, _ = pg_store._pool_for(pg.app_dsn, pg.schema)
    assert (pool.max_size, pool.timeout, pool.max_waiting) == (10, 1.0, 10)


def _hold_connections(pg, how_many):
    """Occupy ``how_many`` pool connections inside open transactions; returns (release, threads)."""
    release = threading.Event()
    entered = threading.Barrier(how_many + 1, timeout=10)
    owner = _store(pg)
    rec = _new(owner, "held by a slow request")

    def holder():
        s = _store(pg)
        first = [True]

        def hold():
            if not first[0]:  # a retry after losing the version race must not block again
                return
            first[0] = False
            entered.wait()
            release.wait(timeout=20)

        s._after_read_hook = hold
        s.update_memory(rec.id, MemoryUpdate(subject="holding"))

    threads = [threading.Thread(target=holder) for _ in range(how_many)]
    [t.start() for t in threads]
    entered.wait()
    return release, threads, rec


def test_a_saturated_pool_is_an_immediate_503_not_a_queue(pg, monkeypatch):
    monkeypatch.setenv("JARVIS_DATABASE_POOL_MAX", "2")
    monkeypatch.setenv("JARVIS_DATABASE_POOL_TIMEOUT_MS", "250")
    pg_store.close_pools()
    release, threads, rec = _hold_connections(pg, 2)
    try:
        started = time.monotonic()
        with pytest.raises(StoreUnavailableError):
            _store(pg).get_memory(rec.id)
        assert time.monotonic() - started < 1.0  # ~250 ms, not the 5 s connect timeout
    finally:
        release.set()
        [t.join(timeout=15) for t in threads]
    assert _store(pg).get_memory(rec.id) is not None  # and it recovers


def test_requests_beyond_the_waiting_list_are_rejected_instantly(pg, monkeypatch):
    monkeypatch.setenv("JARVIS_DATABASE_POOL_MAX", "1")
    monkeypatch.setenv("JARVIS_DATABASE_POOL_MAX_WAITING", "1")
    monkeypatch.setenv("JARVIS_DATABASE_POOL_TIMEOUT_MS", "3000")
    pg_store.close_pools()
    release, threads, rec = _hold_connections(pg, 1)
    waiter_result: list[object] = []
    try:
        waiter = threading.Thread(target=lambda: waiter_result.append(_store(pg).get_memory(rec.id)))
        waiter.start()
        pool, _ = pg_store._pool_for(pg.app_dsn, pg.schema)
        deadline = time.monotonic() + 3
        while pool.get_stats().get("requests_waiting", 0) < 1 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert pool.get_stats()["requests_waiting"] == 1  # one request is queued, as allowed
        started = time.monotonic()
        with pytest.raises(StoreUnavailableError):  # the next one is turned away at once
            _store(pg).get_memory(rec.id)
        assert time.monotonic() - started < 0.5
    finally:
        release.set()
        [t.join(timeout=15) for t in threads]
        waiter.join(timeout=15)
    assert waiter_result and waiter_result[0] is not None  # the queued request was served


def test_pool_exhaustion_over_http_is_503_with_retry_after(pg, monkeypatch):
    monkeypatch.setenv("JARVIS_DATABASE_POOL_MAX", "1")
    monkeypatch.setenv("JARVIS_DATABASE_POOL_MAX_WAITING", "1")
    monkeypatch.setenv("JARVIS_DATABASE_POOL_TIMEOUT_MS", "200")
    pg_store.close_pools()
    release, threads, rec = _hold_connections(pg, 1)
    try:
        monkeypatch.setenv("JARVIS_DATABASE_URL", pg.app_dsn)
        monkeypatch.setenv("JARVIS_DATABASE_SCHEMA", pg.schema)
        monkeypatch.setenv("JARVIS_PG_STORE", "rows")
        from app.store import _stores

        _stores.clear()  # new store object, same (still saturated) pool
        with TestClient(app, raise_server_exceptions=False) as client:
            response = client.get("/api/jarvis/memory")
        assert response.status_code == 503 and response.headers["retry-after"] == "5"
        assert response.json()["code"] == LEDGER_UNAVAILABLE
    finally:
        release.set()
        [t.join(timeout=15) for t in threads]
