from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:  # Optional locally; required by the production Docker dependency set.
    import psycopg
except ModuleNotFoundError:  # pragma: no cover - exercised only without extras installed
    psycopg = None  # type: ignore[assignment]

from app.continuity import (
    content_sha256,
    detect_conflicts,
    ensure_content_hash,
    to_selection,
)
from app.models import (
    ConflictSet,
    MemoryBoard,
    MemoryCreate,
    MemoryRecord,
    MemoryUpdate,
    SelectionProvenance,
    migrate_legacy_record,
)
from app.identity import current_tenant_key


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
        self._dirty_migration = False

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
        self._hydrate(raw)

    def _hydrate(self, raw: dict[str, Any]) -> None:
        board_raw = raw.get("board")
        if isinstance(board_raw, dict):
            try:
                self._board = MemoryBoard(**board_raw)
            except Exception:
                self._board = MemoryBoard()
        for item in raw.get("memories", []):
            if not isinstance(item, dict) or not item.get("id"):
                continue
            try:
                migrated = migrate_legacy_record(item)
                rec = MemoryRecord(**migrated)
                rec = ensure_content_hash(rec)
                self._memories[rec.id] = rec
                # Persist migration if legacy fields were present
                if any(k in item for k in ("category", "state_class", "truth_status", "scope")):
                    self._dirty_migration = True
                if not item.get("content_sha256"):
                    self._dirty_migration = True
            except Exception:
                continue
        if self._dirty_migration:
            self._save()
            self._dirty_migration = False

    def _save(self):
        self._path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "board": self._board.model_dump(),
            "schema": "continuity-ledger-v1",
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
        memory_type: str | None = None,
        status: str | None = None,
        session_id: str | None = None,
        subject: str | None = None,
    ) -> list[MemoryRecord]:
        self._ensure_loaded()
        results = list(self._memories.values())
        if truth_scope:
            lower = truth_scope.lower()
            if lower == "live":
                results = [m for m in results if m.status != "archived"]
            else:
                results = [
                    m
                    for m in results
                    if m.status == lower or m.source_agent == lower
                ]
        if memory_type:
            results = [m for m in results if m.type == memory_type]
        if status:
            results = [m for m in results if m.status == status]
        if session_id:
            results = [m for m in results if m.session_id == session_id]
        if subject:
            results = [m for m in results if m.subject == subject]
        if query:
            q = query.lower()
            results = [
                m
                for m in results
                if q in m.content.lower()
                or any(q in tag.lower() for tag in m.tags)
                or (m.subject and q in m.subject.lower())
                or q in m.type.lower()
                or q in m.source_agent.lower()
                or q in m.session_id.lower()
            ]
        results.sort(key=lambda m: (m.created_at, m.id), reverse=True)
        return results[:limit]

    def retrieve(
        self,
        *,
        truth_scope: str | None = None,
        query: str | None = None,
        limit: int = 50,
        memory_type: str | None = None,
        status: str | None = None,
        session_id: str | None = None,
        subject: str | None = None,
    ) -> tuple[list[MemoryRecord], list[SelectionProvenance], list[ConflictSet]]:
        """List with selection provenance + any subject conflicts among results."""
        memories = self.list_memories(
            truth_scope=truth_scope,
            query=query,
            limit=limit,
            memory_type=memory_type,
            status=status,
            session_id=session_id,
            subject=subject,
        )
        selections = [
            to_selection(
                m,
                query=query,
                truth_scope=truth_scope,
                memory_type=memory_type,
                status=status,
            )
            for m in memories
        ]
        # Conflict scan uses full subject cohort from store (not just page)
        all_for_conflict = self.list_memories(limit=9999, truth_scope="live")
        conflicts = detect_conflicts(all_for_conflict, subject=subject)
        # If subject filter unset, only include conflicts that touch returned ids
        if subject is None:
            returned_ids = {m.id for m in memories}
            conflicts = [
                c
                for c in conflicts
                if any(m.id in returned_ids for m in c.memories)
            ]
        return memories, selections, conflicts

    def get_memory(self, memory_id: str) -> MemoryRecord | None:
        self._ensure_loaded()
        return self._memories.get(memory_id)

    def create_memory(self, data: MemoryCreate) -> MemoryRecord:
        self._ensure_loaded()
        now = _now_iso()
        if data.supersedes and data.supersedes not in self._memories:
            # Allow forward-ref only if empty; otherwise require known id
            raise ValueError(f"supersedes target not found: {data.supersedes}")
        rec = MemoryRecord(
            id=_make_id("mem"),
            content=data.content,
            created_at=now,
            updated_at=now,
            source_agent=data.source_agent,
            session_id=data.session_id,
            type=data.type,
            confidence=data.confidence,
            evidence=list(data.evidence),
            supersedes=data.supersedes,
            status=data.status,
            subject=data.subject,
            tags=data.tags[:],
            content_sha256=content_sha256(data.content),
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
        for key in (
            "content",
            "source_agent",
            "session_id",
            "type",
            "confidence",
            "evidence",
            "supersedes",
            "status",
            "subject",
            "tags",
        ):
            value = getattr(data, key, None)
            if value is not None:
                if key == "evidence":
                    updates[key] = [e.model_dump() if hasattr(e, "model_dump") else e for e in value]
                else:
                    updates[key] = value
        if data.supersedes is not None and data.supersedes != "" and data.supersedes not in self._memories:
            raise ValueError(f"supersedes target not found: {data.supersedes}")
        # Explicit clear of supersedes via empty string not supported; use null through model
        updates["updated_at"] = _now_iso()
        if data.content is not None:
            updates["content_sha256"] = content_sha256(data.content)
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

    def conflicts(self, subject: str | None = None) -> list[ConflictSet]:
        self._ensure_loaded()
        return detect_conflicts(list(self._memories.values()), subject=subject)


class PostgresJarvisStore(JarvisStore):
    """Durable per-tenant ledger backed by managed PostgreSQL.

    The validated Continuity Ledger document is stored as JSONB so the existing
    replay/governance rules remain identical during the storage migration.  The
    tenant key is derived exclusively from OAuth identity in ``get_store``.
    """

    def __init__(self, dsn: str, tenant_key: str):
        super().__init__(path="")
        self._dsn = dsn
        self._tenant_key = tenant_key

    @staticmethod
    def _ensure_schema(conn) -> None:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS jarvis_tenant_ledgers (
                tenant_key TEXT PRIMARY KEY,
                payload JSONB NOT NULL,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """
        )

    def _load(self):
        self._loaded = True
        if psycopg is None:
            raise RuntimeError("PostgreSQL support requires the psycopg package")
        try:
            with psycopg.connect(self._dsn, connect_timeout=5) as conn:
                self._ensure_schema(conn)
                row = conn.execute(
                    "SELECT payload FROM jarvis_tenant_ledgers WHERE tenant_key = %s",
                    (self._tenant_key,),
                ).fetchone()
        except psycopg.Error as exc:
            raise RuntimeError("Jarvis PostgreSQL ledger is unavailable") from exc
        if row and isinstance(row[0], dict):
            self._hydrate(row[0])

    def _save(self):
        data = {
            "board": self._board.model_dump(),
            "schema": "continuity-ledger-v1",
            "memories": [m.model_dump() for m in self._memories.values()],
        }
        if psycopg is None:
            raise RuntimeError("PostgreSQL support requires the psycopg package")
        try:
            with psycopg.connect(self._dsn, connect_timeout=5) as conn:
                self._ensure_schema(conn)
                conn.execute(
                    """
                    INSERT INTO jarvis_tenant_ledgers (tenant_key, payload)
                    VALUES (%s, %s::jsonb)
                    ON CONFLICT (tenant_key) DO UPDATE SET
                        payload = EXCLUDED.payload,
                        updated_at = NOW()
                    """,
                    (self._tenant_key, json.dumps(data, default=str)),
                )
        except psycopg.Error as exc:
            raise RuntimeError("Jarvis PostgreSQL ledger write failed") from exc


_stores: dict[str, JarvisStore] = {}


def get_store(path: str | None = None) -> JarvisStore:
    """Return the request tenant's isolated ledger when OAuth public mode is active."""
    requested = path or os.getenv("JARVIS_STORE_PATH", "data/jarvis-store.json")
    tenant = current_tenant_key()
    database_url = (os.getenv("JARVIS_DATABASE_URL") or "").strip()
    if database_url:
        database_tenant = tenant or "operator"
        cache_key = f"postgres:{database_tenant}"
        if cache_key not in _stores:
            _stores[cache_key] = PostgresJarvisStore(database_url, database_tenant)
        return _stores[cache_key]
    if tenant:
        root = Path(os.getenv("JARVIS_TENANT_STORE_DIR", f"{Path(requested).parent}/tenants"))
        requested = str(root / f"{tenant}.json")
    if requested not in _stores:
        _stores[requested] = JarvisStore(requested)
    return _stores[requested]


def reset_store_for_tests() -> None:
    """Test helper — clear singleton."""
    _stores.clear()
