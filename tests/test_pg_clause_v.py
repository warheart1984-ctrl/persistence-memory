"""The Clause V gate also holds on the PostgreSQL row store (the store the Mint ledger runs on)."""

from __future__ import annotations

import pytest

from app import pg_store
from app.clause_v import ClauseVViolation
from app.models import EvidenceLink, MemoryCreate, MemoryUpdate
from app.pg_schema import migrate
from app.pg_store import PostgresRowStore

pytestmark = pytest.mark.postgres

CHAT = [EvidenceLink(kind="user-request", ref="chat:1")]
FILE = [EvidenceLink(kind="file", ref="docs/POSTGRES.md")]


@pytest.fixture
def pg(pg_schema):
    migrate(pg_schema.admin_dsn, schema=pg_schema.schema, app_role="jarvis_app_test")
    yield pg_schema
    pg_store.close_pools()


@pytest.fixture
def store(pg, monkeypatch):
    monkeypatch.setenv("JARVIS_CLAUSE_V", "enforce")
    monkeypatch.setenv("JARVIS_CLAUSE_V_SOFT", "warn")
    return PostgresRowStore(pg.app_dsn, "alice", schema=pg.schema)


def _new(store, **over):
    base = dict(content="A ledger record.", source_agent="t", session_id="s", type="decision", evidence=CHAT)
    base.update(over)
    return store.create_memory(MemoryCreate(**base))


def _legacy(store, monkeypatch, **over):
    monkeypatch.setenv("JARVIS_CLAUSE_V", "off")
    try:
        return _new(store, **over)
    finally:
        monkeypatch.setenv("JARVIS_CLAUSE_V", "enforce")


def test_create_refuses_forbidden_types_and_missing_evidence(store):
    for kwargs, code in (
        (dict(type="preference", evidence=FILE), "clause_v_preference"),
        (dict(type="task", evidence=FILE), "clause_v_transient_state"),
        (dict(type="external_context", evidence=FILE), "clause_v_external_context"),
        (dict(evidence=[]), "clause_v_evidence_required"),
        (dict(type="fact", evidence=CHAT), "clause_v_evidence_required"),
    ):
        with pytest.raises(ClauseVViolation) as exc:
            _new(store, **kwargs)
        assert code in [r.code for r in exc.value.reasons]
    assert store.list_memories(limit=50) == []  # nothing was written


def test_create_accepts_a_decision_and_an_evidenced_fact(store):
    assert _new(store).type == "decision"
    assert _new(store, type="fact", evidence=FILE, content="An evidenced fact.").type == "fact"


def test_a_record_cannot_be_verified_unless_it_passes_and_the_row_is_left_untouched(store, monkeypatch):
    bad = _legacy(store, monkeypatch, type="preference", evidence=[])
    with pytest.raises(ClauseVViolation):
        store.update_memory(bad.id, MemoryUpdate(status="verified"))
    after = store.get_memory(bad.id)
    assert after.status == "draft" and after.version == bad.version  # the refused update changed nothing
    good = _new(store)
    assert store.update_memory(good.id, MemoryUpdate(status="verified")).status == "verified"
    fixable = _legacy(store, monkeypatch, evidence=[], content="Needs evidence.")
    assert store.update_memory(fixable.id, MemoryUpdate(status="verified", evidence=CHAT)).status == "verified"


def test_retyping_and_leaving_the_archive_go_through_the_gate_but_archiving_does_not(store, monkeypatch):
    good = _new(store, content="Fine decision.")
    with pytest.raises(ClauseVViolation):
        store.update_memory(good.id, MemoryUpdate(type="preference"))
    pref = _legacy(store, monkeypatch, type="preference", evidence=[], content="An old preference.")
    assert store.update_memory(pref.id, MemoryUpdate(tags=["sensitive"])).tags == ["sensitive"]  # old drafts stay manageable
    assert store.update_memory(pref.id, MemoryUpdate(status="archived")).status == "archived"
    with pytest.raises(ClauseVViolation):
        store.update_memory(pref.id, MemoryUpdate(status="draft"))
    assert store.delete_memory(pref.id) is True
