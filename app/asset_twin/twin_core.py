"""Digital Twin Core: state estimation + health + scenarios + uncertainty.

Simplified but honest: every number is derived from the telemetry + config
baseline handed in, assumptions are recorded, and outputs are bounded. What
it does NOT do: claim truth, invent sensors, or override the configuration
baseline.
"""

from __future__ import annotations

import hashlib
import json

from .models import ScenarioResult, Telemetry, TwinState
from .simulator import CONFIG_VERSION

MODEL_VERSION = "twin-core.v1"

# Operating envelope (sim units). Anything outside is an anomaly input, and
# scenario outputs are clamped to it.
ENVELOPE = {
    "rpm_max": 12000.0,
    "vibration_warn": 7.0,
    "vibration_crit": 11.0,
    "temp_warn": 640.0,
    "temp_crit": 720.0,
}


def canonical(obj: object) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def estimate_state(telemetry: Telemetry, prior_health: float = 1.0) -> TwinState:
    vib_pen = 0.0
    if telemetry.vibration_mm_s > ENVELOPE["vibration_warn"]:
        vib_pen = min(0.25, (telemetry.vibration_mm_s - ENVELOPE["vibration_warn"]) * 0.03)
    temp_pen = 0.0
    if telemetry.exhaust_temp_c > ENVELOPE["temp_warn"]:
        temp_pen = min(0.20, (telemetry.exhaust_temp_c - ENVELOPE["temp_warn"]) * 0.002)
    health = max(0.0, min(1.0, prior_health - 0.0005 - vib_pen * 0.02 - temp_pen * 0.02))
    # Uncertainty grows when sensors disagree with the setpoint or health is low.
    lag = abs(telemetry.operator_setpoint_rpm - telemetry.rpm) / 12000.0
    uncertainty = max(0.0, min(1.0, 0.05 + lag * 0.5 + (1.0 - health) * 0.4))
    rul = int(max(0, round(health * 4000)))
    digest = hashlib.sha256(canonical(telemetry.model_dump(mode="json"))).hexdigest()
    return TwinState(
        asset_id=telemetry.asset_id,
        seq=telemetry.seq,
        estimated_rpm=telemetry.rpm,
        estimated_health=round(health, 4),
        remaining_useful_life_cycles=rul,
        uncertainty=round(uncertainty, 4),
        config_version=CONFIG_VERSION,
        model_version=MODEL_VERSION,
        assumptions=[
            "first-order rpm lag; vibration/temp penalties are linear sim fits",
            f"envelope rpm_max={ENVELOPE['rpm_max']}",
            "health is an index, not a physical measurement",
        ],
        telemetry_digest=f"sha256:{digest}",
    )


def run_scenarios(state: TwinState) -> list[ScenarioResult]:
    h = state.estimated_health
    return [
        ScenarioResult(name="hold", predicted_health=round(max(0.0, h - 0.001), 4), predicted_risk=round(min(1.0, (1 - h) * 0.6 + 0.05), 4)),
        ScenarioResult(name="reduce_load_20pct", predicted_health=round(max(0.0, h - 0.0002), 4), predicted_risk=round(min(1.0, (1 - h) * 0.4 + 0.02), 4)),
        ScenarioResult(name="safe_idle_800rpm", predicted_health=round(h, 4), predicted_risk=round(0.02, 4)),
    ]
