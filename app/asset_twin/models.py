"""Wire contracts for the simulation-only load-bearing twin.

Naming follows the diagram: Observed State/Telemetry -> Predicted State /
Evidence -> Recommended Action + Risk -> Approve/Veto -> Approved Control /
Safe State, with every step carrying evidence digests.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


# -- physical (simulated) inputs -------------------------------------------

class Telemetry(BaseModel):
    asset_id: str = Field(min_length=1, max_length=128)
    seq: int = Field(ge=0)
    at: str = Field(min_length=1, max_length=64)  # ISO-8601
    rpm: float = Field(ge=0, le=20000)
    vibration_mm_s: float = Field(ge=0, le=100)
    exhaust_temp_c: float = Field(ge=-50, le=1200)
    oil_pressure_kpa: float = Field(ge=0, le=2000)
    ambient_temp_c: float = Field(ge=-60, le=60)
    ambient_pressure_kpa: float = Field(ge=50, le=120)
    operator_setpoint_rpm: float = Field(ge=0, le=20000)
    maintenance_flag: bool = False


class OperatorInput(BaseModel):
    setpoint_rpm: float = Field(ge=0, le=20000)
    note: str = Field(default="", max_length=500)


class MaintenanceEvent(BaseModel):
    kind: Literal["inspection", "repair", "part_replacement"]
    detail: str = Field(min_length=1, max_length=500)


# -- twin core ---------------------------------------------------------------

class TwinState(BaseModel):
    asset_id: str
    seq: int
    estimated_rpm: float
    estimated_health: float = Field(ge=0, le=1)  # 1 = new
    remaining_useful_life_cycles: int = Field(ge=0)
    uncertainty: float = Field(ge=0, le=1)
    config_version: str = Field(min_length=1, max_length=64)
    model_version: str = Field(min_length=1, max_length=64)
    assumptions: list[str] = Field(default_factory=list)
    telemetry_digest: str


class ScenarioResult(BaseModel):
    name: str
    predicted_health: float = Field(ge=0, le=1)
    predicted_risk: float = Field(ge=0, le=1)


# -- decision support ----------------------------------------------------------

class RecommendedAction(BaseModel):
    action: Literal["hold_setpoint", "reduce_load", "inspect", "safe_shutdown", "maintain"]
    setpoint_rpm: float = Field(ge=0, le=20000)
    rationale: str = Field(min_length=1, max_length=1000)
    evidence_digests: list[str] = Field(default_factory=list)


class DecisionPacket(BaseModel):
    decision_id: str  # sha256 hex, server-computed
    asset_id: str
    seq: int
    anomaly: bool
    anomaly_score: float = Field(ge=0, le=1)
    predicted_risk: float = Field(ge=0, le=1)
    confidence: float = Field(ge=0, le=1)
    recommendation: RecommendedAction
    scenarios: list[ScenarioResult] = Field(default_factory=list)
    twin_state: TwinState
    policy: str = Field(min_length=1, max_length=128)
    evidence_digest: str
    expires_at: str


# -- human veto ------------------------------------------------------------------

class VetoDecision(BaseModel):
    decision_id: str
    verdict: Literal["approve", "veto", "hold"]
    reviewer: str = Field(min_length=1, max_length=128)
    reason: str = Field(default="", max_length=1000)
    at: str


class VetoRecord(BaseModel):
    decision_id: str
    status: Literal["pending", "approved", "vetoed", "held", "expired", "executed", "safe_state"]
    veto: VetoDecision | None = None


# -- execution / safe state --------------------------------------------------------

class ExecutionResult(BaseModel):
    decision_id: str
    executed: bool
    applied_setpoint_rpm: float | None = None
    safe_state_entered: bool = False
    reason: str = Field(min_length=1, max_length=1000)
    at: str


class CycleResponse(BaseModel):
    decision: DecisionPacket
    veto: VetoRecord
    degraded: bool = False
