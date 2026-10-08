"""Human Veto: the decision gate. No automatic action without approval.

States: pending -> approved | vetoed | held -> (approved -> executed).
Expired decisions can never execute. A veto always wins over a prior
approval for the same decision id. Hold keeps the asset at its current
simulated setpoint and routes to safe-state review.
"""

from __future__ import annotations

from datetime import datetime, timezone

from .models import VetoDecision, VetoRecord


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class VetoGate:
    def __init__(self) -> None:
        self._records: dict[str, VetoRecord] = {}

    def propose(self, decision_id: str) -> VetoRecord:
        rec = VetoRecord(decision_id=decision_id, status="pending")
        self._records[decision_id] = rec
        return rec

    def get(self, decision_id: str) -> VetoRecord | None:
        return self._records.get(decision_id)

    def decide(self, verdict: VetoDecision) -> VetoRecord:
        current = self._records.get(verdict.decision_id)
        if current is None:
            raise KeyError("unknown decision")
        if current.status == "executed":
            raise ValueError("already executed; veto is too late")
        if current.status == "vetoed" and verdict.verdict == "approve":
            raise ValueError("vetoed decisions cannot be re-approved; create a new cycle")
        mapping = {"approve": "approved", "veto": "vetoed", "hold": "held"}
        self._records[verdict.decision_id] = VetoRecord(
            decision_id=verdict.decision_id,
            status=mapping[verdict.verdict],  # type: ignore[arg-type]
            veto=verdict,
        )
        return self._records[verdict.decision_id]

    def mark_executed(self, decision_id: str) -> None:
        rec = self._records.get(decision_id)
        if rec is not None and rec.status == "approved":
            self._records[decision_id] = VetoRecord(decision_id=decision_id, status="executed", veto=rec.veto)

    def mark_safe_state(self, decision_id: str) -> None:
        rec = self._records.get(decision_id)
        self._records[decision_id] = VetoRecord(
            decision_id=decision_id, status="safe_state", veto=rec.veto if rec else None
        )
