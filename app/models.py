"""Continuity Ledger schema — evidence-backed memory records (canonical SoT).

Status tags in docs/tests:
  enforced  — covered by automated tests in this package
  partial   — implemented with gaps / operator protocol
  declared  — roadmap / intent only
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_serializer,
    model_validator,
)


MemoryType = Literal[
    "decision",
    "fact",
    "task",
    "preference",
    "architecture",
    "research",
    "external_context",
]
MemoryStatus = Literal["draft", "verified", "archived"]

# Board workspace contexts (wire alias: slots) — UI/workspace UX only.
# NOT ledger write partitions. Conflicts group by subject across agents on one store.
ContextClass = Literal["foundation", "identity", "preference", "operational"]
SlotClass = ContextClass  # backward-compatible alias


class ModuleSlot(BaseModel):
    display_name: str
    summary: str
    linked_subsystem: str = "jarvis"


class BoardContext(BaseModel):
    """UI/workspace context entry — never used as a write partition key."""

    model_config = ConfigDict(populate_by_name=True)

    context_id: str | None = Field(
        default=None,
        validation_alias=AliasChoices("context_id", "slot_id"),
    )
    context_name: str = Field(
        validation_alias=AliasChoices("context_name", "slot_name"),
    )
    accepted_class: ContextClass
    module: ModuleSlot | None = None

    @model_serializer(mode="wrap")
    def _serialize(self, handler):
        data = handler(self)
        # Emit legacy slot_* keys for board UI compatibility.
        data["slot_id"] = data.get("context_id")
        data["slot_name"] = data.get("context_name")
        return data


# Backward-compatible name
BoardSlot = BoardContext


class GovernanceItem(BaseModel):
    action: str
    detail: str = ""


class MemoryBoard(BaseModel):
    """Board UI workspace context. ``slots`` is a deprecated wire alias."""

    model_config = ConfigDict(populate_by_name=True)

    board_id: str = "default_board"
    summary: str = ""
    linked_subsystems: list[str] = Field(default_factory=lambda: ["jarvis"])
    workspace_contexts: list[BoardContext] = Field(
        default_factory=list,
        validation_alias=AliasChoices("workspace_contexts", "slots"),
    )
    governance: list[GovernanceItem] = Field(default_factory=list)

    @property
    def slots(self) -> list[BoardContext]:
        """Deprecated alias for ``workspace_contexts`` (not write partitions)."""
        return self.workspace_contexts

    @model_validator(mode="before")
    @classmethod
    def _accept_slots_alias(cls, data: Any) -> Any:
        if isinstance(data, dict) and "workspace_contexts" not in data and "slots" in data:
            data = dict(data)
            data["workspace_contexts"] = data.get("slots") or []
        return data

    @model_serializer(mode="wrap")
    def _serialize(self, handler):
        data = handler(self)
        # Keep ``slots`` on the wire so existing board UI keeps working.
        data["slots"] = data.get("workspace_contexts", [])
        return data


class EvidenceLink(BaseModel):
    """Pointer to supporting material (path, URL, test id, receipt id, etc.)."""

    kind: str = "ref"
    ref: str = Field(..., min_length=1, max_length=1000)
    note: str = Field(default="", max_length=500)


class MemoryCreate(BaseModel):
    """Create a Continuity Ledger entry. Conversations are transient — store decisions/evidence."""

    content: str = Field(..., min_length=1, max_length=2000)
    source_agent: str = Field(..., min_length=1, max_length=128)
    session_id: str = Field(..., min_length=1, max_length=128)
    type: MemoryType
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    evidence: list[EvidenceLink] = Field(default_factory=list)
    supersedes: str | None = Field(default=None, max_length=64)
    status: MemoryStatus = "draft"
    # Optional conflict-grouping key (same subject + different content = conflict)
    subject: str | None = Field(default=None, max_length=256)
    tags: list[str] = Field(default_factory=list)

    @field_validator("confidence")
    @classmethod
    def _round_confidence(cls, v: float) -> float:
        return round(float(v), 4)


class MemoryUpdate(BaseModel):
    content: str | None = Field(default=None, min_length=1, max_length=2000)
    source_agent: str | None = Field(default=None, min_length=1, max_length=128)
    session_id: str | None = Field(default=None, min_length=1, max_length=128)
    type: MemoryType | None = None
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    evidence: list[EvidenceLink] | None = None
    supersedes: str | None = None
    status: MemoryStatus | None = None
    subject: str | None = Field(default=None, max_length=256)
    tags: list[str] | None = None


class MemoryRecord(BaseModel):
    """Canonical Continuity Ledger record — all fields required on the wire."""

    id: str
    content: str
    # timestamp SoT
    created_at: str  # ISO-8601 UTC
    updated_at: str
    source_agent: str
    session_id: str
    type: MemoryType
    confidence: float
    evidence: list[EvidenceLink] = Field(default_factory=list)
    supersedes: str | None = None
    status: MemoryStatus
    subject: str | None = None
    tags: list[str] = Field(default_factory=list)
    # content hash for drift checks (sha256 hex of normalized content)
    content_sha256: str = ""


class SelectionProvenance(BaseModel):
    """Replay answers: why selected / where from / when / which session."""

    memory_id: str
    why_selected: str
    source_agent: str
    session_id: str
    created_at: str
    type: MemoryType
    status: MemoryStatus
    confidence: float
    supersedes: str | None = None
    evidence: list[EvidenceLink] = Field(default_factory=list)
    subject: str | None = None


class ConflictSet(BaseModel):
    """Disagreeing memories for one subject — never silently merged.

    The ledger surfaces conflicts; it does not adjudicate which claim is true.
    Consumers (Evidence/Knowledge/Understanding engines — declared) decide.
    """

    subject: str
    unresolved: bool
    memories: list[MemoryRecord]
    policy_hint: str = (
        "Do not merge. Ledger preserves both records with provenance. "
        "A writer may later append a record with supersedes=<id> or status=archived "
        "as a continuity claim — other engines evaluate whether that claim is warranted."
    )


class BoardUpdate(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    summary: str | None = None
    linked_subsystems: list[str] | None = None
    workspace_contexts: list[BoardContext] | None = Field(
        default=None,
        validation_alias=AliasChoices("workspace_contexts", "slots"),
    )
    governance: list[GovernanceItem] | None = None

    @property
    def slots(self) -> list[BoardContext] | None:
        return self.workspace_contexts

    @model_serializer(mode="wrap")
    def _serialize(self, handler):
        data = handler(self)
        if "workspace_contexts" in data:
            data["slots"] = data.get("workspace_contexts")
        return data


def migrate_legacy_record(raw: dict[str, Any]) -> dict[str, Any]:
    """Map pre-ledger store rows into the Continuity Ledger shape.

    Legacy fields (category, state_class, truth_status, scope) are read once
    and not required going forward. Migrated rows default to status=draft and
    source_agent=legacy-migration unless already present.
    """
    out = dict(raw)
    if not out.get("source_agent"):
        out["source_agent"] = "legacy-migration"
    if not out.get("session_id"):
        tags = out.get("tags") or []
        session_guess = next(
            (t for t in tags if isinstance(t, str) and t.startswith("sess")),
            None,
        )
        out["session_id"] = session_guess or "legacy-unknown-session"
    if not out.get("type"):
        category = str(out.get("category") or "signal").lower()
        type_map = {
            "signal": "fact",
            "ledger": "fact",
            "decision": "decision",
            "architecture": "architecture",
            "preference": "preference",
            "research": "research",
            "task": "task",
        }
        out["type"] = type_map.get(category, "fact")
    if out.get("confidence") is None:
        truth = str(out.get("truth_status") or "").lower()
        out["confidence"] = 0.8 if truth in ("canonical", "stable_user") else 0.4
    if "evidence" not in out or out["evidence"] is None:
        out["evidence"] = []
    else:
        # normalize plain strings → EvidenceLink dicts
        normalized = []
        for item in out["evidence"]:
            if isinstance(item, str):
                normalized.append({"kind": "ref", "ref": item})
            elif isinstance(item, dict):
                normalized.append(item)
        out["evidence"] = normalized
    if not out.get("status"):
        state = str(out.get("state_class") or "").lower()
        if state == "archived":
            out["status"] = "archived"
        elif str(out.get("truth_status") or "").lower() in ("canonical", "stable_user"):
            out["status"] = "verified"
        else:
            out["status"] = "draft"
    if "supersedes" not in out:
        out["supersedes"] = None
    if "subject" not in out:
        out["subject"] = None
    if "tags" not in out or out["tags"] is None:
        out["tags"] = []
    # Drop obsolete required-ness of legacy-only fields from the model path
    for obsolete in ("category", "scope", "state_class", "truth_status"):
        out.pop(obsolete, None)
    return out
