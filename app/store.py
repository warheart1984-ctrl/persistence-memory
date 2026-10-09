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
from app.ts import parse_utc
from app.twin import TWIN_AGENT
from app.store_errors import StoreUnavailableError, StoreVersionConflict
from app import clause_v
from app import evidence as evidence_objects


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


def parse_ledger_document(raw: dict[str, Any]) -> tuple[MemoryBoard, dict[str, MemoryRecord], bool]:
    """Parse a whole ledger document or raise; never returns a partial ledger.

    Anything skipped here would be deleted from disk by the next save, so every anomaly is
    fatal.  The bad record's id and error go in the exception message (for logs); the HTTP
    response stays generic.  Pure: no file or database is touched.  The third value says
    whether legacy rows were migrated in memory (a file-backed store then re-saves).
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
    return board, memories, dirty_migration


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
        """Parse the whole document or raise; never keep a partial ledger."""
        board, memories, dirty_migration = parse_ledger_document(raw)
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

    def list_latest(
        self,
        *,
        limit: int,
        after: tuple[datetime, str] | None = None,
        memory_type: str | None = None,
        include_superseded: bool = False,
        include_archived: bool = False,
        include_twin: bool = False,
    ) -> list[tuple[MemoryRecord, str | None]]:
        """Newest-first keyset page: ``(record, superseded_by)`` pairs ordered by ``(created_at, id)`` descending.

        ``after`` is the last ``(created_at, id)`` already returned; only strictly older rows follow it.  A record is
        superseded when another record in this tenant names it in ``supersedes`` (the newest such successor is reported).
        """
        self._ensure_loaded()
        successor: dict[str, tuple[datetime, str]] = {}
        for m in self._memories.values():
            if m.supersedes:
                key = (parse_utc(m.created_at), m.id)
                if m.supersedes not in successor or key > successor[m.supersedes]:
                    successor[m.supersedes] = key
        rows: list[tuple[tuple[datetime, str], MemoryRecord, str | None]] = []
        for m in self._memories.values():
            if memory_type and m.type != memory_type:
                continue
            if not include_twin and m.source_agent == TWIN_AGENT:
                continue
            if not include_archived and m.status == "archived":
                continue
            succ = successor.get(m.id)
            if succ is not None and not include_superseded:
                continue
            key = (parse_utc(m.created_at), m.id)
            if after is not None and not key < (parse_utc(after[0]), after[1]):
                continue
            rows.append((key, m, succ[1] if succ else None))
        rows.sort(key=lambda r: r[0], reverse=True)
        return [(m, s) for _, m, s in rows[: max(0, int(limit))]]

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
        evidence_objects.check_links(data.evidence, self._resolve_evidence)
        clause_v.gate_create(data, self._resolve_evidence)
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
        if data.evidence is not None:
            evidence_objects.check_links(data.evidence, self._resolve_evidence)
        clause_v.gate_update(existing, data, self._resolve_evidence)
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

    # -- Evidence Objects (JSON-lines sidecar next to the ledger file) ----------------------------

    def put_evidence_object(self, req: "evidence_objects.EvidenceObjectCreate") -> "tuple[evidence_objects.EvidenceObject, bool]":
        oid, data = evidence_objects.build_object(req)
        with self._lock:
            return evidence_objects.FileEvidenceStore(self._path).put(oid, req, len(data))

    def get_evidence_object(self, oid: str) -> "evidence_objects.EvidenceObject | None":
        with self._lock:
            return evidence_objects.FileEvidenceStore(self._path).get(oid)

    def all_evidence_objects(self) -> "list[evidence_objects.EvidenceObject]":
        with self._lock:
            return evidence_objects.FileEvidenceStore(self._path).all()

    def _resolve_evidence(self, ref: str) -> "evidence_objects.EvidenceInfo | None":
        obj = self.get_evidence_object(ref)
        if obj is None:
            return None
        problems = evidence_objects.verify_stored(obj)
        if problems:
            raise evidence_objects.EvidenceError(
                "evidence_object_invalid",
                "the stored evidence object is damaged",
                [{"code": "evidence_object_hash_mismatch", "ref": ref, "message": problems[0]}],
            )
        return evidence_objects.EvidenceInfo(obj.id, obj.schema_id)

    def delete_memory(self, memory_id: str) -> bool:
        with self._lock:
            self._ensure_loaded()
            removed = self._memories.pop(memory_id, None)
            if removed is None:
                return False
            self._save_or_restore(lambda: self._memories.__setitem__(memory_id, removed))
            return True

    def readiness(self) -> dict[str, str]:
        """Readiness checks for /ready: can this store be served right now?  Raises if it cannot."""
        self._ensure_loaded()
        return {"store": "ok"}

    def history(self, memory_id: str, limit: int = 200) -> list[dict[str, Any]]:
        raise NotImplementedError("record history requires the PostgreSQL row store")

    def verify_history(self, memory_id: str | None = None) -> list[dict[str, Any]]:
        raise NotImplementedError("record history requires the PostgreSQL row store")

    # -- Continuity Blocks (PostgreSQL row store only) ------------------------------------------------

    def seal_blocks(self, **kwargs: Any) -> dict[str, Any]:
        raise NotImplementedError("Continuity Blocks require the PostgreSQL row store")

    def list_blocks(self, after_height: int = 0, limit: int = 100) -> list[dict[str, Any]]:
        raise NotImplementedError("Continuity Blocks require the PostgreSQL row store")

    def get_block(self, height: int) -> dict[str, Any] | None:
        raise NotImplementedError("Continuity Blocks require the PostgreSQL row store")

    def block_head(self) -> dict[str, Any]:
        raise NotImplementedError("Continuity Blocks require the PostgreSQL row store")

    def verify_blocks(self) -> list[dict[str, Any]]:
        raise NotImplementedError("Continuity Blocks require the PostgreSQL row store")

    # -- Replay Contracts (PostgreSQL row store only) -------------------------------------------------

    def replay_state(self, **kwargs: Any) -> Any:
        raise NotImplementedError("Replay Contracts require the PostgreSQL row store")

    def replay_events(self, **kwargs: Any) -> Any:
        raise NotImplementedError("Replay Contracts require the PostgreSQL row store")

    def create_replay_receipt(self, **kwargs: Any) -> Any:
        raise NotImplementedError("Replay Contracts require the PostgreSQL row store")

    def get_replay_receipt(self, receipt_id: str) -> Any:
        raise NotImplementedError("Replay Contracts require the PostgreSQL row store")

    def list_replay_receipts(self, limit: int = 100) -> Any:
        raise NotImplementedError("Replay Contracts require the PostgreSQL row store")

    def verify_replay_receipt(self, receipt_id: str) -> Any:
        raise NotImplementedError("Replay Contracts require the PostgreSQL row store")

    # -- Signatures (PostgreSQL row store only) -------------------------------------------------------

    def list_attestations(self, after_seq: int = 0, limit: int = 100) -> Any:
        raise NotImplementedError("Signatures require the PostgreSQL row store")

    def attestation_head(self) -> Any:
        raise NotImplementedError("Signatures require the PostgreSQL row store")

    def pending_attestations(self) -> Any:
        raise NotImplementedError("Signatures require the PostgreSQL row store")

    def verify_signatures(self) -> Any:
        raise NotImplementedError("Signatures require the PostgreSQL row store")

    def store_attestation(self, body: Any) -> Any:
        raise NotImplementedError("Signatures require the PostgreSQL row store")

    def list_trust_statements(self, after_seq: int = 0, limit: int = 100) -> Any:
        raise NotImplementedError("Signatures require the PostgreSQL row store")

    def trust_state(self) -> Any:
        raise NotImplementedError("Signatures require the PostgreSQL row store")

    def store_trust_statement(self, body: Any) -> Any:
        raise NotImplementedError("Signatures require the PostgreSQL row store")

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

    # Evidence Objects need the PostgreSQL row store (or the JSON file store); the legacy JSONB-blob ledger has neither.
    def put_evidence_object(self, req):
        raise NotImplementedError("evidence objects require the PostgreSQL row store")

    def get_evidence_object(self, oid):
        raise NotImplementedError("evidence objects require the PostgreSQL row store")

    def all_evidence_objects(self):
        raise NotImplementedError("evidence objects require the PostgreSQL row store")

    def _resolve_evidence(self, ref):
        return None  # none can exist here, so every evidence-object link is unresolved

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


def store_bootstrap_enabled() -> bool:
    """The local JSON file store is used only when JARVIS_STORE_BOOTSTRAP is explicitly on (1/true/yes/on)."""
    return (os.getenv("JARVIS_STORE_BOOTSTRAP") or "").strip().lower() in ("1", "true", "yes", "on")


def get_store(path: str | None = None) -> JarvisStore:
    """Return the request tenant's isolated ledger when OAuth public mode is active.

    With JARVIS_DATABASE_URL set this is the PostgreSQL store. Without it the call raises StoreUnavailableError
    (HTTP 503) unless JARVIS_STORE_BOOTSTRAP is on, so a missing database setting can never silently turn into a
    local JSON ledger.
    """
    requested = path or os.getenv("JARVIS_STORE_PATH", "data/jarvis-store.json")
    tenant = current_tenant_key()
    database_url = (os.getenv("JARVIS_DATABASE_URL") or "").strip()
    if database_url:
        database_tenant = tenant or "operator"
        mode = (os.getenv("JARVIS_PG_STORE") or "rows").strip().lower()
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
    if not store_bootstrap_enabled():
        # Fail closed: never fall back to a local JSON file (and never create a data/ folder) unless asked to.
        raise StoreUnavailableError(
            "No database is configured (JARVIS_DATABASE_URL) and the JSON file store is not enabled. "
            "Set JARVIS_STORE_BOOTSTRAP=1 to use the local JSON file store (first run or local development only)."
        )
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
