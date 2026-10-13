"""AMUL GC checkpoints belong to ONE field file: a tenant's chain lives beside that tenant's field, never in the shared file.

Before this, only the field moved per tenant (app.amul._tenant_field_path); every tenant's checkpoints went to the one shared data/amul-checkpoints.jsonl, so tenant B's
verify_gc loaded a chain made for tenant A's field (line ranges and hashes of another file) and reported spurious failures, and tenant-derived state sat in a shared file.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

import app.amul as amul
import app.amul_gc as amul_gc
from app.amul import anchor_memory, get_field, reset_field_for_tests
from app.amul_gc import checkpoints_path_for, compact, gc_status, verify_gc
from app.identity import acting_for
from app.models import MemoryRecord


def _rec(i: int) -> MemoryRecord:
    now = datetime.now(timezone.utc).isoformat()
    return MemoryRecord(id=f"mem-{i:03d}", content=f"particle {i} body", created_at=now, updated_at=now, source_agent="t", session_id="s", type="fact", confidence=0.5,
                        evidence=[], status="draft", subject=None, tags=[], content_sha256="deadbeef")


@pytest.fixture
def world(tmp_path, monkeypatch):
    shared = str(tmp_path / "amul-checkpoints.jsonl")
    monkeypatch.setattr(amul, "FIELD_PATH", str(tmp_path / "amul-field.jsonl"))
    monkeypatch.setattr(amul_gc, "CHECKPOINTS_PATH", shared)
    reset_field_for_tests()
    yield tmp_path, shared
    reset_field_for_tests()


def _fill(tenant: str | None, n: int) -> tuple:
    """A field for the tenant (None = the operator) holding n anchored memories."""
    def build():
        field = get_field()
        recs = [_rec(i) for i in range(n)]
        for r in recs:
            anchor_memory(r, field)
        return field, recs
    if tenant is None:
        return build()
    with acting_for(tenant):
        return build()


def test_a_tenants_checkpoints_path_sits_beside_its_field_and_everything_else_keeps_the_shared_default(world):
    tmp, shared = world
    assert checkpoints_path_for(amul._tenant_field_path("t_alice")) == str(tmp / "tenants" / "t_alice-checkpoints.jsonl")
    assert checkpoints_path_for(amul._tenant_field_path("t_alice")) != checkpoints_path_for(amul._tenant_field_path("t_bob"))
    assert checkpoints_path_for(amul.FIELD_PATH) == shared                                    # the operator: unchanged
    assert checkpoints_path_for(str(tmp / "field.jsonl")) == shared                           # any other path (tests, tools): unchanged
    assert checkpoints_path_for(str(tmp / "tenants" / "stray.jsonl")) == shared               # only the tenant-field naming is rewritten
    assert checkpoints_path_for(str(tmp / "x" / "tenants" / "a-field.jsonl")) == str(tmp / "x" / "tenants" / "a-checkpoints.jsonl")


def test_one_tenants_compaction_writes_beside_its_own_field_and_touches_nobody_elses(world):
    tmp, shared = world
    a_field, a_recs = _fill("t_alice", 4)
    b_field, b_recs = _fill("t_bob", 2)
    report = compact(a_field, a_recs)                                                         # default path: derived from the field
    assert report.compacted is True
    assert (tmp / "tenants" / "t_alice-checkpoints.jsonl").exists()
    assert not (tmp / "tenants" / "t_bob-checkpoints.jsonl").exists()
    assert not Path(shared).exists(), "a tenant's checkpoints must not be written to the shared file"


def test_a_tenants_verify_is_unaffected_by_another_tenants_chain(world):
    tmp, shared = world
    a_field, a_recs = _fill("t_alice", 4)
    b_field, b_recs = _fill("t_bob", 2)
    compact(a_field, a_recs)
    a = verify_gc(a_field)
    assert a.mode == "checkpoint_chain" and a.integrity_ok and a.checkpoints_checked == 1       # positive control: alice's own chain verifies
    b = verify_gc(b_field)
    assert b.mode == "full_rehash" and b.integrity_ok and b.checkpoints_checked == 0 and not b.integrity_failures
    assert gc_status(b_field)["total_checkpoints"] == 0 and gc_status(a_field)["total_checkpoints"] == 1
    assert gc_status(a_field)["path"].endswith("t_alice-checkpoints.jsonl")
    compact(b_field, b_recs)                                                                  # and bob can keep his own, independent chain
    assert verify_gc(b_field).mode == "checkpoint_chain" and verify_gc(b_field).integrity_ok
    assert verify_gc(a_field).integrity_ok and gc_status(a_field)["total_checkpoints"] == 1


def test_the_operators_checkpoints_stay_in_the_shared_file_exactly_as_before(world):
    tmp, shared = world
    field, recs = _fill(None, 3)
    compact(field, recs)
    assert Path(shared).exists() and len(Path(shared).read_text().splitlines()) == 1
    assert verify_gc(field).mode == "checkpoint_chain"
    assert not (tmp / "tenants").exists() or not list((tmp / "tenants").glob("*-checkpoints.jsonl"))


def test_the_operators_chain_does_not_leak_into_a_tenants_verify(world):
    """The reverse direction: the operator's compaction used to be the chain every tenant loaded."""
    tmp, shared = world
    o_field, o_recs = _fill(None, 5)
    t_field, t_recs = _fill("t_alice", 2)
    compact(o_field, o_recs)
    t = verify_gc(t_field)
    assert t.mode == "full_rehash" and t.integrity_ok and not t.integrity_failures


def test_verify_field_for_a_tenant_goes_through_its_own_chain(world):
    tmp, shared = world
    a_field, a_recs = _fill("t_alice", 4)
    b_field, b_recs = _fill("t_bob", 2)
    compact(a_field, a_recs)
    report = amul.verify_field(b_field, b_recs)
    assert report.gc_mode == "full_rehash" and report.checkpoints_checked == 0
