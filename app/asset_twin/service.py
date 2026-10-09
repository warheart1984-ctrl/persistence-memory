"""Orchestrator: telemetry -> twin -> decision -> veto-pending -> evidence.

`run_cycle` never executes. Execution happens only via `approve_and_execute`
after an explicit human approve, or `execute` refusing anything unapproved.
This is the load-bearing rule made structural: the cycle endpoint cannot
move the asset, simulated or otherwise.
"""

from __future__ import annotations

import os
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
_cycle_locks: dict[str, threading.Lock] = {}  # "tenant:asset" -> serialises that asset's cycles
_latest: dict[str, str] = {}  # "tenant:asset" -> decision id of the newest published cycle
_lock = threading.Lock()


class StaleTelemetryError(ValueError):
    """Telemetry whose sequence is not newer than the last accepted one for that asset."""


class TooManyAssetsError(ValueError):
    """A tenant tried to track more assets than the service is configured to hold in memory."""


_tenant_assets: dict[str, set[str]] = {}   # tenant -> asset ids it has submitted
_history: dict[str, list[str]] = {}        # "tenant:asset" -> decision ids, oldest first


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, "") or default)
    except ValueError:
        return default


def _max_assets() -> int:
    return max(1, _int_env("JARVIS_ASSET_TWIN_MAX_ASSETS", 1000))


def _retain() -> int:
    """Decision packets kept per asset, newest first, once they can no longer act. Older terminal ones are dropped."""
    return max(1, _int_env("JARVIS_ASSET_TWIN_RETAIN", 50))


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


def _cycle_lock(key: str) -> threading.Lock:
    with _lock:
        lock = _cycle_locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _cycle_locks[key] = lock
        return lock


def run_cycle(tenant: str, telemetry: Telemetry) -> CycleResponse:
    """One cycle for one asset at a time: the whole read-modify-write (sequence check, health, decision, evidence) runs under
    that asset's lock, so sequence N+1 cannot finish first and then be overwritten by an older N still in flight."""
    key = f"{tenant}:{telemetry.asset_id}"
    ledger = get_ledger()
    with _lock:  # bound what a caller can make the service hold: unique asset ids would otherwise grow memory without limit
        known = _tenant_assets.setdefault(tenant, set())
        if telemetry.asset_id not in known:
            if len(known) >= _max_assets():
                raise TooManyAssetsError(f"tenant {tenant!r} already tracks {len(known)} assets (JARVIS_ASSET_TWIN_MAX_ASSETS)")
            known.add(telemetry.asset_id)
    with _cycle_lock(key):
        ledger.check(tenant)  # also verifies the chain the recovered sequence below is read from
        # A late or replayed sample must not produce a fresh decision: a delayed "all clear" would otherwise supersede a later
        # critical reading. After a restart the in-memory sequence is empty, so it is recovered from the verified evidence.
        with _lock:
            previous_seq = _last_seq.get(key)
        if previous_seq is None:
            previous_seq = ledger.last_measurement_seq(tenant, telemetry.asset_id)
        if previous_seq is not None and telemetry.seq <= previous_seq:
            with _lock:
                _last_seq.setdefault(key, previous_seq)
            raise StaleTelemetryError(
                f"stale telemetry for {telemetry.asset_id!r}: seq {telemetry.seq} is not newer than {previous_seq}"
            )
        response = _run_accepted_cycle(tenant, telemetry)
        with _lock:
            _last_seq[key] = telemetry.seq  # only a cycle that completed consumes its sequence
        return response


def _prune(tenant: str, key: str, gate: VetoGate, surplus: list[str]) -> None:
    """Forget old packets that can never act again; one that is still live (e.g. mid-execution) stays on the list for later."""
    keep: list[str] = []
    for decision_id in surplus:
        if gate.forget(decision_id):
            with _lock:
                _decisions.pop((tenant, decision_id), None)
        else:
            keep.append(decision_id)
    if keep:
        with _lock:
            _history.setdefault(key, [])[:0] = keep


def _run_accepted_cycle(tenant: str, telemetry: Telemetry) -> CycleResponse:
    ledger = get_ledger()
    ledger.check(tenant)
    key = f"{tenant}:{telemetry.asset_id}"
    _asset_for(tenant, telemetry.asset_id)
    with _lock:
        prior = _health.get(key)
    if prior is None:  # after a restart the last verified estimate is on file; do not restart from "perfectly healthy"
        prior = ledger.last_health(tenant, telemetry.asset_id)
    if prior is None:
        prior = 1.0
    state = estimate_state(telemetry, prior_health=prior)
    decision = build_decision(telemetry, state)
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
    # Publish only now that all of the cycle's evidence is on file: a failed append leaves no approvable decision and no
    # health update behind, so a retry starts from the same state. The previous recommendation for this asset is expired
    # FIRST, then the new one becomes visible: it was made on older telemetry and must not be approvable or executable once a
    # newer cycle exists (execution takes this asset's lock too, so the two cannot interleave).
    gate = _gate_for(tenant)
    with _lock:
        older = _latest.get(key)
    if older is not None and older != decision.decision_id:
        gate.supersede(older)
    veto = gate.propose(decision.decision_id)
    with _lock:
        _health[key] = state.estimated_health
        _decisions[(tenant, decision.decision_id)] = decision
        _latest[key] = decision.decision_id
        history = _history.setdefault(key, [])
        history.append(decision.decision_id)
        surplus = history[:-_retain()]
        del history[:-_retain()]
    _prune(tenant, key, gate, surplus)
    degraded = decision.confidence < 0.5 or decision.predicted_risk > 0.7
    return CycleResponse(decision=decision, veto=veto, degraded=degraded)


def decide_human(tenant: str, verdict: VetoDecision, actor: str | None = None) -> dict:
    with _lock:
        owned = (tenant, verdict.decision_id) in _decisions
    if not owned:
        raise ValueError("unknown decision")
    gate = _gate_for(tenant)
    ledger = get_ledger()
    record = {
        "decision_id": verdict.decision_id, "verdict": verdict.verdict,
        "reviewer": verdict.reviewer, "reason": verdict.reason, "at": verdict.at,
        "actor": actor or tenant,
    }
    try:
        if verdict.verdict == "approve":
            # An approval makes something executable, so it is written to the audit trail BEFORE it takes effect (the gate runs
            # the write after validating the transition and before applying it): if the write fails, nothing was approved.
            ledger.check(tenant)
            rec = gate.decide(verdict, before_commit=lambda: ledger.append(tenant, "veto", record))
        else:
            # A veto or hold only ever removes the ability to act, so it takes effect first and is recorded straight after.
            rec = gate.decide(verdict)
            ledger.append(tenant, "veto", record)
    except KeyError as exc:
        raise ValueError("unknown decision") from exc
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

    def record_intent() -> None:
        # Written AFTER the approval is claimed and BEFORE the asset moves. If this cannot be written, nothing moves and the
        # claim is released; if the outcome record below cannot be written, the intent is still on file.
        ledger.append(tenant, "execution_intent", {
            "decision_id": decision_id, "asset": decision.asset_id, "requested_asset": asset_id,
            "action": decision.recommendation.action, "setpoint": decision.recommendation.setpoint_rpm,
        })

    with _cycle_lock(f"{tenant}:{decision.asset_id}"):
        result = execute_approved(decision, _gate_for(tenant), asset, before_apply=record_intent)
    outcome = {
        "decision_id": decision_id, "asset": decision.asset_id, "requested_asset": asset_id,
        "executed": result.executed,
        "setpoint": result.applied_setpoint_rpm,
        "safe_state": result.safe_state_entered, "reason": result.reason,
    }
    try:
        ledger.append(tenant, "execution", outcome)
    except Exception as exc:
        if not result.executed:
            raise
        # The control WAS applied and its intent is on file; say so rather than reporting a failure that did not happen.
        return result.model_copy(update={
            "reason": f"{result.reason} [WARNING: the outcome record could not be written ({type(exc).__name__}); "
                      "the execution_intent record is on file]"[:1000],
        })
    return result
