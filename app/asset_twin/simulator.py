"""Simulated physical asset. No real I/O by construction.

A deterministic turbine-like model: rpm tracks the operator setpoint with
lag, vibration rises with rpm + wear, exhaust temp rises with load, health
decays slowly and faster under high vibration / high temp. All outputs are
clamped to the Telemetry bounds so the simulator can never emit an
out-of-schema reading.
"""

from __future__ import annotations

from datetime import datetime, timezone

from .models import MaintenanceEvent, Telemetry

MODEL_VERSION = "sim-asset.v1"
CONFIG_VERSION = "asset-config.v1"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class SimulatedAsset:
    """In-process stand-in for the left column of the diagram."""

    def __init__(self, asset_id: str = "turbine-demo-01") -> None:
        self.asset_id = asset_id
        self.seq = 0
        self.rpm = 0.0
        self.health = 1.0
        self.setpoint_rpm = 3000.0
        self.maintenance_log: list[MaintenanceEvent] = []

    def apply_operator_setpoint(self, rpm: float) -> None:
        self.setpoint_rpm = max(0.0, min(20000.0, float(rpm)))

    def apply_maintenance(self, event: MaintenanceEvent) -> None:
        self.maintenance_log.append(event)
        if event.kind == "repair":
            self.health = min(1.0, self.health + 0.05)
        elif event.kind == "part_replacement":
            self.health = min(1.0, self.health + 0.20)

    def apply_control(self, rpm: float) -> None:
        """Only the simulated Execution block calls this. Simulated only."""
        self.apply_operator_setpoint(rpm)

    def enter_safe_state(self) -> None:
        self.setpoint_rpm = 800.0  # idle, simulated

    def step(
        self,
        *,
        ambient_temp_c: float = 20.0,
        ambient_pressure_kpa: float = 101.3,
    ) -> Telemetry:
        # First-order lag toward setpoint.
        self.rpm += 0.25 * (self.setpoint_rpm - self.rpm)
        load = self.rpm / 12000.0
        wear_rate = 0.0002 + 0.0015 * max(0.0, load - 0.55)
        vibration = 1.2 + 4.5 * load + (1.0 - self.health) * 9.0
        temp = 320.0 + 320.0 * load + (ambient_temp_c - 20.0) * 0.6
        pressure = 320.0 + 180.0 * load
        self.health = max(0.0, self.health - wear_rate)
        self.seq += 1
        return Telemetry(
            asset_id=self.asset_id,
            seq=self.seq,
            at=_utc_now_iso(),
            rpm=round(max(0.0, min(20000.0, self.rpm)), 2),
            vibration_mm_s=round(max(0.0, min(100.0, vibration)), 2),
            exhaust_temp_c=round(max(-50.0, min(1200.0, temp)), 2),
            oil_pressure_kpa=round(max(0.0, min(2000.0, pressure)), 2),
            ambient_temp_c=ambient_temp_c,
            ambient_pressure_kpa=ambient_pressure_kpa,
            operator_setpoint_rpm=round(self.setpoint_rpm, 2),
            maintenance_flag=bool(self.maintenance_log and self.seq - 0 < 3),
        )
