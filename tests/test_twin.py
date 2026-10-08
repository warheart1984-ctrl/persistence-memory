"""AI twin component/score/invariant tests — pure functions, no store needed."""

from __future__ import annotations

from app.models import MemoryRecord
from app.twin import (
    TWIN_AGENT,
    _WEIGHTS,
    generate_twin_intelligence,
    twin_components,
    twin_memory_payload,
    twin_mission,
    twin_score,
    weakest_component,
)


def _rec(**kw) -> MemoryRecord:
    base = dict(
        id=kw.pop("id", "m1"),
        content="x",
        created_at=kw.pop("created_at", "2026-01-01T00:00:00Z"),
        updated_at="2026-01-01T00:00:00Z",
        source_agent=kw.pop("source_agent", "devin"),
        session_id="s1",
        confidence=0.5,
        type=kw.pop("type", "fact"),
        status=kw.pop("status", "draft"),
    )
    return MemoryRecord(**{**base, **kw})


def test_weights_sum_to_one():
    assert abs(sum(_WEIGHTS.values()) - 1.0) < 1e-9


def test_empty_ledger_fails_closed():
    c = twin_components([])
    assert all(v == 0.0 for v in c.values())
    packet = generate_twin_intelligence([], [])
    assert packet["score"] == 0.0
    assert "evidence" in packet["mission"].lower() or "verified" in packet["mission"].lower()


def test_self_dealing_excluded():
    real = _rec(id="m1", status="verified", evidence=[{"kind": "ref", "ref": "x"}])
    own = _rec(id="m2", source_agent=TWIN_AGENT, status="verified",
               evidence=[{"kind": "ref", "ref": "x"}])
    with_own = twin_components([real, own])
    without_own = twin_components([real])
    assert with_own == without_own


def test_unresolved_conflicts_surface_verbatim():
    records = [_rec(id=str(i)) for i in range(3)]
    conflicts = [
        {"subject": "deploy.target", "unresolved": True},
        {"subject": "schema.version", "unresolved": False},
    ]
    packet = generate_twin_intelligence(records, conflicts)
    assert packet["conflicts"]["unresolved"] == 1
    assert packet["conflicts"]["subjects"] == ["deploy.target"]
    assert "Do not merge" in packet["conflicts"]["policy_hint"]
    assert twin_components(records, conflicts)["C"] == 0.5


def test_mission_targets_weakest_component():
    # 3 verified+evidenced records, all drafts→verified, single type, no lineage,
    # single agent, no conflicts → L should be weakest (nothing supersedes).
    records = [
        _rec(id=f"m{i}", status="verified", evidence=[{"kind": "ref", "ref": f"e{i}"}])
        for i in range(3)
    ]
    c = twin_components(records)
    assert weakest_component(c) == "L"
    assert "supersedes" in twin_mission(c)


def test_score_and_packet_are_deterministic():
    records = [_rec(id=str(i), type=t) for i, t in enumerate(
        ["decision", "fact", "task", "preference", "architecture"])]
    conflicts = [{"subject": "s", "unresolved": True}]
    a = generate_twin_intelligence(records, conflicts)
    b = generate_twin_intelligence(records, conflicts)
    assert a == b
    assert 0.0 <= a["score"] <= 1.0


def test_twin_memory_payload_is_ordinary_memory_create_shape():
    packet = generate_twin_intelligence([_rec()], [])
    payload = twin_memory_payload(packet, session_id="sess-x")
    from app.models import MemoryCreate
    created = MemoryCreate(**payload)
    assert created.source_agent == TWIN_AGENT
    assert created.status == "draft"
    assert created.subject == "twin:daily:default"
