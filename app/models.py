from __future__ import annotations

from typing import Any, Literal
from pydantic import BaseModel, Field


SlotClass = Literal["foundation", "identity", "preference", "operational"]
TruthStatus = Literal["canonical", "stable_user", "signal", "pending"]
Scope = Literal["persistent", "session"]
StateClass = Literal["live", "archived"]


class ModuleSlot(BaseModel):
    display_name: str
    summary: str
    linked_subsystem: str = "jarvis"


class BoardSlot(BaseModel):
    slot_id: str | None = None
    slot_name: str
    accepted_class: SlotClass
    module: ModuleSlot | None = None


class GovernanceItem(BaseModel):
    action: str
    detail: str = ""


class MemoryBoard(BaseModel):
    board_id: str = "default_board"
    summary: str = ""
    linked_subsystems: list[str] = Field(default_factory=lambda: ["jarvis"])
    slots: list[BoardSlot] = Field(default_factory=list)
    governance: list[GovernanceItem] = Field(default_factory=list)


class MemoryCreate(BaseModel):
    content: str = Field(..., min_length=1, max_length=2000)
    category: str = "signal"
    tags: list[str] = Field(default_factory=list)
    scope: Scope = "session"
    state_class: StateClass = "live"
    truth_status: TruthStatus = "pending"


class MemoryUpdate(BaseModel):
    content: str | None = Field(default=None, min_length=1, max_length=2000)
    category: str | None = None
    tags: list[str] | None = None
    scope: Scope | None = None
    state_class: StateClass | None = None
    truth_status: TruthStatus | None = None


class MemoryRecord(BaseModel):
    id: str
    content: str
    category: str
    tags: list[str]
    scope: Scope
    state_class: StateClass
    truth_status: TruthStatus
    created_at: str
    updated_at: str


class BoardUpdate(BaseModel):
    summary: str | None = None
    linked_subsystems: list[str] | None = None
    slots: list[BoardSlot] | None = None
    governance: list[GovernanceItem] | None = None
