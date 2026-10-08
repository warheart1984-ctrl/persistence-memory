"""TwinState.v1 tests — every field rule is deterministic and citable."""

from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone

import pytest

from app.models import MemoryRecord
from app.twin import FilteredRecords
from app.twin_state import (
    ACTIVE_WINDOW_DAYS,
    STALE_DAYS,
    build_twin_state,
)

_NOW = datetime(2026, 3, 15, tzinfo=timezone.utc)


def _rec(**kw) -> MemoryRecord:
    base = dict(
        id=kw.pop("id", "m1"),
        content=kw.pop("content", "x"),
        created_at=kw.pop("created_at", "2026-03-10T00:00:00Z"),
        updated_at=kw.pop("updated_at", "2026-03-10T00:00:00Z"),
        source_agent=kw.pop("source_agent", "devin"),
        session_id=kw.pop("session_id", "s1"),
        confidence=kw.pop("confidence", 0.5),
        type=kw.pop("type", "fact"),
        status=kw.pop("status", "draft"),
    )
    return MemoryRecord(**{**base, **kw})


def _fr(records) -> FilteredRecords:
    return FilteredRecords.from_records(records)


def _days_ago(n: int) -> str:
    return (_NOW - timedelta(days=n)).isoformat()


# --- envelope fields ---

def test_state_envelope_fields():
    s = build_twin_state(_fr([_rec()]), identity_id="me", now=_NOW)
    assert s["schema"] == "TwinState.v1"
    assert s["as_of"] == _NOW.isoformat()
    assert s["identity"] == "me"
    assert len(s["state_digest"]) == 64
    assert len(s["twin_input_digest"]) == 64
    assert "not say whether any memory is true" in s["disclaimer"]
    assert set(s["components"]) == set("VPLWSTCN")
    assert s["weakest_component"] in s["components"]
    assert isinstance(s["recommended_mission"], str) and s["recommended_mission"]
    assert s["record_count"] == 1 and s["skipped_records"] == []


def test_empty_ledger_state():
    s = build_twin_state(_fr([]), now=_NOW)
    assert s["record_count"] == 0
    assert s["coverage_index"] == 0.0
    assert s["active_projects"] == []
    assert s["recent_accomplishments"] == []
    assert s["open_risks"] == [] and s["stale_commitments"] == []
    assert s["state_digest"] and s["twin_input_digest"]


# --- active_projects: tags, not subjects; 14-day window ---

def test_active_projects_window_and_tag_axis():
    fresh = _rec(id="f", tags=["alpha", "shared"], created_at=_days_ago(2))
    old = _rec(id="o", tags=["oldtag"], created_at=_days_ago(ACTIVE_WINDOW_DAYS + 3))
    also_fresh = _rec(id="f2", tags=["shared"], created_at=_days_ago(1))
    boundary = _rec(id="b", tags=["edge"], created_at=_days_ago(ACTIVE_WINDOW_DAYS))
    s = build_twin_state(_fr([fresh, old, also_fresh, boundary]), now=_NOW)
    assert s["active_projects"] == sorted(["alpha", "shared", "edge"])
    assert "oldtag" not in s["active_projects"]


def test_active_projects_ignores_subjects_and_archived():
    r = _rec(id="a", subject="some phrase", tags=[], created_at=_days_ago(0))
    archived = _rec(id="arc", tags=["ghost"], status="archived", created_at=_days_ago(0))
    s = build_twin_state(_fr([r, archived]), now=_NOW)
    assert s["active_projects"] == []  # subject is not a project axis


# --- recent_accomplishments: verified only, newest first, top 5, verbatim ---

def test_recent_accomplishments_rules():
    recs = [_rec(id=f"v{i}", status="verified", content=f"did thing {i}",
                 created_at=_days_ago(i)) for i in range(7)]
    recs += [_rec(id="d1", status="draft", content="not yet", created_at=_days_ago(0))]
    s = build_twin_state(_fr(recs), now=_NOW)
    acc = s["recent_accomplishments"]
    assert len(acc) == 5
    assert [a["record_id"] for a in acc] == ["v0", "v1", "v2", "v3", "v4"]
    assert acc[0]["summary"] == "did thing 0"  # verbatim content
    assert acc[0]["subject"] == ""


# --- open_risks: unresolved conflicts + EXACTLY the "risk" tag ---

def test_open_risks_includes_conflicts_and_risk_tag():
    a = _rec(id="ca", subject="deploy", content="blue")
    b = _rec(id="cb", subject="deploy", content="green")
    tagged = _rec(id="rk", tags=["risk"], content="key rotation overdue")
    s = build_twin_state(_fr([a, b, tagged]), now=_NOW)
    kinds = {r["kind"] for r in s["open_risks"]}
    assert kinds == {"conflict", "tag"}
    conflict = next(r for r in s["open_risks"] if r["kind"] == "conflict")
    assert conflict["subject"] == "deploy"
    assert set(conflict["record_ids"]) == {"ca", "cb"}
    tag = next(r for r in s["open_risks"] if r["kind"] == "tag")
    assert tag["record_id"] == "rk" and tag["text"] == "key rotation overdue"


def test_risk_tag_is_exactly_risk_not_security_or_todo():
    """PINNED: only the literal tag 'risk' contributes to open_risks.

    'security'/'todo' are ordinary tags; a later change must not silently
    widen the risk surface by absorbing synonyms.
    """
    risk = _rec(id="r1", tags=["risk"])
    sec = _rec(id="r2", tags=["security"])
    todo = _rec(id="r3", tags=["todo"])
    almost = _rec(id="r4", tags=["Risk", "risky", "at-risk"])
    s = build_twin_state(_fr([risk, sec, todo, almost]), now=_NOW)
    tag_ids = {r["record_id"] for r in s["open_risks"] if r["kind"] == "tag"}
    assert tag_ids == {"r1"}


# --- stale_commitments: task + archived + superseded + 14-day silence ---

def test_stale_commitments_rules():
    old_task = _rec(id="t1", type="task", subject="db", created_at=_days_ago(30))
    s = build_twin_state(_fr([old_task]), now=_NOW)
    assert [c["record_id"] for c in s["stale_commitments"]] == ["t1"]
    assert s["stale_commitments"][0]["days_since_update"] >= 30
    assert s["stale_commitments"][0]["summary"] == "x"

    fresh = _rec(id="t2", type="task", subject="db", created_at=_days_ago(2))
    s2 = build_twin_state(_fr([fresh]), now=_NOW)
    assert s2["stale_commitments"] == []

    # newer same-subject activity rescues the task
    activity = _rec(id="n1", subject="db", created_at=_days_ago(1))
    s3 = build_twin_state(_fr([old_task, activity]), now=_NOW)
    assert s3["stale_commitments"] == []

    # superseded task is gone
    sup = _rec(id="t4", type="task", subject="x", created_at=_days_ago(30))
    killer = _rec(id="k", supersedes="t4", subject="x", created_at=_days_ago(29))
    s4 = build_twin_state(_fr([sup, killer]), now=_NOW)
    assert "t4" not in [c["record_id"] for c in s4["stale_commitments"]]

    # archived task is gone
    arch = _rec(id="t5", type="task", subject="y", status="archived",
                created_at=_days_ago(30))
    s5 = build_twin_state(_fr([arch]), now=_NOW)
    assert s5["stale_commitments"] == []


# --- determinism: shuffle + digest ---

def test_state_deterministic_under_shuffle_and_digest_stable():
    recs = [
        _rec(id="a", status="verified", tags=["g1"], evidence=[{"kind": "r", "ref": "e"}]),
        _rec(id="b", type="task", subject="db", created_at=_days_ago(30)),
        _rec(id="c", subject="deploy", content="blue"),
        _rec(id="d", subject="deploy", content="green"),
        _rec(id="e", tags=["risk"]),
    ]
    baseline = build_twin_state(_fr(recs), now=_NOW)
    rng = random.Random(9)
    for _ in range(50):
        shuffled = list(recs)
        rng.shuffle(shuffled)
        s = build_twin_state(_fr(shuffled), now=_NOW)
        assert s == baseline
        assert s["state_digest"] == baseline["state_digest"]


def test_state_digest_excludes_itself_and_tracks_content():
    s1 = build_twin_state(_fr([_rec(id="x")]), now=_NOW)
    s2 = build_twin_state(_fr([_rec(id="x")]), now=_NOW)
    assert s1["state_digest"] == s2["state_digest"]
    changed = build_twin_state(_fr([_rec(id="x", content="different")]), now=_NOW)
    assert changed["state_digest"] != s1["state_digest"]
    assert "state_digest" not in repr(s1) or True  # digest input excludes itself
    # prove self-exclusion: recompute over dict minus digest matches
    import hashlib, json
    payload = {k: v for k, v in s1.items() if k != "state_digest"}
    expected = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    assert s1["state_digest"] == expected


# --- twin-authored records are invisible to state ---

def test_twin_authored_records_do_not_change_state():
    human = [_rec(id="h1", status="verified", tags=["g"]),
             _rec(id="h2", subject="deploy", content="blue")]
    s1 = build_twin_state(_fr(human), now=_NOW)
    twin_recs = [
        _rec(id="t1", source_agent="ai-twin", status="verified",
             tags=["risk", "newtag"], type="task", subject="db",
             created_at=_days_ago(60)),
        _rec(id="t2", source_agent="ai-twin", subject="deploy", content="green"),
    ]
    s2 = build_twin_state(_fr(human + twin_recs), now=_NOW)
    assert s2 == s1


# --- skipped records surface in state ---

def test_skipped_records_listed():
    fr = _fr([_rec(id="ok"), {"not": "a record"}, "garbage"])
    s = build_twin_state(fr, now=_NOW)
    assert s["record_count"] == 1
    assert len(s["skipped_records"]) == 2


def test_build_twin_state_requires_filtered_records():
    with pytest.raises(TypeError):
        build_twin_state([_rec()], now=_NOW)  # raw list refused
