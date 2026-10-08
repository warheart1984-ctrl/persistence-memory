"""Decision Support: anomaly -> prediction -> ranked recommendation + risk.

Bounds first: setpoints are clamped to the envelope, risk/confidence to
[0,1], and every recommendation names its evidence. Uncertain or anomalous
input fails toward hold/inspect, never toward more load.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone

from .models import DecisionPacket, RecommendedAction, Telemetry, TwinState
from .twin_core import ENVELOPE, canonical, run_scenarios

POLICY = "sim-policy.v1"
DECISION_TTL_S = 300


def _utc(seconds_from_now: int = 0) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds_from_now)).isoformat(timespec="milliseconds")


def build_decision(telemetry: Telemetry, state: TwinState) -> DecisionPacket:
    vib = telemetry.vibration_mm_s
    temp = telemetry.exhaust_temp_c
    anomaly_score = 0.0
    if vib > ENVELOPE["vibration_warn"]:
        anomaly_score = max(anomaly_score, min(1.0, (vib - ENVELOPE["vibration_warn"]) / 5.0))
    if temp > ENVELOPE["temp_warn"]:
        anomaly_score = max(anomaly_score, min(1.0, (temp - ENVELOPE["temp_warn"]) / 120.0))
    if telemetry.rpm > ENVELOPE["rpm_max"]:
        anomaly_score = 1.0
    anomaly = anomaly_score >= 0.5

    scenarios = run_scenarios(state)
    base_risk = min(1.0, (1.0 - state.estimated_health) * 0.7 + anomaly_score * 0.5 + state.uncertainty * 0.3)
    predicted_risk = round(base_risk, 4)
    confidence = round(max(0.0, min(1.0, 1.0 - state.uncertainty * 0.8 - anomaly_score * 0.2)), 4)

    if anomaly_score >= 0.8 or temp > ENVELOPE["temp_crit"] or vib > ENVELOPE["vibration_crit"]:
        rec = RecommendedAction(
            action="safe_shutdown",
            setpoint_rpm=800.0,
            rationale=f"critical threshold crossed (vib={vib}, temp={temp}); fail safe to idle",
            evidence_digests=[state.telemetry_digest],
        )
    elif anomaly or predicted_risk > 0.55 or confidence < 0.5:
        rec = RecommendedAction(
            action="reduce_load",
            setpoint_rpm=round(max(800.0, min(ENVELOPE["rpm_max"], telemetry.operator_setpoint_rpm * 0.8)), 2),
            rationale=f"anomaly_score={round(anomaly_score,3)} risk={predicted_risk} confidence={confidence}; reduce load and inspect",
            evidence_digests=[state.telemetry_digest],
        )
    elif state.estimated_health < 0.55:
        rec = RecommendedAction(
            action="inspect",
            setpoint_rpm=round(max(800.0, min(ENVELOPE["rpm_max"], telemetry.operator_setpoint_rpm)), 2),
            rationale=f"health={state.estimated_health} below 0.55; hold and schedule inspection",
            evidence_digests=[state.telemetry_digest],
        )
    else:
        rec = RecommendedAction(
            action="hold_setpoint",
            setpoint_rpm=round(max(800.0, min(ENVELOPE["rpm_max"], telemetry.operator_setpoint_rpm)), 2),
            rationale="within envelope; no anomaly; hold",
            evidence_digests=[state.telemetry_digest],
        )

    body = {
        "asset": telemetry.asset_id, "seq": telemetry.seq,
        "anomaly": anomaly, "risk": predicted_risk,
        "action": rec.action, "setpoint": rec.setpoint_rpm,
        "twin": state.model_dump(mode="json"),
    }
    decision_id = hashlib.sha256(canonical(body)).hexdigest()
    ev = hashlib.sha256(canonical({**body, "policy": POLICY})).hexdigest()
    return DecisionPacket(
        decision_id=decision_id,
        asset_id=telemetry.asset_id,
        seq=telemetry.seq,
        anomaly=anomaly,
        anomaly_score=round(anomaly_score, 4),
        predicted_risk=predicted_risk,
        confidence=confidence,
        recommendation=rec,
        scenarios=scenarios,
        twin_state=state,
        policy=POLICY,
        evidence_digest=f"sha256:{ev}",
        expires_at=_utc(DECISION_TTL_S),
    )
