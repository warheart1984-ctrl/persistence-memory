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


def test_cycle_never_executes_and_veto_wins(tmp_path) -> None:
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


def test_tenant_isolation_for_packets_and_audit(tmp_path) -> None:
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


def test_measurement_and_assumption_are_separate_kinds(tmp_path) -> None:
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
