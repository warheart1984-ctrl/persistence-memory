"""Newest-first keyset listing (``list_latest``) on both backends: JSON file store and Postgres row store."""

from __future__ import annotations

import time

import pytest

from app import pg_store
from app.models import MemoryCreate, MemoryUpdate
from app.pg_schema import migrate
from app.pg_store import PostgresRowStore
from app.store import JarvisStore
from app.ts import parse_utc


@pytest.fixture(params=["json", "postgres"])
def ledger(request, tmp_path):
    if request.param == "json":
        yield JarvisStore(str(tmp_path / "latest-store.json"))
        return
    schema = request.getfixturevalue("pg_schema")  # skips when no throwaway server is configured
    migrate(schema.admin_dsn, schema=schema.schema, app_role="jarvis_app_test")
    try:
        yield PostgresRowStore(schema.app_dsn, "latest", schema=schema.schema)
    finally:
        pg_store.close_pools()


def _new(store, content, **kw):
    base = dict(content=content, source_agent="agent", session_id="sess", type="fact")
    base.update(kw)
    rec = store.create_memory(MemoryCreate(**base))
    time.sleep(0.003)
    return rec


def _ids(rows):
    return [m.id for m, _ in rows]


def test_newest_first_and_keyset_walk_has_no_gaps_or_repeats(ledger):
    made = [_new(ledger, f"record {i}") for i in range(7)]
    expected = [m.id for m in reversed(made)]
    assert _ids(ledger.list_latest(limit=50)) == expected
    seen: list[str] = []
    after = None
    while True:
        page = ledger.list_latest(limit=3, after=after)
        if not page:
            break
        seen += _ids(page)
        last = page[-1][0]
        after = (parse_utc(last.created_at), last.id)
    assert seen == expected


def test_default_exclusions_and_flags(ledger):
    plain = _new(ledger, "plain")
    old = _new(ledger, "old version")
    new = _new(ledger, "new version", supersedes=old.id)
    archived = _new(ledger, "archived", status="archived")
    twin = _new(ledger, "twin note", source_agent="ai-twin", type="research")
    other_type = _new(ledger, "a decision", type="decision")
    assert set(_ids(ledger.list_latest(limit=50))) == {plain.id, new.id, other_type.id}
    assert old.id in _ids(ledger.list_latest(limit=50, include_superseded=True))
    assert archived.id in _ids(ledger.list_latest(limit=50, include_archived=True))
    assert twin.id in _ids(ledger.list_latest(limit=50, include_twin=True))
    assert _ids(ledger.list_latest(limit=50, memory_type="decision")) == [other_type.id]


def test_superseded_by_reports_the_successor_on_a_chain(ledger):
    a = _new(ledger, "A")
    b = _new(ledger, "B", supersedes=a.id)
    c = _new(ledger, "C", supersedes=b.id)
    rows = {m.id: s for m, s in ledger.list_latest(limit=50, include_superseded=True)}
    assert rows == {a.id: b.id, b.id: c.id, c.id: None}


def test_after_cursor_is_strictly_older_and_listing_is_read_only(ledger):
    a = _new(ledger, "first")
    b = _new(ledger, "second")
    before = [m.model_dump() for m in ledger.list_memories(limit=50)]
    assert _ids(ledger.list_latest(limit=10, after=(parse_utc(b.created_at), b.id))) == [a.id]
    assert ledger.list_latest(limit=10, after=(parse_utc(a.created_at), a.id)) == []
    assert [m.model_dump() for m in ledger.list_memories(limit=50)] == before
    ledger.update_memory(a.id, MemoryUpdate(subject="touch"))  # an update does not reorder: created_at is the sort key
    assert _ids(ledger.list_latest(limit=10)) == [b.id, a.id]
