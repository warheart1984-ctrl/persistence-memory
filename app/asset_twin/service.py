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
_last_seq: dict[str, int] = {}  # "tenant:asset" -> newest accepted telemetry sequence
_lock = threading.Lock()


class StaleTelemetryError(ValueError):
    """Telemetry whose sequence is not newer than the last accepted one for that asset."""


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
    key = f"{tenant}:{telemetry.asset_id}"
    # A late or replayed sample must not produce a fresh decision: a delayed "all clear" would otherwise supersede a later
    # critical reading. The sequence is reserved before anything is updated, and given back if the cycle fails.
    with _lock:
        previous_seq = _last_seq.get(key)
        if previous_seq is not None and telemetry.seq <= previous_seq:
            raise StaleTelemetryError(
                f"stale telemetry for {telemetry.asset_id!r}: seq {telemetry.seq} is not newer than {previous_seq}"
            )
        _last_seq[key] = telemetry.seq
    try:
        return _run_accepted_cycle(tenant, telemetry)
    except Exception:
        with _lock:
            if _last_seq.get(key) == telemetry.seq:
                if previous_seq is None:
                    _last_seq.pop(key, None)
                else:
                    _last_seq[key] = previous_seq
        raise


def _run_accepted_cycle(tenant: str, telemetry: Telemetry) -> CycleResponse:
    get_ledger().check(tenant)
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
    if verdict.verdict == "approve":
        get_ledger().check(tenant)  # an approval nobody can audit must not be granted
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
    from datetime import datetime, timezone

    def _at() -> str:
        return datetime.now(timezone.utc).isoformat(timespec="milliseconds")

    with _lock:
        decision = _decisions.get((tenant, decision_id))
    if decision is None:
        return ExecutionResult(
            decision_id=decision_id, executed=False,
            reason="unknown decision; run a cycle first", at=_at(),
        )
    ledger = get_ledger()
    ledger.check(tenant)  # before the asset can move: no audit trail, no action
    # An approval is for ONE asset. The target is the decision's own; a caller naming a different one is refused, and the
    # attempt is recorded so the ledger shows what was asked for as well as what was decided.
    if asset_id != decision.asset_id:
        result = ExecutionResult(
            decision_id=decision_id, executed=False, at=_at(),
            reason=f"refused: decision is for asset {decision.asset_id!r}, not {asset_id!r}",
        )
        ledger.append(tenant, "execution", {
            "decision_id": decision_id, "asset": decision.asset_id, "requested_asset": asset_id,
            "executed": False, "setpoint": None, "safe_state": False, "reason": result.reason,
        })
        return result
    asset = _asset_for(tenant, decision.asset_id)
    result = execute_approved(decision, _gate_for(tenant), asset)
    ledger.append(tenant, "execution", {
        "decision_id": decision_id, "asset": decision.asset_id, "requested_asset": asset_id,
        "executed": result.executed,
        "setpoint": result.applied_setpoint_rpm,
        "safe_state": result.safe_state_entered, "reason": result.reason,
    })
    return result
