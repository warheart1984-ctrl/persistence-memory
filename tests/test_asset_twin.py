"""Asset twin: simulation-only full loop with human veto gate."""

from __future__ import annotations

import os

os.environ["JARVIS_TWIN_ENABLED"] = "1"
os.environ["JARVIS_ASSET_TWIN_ENABLED"] = "1"

from fastapi.testclient import TestClient

from app.asset_twin.decision import build_decision
from app.asset_twin.evidence import EvidenceLedger
from app.asset_twin.execution import execute_approved
from app.asset_twin.models import Telemetry, VetoDecision
from app.asset_twin.simulator import SimulatedAsset
from app.asset_twin.twin_core import estimate_state
from app.asset_twin.veto import VetoGate
from app.main import app

client = TestClient(app)


def _telemetry(**over: object) -> Telemetry:
    base: dict = {
        "asset_id": "turbine-demo-01", "seq": 1, "at": "2026-10-08T00:00:00Z",
        "rpm": 3000.0, "vibration_mm_s": 2.0, "exhaust_temp_c": 420.0,
        "oil_pressure_kpa": 500.0, "ambient_temp_c": 20.0,
        "ambient_pressure_kpa": 101.3, "operator_setpoint_rpm": 3000.0,
    }
    base.update(over)
    return Telemetry(**base)


def test_cycle_never_executes_and_veto_wins(isolated) -> None:
    asset = SimulatedAsset()
    tel = _telemetry()
    state = estimate_state(tel)
    decision = build_decision(tel, state)
    gate = VetoGate()
    gate.propose(decision.decision_id)

    # Execution without approval is refused.
    refused = execute_approved(decision, gate, asset)
    assert refused.executed is False

    # Veto blocks even a later approve attempt path (approve after veto refused).
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc).isoformat()
    gate.decide(VetoDecision(decision_id=decision.decision_id, verdict="veto", reviewer="op-1", at=now))
    blocked = execute_approved(decision, gate, asset)
    assert blocked.executed is False
    assert "vetoed" in blocked.reason


def test_approve_then_execute_simulator_only() -> None:
    asset = SimulatedAsset()
    tel = _telemetry()
    state = estimate_state(tel)
    decision = build_decision(tel, state)
    gate = VetoGate()
    gate.propose(decision.decision_id)
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc).isoformat()
    gate.decide(VetoDecision(decision_id=decision.decision_id, verdict="approve", reviewer="op-1", at=now))
    done = execute_approved(decision, gate, asset)
    assert done.executed is True
    assert done.applied_setpoint_rpm is not None
    assert done.applied_setpoint_rpm <= 12000.0


def test_critical_telemetry_fails_safe() -> None:
    asset = SimulatedAsset()
    tel = _telemetry(vibration_mm_s=14.0, exhaust_temp_c=760.0)
    state = estimate_state(tel)
    decision = build_decision(tel, state)
    assert decision.recommendation.action == "safe_shutdown"
    assert decision.anomaly is True


def test_evidence_chain_detects_tamper(tmp_path) -> None:
    ledger = EvidenceLedger(path=tmp_path / "audit.jsonl")
    ledger.append("t1", "cycle", {"a": 1})
    ledger.append("t1", "veto", {"b": 2})
    ok, _ = ledger.verify("t1")
    assert ok is True
    # Tamper the file.
    lines = (tmp_path / "audit.jsonl").read_text().splitlines()
    import json

    rec = json.loads(lines[0])
    rec["body"]["a"] = 999
    lines[0] = json.dumps(rec)
    (tmp_path / "audit.jsonl").write_text("\n".join(lines) + "\n")
    ok2, problems = ledger.verify("t1")
    assert ok2 is False
    assert problems


def test_endpoints_dark_without_flag() -> None:
    os.environ["JARVIS_ASSET_TWIN_ENABLED"] = ""
    r = client.post("/api/jarvis/asset-twin/cycle", json=_telemetry().model_dump(mode="json"))
    assert r.status_code == 404
    os.environ["JARVIS_ASSET_TWIN_ENABLED"] = "1"


def _now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


def test_expired_ttl_refuses_execute() -> None:
    asset = SimulatedAsset()
    tel = _telemetry()
    state = estimate_state(tel)
    decision = build_decision(tel, state)
    # Force expiry into the past.
    decision.expires_at = "2000-01-01T00:00:00+00:00"
    gate = VetoGate()
    gate.propose(decision.decision_id)
    gate.decide(VetoDecision(decision_id=decision.decision_id, verdict="approve", reviewer="op-1", at=_now_iso()))
    refused = execute_approved(decision, gate, asset)
    assert refused.executed is False
    assert "expired" in refused.reason


def test_veto_after_approval_wins() -> None:
    asset = SimulatedAsset()
    tel = _telemetry()
    state = estimate_state(tel)
    decision = build_decision(tel, state)
    gate = VetoGate()
    gate.propose(decision.decision_id)
    gate.decide(VetoDecision(decision_id=decision.decision_id, verdict="approve", reviewer="op-1", at=_now_iso()))
    gate.decide(VetoDecision(decision_id=decision.decision_id, verdict="veto", reviewer="op-2", reason="second look", at=_now_iso()))
    assert gate.get(decision.decision_id).status == "vetoed"  # type: ignore[union-attr]
    blocked = execute_approved(decision, gate, asset)
    assert blocked.executed is False


def test_double_execute_refused() -> None:
    asset = SimulatedAsset()
    tel = _telemetry()
    state = estimate_state(tel)
    decision = build_decision(tel, state)
    gate = VetoGate()
    gate.propose(decision.decision_id)
    gate.decide(VetoDecision(decision_id=decision.decision_id, verdict="approve", reviewer="op-1", at=_now_iso()))
    first = execute_approved(decision, gate, asset)
    assert first.executed is True
    second = execute_approved(decision, gate, asset)
    assert second.executed is False
    assert "executed" in second.reason or "approved" in second.reason


def test_setpoint_above_envelope_clamped() -> None:
    asset = SimulatedAsset()
    tel = _telemetry(operator_setpoint_rpm=20000.0, rpm=19000.0)
    state = estimate_state(tel)
    decision = build_decision(tel, state)
    assert decision.recommendation.setpoint_rpm <= 12000.0
    # Even a forged high setpoint is clamped at execution.
    decision.recommendation.setpoint_rpm = 20000.0
    gate = VetoGate()
    gate.propose(decision.decision_id)
    gate.decide(VetoDecision(decision_id=decision.decision_id, verdict="approve", reviewer="op-1", at=_now_iso()))
    done = execute_approved(decision, gate, asset)
    assert done.executed is True
    assert done.applied_setpoint_rpm is not None
    assert done.applied_setpoint_rpm <= 12000.0


def test_tampered_audit_line_caught_by_verify(tmp_path) -> None:
    import json

    ledger = EvidenceLedger(path=tmp_path / "audit2.jsonl")
    ledger.append("t9", "measurement", {"rpm": 3000})
    ledger.append("t9", "assumption", {"model": "twin-core.v1"})
    ok, _ = ledger.verify("t9")
    assert ok is True
    lines = (tmp_path / "audit2.jsonl").read_text().splitlines()
    rec = json.loads(lines[1])
    rec["body"]["model"] = "evil.v9"
    lines[1] = json.dumps(rec)
    (tmp_path / "audit2.jsonl").write_text("\n".join(lines) + "\n")
    ok2, problems = ledger.verify("t9")
    assert ok2 is False
    assert problems


def test_tenant_isolation_for_packets_and_audit(isolated) -> None:
    import app.asset_twin.service as svc

    tel_a = _telemetry(asset_id="iso-01", seq=11)
    cycle_a = svc.run_cycle("tenant-A", tel_a)
    did = cycle_a.decision.decision_id

    # Tenant B cannot approve tenant A's packet.
    import pytest

    with pytest.raises(ValueError, match="unknown decision"):
        svc.decide_human("tenant-B", VetoDecision(decision_id=did, verdict="veto", reviewer="op-b", at=_now_iso()))

    # Tenant B cannot execute tenant A's packet.
    refused = svc.execute("tenant-B", did, "iso-01")
    assert refused.executed is False

    # Tenant B audit does not contain A's decision; A audit does.
    import json

    kinds_b: list[str] = []
    audit_path = svc.get_ledger()._path
    if audit_path.exists():
        for line in audit_path.read_text().splitlines():
            rec = json.loads(line)
            if rec.get("tenant") == "tenant-B" and rec.get("body", {}).get("decision_id") == did:
                kinds_b.append(rec.get("kind", ""))
    assert kinds_b == []

    # Actor identity is recorded on veto/hold.
    svc.decide_human("tenant-A", VetoDecision(decision_id=did, verdict="hold", reviewer="op-a", at=_now_iso()), actor="tenant-A")
    found_actor = False
    for line in audit_path.read_text().splitlines():
        rec = json.loads(line)
        body = rec.get("body", {})
        if rec.get("tenant") == "tenant-A" and body.get("decision_id") == did and rec.get("kind") == "veto":
            assert body.get("actor") == "tenant-A"
            assert body.get("reviewer") == "op-a"
            found_actor = True
    assert found_actor


def test_measurement_and_assumption_are_separate_kinds(isolated) -> None:
    import app.asset_twin.service as svc

    tel = _telemetry(asset_id="kinds-01", seq=21)
    cycle = svc.run_cycle("tenant-K", tel)
    did = cycle.decision.decision_id
    import json

    kinds: dict[str, dict] = {}
    for line in svc.get_ledger()._path.read_text().splitlines():
        rec = json.loads(line)
        if rec.get("tenant") == "tenant-K" and rec.get("body", {}).get("decision_id") == did:
            kinds[rec.get("kind")] = rec.get("body", {})
    assert "measurement" in kinds
    assert "assumption" in kinds
    assert "cycle" in kinds
    assert "rpm" in kinds["measurement"]
    assert "telemetry_digest" in kinds["measurement"]
    assert "model_version" in kinds["assumption"]
    assert "measurement_ref" in kinds["cycle"]
    assert "assumption_ref" in kinds["cycle"]


# -- review fixes: restart-safe evidence, atomic veto/execute, asset binding, stale telemetry ------------------------

import threading

import pytest

from app.asset_twin.evidence import GENESIS, EvidenceChainError


@pytest.fixture()
def isolated(tmp_path, monkeypatch):
    """A clean service: its own audit file and empty in-memory state."""
    import app.asset_twin.evidence as ev
    import app.asset_twin.service as svc

    monkeypatch.setenv("JARVIS_ASSET_TWIN_DIR", str(tmp_path))
    monkeypatch.setattr(ev, "_ledger", None)
    for name in ("_assets", "_gates", "_decisions", "_health", "_last_seq"):
        monkeypatch.setattr(svc, name, {})
    return svc


def _approve(svc, tenant: str, decision_id: str) -> None:
    svc.decide_human(tenant, VetoDecision(decision_id=decision_id, verdict="approve", reviewer="op-1", at=_now_iso()), actor=tenant)


class SlowAsset(SimulatedAsset):
    """An asset whose control takes a moment, so another request can land while it is being applied."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.applied = 0
        self.started = threading.Event()
        self.release = threading.Event()
        self.block = False

    def apply_control(self, setpoint_rpm):  # type: ignore[override]
        self.started.set()
        if self.block:
            assert self.release.wait(10)
        self.applied += 1
        return super().apply_control(setpoint_rpm)


def _approved_decision(seq: int = 1):
    tel = _telemetry(seq=seq)
    decision = build_decision(tel, estimate_state(tel))
    assert decision.recommendation.action != "safe_shutdown"
    gate = VetoGate()
    gate.propose(decision.decision_id)
    gate.decide(VetoDecision(decision_id=decision.decision_id, verdict="approve", reviewer="op-1", at=_now_iso()))
    return decision, gate


# 1. the evidence chain survives a restart

def test_evidence_chain_continues_after_a_restart(tmp_path) -> None:
    path = tmp_path / "audit.jsonl"
    first = EvidenceLedger(path)
    first.append("A", "measurement", {"n": 1})
    last_a = first.append("A", "cycle", {"n": 2})
    last_b = first.append("B", "measurement", {"n": 3})

    restarted = EvidenceLedger(path)  # same file, empty in-memory heads
    nxt = restarted.append("A", "execution", {"n": 4})
    assert nxt["prev"] == last_a["digest"], "the first record after a restart must point at the persisted head"
    assert restarted.append("B", "veto", {"n": 5})["prev"] == last_b["digest"]
    assert restarted.append("C", "measurement", {"n": 6})["prev"] == GENESIS
    for tenant in ("A", "B", "C"):
        assert restarted.verify(tenant) == (True, []), tenant


def test_a_broken_chain_is_not_appended_to(tmp_path) -> None:
    import json

    path = tmp_path / "audit.jsonl"
    ledger = EvidenceLedger(path)
    ledger.append("A", "measurement", {"n": 1})
    ledger.append("A", "cycle", {"n": 2})
    ledger.append("B", "measurement", {"n": 3})
    lines = path.read_text().splitlines()
    rec = json.loads(lines[0])
    rec["body"]["n"] = 999
    lines[0] = json.dumps(rec)
    path.write_text("\n".join(lines) + "\n")

    restarted = EvidenceLedger(path)
    with pytest.raises(EvidenceChainError, match="broken"):
        restarted.append("A", "execution", {"n": 4})
    with pytest.raises(EvidenceChainError):
        restarted.check("A")
    assert restarted.append("B", "veto", {"n": 5})["prev"].startswith("sha256:"), "another tenant's chain is unaffected"

    path.write_text(path.read_text() + "{not json\n")
    with pytest.raises(EvidenceChainError, match="unreadable"):
        EvidenceLedger(path).append("B", "x", {})


def test_nothing_acts_when_the_audit_trail_is_broken(isolated) -> None:
    svc = isolated
    cycle = svc.run_cycle("T", _telemetry(asset_id="trail-01", seq=1))
    did = cycle.decision.decision_id
    _approve(svc, "T", did)
    audit = svc.get_ledger()._path
    audit.write_text(audit.read_text().replace('"rpm":3000.0', '"rpm":1.0'))
    svc.get_ledger()._last.clear()  # as after a restart

    with pytest.raises(EvidenceChainError):
        svc.execute("T", did, "trail-01")
    assert svc._gates["T"].get(did).status == "approved", "the approval was not consumed"
    with pytest.raises(EvidenceChainError):
        svc.run_cycle("T", _telemetry(asset_id="trail-01", seq=2))


# 2. a decision acts on its own asset only

def test_execution_is_bound_to_the_asset_the_decision_names(isolated) -> None:
    svc = isolated
    cycle = svc.run_cycle("T", _telemetry(asset_id="alpha-01", seq=1))
    did = cycle.decision.decision_id
    _approve(svc, "T", did)

    wrong = svc.execute("T", did, "bravo-02")
    assert wrong.executed is False
    assert "alpha-01" in wrong.reason and "bravo-02" in wrong.reason
    assert "T:bravo-02" not in svc._assets, "the other asset was not even created"
    assert svc._gates["T"].get(did).status == "approved", "a refused attempt does not use up the approval"

    import json

    records = [json.loads(line) for line in svc.get_ledger()._path.read_text().splitlines()]
    refusal = [r for r in records if r["kind"] == "execution" and r["body"]["decision_id"] == did][-1]["body"]
    assert refusal["requested_asset"] == "bravo-02" and refusal["asset"] == "alpha-01" and refusal["executed"] is False

    right = svc.execute("T", did, "alpha-01")
    assert right.executed is True


def test_execute_approved_refuses_a_different_simulator_asset() -> None:
    decision, gate = _approved_decision()
    other = SlowAsset(asset_id="some-other-asset")
    refused = execute_approved(decision, gate, other)
    assert refused.executed is False and "some-other-asset" in refused.reason
    assert other.applied == 0 and gate.get(decision.decision_id).status == "approved"  # type: ignore[union-attr]


# 3. a veto and an execute cannot both win

def test_a_veto_that_arrives_while_the_control_is_being_applied_is_refused() -> None:
    decision, gate = _approved_decision()
    asset = SlowAsset(asset_id=decision.asset_id)
    asset.block = True
    result: dict = {}
    worker = threading.Thread(target=lambda: result.setdefault("exec", execute_approved(decision, gate, asset)))
    worker.start()
    assert asset.started.wait(10), "execution did not reach the asset"
    with pytest.raises(ValueError, match="too late"):
        gate.decide(VetoDecision(decision_id=decision.decision_id, verdict="veto", reviewer="op-2", at=_now_iso()))
    assert gate.get(decision.decision_id).status == "executing"  # type: ignore[union-attr]
    asset.release.set()
    worker.join(10)
    assert result["exec"].executed is True and asset.applied == 1
    assert gate.get(decision.decision_id).status == "executed"  # type: ignore[union-attr]


def test_two_overlapping_executes_apply_the_control_once() -> None:
    decision, gate = _approved_decision()
    asset = SlowAsset(asset_id=decision.asset_id)
    asset.block = True
    first: dict = {}
    worker = threading.Thread(target=lambda: first.setdefault("r", execute_approved(decision, gate, asset)))
    worker.start()
    assert asset.started.wait(10)
    second = execute_approved(decision, gate, asset)  # lands while the first is applying
    assert second.executed is False and "executing" in second.reason
    asset.release.set()
    worker.join(10)
    assert first["r"].executed is True and asset.applied == 1


def test_a_failed_application_gives_the_approval_back() -> None:
    decision, gate = _approved_decision()

    class Boom(SlowAsset):
        def apply_control(self, setpoint_rpm):  # type: ignore[override]
            raise RuntimeError("actuator fault")

    with pytest.raises(RuntimeError):
        execute_approved(decision, gate, Boom(asset_id=decision.asset_id))
    assert gate.get(decision.decision_id).status == "approved"  # type: ignore[union-attr]
    again = SlowAsset(asset_id=decision.asset_id)
    assert execute_approved(decision, gate, again).executed is True


def test_veto_and_execute_race_has_exactly_one_winner() -> None:
    for i in range(150):
        decision, gate = _approved_decision(seq=i + 1)
        asset = SlowAsset(asset_id=decision.asset_id)
        barrier = threading.Barrier(2)
        out: dict = {}

        def run_execute() -> None:
            barrier.wait()
            out["exec"] = execute_approved(decision, gate, asset)

        def run_veto() -> None:
            barrier.wait()
            try:
                gate.decide(VetoDecision(decision_id=decision.decision_id, verdict="veto", reviewer="op-2", at=_now_iso()))
                out["vetoed"] = True
            except ValueError:
                out["vetoed"] = False

        threads = [threading.Thread(target=run_execute), threading.Thread(target=run_veto)]
        [t.start() for t in threads]
        [t.join(10) for t in threads]
        assert out["exec"].executed != out["vetoed"], f"round {i}: both or neither won"
        assert asset.applied == (1 if out["exec"].executed else 0)


# 4. stale and replayed telemetry

def test_stale_or_replayed_telemetry_is_rejected_before_anything_is_updated(isolated) -> None:
    svc = isolated
    svc.run_cycle("T", _telemetry(asset_id="seq-01", seq=5))
    health_after_five = dict(svc._health)
    decisions_after_five = len(svc._decisions)
    for stale in (4, 5):
        with pytest.raises(svc.StaleTelemetryError, match="not newer"):
            svc.run_cycle("T", _telemetry(asset_id="seq-01", seq=stale, vibration_mm_s=0.1))
    assert svc._health == health_after_five and len(svc._decisions) == decisions_after_five
    svc.run_cycle("T", _telemetry(asset_id="seq-01", seq=6))
    # independent per asset and per tenant
    svc.run_cycle("T", _telemetry(asset_id="seq-02", seq=1))
    svc.run_cycle("U", _telemetry(asset_id="seq-01", seq=1))


def test_a_cycle_that_fails_does_not_use_up_its_sequence(isolated, monkeypatch) -> None:
    svc = isolated
    real = svc._run_accepted_cycle
    monkeypatch.setattr(svc, "_run_accepted_cycle", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    with pytest.raises(RuntimeError):
        svc.run_cycle("T", _telemetry(asset_id="retry-01", seq=3))
    monkeypatch.setattr(svc, "_run_accepted_cycle", real)
    assert svc.run_cycle("T", _telemetry(asset_id="retry-01", seq=3)).decision.seq == 3


def test_the_api_answers_409_for_stale_telemetry_and_503_for_a_broken_chain(isolated) -> None:
    svc = isolated
    body = _telemetry(asset_id="api-seq-01", seq=7).model_dump(mode="json")
    assert client.post("/api/jarvis/asset-twin/cycle", json=body).status_code == 200
    stale = client.post("/api/jarvis/asset-twin/cycle", json=body)
    assert stale.status_code == 409 and "not newer" in stale.text

    audit = svc.get_ledger()._path
    audit.write_text(audit.read_text().replace('"rpm":3000.0', '"rpm":1.0'))
    svc.get_ledger()._last.clear()
    broken = client.post("/api/jarvis/asset-twin/cycle", json=_telemetry(asset_id="api-seq-01", seq=8).model_dump(mode="json"))
    assert broken.status_code == 503 and "broken" in broken.text
    assert client.get("/api/jarvis/asset-twin/audit").json()["chain_valid"] is False


# -- second round: heads revalidated, sequences recovered, cycles serialised, intent written first --------------------

import time


def test_a_running_ledger_notices_the_file_changing_underneath_it(tmp_path) -> None:
    import json

    path = tmp_path / "audit.jsonl"
    ledger = EvidenceLedger(path)
    ledger.append("A", "measurement", {"n": 1})
    ledger.append("B", "measurement", {"n": 2})
    ledger.append("A", "cycle", {"n": 3})
    ledger.append("B", "cycle", {"n": 4})
    ledger.check("A")  # fine, even though B's records follow A's last one

    original = path.read_text()
    lines = original.splitlines()

    path.write_text("\n".join(lines[:-2]) + "\n")  # A's newest record (and B's) removed: a TRUNCATED chain still verifies
    with pytest.raises(EvidenceChainError, match="changed while the service was running"):
        ledger.check("A")
    with pytest.raises(EvidenceChainError):
        ledger.append("A", "execution", {"n": 5})

    # an edited tail record is caught the same way
    path.write_text(original)
    ledger2 = EvidenceLedger(path)
    ledger2.append("A", "veto", {"n": 6})
    lines = path.read_text().splitlines()
    rec = json.loads(lines[-1])
    rec["body"]["n"] = 7
    lines[-1] = json.dumps(rec, sort_keys=True, separators=(",", ":"))
    path.write_text("\n".join(lines) + "\n")
    with pytest.raises(EvidenceChainError):
        ledger2.check("A")


def test_the_tail_check_finds_a_tenants_record_past_other_tenants_noise(tmp_path) -> None:
    ledger = EvidenceLedger(tmp_path / "audit.jsonl")
    ledger.append("A", "measurement", {"n": 0})
    for i in range(400):  # far more than one read block of other tenants' records after A's last
        ledger.append("B", "measurement", {"n": i, "pad": "x" * 200})
    ledger.check("A")
    assert ledger.append("A", "cycle", {"n": 1})["prev"].startswith("sha256:")
    ledger.check("B")


def test_telemetry_sequence_survives_a_restart(isolated) -> None:
    svc = isolated
    svc.run_cycle("T", _telemetry(asset_id="boot-01", seq=5))
    # an ordinary restart: in-memory state gone, the audit file stays
    svc._last_seq.clear()
    svc._health.clear()
    svc._decisions.clear()
    svc.get_ledger()._last.clear()
    for replay in (5, 4, 1):
        with pytest.raises(svc.StaleTelemetryError, match="not newer than 5"):
            svc.run_cycle("T", _telemetry(asset_id="boot-01", seq=replay))
    assert svc.run_cycle("T", _telemetry(asset_id="boot-01", seq=6)).decision.seq == 6
    # another asset, another tenant: unaffected by boot-01's history
    svc.run_cycle("T", _telemetry(asset_id="boot-02", seq=1))
    svc.run_cycle("U", _telemetry(asset_id="boot-01", seq=1))


def test_cycles_for_one_asset_run_one_at_a_time_so_an_older_one_cannot_overwrite_a_newer(isolated, monkeypatch) -> None:
    svc = isolated
    real = svc.estimate_state
    entered, release = threading.Event(), threading.Event()
    started: list[int] = []

    def slow(telemetry, prior_health=1.0):
        started.append(telemetry.seq)
        if telemetry.seq == 1:
            entered.set()
            assert release.wait(10)
        return real(telemetry, prior_health=prior_health)

    monkeypatch.setattr(svc, "estimate_state", slow)
    calm = _telemetry(asset_id="ser-01", seq=1, vibration_mm_s=0.5)
    critical = _telemetry(asset_id="ser-01", seq=2, vibration_mm_s=14.0, exhaust_temp_c=640.0)
    errors: list[BaseException] = []

    def run(tel):
        try:
            svc.run_cycle("T", tel)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    first = threading.Thread(target=run, args=(calm,))
    second = threading.Thread(target=run, args=(critical,))
    first.start()
    assert entered.wait(10)
    second.start()
    time.sleep(0.4)
    assert started == [1], "sequence 2 must wait for sequence 1 on the same asset"
    release.set()
    first.join(10)
    second.join(10)
    assert not errors, errors
    assert started == [1, 2]
    h1 = real(calm, prior_health=1.0).estimated_health
    assert svc._health["T:ser-01"] == real(critical, prior_health=h1).estimated_health, "the newer reading's health is the one that stands"
    import json

    seqs = [json.loads(line)["body"]["seq"] for line in svc.get_ledger()._path.read_text().splitlines() if json.loads(line)["kind"] == "measurement"]
    assert seqs == [1, 2]


def test_other_assets_are_not_serialised_behind_a_slow_one(isolated, monkeypatch) -> None:
    svc = isolated
    real = svc.estimate_state
    entered, release = threading.Event(), threading.Event()

    def slow(telemetry, prior_health=1.0):
        if telemetry.asset_id == "slow-01":
            entered.set()
            assert release.wait(10)
        return real(telemetry, prior_health=prior_health)

    monkeypatch.setattr(svc, "estimate_state", slow)
    worker = threading.Thread(target=lambda: svc.run_cycle("T", _telemetry(asset_id="slow-01", seq=1)))
    worker.start()
    assert entered.wait(10)
    assert svc.run_cycle("T", _telemetry(asset_id="fast-01", seq=1)).decision.asset_id == "fast-01"
    release.set()
    worker.join(10)


def _flaky(ledger, monkeypatch, failing_kind: str) -> None:
    real = ledger.append

    def append(tenant, kind, body):
        if kind == failing_kind:
            raise OSError("no space left on device")
        return real(tenant, kind, body)

    monkeypatch.setattr(ledger, "append", append)


def _approved_in_service(svc, asset: str = "wal-01"):
    cycle = svc.run_cycle("T", _telemetry(asset_id=asset, seq=1))
    did = cycle.decision.decision_id
    _approve(svc, "T", did)
    return did


def test_the_intent_is_on_file_before_the_asset_moves(isolated, monkeypatch) -> None:
    svc = isolated
    did = _approved_in_service(svc)
    order: list[str] = []
    real_apply = SimulatedAsset.apply_control
    ledger = svc.get_ledger()
    real_append = ledger.append
    monkeypatch.setattr(ledger, "append", lambda t, k, b: (order.append(k), real_append(t, k, b))[1])
    monkeypatch.setattr(SimulatedAsset, "apply_control", lambda self, sp: (order.append("MOVE"), real_apply(self, sp))[1])
    assert svc.execute("T", did, "wal-01").executed is True
    assert order == ["execution_intent", "MOVE", "execution"]
    import json

    intent = [json.loads(line) for line in ledger._path.read_text().splitlines() if '"execution_intent"' in line][0]["body"]
    assert intent["decision_id"] == did and intent["asset"] == "wal-01" and "setpoint" in intent and "action" in intent


def test_if_the_intent_cannot_be_written_nothing_moves(isolated, monkeypatch) -> None:
    svc = isolated
    did = _approved_in_service(svc)
    moved: list[float] = []
    real_apply = SimulatedAsset.apply_control
    monkeypatch.setattr(SimulatedAsset, "apply_control", lambda self, sp: (moved.append(sp), real_apply(self, sp))[1])
    _flaky(svc.get_ledger(), monkeypatch, "execution_intent")
    with pytest.raises(OSError):
        svc.execute("T", did, "wal-01")
    assert moved == []
    assert svc._gates["T"].get(did).status == "approved", "the approval was given back, not consumed"


def test_if_only_the_outcome_cannot_be_written_the_execution_is_reported_truthfully(isolated, monkeypatch) -> None:
    svc = isolated
    did = _approved_in_service(svc)
    _flaky(svc.get_ledger(), monkeypatch, "execution")
    result = svc.execute("T", did, "wal-01")
    assert result.executed is True, "the control was applied; do not report a failure that did not happen"
    assert "WARNING" in result.reason and "execution_intent" in result.reason
    assert svc._gates["T"].get(did).status == "executed"
    assert any('"execution_intent"' in line for line in svc.get_ledger()._path.read_text().splitlines())
