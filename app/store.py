from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.models import (
    BoardSlot,
    GovernanceItem,
    MemoryBoard,
    MemoryCreate,
    MemoryRecord,
    MemoryUpdate,
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _make_id(prefix: str = "mem") -> str:
    import uuid
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


class JarvisStore:
    def __init__(self, path: str = "data/jarvis-store.json"):
        self._path = Path(path)
        self._board: MemoryBoard = MemoryBoard()
        self._memories: dict[str, MemoryRecord] = {}
        self._loaded = False

    def _ensure_loaded(self):
        if not self._loaded:
            self._load()

    def _load(self):
        self._loaded = True
        if not self._path.exists():
            return
        try:
            raw = json.loads(self._path.read_text("utf-8"))
        except (json.JSONDecodeError, OSError):
            return
        board_raw = raw.get("board")
        if isinstance(board_raw, dict):
            self._board = MemoryBoard(**board_raw)
        for item in raw.get("memories", []):
            if isinstance(item, dict) and item.get("id"):
                try:
                    rec = MemoryRecord(**item)
                    self._memories[rec.id] = rec
                except Exception:
                    pass

    def _save(self):
        self._path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "board": self._board.model_dump(),
            "memories": [m.model_dump() for m in self._memories.values()],
        }
        self._path.write_text(
            json.dumps(data, indent=2, default=str),
            "utf-8",
        )

    # --- Board ---

    def get_board(self) -> MemoryBoard:
        self._ensure_loaded()
        return self._board

    def set_board(self, board: MemoryBoard) -> MemoryBoard:
        self._ensure_loaded()
        self._board = board
        self._save()
        return self._board

    def patch_board(self, updates: dict[str, Any]) -> MemoryBoard:
        self._ensure_loaded()
        current = self._board.model_dump()
        for key, value in updates.items():
            if value is not None:
                current[key] = value
        self._board = MemoryBoard(**current)
        self._save()
        return self._board

    # --- Memories ---

    def list_memories(
        self,
        truth_scope: str | None = None,
        query: str | None = None,
        limit: int = 50,
    ) -> list[MemoryRecord]:
        self._ensure_loaded()
        results = list(self._memories.values())
        if truth_scope:
            lower = truth_scope.lower()
            results = [
                m for m in results
                if m.state_class == lower or m.truth_status == lower
            ]
        if query:
            q = query.lower()
            results = [
                m for m in results
                if q in m.content.lower()
                or any(q in tag.lower() for tag in m.tags)
                or q in m.category.lower()
            ]
        results.sort(key=lambda m: (m.created_at, m.id), reverse=True)
        return results[:limit]

    def get_memory(self, memory_id: str) -> MemoryRecord | None:
        self._ensure_loaded()
        return self._memories.get(memory_id)

    def create_memory(self, data: MemoryCreate) -> MemoryRecord:
        self._ensure_loaded()
        now = _now_iso()
        rec = MemoryRecord(
            id=_make_id("mem"),
            content=data.content,
            category=data.category,
            tags=data.tags[:],
            scope=data.scope,
            state_class=data.state_class,
            truth_status=data.truth_status,
            created_at=now,
            updated_at=now,
        )
        self._memories[rec.id] = rec
        self._save()
        return rec

    def update_memory(self, memory_id: str, data: MemoryUpdate) -> MemoryRecord | None:
        self._ensure_loaded()
        existing = self._memories.get(memory_id)
        if not existing:
            return None
        updates = existing.model_dump()
        for key in ("content", "category", "tags", "scope", "state_class", "truth_status"):
            value = getattr(data, key, None)
            if value is not None:
                updates[key] = value
        updates["updated_at"] = _now_iso()
        updated = MemoryRecord(**updates)
        self._memories[memory_id] = updated
        self._save()
        return updated

    def delete_memory(self, memory_id: str) -> bool:
        self._ensure_loaded()
        if memory_id in self._memories:
            del self._memories[memory_id]
            self._save()
            return True
        return False


_store: JarvisStore | None = None


def get_store(path: str | None = None) -> JarvisStore:
    global _store
    if _store is None:
        _store = JarvisStore(path or os.getenv("JARVIS_STORE_PATH", "data/jarvis-store.json"))
    return _store
