from __future__ import annotations

import json
import os
import tempfile
import threading
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
from app.store_errors import StoreUnavailableError, StoreVersionConflict


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _make_id(prefix: str = "mem") -> str:
    import uuid

    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def memory_matches_query(m: MemoryRecord, q: str) -> bool:
    """Case-insensitive substring match used by every ledger backend (``q`` lower-cased)."""
    return bool(
        q in m.content.lower()
        or any(q in tag.lower() for tag in m.tags)
        or (m.subject and q in m.subject.lower())
        or q in m.type.lower()
        or q in m.source_agent.lower()
        or q in m.session_id.lower()
    )


def ledger_retrieve(
    store: Any,
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
    memories = store.list_memories(
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
    all_for_conflict = store.list_memories(limit=9999, truth_scope="live")
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


class JarvisStore:
    def __init__(self, path: str = "data/jarvis-store.json"):
        self._path = Path(path)
        self._board: MemoryBoard = MemoryBoard()
        self._memories: dict[str, MemoryRecord] = {}
        self._loaded = False
        self._dirty_migration = False
        # Serialises load and every mutate+save; re-entrant because load may save a migration.
        self._lock = threading.RLock()

    def _ensure_loaded(self):
        if self._loaded:
            return
        with self._lock:
            if not self._loaded:
                self._load()

    def _load(self):
        """Load the ledger.  A file that exists but cannot be parsed is never "empty"."""
        if not self._path.exists():
            self._loaded = True
            return
        try:
            raw = json.loads(self._path.read_text("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError, OSError) as exc:
            raise StoreUnavailableError(
                f"Ledger file exists but cannot be read ({type(exc).__name__}); refusing to start empty"
            ) from exc
        if not isinstance(raw, dict):
            raise StoreUnavailableError("Ledger file is not a JSON object; refusing to start empty")
        self._board = MemoryBoard()
        self._memories = {}
        self._hydrate(raw)
        self._loaded = True

    def _hydrate(self, raw: dict[str, Any]) -> None:
        """Parse the whole document or raise; never keep a partial ledger.

        Anything skipped here would be deleted from disk by the next _save, so every
        anomaly is fatal.  The bad record's id and error go in the exception message
        (for logs); the HTTP response stays generic.
        """
        if "memories" not in raw:
            stray = sorted(k for k, v in raw.items() if k not in ("board", "schema") and v)
            if stray:
                raise StoreUnavailableError(
                    f"Ledger has no 'memories' key but has other data under {stray}; refusing to load as empty"
                )
        board = MemoryBoard()
        board_raw = raw.get("board")
        if board_raw is not None:
            try:
                if not isinstance(board_raw, dict):
                    raise TypeError(f"board is {type(board_raw).__name__}, not an object")
                board = MemoryBoard(**board_raw)
            except Exception as exc:
                raise StoreUnavailableError(f"Ledger board is invalid: {exc}") from exc
        items = raw.get("memories", [])
        if not isinstance(items, list):
            raise StoreUnavailableError(f"Ledger 'memories' is {type(items).__name__}, not a list")
        memories: dict[str, MemoryRecord] = {}
        dirty_migration = False
        for index, item in enumerate(items):
            label = item.get("id") if isinstance(item, dict) and item.get("id") else f"#{index}"
            try:
                if not isinstance(item, dict) or not item.get("id"):
                    raise ValueError("record is not an object with an id")
                if item["id"] in memories:
                    raise ValueError("duplicate record id")
                migrated = migrate_legacy_record(item)
                rec = ensure_content_hash(MemoryRecord(**migrated))
            except Exception as exc:
                raise StoreUnavailableError(f"Ledger record {label} failed validation: {exc}") from exc
            memories[rec.id] = rec
            # Persist migration if legacy fields were present
            if any(k in item for k in ("category", "state_class", "truth_status", "scope")):
                dirty_migration = True
            if not item.get("content_sha256"):
                dirty_migration = True
        self._board = board
        self._memories = memories
        if dirty_migration:
            # Only reached after a fully clean parse, so nothing can be dropped by this save.
            self._save()

    def _save(self):
        """Atomically replace the ledger file: temp file, fsync, os.replace."""
        data = {
            "board": self._board.model_dump(),
            "schema": "continuity-ledger-v1",
            "memories": [m.model_dump() for m in self._memories.values()],
        }
        payload = json.dumps(data, indent=2, default=str).encode("utf-8")
        tmp_name: str | None = None
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp_name = tempfile.mkstemp(
                dir=str(self._path.parent), prefix=f"{self._path.name}.", suffix=".tmp"
            )
            with os.fdopen(fd, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_name, self._path)
            tmp_name = None
        except OSError as exc:
            raise StoreUnavailableError(f"Ledger write failed ({type(exc).__name__})") from exc
        finally:
            if tmp_name is not None:
                try:
                    os.unlink(tmp_name)
                except OSError:
                    pass

    def _save_or_restore(self, restore) -> None:
        """Persist; if that fails, undo the in-memory change so memory never leads disk."""
        try:
            self._save()
        except BaseException:
            restore()
            raise


    # --- Board ---

    def get_board(self) -> MemoryBoard:
        self._ensure_loaded()
        return self._board

    def _swap_board(self, board: MemoryBoard) -> MemoryBoard:
        previous = self._board
        self._board = board

        def restore() -> None:
            self._board = previous

        self._save_or_restore(restore)
        return self._board

    def set_board(self, board: MemoryBoard) -> MemoryBoard:
        with self._lock:
            self._ensure_loaded()
            return self._swap_board(board)

    def patch_board(self, updates: dict[str, Any]) -> MemoryBoard:
        with self._lock:
            self._ensure_loaded()
            current = self._board.model_dump()
            for key, value in updates.items():
                if value is not None:
                    current[key] = value
            return self._swap_board(MemoryBoard(**current))

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
            results = [m for m in results if memory_matches_query(m, q)]
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
        return ledger_retrieve(
            self,
            truth_scope=truth_scope,
            query=query,
            limit=limit,
            memory_type=memory_type,
            status=status,
            session_id=session_id,
            subject=subject,
        )

    def get_memory(self, memory_id: str) -> MemoryRecord | None:
        self._ensure_loaded()
        return self._memories.get(memory_id)

    def create_memory(self, data: MemoryCreate) -> MemoryRecord:
        with self._lock:
            return self._create_memory(data)

    def _create_memory(self, data: MemoryCreate) -> MemoryRecord:
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
        self._save_or_restore(lambda: self._memories.pop(rec.id, None))
        return rec

    def update_memory(self, memory_id: str, data: MemoryUpdate) -> MemoryRecord | None:
        with self._lock:
            return self._update_memory(memory_id, data)

    def _update_memory(self, memory_id: str, data: MemoryUpdate) -> MemoryRecord | None:
        self._ensure_loaded()
        existing = self._memories.get(memory_id)
        if not existing:
            return None
        if data.expected_version is not None and data.expected_version != existing.version:
            raise StoreVersionConflict(
                f"version conflict: expected {data.expected_version}, current {existing.version}"
            )
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
        updates["version"] = existing.version + 1
        if data.content is not None:
            updates["content_sha256"] = content_sha256(data.content)
        updated = MemoryRecord(**updates)
        self._memories[memory_id] = updated
        self._save_or_restore(lambda: self._memories.__setitem__(memory_id, existing))
        return updated

    def delete_memory(self, memory_id: str) -> bool:
        with self._lock:
            self._ensure_loaded()
            removed = self._memories.pop(memory_id, None)
            if removed is None:
                return False
            self._save_or_restore(lambda: self._memories.__setitem__(memory_id, removed))
            return True

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
        if psycopg is None:
            raise StoreUnavailableError("PostgreSQL support requires the psycopg package")
        try:
            with psycopg.connect(self._dsn, connect_timeout=5) as conn:
                self._ensure_schema(conn)
                row = conn.execute(
                    "SELECT payload FROM jarvis_tenant_ledgers WHERE tenant_key = %s",
                    (self._tenant_key,),
                ).fetchone()
        except psycopg.Error as exc:
            raise StoreUnavailableError("Jarvis PostgreSQL ledger is unavailable") from exc
        if row and isinstance(row[0], dict):
            self._hydrate(row[0])
        self._loaded = True

    def _save(self):
        data = {
            "board": self._board.model_dump(),
            "schema": "continuity-ledger-v1",
            "memories": [m.model_dump() for m in self._memories.values()],
        }
        if psycopg is None:
            raise StoreUnavailableError("PostgreSQL support requires the psycopg package")
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
            raise StoreUnavailableError("Jarvis PostgreSQL ledger write failed") from exc


_stores: dict[str, JarvisStore] = {}


def get_store(path: str | None = None) -> JarvisStore:
    """Return the request tenant's isolated ledger when OAuth public mode is active."""
    requested = path or os.getenv("JARVIS_STORE_PATH", "data/jarvis-store.json")
    tenant = current_tenant_key()
    database_url = (os.getenv("JARVIS_DATABASE_URL") or "").strip()
    if database_url:
        database_tenant = tenant or "operator"
        mode = (os.getenv("JARVIS_PG_STORE") or "blob").strip().lower()
        if mode == "rows":
            schema = (os.getenv("JARVIS_DATABASE_SCHEMA") or "").strip() or None
            cache_key = f"postgres-rows:{schema or ''}:{database_tenant}"
            if cache_key not in _stores:
                from app.pg_store import PostgresRowStore

                _stores[cache_key] = PostgresRowStore(database_url, database_tenant, schema=schema)  # type: ignore[assignment]
            return _stores[cache_key]
        if mode != "blob":
            raise StoreUnavailableError("JARVIS_PG_STORE must be 'rows' or 'blob'")
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
    import sys

    if "app.pg_store" in sys.modules:
        sys.modules["app.pg_store"].close_pools()
