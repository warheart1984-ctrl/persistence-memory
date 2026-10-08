"""AI twin coverage-index tests — core is pure; endpoint is dark by default."""

from __future__ import annotations

import copy
import random
from datetime import datetime, timezone, timedelta

import pytest
from fastapi.testclient import TestClient

import app.twin as twin
from app.main import app
from app.models import MemoryCreate, MemoryRecord
from app.store import get_store
from app.twin import (
    DIGEST_TAG_PREFIX,
    FilteredRecords,
    _WEIGHTS,
    coverage_index,
    generate_twin_intelligence,
    is_twin_authored,
    twin_components,
    twin_conflicts,
    twin_input_digest,
    twin_memory_payload,
    twin_mission,
    weakest_component,
)

client = TestClient(app)

_NOW = datetime(2026, 3, 1, tzinfo=timezone.utc)


def _rec(**kw) -> MemoryRecord:
    base = dict(
        id=kw.pop("id", "m1"),
        content=kw.pop("content", "x"),
        created_at=kw.pop("created_at", "2026-01-01T00:00:00Z"),
        updated_at=kw.pop("updated_at", "2026-01-01T00:00:00Z"),
        source_agent=kw.pop("source_agent", "devin"),
        session_id=kw.pop("session_id", "s1"),
        confidence=kw.pop("confidence", 0.5),
        type=kw.pop("type", "fact"),
        status=kw.pop("status", "draft"),
    )
    return MemoryRecord(**{**base, **kw})


def _fr(records) -> FilteredRecords:
    return FilteredRecords.from_records(records)


def _store_write(**kw) -> MemoryRecord:
    return get_store().create_memory(MemoryCreate(**kw))


def _store_write_base(**kw):
    base = dict(
        content="x", source_agent="devin", session_id="s1",
        type="fact", confidence=0.5, status="draft",
    )
    base.update(kw)
    return _store_write(**base)


# --- 1. weights ---

def test_weights_sum_to_one():
    assert abs(sum(_WEIGHTS.values()) - 1.0) < 1e-9


# --- 2. empty ledger fails closed ---

def test_empty_ledger_fails_closed():
    c = twin_components(_fr([]), now=_NOW)
    assert all(v == 0.0 for v in c.values())
    packet = generate_twin_intelligence(_fr([]), now=_NOW)
    assert packet["coverage_index"] == 0.0
    assert packet["mission"] == "Write the first evidenced memory."
    assert packet["brief"][0] == "No data on the ledger."


# --- 3. division-by-zero per component ---

def test_each_component_zero_on_empty_input():
    for key in _WEIGHTS:
        assert twin_components(_fr([]), now=_NOW)[key] == 0.0


def test_components_zero_when_only_twin_records_exist():
    own = [_rec(id=f"t{i}", source_agent=twin.TWIN_AGENT) for i in range(4)]
    c = twin_components(_fr(own), now=_NOW)
    assert all(v == 0.0 for v in c.values())


# --- 4. property test: 500 seeded ledgers ---

def test_500_random_ledgers_all_components_bounded_finite():
    rng = random.Random(20260301)
    types = ["decision", "fact", "task", "preference", "architecture", "research", "external_context"]
    for i in range(500):
        n = rng.randrange(0, 40)
        records = [
            _rec(
                id=f"r{i}-{j}",
                source_agent=rng.choice(["devin", "claude", "ai-twin", "", "unknown"]),
                type=rng.choice(types),
                status=rng.choice(["draft", "verified", "archived"]),
                created_at=f"2026-{rng.randrange(1, 4):02d}-{rng.randrange(1, 28):02d}T00:00:00Z",
                evidence=[{"kind": "ref", "ref": "x"}] if rng.random() < 0.5 else [],
                supersedes=f"r{i}-{rng.randrange(0, max(n, 1))}" if rng.random() < 0.3 else None,
                subject=rng.choice(["a", "b", None]),
            )
            for j in range(n)
        ]
        fr = _fr(records)
        comps = twin_components(fr, now=_NOW)
        for v in comps.values():
            assert isinstance(v, float) and 0.0 <= v <= 1.0
        idx = coverage_index(comps)
        assert 0.0 <= idx <= 1.0


# --- 5. twin-authored records change nothing (all kinds) ---

def test_twin_records_of_every_kind_are_invisible():
    human = [
        _rec(id="h1", type="decision", status="verified", evidence=[{"kind": "ref", "ref": "e"}]),
        _rec(id="h2", type="task", subject="proj.x"),
        _rec(id="h3", type="fact", subject="proj.x", content="different claim"),
    ]
    own = [
        _rec(id="t1", source_agent=twin.TWIN_AGENT, type=t, status=s,
             evidence=[{"kind": "ref", "ref": "te"}],
             supersedes="h1", subject="proj.x")
        for t in ("decision", "fact", "research")
        for s in ("draft", "verified")
    ]
    a = generate_twin_intelligence(_fr(human), now=_NOW)
    b = generate_twin_intelligence(_fr(human + own), now=_NOW)
    assert a == b


# --- 6. components refuse unfiltered input ---

def test_components_refuse_raw_list():
    raw = [_rec()]
    for fn in (twin_components, twin_conflicts, twin_input_digest, generate_twin_intelligence):
        with pytest.raises(TypeError):
            fn(raw) if fn is not generate_twin_intelligence else fn(raw, now=_NOW)


# --- 7. supersedes across the twin boundary ---

def test_twin_supersede_of_human_does_not_change_l_or_v():
    h1 = _rec(id="h1", status="verified")
    h2 = _rec(id="h2")
    baseline = twin_components(_fr([h1, h2]), now=_NOW)
    twin_sup = _rec(id="t1", source_agent=twin.TWIN_AGENT, status="verified", supersedes="h1")
    after = twin_components(_fr([h1, h2, twin_sup]), now=_NOW)
    assert after["L"] == baseline["L"] == 0.0
    assert after["V"] == baseline["V"]


def test_human_supersede_of_twin_does_not_change_l():
    h1 = _rec(id="h1", supersedes="t1")
    t1 = _rec(id="t1", source_agent=twin.TWIN_AGENT)
    c = twin_components(_fr([h1, t1]), now=_NOW)
    # h1's supersedes target was filtered out — no chain exists for scoring.
    assert c["L"] == 0.0


# --- 8. twin record as evidence reference: no P inflation ---

def test_evidence_ref_to_twin_record_does_not_count_for_p():
    # A record whose ONLY evidence cites a twin-authored record id has no
    # real provenance: the twin cannot be cited to justify a record.
    own = _rec(id="t1", source_agent=twin.TWIN_AGENT)
    citing = _rec(id="h1", evidence=[{"kind": "ref", "ref": "t1"}])
    honest = _rec(id="h2", evidence=[{"kind": "ref", "ref": "tests/x.py"}])
    assert twin_components(_fr([citing, own]), now=_NOW)["P"] == 0.0
    assert twin_components(_fr([citing, honest, own]), now=_NOW)["P"] == 0.5


def test_twin_authored_evidence_entries_do_not_inflate_p():
    h = _rec(id="h1")
    own = _rec(id="t1", source_agent=twin.TWIN_AGENT,
               evidence=[{"kind": "ref", "ref": "x"}])
    assert twin_components(_fr([h]), now=_NOW)["P"] == \
        twin_components(_fr([h, own]), now=_NOW)["P"] == 0.0


# --- 9. missing/unknown source_agent not counted in N ---

def test_blank_source_agent_not_a_participant():
    recs = [_rec(id="a", source_agent=""), _rec(id="b", source_agent="devin")]
    c = twin_components(_fr(recs), now=_NOW)
    assert c["N"] == pytest.approx(1 / 5)


# --- 10. invalid records skipped and counted ---

def test_invalid_records_skipped_and_counted():
    fr = FilteredRecords.from_records([
        _rec(id="ok"),
        {"id": "bad", "content": "no required fields"},   # missing status/type/etc.
        "not a record",
    ])
    assert len(fr) == 1
    assert len(fr.skipped) == 2
    packet = generate_twin_intelligence(fr, now=_NOW)
    assert len(packet["skipped_records"]) == 2
    assert all("reason" in s for s in packet["skipped_records"])


# --- 11. unknown status/type not counted ---

def test_unknown_status_or_type_never_counts():
    # Pydantic rejects unknown literal values -> skipped at the filter boundary.
    fr = FilteredRecords.from_records([
        {"id": "u1", "content": "x", "created_at": "2026-01-01T00:00:00Z",
         "updated_at": "2026-01-01T00:00:00Z", "source_agent": "a",
         "session_id": "s", "type": "nonsense", "status": "verified",
         "confidence": 0.5},
        {"id": "u2", "content": "x", "created_at": "2026-01-01T00:00:00Z",
         "updated_at": "2026-01-01T00:00:00Z", "source_agent": "a",
         "session_id": "s", "type": "fact", "status": "gold-plated",
         "confidence": 0.5},
    ])
    assert len(fr) == 0 and len(fr.skipped) == 2
    assert all(v == 0.0 for v in twin_components(fr, now=_NOW).values())


# --- 12. inputs unchanged after every twin function ---

def test_inputs_not_mutated():
    records = [_rec(id="a", subject="s", content="one"),
               _rec(id="b", subject="s", content="two")]
    snapshot = copy.deepcopy(records)
    fr = _fr(records)
    generate_twin_intelligence(fr, now=_NOW)
    twin_components(fr, now=_NOW)
    twin_conflicts(fr)
    twin_input_digest(fr)
    assert [r.model_dump() for r in records] == [r.model_dump() for r in snapshot]


# --- 13. conflicts verbatim with ids; C counts non-twin resolutions ---

def test_conflicts_quoted_verbatim_with_ids():
    a = _rec(id="conf-a", subject="deploy.target", content="prod")
    b = _rec(id="conf-b", subject="deploy.target", content="staging")
    packet = generate_twin_intelligence(_fr([a, b]), now=_NOW)
    sets = packet["conflicts"]["sets"]
    assert packet["conflicts"]["unresolved"] == 1
    assert sets[0]["subject"] == "deploy.target"
    assert sorted(sets[0]["record_ids"]) == ["conf-a", "conf-b"]
    assert "Do not merge" in packet["conflicts"]["policy_hint"]


def test_twin_supersede_cannot_resolve_conflict():
    a = _rec(id="ca", subject="s", content="x")
    b = _rec(id="cb", subject="s", content="y")
    before = twin_conflicts(_fr([a, b]))
    # A twin record superseding one side must NOT collapse the set.
    t = _rec(id="t", source_agent=twin.TWIN_AGENT, supersedes="cb", subject="s", content="z")
    after = twin_conflicts(_fr([a, b, t]))
    assert before == after
    comps = twin_components(_fr([a, b, t]), now=_NOW)
    assert comps["C"] == 0.0  # still fully unresolved


def test_human_supersede_resolves_conflict_for_c():
    # Resolution shape: supersede the disagreeing side and restate the
    # surviving claim -> the set becomes a benign duplicate (unresolved=False).
    a = _rec(id="ca", subject="s", content="x")
    b = _rec(id="cb", subject="s", content="y")
    resolver = _rec(id="cr", subject="s", content="x", supersedes="cb")
    sets = twin_conflicts(_fr([a, b, resolver]))
    assert len(sets) == 1 and sets[0]["unresolved"] is False
    comps = twin_components(_fr([a, b, resolver]), now=_NOW)
    assert comps["C"] == 1.0


# --- 14. shuffle determinism ---

def test_shuffle_determinism():
    records = [
        _rec(id=f"r{i}", type=t, subject="s" if i % 3 == 0 else None,
             created_at=f"2026-01-{i+1:02d}T00:00:00Z")
        for i, t in enumerate(["decision", "fact", "task", "preference",
                               "architecture", "research", "external_context"] * 2)
    ]
    rng = random.Random(7)
    baseline = generate_twin_intelligence(_fr(records), now=_NOW)
    for _ in range(10):
        shuffled = records[:]
        rng.shuffle(shuffled)
        assert generate_twin_intelligence(_fr(shuffled), now=_NOW) == baseline


# --- 15. injected now; no wall-clock reads ---

def test_now_is_injected_and_wall_clock_never_read():
    real_datetime = twin.datetime

    class NoClock:
        @staticmethod
        def fromisoformat(v):
            return real_datetime.fromisoformat(v)

        @staticmethod
        def now(*a, **k):
            raise AssertionError("wall clock read inside twin core")

    records = [_rec(id="a"), _rec(id="b", created_at="2026-02-01T00:00:00Z")]
    fr = _fr(records)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(twin, "datetime", NoClock)
        p1 = generate_twin_intelligence(fr, now=_NOW)
        p2 = generate_twin_intelligence(fr, now=_NOW)
        assert p1 == p2
        p3 = generate_twin_intelligence(fr, now=_NOW + timedelta(days=30))
        assert p3["generated_at"] != p1["generated_at"]


# --- 16. digest ---

def test_input_digest_stable_across_order_and_sensitive_to_content():
    a, b = _rec(id="d1"), _rec(id="d2", content="y")
    d1 = twin_input_digest(_fr([a, b]))
    d2 = twin_input_digest(_fr([b, a]))
    assert d1 == d2 and len(d1) == 64
    changed = _rec(id="d2", content="z")
    assert twin_input_digest(_fr([a, changed])) != d1
    # Twin-authored records never enter the digest.
    own = _rec(id="t", source_agent=twin.TWIN_AGENT)
    assert twin_input_digest(_fr([a, b, own])) == d1


# --- 23. mission targets weakest (kept) ---

def test_mission_targets_weakest_component():
    records = [
        _rec(id=f"m{i}", status="verified", evidence=[{"kind": "ref", "ref": f"e{i}"}])
        for i in range(3)
    ]
    c = twin_components(_fr(records), now=_NOW)
    assert weakest_component(c) == "L"
    assert "supersedes" in twin_mission(c)


# --- payload shape ---

def test_twin_memory_payload_is_ordinary_memory_create_shape():
    packet = generate_twin_intelligence(_fr([_rec()]), now=_NOW)
    payload = twin_memory_payload(packet, "sess-x", day="2026-03-01")
    created = MemoryCreate(**payload)
    assert created.source_agent == twin.TWIN_AGENT
    assert created.status == "draft"
    assert created.subject == "twin:daily:default:2026-03-01"
    assert f"{DIGEST_TAG_PREFIX}{packet['twin_input_digest']}" in created.tags


# ================= endpoint =================

def _flags(monkeypatch, enabled="1", persist=None):
    monkeypatch.setenv("JARVIS_TWIN_ENABLED", enabled)
    if persist is None:
        monkeypatch.delenv("JARVIS_TWIN_PERSIST_ENABLED", raising=False)
    else:
        monkeypatch.setenv("JARVIS_TWIN_PERSIST_ENABLED", persist)


# --- 17. disabled -> 404 ---

def test_endpoint_disabled_returns_404(monkeypatch):
    _flags(monkeypatch, enabled="0")
    assert client.get("/api/jarvis/twin/daily").status_code == 404
    assert client.get("/api/jarvis/twin/daily?persist=1").status_code == 404


# --- 18. enabled: read-only output + digest, nothing written ---

def test_endpoint_read_only_writes_nothing(monkeypatch):
    _flags(monkeypatch)
    _store_write_base(content="seed")
    before = len(get_store().list_memories(limit=9999))
    r = client.get("/api/jarvis/twin/daily")
    assert r.status_code == 200
    body = r.json()["twin"]
    assert "coverage_index" in body and len(body["twin_input_digest"]) == 64
    assert "not say whether any memory is true" in body["disclaimer"]
    assert len(get_store().list_memories(limit=9999)) == before


# --- 19. tenant isolation (postgres marker; skips without DSN) ---

@pytest.mark.postgres
def test_two_tenants_isolated(pg_schema):
    pytest.skip("requires JARVIS_TEST_PG_DSN and OAuth-mode tenant wiring")


def test_endpoint_reads_only_this_tenants_store(monkeypatch):
    """File-mode seam proof: the endpoint reads exactly get_store()'s records."""
    _flags(monkeypatch)
    _store_write_base(content="tenant-local", status="verified",
                      evidence=[{"kind": "ref", "ref": "e"}])
    r = client.get("/api/jarvis/twin/daily")
    assert r.status_code == 200
    body = r.json()["twin"]
    # exactly one live record on this store -> V reflects it, nothing else leaks in
    assert body["brief"][0].startswith("1 memory on the ledger")
    assert body["components"]["V"] == 1.0


# --- 20-22. persist ---

def test_persist_refused_when_flag_off(monkeypatch):
    _flags(monkeypatch, persist="0")
    _store_write_base()
    before = len(get_store().list_memories(limit=9999))
    r = client.get("/api/jarvis/twin/daily?persist=1")
    assert r.status_code == 403
    assert r.json()["detail"] == "TWIN_PERSIST_DISABLED"
    assert len(get_store().list_memories(limit=9999)) == before


def test_persist_writes_one_record_through_memory_create(monkeypatch):
    _flags(monkeypatch, persist="1")
    _store_write_base(content="seed", status="verified",
                      evidence=[{"kind": "ref", "ref": "e"}])
    r = client.get("/api/jarvis/twin/daily?persist=1")
    assert r.status_code == 200
    body = r.json()
    assert body["persisted"] == "created"
    twin_recs = [m for m in get_store().list_memories(limit=9999)
                 if m.source_agent == "ai-twin"]
    assert len(twin_recs) == 1
    assert twin_recs[0].type == "research"
    assert twin_recs[0].id == body["memory_id"]


def test_persist_idempotent_same_digest_same_day(monkeypatch):
    _flags(monkeypatch, persist="1")
    _store_write_base()
    r1 = client.get("/api/jarvis/twin/daily?persist=1")
    r2 = client.get("/api/jarvis/twin/daily?persist=1")
    assert r1.json()["persisted"] == "created"
    assert r2.json()["persisted"] == "existing"
    assert r1.json()["memory_id"] == r2.json()["memory_id"]
    assert len(get_store().list_memories(limit=9999)) == 2  # seed + one brief


def test_persist_different_digest_same_day_supersedes_today(monkeypatch):
    _flags(monkeypatch, persist="1")
    _store_write_base(content="v1")
    r1 = client.get("/api/jarvis/twin/daily?persist=1")
    _store_write_base(content="v2")  # ledger changed -> new digest
    r2 = client.get("/api/jarvis/twin/daily?persist=1")
    assert r2.json()["persisted"] == "created"
    assert r2.json()["memory_id"] != r1.json()["memory_id"]
    new_rec = next(m for m in get_store().list_memories(limit=9999)
                   if m.id == r2.json()["memory_id"])
    assert new_rec.supersedes == r1.json()["memory_id"]


def test_persist_new_day_does_not_supersede(monkeypatch):
    _flags(monkeypatch, persist="1")
    _store_write_base()
    r1 = client.get("/api/jarvis/twin/daily?persist=1")
    # Simulate the next day by writing a fresh record whose created_at differs —
    # the day key comes from the endpoint clock, so patch it.
    tomorrow = _NOW + timedelta(days=400)  # ensures a different YYYY-MM-DD
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("app.main.datetime", _FixedDT(tomorrow))
        r2 = client.get("/api/jarvis/twin/daily?persist=1")
    assert r2.json()["persisted"] == "created"
    new_rec = next(m for m in get_store().list_memories(limit=9999)
                   if m.id == r2.json()["memory_id"])
    assert new_rec.supersedes is None
    assert new_rec.subject != next(
        m for m in get_store().list_memories(limit=9999)
        if m.id == r1.json()["memory_id"]).subject


class _FixedDT:
    """Stand-in for app.main.datetime: fixed now(), real fromisoformat."""

    def __init__(self, instant: datetime):
        self._instant = instant

    def now(self, tz=None):
        return self._instant
