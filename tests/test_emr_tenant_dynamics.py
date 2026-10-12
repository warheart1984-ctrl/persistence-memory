"""The EMR reinforcement overlay is per tenant (the leak the isolation matrix found).

It used to be one process-global map keyed by memory id, saved to one shared sidecar, and GET /api/jarvis/memory/emr/status reported every tenant's entries
(memory ids, outcome ids).  Now the operator's entries are keyed by the bare id (as short-term memory is: so the legacy file and every existing caller keep
working) and a tenant's by "<tenant>\\x1f<id>", and nothing reads or reports another tenant's.  A sidecar written before the split is the operator's: it is
relabelled on load, backed up before it is first rewritten, and still readable by the code that wrote it.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app import emr
from app.emr import PositiveOutcomeSignal, OperatorCorrectionSignal
from app.identity import Principal, reset_principal, set_principal


def tenant(name: str):
    return set_principal(Principal(subject=name, scopes=frozenset({"memory.read", "memory.write"}), issuer="https://issuer.example"))


def outcome(oid: str) -> PositiveOutcomeSignal:
    return PositiveOutcomeSignal(signal="positive", source="user", outcome_id=oid)


def as_tenant(name, fn):
    token = tenant(name)
    try:
        return fn()
    finally:
        reset_principal(token)


SAME_ID = "mem-aaaaaaaaaaaa"      # the SAME id string in two tenants: ids are random, but nothing may depend on that


def test_a_tenants_reinforcement_is_invisible_to_another_even_for_the_same_memory_id():
    as_tenant("alice", lambda: emr.reinforce_ids({SAME_ID}, [SAME_ID], outcome=outcome("alice-outcome-1")))
    assert as_tenant("alice", lambda: emr.get_reinforcement(SAME_ID)).use_count == 1
    assert as_tenant("bob", lambda: emr.get_reinforcement(SAME_ID)) is None
    assert emr.get_reinforcement(SAME_ID) is None                     # and the operator's view is its own, too


def test_reinforcing_the_same_id_in_two_tenants_keeps_two_separate_states():
    as_tenant("alice", lambda: emr.reinforce_ids({SAME_ID}, [SAME_ID], outcome=outcome("a-1")))
    as_tenant("alice", lambda: emr.reinforce_ids({SAME_ID}, [SAME_ID], outcome=outcome("a-2")))
    as_tenant("bob", lambda: emr.reinforce_ids({SAME_ID}, [SAME_ID], outcome=outcome("b-1")))
    a = as_tenant("alice", lambda: emr.get_reinforcement(SAME_ID))
    b = as_tenant("bob", lambda: emr.get_reinforcement(SAME_ID))
    assert (a.use_count, a.outcome_ids) == (2, ["a-1", "a-2"]) and (b.use_count, b.outcome_ids) == (1, ["b-1"])


def test_the_same_outcome_id_is_not_a_replay_across_tenants():
    as_tenant("alice", lambda: emr.reinforce_ids({SAME_ID}, [SAME_ID], outcome=outcome("shared-outcome")))
    reinforced, unknown, replayed = as_tenant("bob", lambda: emr.reinforce_ids({SAME_ID}, [SAME_ID], outcome=outcome("shared-outcome")))
    assert len(reinforced) == 1 and replayed == []                    # bob's first use of that outcome id is not "already seen" because alice used it


def test_a_correction_in_one_tenant_does_not_reset_another_tenants_reinforcement():
    for who in ("alice", "bob"):
        as_tenant(who, lambda: emr.reinforce_ids({SAME_ID}, [SAME_ID], outcome=outcome(f"{who}-1")))
    as_tenant("alice", lambda: emr.correct_memory_ids({SAME_ID}, [SAME_ID], correction=OperatorCorrectionSignal(source="operator", correction_id="c-1")))
    assert as_tenant("alice", lambda: emr.get_reinforcement(SAME_ID)).salience == 0.0
    assert as_tenant("bob", lambda: emr.get_reinforcement(SAME_ID)).salience > 0.0


def _record(rid: str):
    from datetime import datetime, timezone
    from app.models import MemoryRecord

    now = datetime.now(timezone.utc).isoformat()
    return MemoryRecord(id=rid, content="launch codename bluebird", created_at=now, updated_at=now, source_agent="t", session_id="s", type="fact", confidence=0.5,
                        evidence=[], status="draft", subject=None, tags=[], content_sha256="deadbeef")


def test_the_overlay_never_boosts_another_tenants_score():
    """Scoring reads the overlay through the tenant-aware accessor: alice reinforcing an id raises HER activation of it, and gives bob's same-id record nothing."""
    rec = _record(SAME_ID)
    before = {w: as_tenant(w, lambda: emr.activate(rec, query="bluebird codename").A) for w in ("alice", "bob")}
    for i in range(6):
        as_tenant("alice", lambda i=i: emr.reinforce_ids({SAME_ID}, [SAME_ID], outcome=outcome(f"a-{i}")))
    alice = as_tenant("alice", lambda: emr.activate(rec, query="bluebird codename").A)
    bob = as_tenant("bob", lambda: emr.activate(rec, query="bluebird codename").A)
    assert alice > before["alice"]                                    # her reinforcement works for her
    assert bob == pytest.approx(before["bob"])                        # and does nothing for anyone else's record of the same id


def test_the_status_view_reports_only_the_callers_entries_and_counts():
    as_tenant("alice", lambda: emr.reinforce_ids({SAME_ID, "mem-bbbbbbbbbbbb"}, [SAME_ID, "mem-bbbbbbbbbbbb"], outcome=outcome("a-1")))
    as_tenant("bob", lambda: emr.reinforce_ids({"mem-cccccccccccc"}, ["mem-cccccccccccc"], outcome=outcome("b-1")))
    a = as_tenant("alice", emr.emr_status)["dynamics"]
    b = as_tenant("bob", emr.emr_status)["dynamics"]
    carol = as_tenant("carol", emr.emr_status)["dynamics"]
    op = emr.emr_status()["dynamics"]
    assert (a["reinforced_particles"], b["reinforced_particles"], carol["reinforced_particles"], op["reinforced_particles"]) == (2, 1, 0, 0)
    assert a["sidecar"]["persisted_particles"] == 2 and b["sidecar"]["persisted_particles"] == 1
    assert {s["memory_id"] for s in a["top"]} == {SAME_ID, "mem-bbbbbbbbbbbb"} and [s["memory_id"] for s in b["top"]] == ["mem-cccccccccccc"] and carol["top"] == []


def test_the_sidecar_path_is_the_operators_to_see_not_a_tenants():
    assert emr.emr_status()["dynamics"]["sidecar"]["path"] == emr.DYNAMICS_PATH
    assert as_tenant("alice", emr.emr_status)["dynamics"]["sidecar"]["path"] is None


# ----------------------------------------------------------------------------------------------------------- persistence
def test_the_sidecar_round_trips_every_tenant_and_keeps_the_operators_where_older_readers_look(tmp_path):
    emr.reinforce_ids({"mem-op0000000000"}, ["mem-op0000000000"], outcome=outcome("op-1"))
    as_tenant("alice", lambda: emr.reinforce_ids({SAME_ID}, [SAME_ID], outcome=outcome("a-1")))
    data = json.loads(Path(emr.DYNAMICS_PATH).read_text(encoding="utf-8"))
    assert data["schema"] == "emr-dynamics-v2"
    assert list(data["reinforcement"]) == ["mem-op0000000000"]       # the operator's, flat, exactly where the code before the split reads them
    (key,) = data["tenants"]
    assert list(data["tenants"][key]) == [SAME_ID]                    # alice's, under her tenant key, never in the flat map
    # a restart: forget everything in memory and load the file again
    emr._REINFORCEMENT.clear()
    emr._dynamics_loaded = False
    emr._ensure_dynamics()
    assert emr.get_reinforcement("mem-op0000000000").use_count == 1
    assert as_tenant("alice", lambda: emr.get_reinforcement(SAME_ID)).use_count == 1
    assert as_tenant("bob", lambda: emr.get_reinforcement(SAME_ID)) is None


def test_a_sidecar_written_before_the_split_is_the_operators_and_is_backed_up_before_it_is_rewritten():
    legacy = {"schema": "emr-dynamics-v1", "saved_at": "2026-10-01T00:00:00+00:00", "note": "old",
              "reinforcement": {"mem-111111111111": {"memory_id": "mem-111111111111", "use_count": 3, "salience": 0.15, "decay_damp": 0.09,
                                                     "last_reinforced_at": "2026-10-01T00:00:00+00:00", "outcome_ids": ["o1", "o2", "o3"], "correction_ids": []}}}
    path = Path(emr.DYNAMICS_PATH)
    path.write_text(json.dumps(legacy), encoding="utf-8")
    emr._REINFORCEMENT.clear()
    emr._dynamics_loaded = False
    emr._ensure_dynamics()
    assert emr.get_reinforcement("mem-111111111111").use_count == 3                           # the operator's, with no migration step to run
    assert as_tenant("alice", lambda: emr.get_reinforcement("mem-111111111111")) is None      # and nobody else's
    assert as_tenant("alice", emr.emr_status)["dynamics"]["reinforced_particles"] == 0
    assert not path.with_suffix(path.suffix + ".pre-tenant.bak").exists()                     # reading changes nothing on disk
    emr.reinforce_ids({"mem-222222222222"}, ["mem-222222222222"], outcome=outcome("o4"))      # the first write after the load
    backup = path.with_suffix(path.suffix + ".pre-tenant.bak")
    assert backup.exists() and json.loads(backup.read_text(encoding="utf-8")) == legacy       # the old file, byte for byte what it was
    assert json.loads(path.read_text(encoding="utf-8"))["schema"] == "emr-dynamics-v2"


def test_the_legacy_backup_is_made_once_and_never_overwritten():
    legacy = {"schema": "emr-dynamics-v1", "reinforcement": {}}
    path = Path(emr.DYNAMICS_PATH)
    path.write_text(json.dumps(legacy), encoding="utf-8")
    emr._dynamics_loaded = False
    emr.reinforce_ids({SAME_ID}, [SAME_ID], outcome=outcome("o1"))
    backup = path.with_suffix(path.suffix + ".pre-tenant.bak")
    first = backup.read_bytes()
    emr.reinforce_ids({SAME_ID}, [SAME_ID], outcome=outcome("o2"))                            # now a v2 file: nothing more to back up
    assert backup.read_bytes() == first


def test_a_corrupt_or_alien_sidecar_is_ignored_as_before():
    Path(emr.DYNAMICS_PATH).write_text("{not json", encoding="utf-8")
    emr._REINFORCEMENT.clear()
    emr._dynamics_loaded = False
    emr._ensure_dynamics()
    assert emr.get_reinforcement(SAME_ID) is None
