"""EMR -> STM -> LTM pipeline tests.

Covers the governed end-to-end flow:
- EMR reads LTM and promotes records into the STM working set.
- STM is consolidated back to LTM ONLY as a governed DRAFT via emr_write
  (conflict-check / transcript gate / provenance-preserving), never verified.
- Abstention: no junk writes when nothing is promoted or telemetry is stale.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.emr_pipeline import ConsolidationRequest, consolidate_to_ltm, pipeline
from app.emr_write import emr_remember, EmrRememberRequest
from app.models import MemoryCreate, MemoryRecord
from app.store import JarvisStore


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _seed(store: JarvisStore, **kwargs) -> MemoryRecord:
    now = _now()
    base = dict(
        content="Default memory content",
        source_agent="test",
        session_id="sess-seed",
        type="fact",
        confidence=0.6,
        evidence=[],
        status="draft",
        subject=None,
        tags=[],
    )
    base.update(kwargs)
    return store.create_memory(MemoryCreate(**base))


@pytest.fixture(autouse=True)
def _enable_write(monkeypatch):
    monkeypatch.setenv("JARVIS_MCP_WRITE_ENABLED", "true")
    yield


@pytest.fixture()
def store(tmp_path):
    from app.store import reset_store_for_tests
    reset_store_for_tests()
    s = JarvisStore(str(tmp_path / "pipeline.json"))
    yield s
    reset_store_for_tests()


def test_pipeline_emr_stm_ltm_consolidates_draft(store):
    _seed(
        store,
        content="Axiom-X: deterministic governed GPU delegation to worker lanes.",
        subject="governance",
        type="architecture",
        status="verified",
        confidence=0.9,
    )
    req = ConsolidationRequest(
        query="axiom gpu delegation worker lanes",
        session_key="s1",
        session_id="sess-1",
        source_agent="pipeline-test",
        memory_type="architecture",
        subject="governance",
        user_requested=True,
    )
    trace = pipeline(store, req)

    # EMR -> STM: records promoted into the working set.
    assert trace.promoted, "expected promoted STM entries"
    assert trace.stm, "expected an STM working set"

    # STM -> LTM: consolidated as a single governed DRAFT.
    assert trace.consolidate.outcome == "consolidated"
    assert trace.consolidate.memory_id

    drafted = store.get_memory(trace.consolidate.memory_id)
    assert drafted is not None
    # Draft-only: the pipeline never auto-verifies.
    assert drafted.status == "draft"
    assert trace.manifest["verified"] == 0
    # Provenance retained.
    assert any(e.kind == "stm-provenance" for e in drafted.evidence)
    assert drafted.source_agent.endswith("pipeline-test")


def test_pipeline_abstains_without_promotions(store):
    # Seed a record unrelated to the query so nothing promotes.
    _seed(store, content="Tomato watering schedule for summer beds.", subject="garden")
    req = ConsolidationRequest(
        query="quantum chromodynamics lattice gauge",
        session_key="s2",
        session_id="sess-2",
        user_requested=True,
    )
    trace = pipeline(store, req)

    assert trace.manifest["stm_promoted"] == 0
    assert trace.consolidate.outcome == "abstained"
    assert trace.consolidate.memory_id == ""


def test_consolidate_writes_draft_via_governed_gateway(store):
    promoted = [{"memory_id": "mid-1", "summary": "Consolidated summary one."}]
    outcome = consolidate_to_ltm(
        store,
        promoted=promoted,
        source_agent="cfg-test",
        session_id="sess-3",
        memory_type="fact",
        subject=None,
        user_requested=True,
    )
    assert outcome.outcome == "consolidated"
    rec = store.get_memory(outcome.memory_id)
    assert rec is not None
    assert rec.status == "draft"
    assert "mid-1" in [e.ref for e in rec.evidence]
    assert "stm-consolidated" in rec.tags


def test_consolidate_abstains_on_empty_promoted(store):
    outcome = consolidate_to_ltm(
        store,
        promoted=[],
        source_agent="cfg-test",
        session_id="sess-4",
        memory_type="fact",
        subject=None,
        user_requested=True,
    )
    assert outcome.outcome == "abstained"
    assert outcome.memory_id == ""
    # No junk record written.
    assert store.list_memories(limit=100, truth_scope="live") == []


def test_pipeline_gate_refused_when_write_flag_disabled(store, monkeypatch):
    monkeypatch.setenv("JARVIS_MCP_WRITE_ENABLED", "false")
    _seed(
        store,
        content="Axiom-X: deterministic governed GPU delegation to worker lanes.",
        subject="governance",
        type="architecture",
        status="verified",
        confidence=0.9,
    )
    req = ConsolidationRequest(
        query="axiom gpu delegation worker lanes",
        session_key="s5",
        session_id="sess-5",
        user_requested=True,
    )
    trace = pipeline(store, req)
    if trace.manifest["stm_promoted"]:
        assert trace.consolidate.outcome == "gate_refused"


def test_emr_remember_honors_user_requested_require():
    # Guards against accidental auto-write: user_requested=false aborts.
    store_backing = JarvisStore.__new__(JarvisStore)

    class _FakeStore:
        def list_memories(self, limit=9999, truth_scope="live", subject=None):
            return []

    resp = emr_remember(
        _FakeStore(),
        EmrRememberRequest(
            content="x",
            source_agent="t",
            session_id="s",
            type="fact",
            user_requested=False,
        ),
    )
    assert resp.accepted is False
    assert resp.refuse_reason == "user-intent-required"
