"""Execution: simulated controller only. Refuses everything unsafe.

Rules enforced here, not just documented:
- only `approved` decisions execute; pending/vetoed/held/expired never do
- the approval is CLAIMED atomically before the asset moves (a veto cannot race it, two executes cannot both win)
- a decision only ever acts on the asset it names
- expired (past expires_at) decisions are refused even if approved
- setpoints are clamped to the envelope before touching the simulator
- safe_shutdown recommendations enter safe state (idle 800 rpm simulated)
- every refusal names its reason; nothing fails silently to full load
"""

from __future__ import annotations

from datetime import datetime, timezone

from .decision import DECISION_TTL_S  # noqa: F401 (documents the TTL owner)
from .models import DecisionPacket, ExecutionResult
from .simulator import SimulatedAsset
from .twin_core import ENVELOPE
from .veto import VetoGate


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _expired(decision: DecisionPacket) -> bool:
    try:
        exp = datetime.fromisoformat(decision.expires_at)
    except ValueError:
        return True
    now = datetime.now(timezone.utc)
    if exp.tzinfo is None:
        exp = exp.replace(tzinfo=timezone.utc)
    return now > exp


def execute_approved(
    decision: DecisionPacket,
    gate: VetoGate,
    asset: SimulatedAsset,
) -> ExecutionResult:
    rec = gate.get(decision.decision_id)
    if rec is None:
        return ExecutionResult(decision_id=decision.decision_id, executed=False, reason="unknown decision; no veto record", at=_now())
    if asset.asset_id != decision.asset_id:
        return ExecutionResult(
            decision_id=decision.decision_id, executed=False, at=_now(),
            reason=f"refused: decision is for asset {decision.asset_id!r}, not {asset.asset_id!r}",
        )
    if rec.status != "approved":
        return ExecutionResult(decision_id=decision.decision_id, executed=False, reason=f"refused: veto status is {rec.status}, need approved", at=_now())
    if _expired(decision):
        return ExecutionResult(decision_id=decision.decision_id, executed=False, reason="refused: decision expired; re-run cycle", at=_now())

    # The authoritative check: only one caller can turn `approved` into `executing`, and a veto that lands first wins.
    if gate.claim_for_execution(decision.decision_id) is None:
        now = gate.get(decision.decision_id)
        return ExecutionResult(
            decision_id=decision.decision_id, executed=False, at=_now(),
            reason=f"refused: veto status is {now.status if now else 'unknown'}, need approved",
        )
    try:
        if decision.recommendation.action == "safe_shutdown":
            asset.enter_safe_state()
            gate.mark_safe_state(decision.decision_id)
            return ExecutionResult(
                decision_id=decision.decision_id, executed=True,
                applied_setpoint_rpm=800.0, safe_state_entered=True,
                reason="safe_shutdown approved: entered simulated safe idle", at=_now(),
            )
        sp = max(800.0, min(ENVELOPE["rpm_max"], decision.recommendation.setpoint_rpm))
        asset.apply_control(sp)
        gate.mark_executed(decision.decision_id)
        return ExecutionResult(
            decision_id=decision.decision_id, executed=True,
            applied_setpoint_rpm=sp, safe_state_entered=False,
            reason=f"approved {decision.recommendation.action} applied to simulator only", at=_now(),
        )
    except Exception:
        gate.release_claim(decision.decision_id)
        raise
