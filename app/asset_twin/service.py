"""Orchestrator: telemetry -> twin -> decision -> veto-pending -> evidence.

`run_cycle` never executes. Execution happens only via `approve_and_execute`
after an explicit human approve, or `execute` refusing anything unapproved.
This is the load-bearing rule made structural: the cycle endpoint cannot
move the asset, simulated or otherwise.
"""

from __future__ import annotations

import threading

from .decision import build_decision
from .evidence import get_ledger
from .execution import execute_approved
from .models import CycleResponse, DecisionPacket, ExecutionResult, Telemetry, VetoDecision
from .simulator import SimulatedAsset
from .twin_core import estimate_state
from .veto import VetoGate

_assets: dict[str, SimulatedAsset] = {}
_gates: dict[str, VetoGate] = {}
_decisions: dict[tuple[str, str], DecisionPacket] = {}
_health: dict[str, float] = {}
_lock = threading.Lock()


def _asset_for(tenant: str, asset_id: str) -> SimulatedAsset:
    key = f"{tenant}:{asset_id}"
    with _lock:
        asset = _assets.get(key)
        if asset is None:
            asset = SimulatedAsset(asset_id=asset_id)
            _assets[key] = asset
        return asset


def _gate_for(tenant: str) -> VetoGate:
    with _lock:
        gate = _gates.get(tenant)
        if gate is None:
            gate = VetoGate()
            _gates[tenant] = gate
        return gate


def run_cycle(tenant: str, telemetry: Telemetry) -> CycleResponse:
    _asset_for(tenant, telemetry.asset_id)
    prior = _health.get(f"{tenant}:{telemetry.asset_id}", 1.0)
    state = estimate_state(telemetry, prior_health=prior)
    _health[f"{tenant}:{telemetry.asset_id}"] = state.estimated_health
    decision = build_decision(telemetry, state)
    gate = _gate_for(tenant)
    veto = gate.propose(decision.decision_id)
    with _lock:
        _decisions[(tenant, decision.decision_id)] = decision
    ledger = get_ledger()
    measurement = ledger.append(tenant, "measurement", {
        "decision_id": decision.decision_id, "asset": telemetry.asset_id,
        "seq": telemetry.seq, "at": telemetry.at,
        "rpm": telemetry.rpm, "vibration_mm_s": telemetry.vibration_mm_s,
        "exhaust_temp_c": telemetry.exhaust_temp_c,
        "oil_pressure_kpa": telemetry.oil_pressure_kpa,
        "telemetry_digest": state.telemetry_digest,
    })
    assumption = ledger.append(tenant, "assumption", {
        "decision_id": decision.decision_id,
        "model_version": state.model_version,
        "config_version": state.config_version,
        "assumptions": state.assumptions,
        "uncertainty": state.uncertainty,
        "estimated_health": state.estimated_health,
    })
    ledger.append(tenant, "cycle", {
        "decision_id": decision.decision_id, "asset": decision.asset_id,
        "seq": decision.seq, "anomaly": decision.anomaly,
        "risk": decision.predicted_risk, "action": decision.recommendation.action,
        "evidence": decision.evidence_digest, "policy": decision.policy,
        "measurement_ref": measurement["digest"],
        "assumption_ref": assumption["digest"],
    })
    degraded = decision.confidence < 0.5 or decision.predicted_risk > 0.7
    return CycleResponse(decision=decision, veto=veto, degraded=degraded)


def decide_human(tenant: str, verdict: VetoDecision, actor: str | None = None) -> dict:
    with _lock:
        owned = (tenant, verdict.decision_id) in _decisions
    if not owned:
        raise ValueError("unknown decision")
    gate = _gate_for(tenant)
    try:
        rec = gate.decide(verdict)
    except KeyError as exc:
        raise ValueError("unknown decision") from exc
    get_ledger().append(tenant, "veto", {
        "decision_id": verdict.decision_id, "verdict": verdict.verdict,
        "reviewer": verdict.reviewer, "reason": verdict.reason, "at": verdict.at,
        "actor": actor or tenant,
    })
    return {"veto": rec.model_dump(mode="json")}


def execute(tenant: str, decision_id: str, asset_id: str) -> ExecutionResult:
    with _lock:
        decision = _decisions.get((tenant, decision_id))
    if decision is None:
        from datetime import datetime, timezone

        return ExecutionResult(
            decision_id=decision_id, executed=False,
            reason="unknown decision; run a cycle first",
            at=datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
        )
    asset = _asset_for(tenant, asset_id)
    result = execute_approved(decision, _gate_for(tenant), asset)
    get_ledger().append(tenant, "execution", {
        "decision_id": decision_id, "executed": result.executed,
        "setpoint": result.applied_setpoint_rpm,
        "safe_state": result.safe_state_entered, "reason": result.reason,
    })
    return result
