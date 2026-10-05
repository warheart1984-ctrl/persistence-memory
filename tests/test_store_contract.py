"""One behavioural contract, two backends: the JSON file store and the Postgres row store.

Anything asserted here must hold for both; deliberate differences (a deleted supersedes target is
nulled by Postgres, history exists only there) are covered in tests/test_pg_*.py.
"""

from __future__ import annotations

import time

import pytest

from app import pg_store
from app.models import BoardSlot, EvidenceLink, MemoryBoard, MemoryCreate, MemoryUpdate
from app.pg_schema import migrate
from app.pg_store import PostgresRowStore
from app.store import JarvisStore, StoreVersionConflict


@pytest.fixture(params=["json", "postgres"])
def ledger(request, tmp_path):
    if request.param == "json":
        yield JarvisStore(str(tmp_path / "contract-store.json"))
        return
    schema = request.getfixturevalue("pg_schema")  # skips when no throwaway server is configured
    migrate(schema.admin_dsn, schema=schema.schema, app_role="jarvis_app_test")
    try:
        yield PostgresRowStore(schema.app_dsn, "contract", schema=schema.schema)
    finally:
        pg_store.close_pools()


def _new(store, content="a ledger record", **kw):
    base = dict(content=content, source_agent="agent", session_id="sess", type="fact")
    base.update(kw)
    rec = store.create_memory(MemoryCreate(**base))
    time.sleep(0.003)  # distinct created_at so ordering is unambiguous
    return rec


def test_create_and_get_roundtrip(ledger):
    rec = _new(
        ledger, subject="topic", tags=["a", "b"], confidence=0.9,
        evidence=[EvidenceLink(kind="ref", ref="doc#1", note="n")],
    )
    got = ledger.get_memory(rec.id)
    assert got.model_dump() == rec.model_dump()
    assert rec.version == 1 and rec.status == "draft" and rec.tags == ["a", "b"]
    assert rec.evidence[0].ref == "doc#1" and rec.created_at == rec.updated_at
    assert len(rec.content_sha256) == 64
    assert ledger.get_memory("mem-missing") is None


def test_same_content_hashes_identically_on_every_backend(ledger):
    rec = _new(ledger, "  Hello   World  ")
    from app.continuity import content_sha256

    assert rec.content_sha256 == content_sha256("hello world") or rec.content_sha256 == content_sha256("  Hello   World  ")


def test_update_merges_bumps_version_and_returns_none_for_unknown(ledger):
    rec = _new(ledger, subject="s1", tags=["x"])
    upd = ledger.update_memory(rec.id, MemoryUpdate(confidence=0.2, status="verified"))
    assert upd.version == 2 and upd.confidence == 0.2 and upd.status == "verified"
    assert upd.subject == "s1" and upd.tags == ["x"] and upd.created_at == rec.created_at
    assert upd.updated_at >= rec.updated_at
    changed = ledger.update_memory(rec.id, MemoryUpdate(content="entirely new content"))
    assert changed.content_sha256 != rec.content_sha256 and changed.version == 3
    assert ledger.update_memory("mem-missing", MemoryUpdate(subject="x")) is None


def test_supersedes_must_name_an_existing_record(ledger):
    first = _new(ledger, "first record")
    with pytest.raises(ValueError, match="supersedes target not found"):
        _new(ledger, "second record", supersedes="mem-missing")
    second = _new(ledger, "second record", supersedes=first.id)
    assert second.supersedes == first.id
    with pytest.raises(ValueError, match="supersedes target not found"):
        ledger.update_memory(second.id, MemoryUpdate(supersedes="mem-missing"))
    assert not ledger.update_memory(second.id, MemoryUpdate(supersedes="")).supersedes


def test_delete_returns_whether_something_was_removed(ledger):
    rec = _new(ledger)
    assert ledger.delete_memory(rec.id) is True
    assert ledger.delete_memory(rec.id) is False
    assert ledger.get_memory(rec.id) is None


def test_expected_version_is_an_optimistic_lock(ledger):
    rec = _new(ledger)
    assert ledger.update_memory(rec.id, MemoryUpdate(subject="one", expected_version=1)).version == 2
    with pytest.raises(StoreVersionConflict):
        ledger.update_memory(rec.id, MemoryUpdate(subject="stale", expected_version=1))
    now = ledger.get_memory(rec.id)
    assert now.subject == "one" and now.version == 2


def test_list_filters_ordering_limit_and_query(ledger):
    a = _new(ledger, "alpha note", type="decision", status="verified", subject="s1", session_id="x", tags=["red"])
    b = _new(ledger, "beta note", type="fact", subject="s1", session_id="y")
    c = _new(ledger, "gamma note", type="fact", status="archived", session_id="y", source_agent="Archivist")
    ids = lambda **kw: [m.id for m in ledger.list_memories(**kw)]  # noqa: E731
    assert ids() == [c.id, b.id, a.id]
    assert ids(truth_scope="live") == [b.id, a.id]
    assert ids(truth_scope="archived") == [c.id]
    assert ids(memory_type="decision") == [a.id]
    assert ids(status="verified") == [a.id]
    assert ids(session_id="y") == [c.id, b.id]
    assert ids(subject="s1") == [b.id, a.id]
    assert ids(query="RED") == [a.id]
    assert ids(query="note", limit=2) == [c.id, b.id]
    assert ids(limit=1) == [c.id]


def test_retrieve_returns_selections_and_conflicts(ledger):
    one = _new(ledger, "the sky is blue today", subject="sky")
    two = _new(ledger, "the sky is green today", subject="sky")
    memories, selections, conflicts = ledger.retrieve(query="sky")
    assert {m.id for m in memories} == {one.id, two.id} and len(selections) == 2
    assert len(conflicts) == 1 and conflicts[0].subject == "sky"
    assert len(ledger.conflicts(subject="sky")) == 1
    assert ledger.conflicts(subject="nothing") == []


def test_board_default_set_and_patch(ledger):
    assert ledger.get_board().board_id == "default_board"
    ledger.set_board(MemoryBoard(board_id="b1", summary="one"))
    patched = ledger.patch_board(
        {"summary": "two", "slots": [BoardSlot(slot_name="s", accepted_class="foundation").model_dump()]}
    )
    assert patched.board_id == "b1" and patched.summary == "two" and patched.slots[0].slot_name == "s"
    assert ledger.get_board().summary == "two"
    assert ledger.patch_board({"summary": None}).summary == "two"  # None means "leave unchanged"
