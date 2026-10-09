"""Human Veto: the decision gate. No automatic action without approval.

States: pending -> approved | vetoed | held -> (approved -> executing -> executed).
Expired decisions can never execute. A veto always wins over a prior
approval for the same decision id. Hold keeps the asset at its current
simulated setpoint and routes to safe-state review.

Every transition happens under one lock, and execution CLAIMS an approval
(approved -> executing) before it touches the asset. So a veto and an
execute that overlap are strictly ordered: if the veto got the lock first the
claim fails and nothing moves; if the claim got it first the veto is refused
("too late"). Two overlapping executes cannot both claim the same approval.
"""

from __future__ import annotations

import threading
from datetime import datetime, timezone

from .models import VetoDecision, VetoRecord


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class VetoGate:
    def __init__(self) -> None:
        self._records: dict[str, VetoRecord] = {}
        self._lock = threading.RLock()

    def propose(self, decision_id: str) -> VetoRecord:
        with self._lock:
            rec = VetoRecord(decision_id=decision_id, status="pending")
            self._records[decision_id] = rec
            return rec

    def get(self, decision_id: str) -> VetoRecord | None:
        with self._lock:
            return self._records.get(decision_id)

    def decide(self, verdict: VetoDecision) -> VetoRecord:
        with self._lock:
            current = self._records.get(verdict.decision_id)
            if current is None:
                raise KeyError("unknown decision")
            if current.status in ("executing", "executed", "safe_state"):
                raise ValueError("already executing or executed; the veto is too late")
            if current.status == "vetoed" and verdict.verdict == "approve":
                raise ValueError("vetoed decisions cannot be re-approved; create a new cycle")
            mapping = {"approve": "approved", "veto": "vetoed", "hold": "held"}
            self._records[verdict.decision_id] = VetoRecord(
                decision_id=verdict.decision_id,
                status=mapping[verdict.verdict],  # type: ignore[arg-type]
                veto=verdict,
            )
            return self._records[verdict.decision_id]

    def claim_for_execution(self, decision_id: str) -> VetoRecord | None:
        """Atomically turn an approval into `executing`; None if it is not (or no longer) approved."""
        with self._lock:
            rec = self._records.get(decision_id)
            if rec is None or rec.status != "approved":
                return None
            claimed = VetoRecord(decision_id=decision_id, status="executing", veto=rec.veto)
            self._records[decision_id] = claimed
            return claimed

    def release_claim(self, decision_id: str) -> None:
        """The control was NOT applied (it raised): give the approval back."""
        with self._lock:
            rec = self._records.get(decision_id)
            if rec is not None and rec.status == "executing":
                self._records[decision_id] = VetoRecord(decision_id=decision_id, status="approved", veto=rec.veto)

    def mark_executed(self, decision_id: str) -> None:
        with self._lock:
            rec = self._records.get(decision_id)
            if rec is not None and rec.status == "executing":
                self._records[decision_id] = VetoRecord(decision_id=decision_id, status="executed", veto=rec.veto)

    def mark_safe_state(self, decision_id: str) -> None:
        with self._lock:
            rec = self._records.get(decision_id)
            if rec is not None and rec.status == "executing":
                self._records[decision_id] = VetoRecord(decision_id=decision_id, status="safe_state", veto=rec.veto)
