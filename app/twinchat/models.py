"""Wire contracts for the Digital Twin chat surface — schema ChatTurnReceipt.v1.

Tenant identity never appears in these models: it is derived from the
authenticated caller at the endpoint boundary and carried internally as a
database key only.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class ChatRequest(BaseModel):
    session_id: str = Field(min_length=1, max_length=128)
    message: str = Field(min_length=1, max_length=8000)
    provider: str | None = Field(default=None, max_length=128)
    persist: bool = False
    # No client-supplied history in v1. Context comes only from the
    # tenant-bound SessionStore.


class Turn(BaseModel):
    """One server-owned session-context turn."""

    role: Literal["user", "assistant"]
    content: str = Field(max_length=8000)
    receipt_digest: str | None = None


class SignalReading(BaseModel):
    """Bounded projection of a humansignal CompileResponse.

    Style hint only — never citable evidence and never raw spans.
    """

    available: bool = False
    dominant_emotion: str | None = None
    signal_strength: float | None = None
    recommended_style: str | None = None
    evidence_features: list[str] = Field(default_factory=list)


class ProposedClaim(BaseModel):
    """An extracted candidate. Only 'decision'/'user' proposals persist in v1."""

    claim_type: Literal["decision", "fact", "research"]
    content: str = Field(min_length=1, max_length=2000)
    subject: str | None = Field(default=None, max_length=256)
    attribution: Literal["user", "twin"]
    evidence_kind: Literal["user-request", "receipt"]
    extractor: str = Field(min_length=1, max_length=64)  # "rule:<name>" or "model"


class DropFinding(BaseModel):
    index: int
    reason: str


class ChatTurnReceipt(BaseModel):
    """Immutable record of one governed turn — the base evidence object."""

    schema: Literal["ChatTurnReceipt.v1"] = "ChatTurnReceipt.v1"
    receipt_digest: str = ""
    prev_digest: str = "sha256:genesis"
    session_id: str = ""
    turn_index: int = 0
    at: str = ""
    request_digest: str = ""
    state_digest: str | None = None
    context_reset: bool = False
    recalled_ids: list[str] = Field(default_factory=list)
    conflict_subjects: list[str] = Field(default_factory=list)
    abstained: bool = False
    abstention_reason: str | None = None
    signal: SignalReading | None = None
    backend: str = ""
    model: str = ""
    gate_mode: str = ""
    gate_dropped: list[DropFinding] = Field(default_factory=list)
    fallback_used: bool = False
    fallback_reason: str | None = None
    prompt_digest: str = ""
    raw_output_digest: str = ""
    reply_digest: str = ""
    proposed_claims: list[ProposedClaim] = Field(default_factory=list)
    latency_ms: int = 0
    usage: dict | None = None


class PersistFailure(BaseModel):
    """Code + index only — never rejected or raw text."""

    proposal_index: int
    code: str


class ChatPersistReceipt(BaseModel):
    """Linked outcome event for one turn's persist phase."""

    schema: Literal["ChatPersistReceipt.v1"] = "ChatPersistReceipt.v1"
    persist_digest: str = ""
    turn_receipt_digest: str = ""
    session_id: str = ""
    turn_index: int = 0
    persisted_ids: list[str] = Field(default_factory=list)
    failures: list[PersistFailure] = Field(default_factory=list)


class ChatResponse(BaseModel):
    turn_id: str
    reply: str
    receipt: ChatTurnReceipt
    persist_receipt: ChatPersistReceipt | None = None
    degraded: bool = False
