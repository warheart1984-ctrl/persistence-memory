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
from app.amul import AmulField, anchor_memory, get_field, reset_field_for_tests
from app.amul_gc import GCViolation, checkpoints_path_for, compact, gc_status, verify_gc
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


def test_a_tenants_field_carries_its_own_chain_beside_it_and_everything_else_keeps_the_shared_default(world):
    tmp, shared = world
    with acting_for("t_alice"):
        alice = get_field()
    with acting_for("t_bob"):
        bob = get_field()
    assert checkpoints_path_for(alice) == str(tmp / "tenants" / "t_alice-checkpoints.jsonl")
    assert checkpoints_path_for(alice) != checkpoints_path_for(bob)
    assert checkpoints_path_for(get_field()) == shared                                       # the operator (no tenant context): unchanged
    assert checkpoints_path_for(AmulField(str(tmp / "field.jsonl"))) == shared               # an explicitly pathed field (tests, tools): unchanged


def test_an_operator_field_that_merely_looks_like_a_tenants_keeps_the_configured_chain(world, monkeypatch):
    """Review finding: JARVIS_AMUL_PATH=/srv/tenants/operator-field.jsonl is the OPERATOR's field. Ownership comes from the tenant context, not from a file name."""
    tmp, shared = world
    lookalike = tmp / "srv" / "tenants" / "operator-field.jsonl"
    lookalike.parent.mkdir(parents=True)
    monkeypatch.setattr(amul, "FIELD_PATH", str(lookalike))
    reset_field_for_tests()
    field = get_field()                                                                       # no tenant context: the operator
    assert field.path == str(lookalike) and checkpoints_path_for(field) == shared
    recs = [_rec(i) for i in range(3)]
    for r in recs:
        anchor_memory(r, field)
    compact(field, recs)
    assert Path(shared).exists() and not list(lookalike.parent.glob("*-checkpoints.jsonl")), "the operator's chain must stay at the configured path"
    assert verify_gc(field).mode == "checkpoint_chain"


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


def test_a_tenants_legacy_chain_in_the_shared_file_is_adopted_when_it_authenticates_against_its_field(world):
    """Upgrade path (review finding): a chain a tenant made before checkpoints were per tenant must not be abandoned; compaction would restart with no prior cold set (G2)."""
    tmp, shared = world
    a_field, a_recs = _fill("t_alice", 4)
    compact(a_field, a_recs, checkpoints_path=shared)                                          # the OLD behaviour: the tenant's chain in the shared file
    own = tmp / "tenants" / "t_alice-checkpoints.jsonl"
    assert not own.exists()
    report = verify_gc(a_field)                                                                  # new default: adopts, then verifies through the chain
    assert own.exists() and own.read_bytes() == Path(shared).read_bytes()
    assert report.mode == "checkpoint_chain" and report.integrity_ok and report.checkpoints_checked == 1
    for r in [_rec(10), _rec(11)]:                                                               # and the chain CONTINUES from the adopted one
        anchor_memory(r, a_field)
    compact(a_field, a_recs + [_rec(10), _rec(11)])
    assert gc_status(a_field)["total_checkpoints"] == 2 and verify_gc(a_field).integrity_ok


def test_a_chain_made_for_another_field_is_never_adopted(world):
    tmp, shared = world
    o_field, o_recs = _fill(None, 4)
    compact(o_field, o_recs)                                                                     # the operator's chain, in the shared file
    t_field, t_recs = _fill("t_alice", 5)                                                        # longer than the operator's chain, so only the hashes can tell them apart
    report = verify_gc(t_field)
    assert report.mode == "full_rehash" and report.integrity_ok
    assert not (tmp / "tenants" / "t_alice-checkpoints.jsonl").exists(), "the operator's chain must not be copied to a tenant"
    assert verify_gc(o_field).mode == "checkpoint_chain"


def test_byte_identical_tenant_fields_make_the_legacy_chain_ambiguous_so_nobody_adopts_it(world):
    """Review finding: a cloned or restored tenant has the same field bytes, so matching bytes alone cannot say whose chain it is."""
    tmp, shared = world
    a_field, a_recs = _fill("t_alice", 3)
    compact(a_field, a_recs, checkpoints_path=shared)                                           # the legacy chain, made for alice
    (tmp / "tenants" / "t_clone-field.jsonl").write_bytes(Path(a_field.path).read_bytes())      # a clone of her field
    reset_field_for_tests()
    with acting_for("t_alice"):
        a2 = get_field()
    with acting_for("t_clone"):
        c = get_field()
    assert verify_gc(a2).mode == "full_rehash" and verify_gc(c).mode == "full_rehash"
    assert not (tmp / "tenants" / "t_alice-checkpoints.jsonl").exists() and not (tmp / "tenants" / "t_clone-checkpoints.jsonl").exists()


def test_a_torn_shared_file_is_never_adopted_and_what_is_copied_is_what_was_validated(world):
    tmp, shared = world
    a_field, a_recs = _fill("t_alice", 3)
    compact(a_field, a_recs, checkpoints_path=shared)
    good = Path(shared).read_bytes()
    Path(shared).write_bytes(good + b'{"schema": "amul-gc-checkpoint-v1", "range_sta')           # a concurrent compaction caught mid-append
    assert verify_gc(a_field).mode == "full_rehash"
    assert not (tmp / "tenants" / "t_alice-checkpoints.jsonl").exists()
    Path(shared).write_bytes(good)
    assert verify_gc(a_field).mode == "checkpoint_chain"
    assert (tmp / "tenants" / "t_alice-checkpoints.jsonl").read_bytes() == good


def test_a_compaction_appending_between_validation_and_copy_cannot_slip_into_the_adopted_chain(world, monkeypatch):
    tmp, shared = world
    a_field, a_recs = _fill("t_alice", 3)
    compact(a_field, a_recs, checkpoints_path=shared)
    good = Path(shared).read_bytes()
    real = amul_gc._authenticates

    def authenticates_then_a_writer_appends(cps, raw):
        ok = real(cps, raw)
        with open(shared, "ab") as fh:
            fh.write(b'{"torn": ')                                                            # the operator's compaction lands right after the validation read
        return ok

    monkeypatch.setattr(amul_gc, "_authenticates", authenticates_then_a_writer_appends)
    amul_gc._adopt_legacy_chain(a_field, str(tmp / "tenants" / "t_alice-checkpoints.jsonl"))
    assert (tmp / "tenants" / "t_alice-checkpoints.jsonl").read_bytes() == good               # exactly the validated snapshot, not what the file became


def test_compaction_is_blocked_while_the_legacy_chain_is_ambiguous_but_resolving_it_unblocks(world):
    """Review finding: declining to adopt must not let compact() start a replacement chain (it would lose the owner's prior cold set, G2)."""
    tmp, shared = world
    a_field, a_recs = _fill("t_alice", 3)
    compact(a_field, a_recs, checkpoints_path=shared)
    (tmp / "tenants" / "t_clone-field.jsonl").write_bytes(Path(a_field.path).read_bytes())
    reset_field_for_tests()
    with acting_for("t_alice"):
        a2 = get_field()
    with pytest.raises(GCViolation, match="more than one field"):
        compact(a2, a_recs)
    assert not (tmp / "tenants" / "t_alice-checkpoints.jsonl").exists()
    (tmp / "tenants" / "t_alice-checkpoints.jsonl").write_bytes(Path(shared).read_bytes())   # the operator resolves ownership by hand
    assert compact(a2, a_recs + [_rec(10)]).compacted in (True, False) and gc_status(a2)["total_checkpoints"] >= 1


def test_a_tenant_cloned_from_the_operators_field_does_not_adopt_the_operators_chain(world):
    tmp, shared = world
    o_field, o_recs = _fill(None, 3)
    compact(o_field, o_recs)                                                                     # the operator's chain in the shared file
    (tmp / "tenants").mkdir(exist_ok=True)
    (tmp / "tenants" / "t_clone-field.jsonl").write_bytes(Path(o_field.path).read_bytes())    # a tenant restored from the operator's field
    reset_field_for_tests()
    with acting_for("t_clone"):
        c = get_field()
    assert verify_gc(c).mode == "full_rehash"
    assert not (tmp / "tenants" / "t_clone-checkpoints.jsonl").exists()
    with pytest.raises(GCViolation):
        compact(c, o_recs)
    o2 = get_field()                                                                             # the operator itself is unaffected
    assert verify_gc(o2).mode == "checkpoint_chain" and verify_gc(o2).integrity_ok
