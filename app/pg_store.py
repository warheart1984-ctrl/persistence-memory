"""Row-level PostgreSQL ledger store (one row per record, optimistic locking, RLS).

Selected with ``JARVIS_PG_STORE=rows`` together with ``JARVIS_DATABASE_URL``.  The
application role must be an ordinary (non-superuser, non-BYPASSRLS) role; every
transaction sets ``jarvis.tenant_key`` so row-level security is a second tenant fence
behind the explicit ``tenant_key`` filters in each query.

Fail-closed: any database error becomes ``StoreUnavailableError`` (HTTP 503); there is
never a fallback to another store.  Details are logged on ``jarvis.store`` only.
"""

from __future__ import annotations

import contextlib
import logging
import os
import random
import re
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable, Iterator

import psycopg
from psycopg import sql
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from app.continuity import detect_conflicts, content_sha256
from app.models import (
    ConflictSet,
    MemoryBoard,
    MemoryCreate,
    MemoryRecord,
    MemoryUpdate,
    SelectionProvenance,
)
from app.pg_schema import check_schema_version, validate_schema_name
from app.store import _make_id, ledger_retrieve, memory_matches_query
from app.store_errors import StoreUnavailableError, StoreVersionConflict

_log = logging.getLogger("jarvis.store")

MAX_UPDATE_ATTEMPTS = 5

_COLUMNS = (
    "id, version, content, content_sha256, created_at, updated_at, source_agent, session_id, "
    "type, status, confidence, subject, supersedes, tags, evidence"
)

_pools: dict[tuple[str, str], ConnectionPool] = {}
_ready: set[tuple[str, str]] = set()
_legacy_ok: set[tuple[tuple[str, str], str]] = set()
_pools_lock = threading.Lock()


def _env_int(name: str, default: int) -> int:
    try:
        return max(1, int(os.getenv(name, "") or default))
    except ValueError:
        return default


def _connect_timeout() -> float:
    return float(_env_int("JARVIS_DATABASE_CONNECT_TIMEOUT", 5))


def _pool_for(dsn: str, schema: str | None) -> tuple[ConnectionPool, tuple[str, str]]:
    key = (dsn, schema or "")
    with _pools_lock:
        pool = _pools.get(key)
        if pool is None:
            options = [
                "-c timezone=UTC",
                f"-c statement_timeout={_env_int('JARVIS_DATABASE_STATEMENT_TIMEOUT_MS', 10_000)}",
                f"-c lock_timeout={_env_int('JARVIS_DATABASE_LOCK_TIMEOUT_MS', 5_000)}",
            ]
            if schema:
                options.append(f"-c search_path={validate_schema_name(schema)}")
            pool_max = _env_int("JARVIS_DATABASE_POOL_MAX", 10)
            pool = ConnectionPool(
                dsn,
                min_size=1,
                max_size=pool_max,  # fixed: the pool never grows beyond this
                # A request that finds every connection busy waits at most this long (default 1 s) and
                # then fails with 503; at most max_waiting requests may wait at all, the rest fail at
                # once (TooManyRequests).  Saturation is shed, never queued behind a slow database.
                timeout=_env_int("JARVIS_DATABASE_POOL_TIMEOUT_MS", 1000) / 1000.0,
                max_waiting=_env_int("JARVIS_DATABASE_POOL_MAX_WAITING", pool_max),
                kwargs={"options": " ".join(options), "connect_timeout": int(_connect_timeout())},
                check=ConnectionPool.check_connection,
                open=False,
            )
            pool.open(wait=False)
            _pools[key] = pool
        return pool, key


def close_pools() -> None:
    """Close every pooled connection (tests, shutdown)."""
    with _pools_lock:
        pools = list(_pools.values())
        _pools.clear()
        _ready.clear()
        _legacy_ok.clear()
    for pool in pools:
        with contextlib.suppress(Exception):
            pool.close(timeout=2)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _record(row: dict[str, Any]) -> MemoryRecord:
    return MemoryRecord(
        id=row["id"],
        version=row["version"],
        content=row["content"],
        content_sha256=row["content_sha256"],
        created_at=_iso(row["created_at"]),
        updated_at=_iso(row["updated_at"]),
        source_agent=row["source_agent"],
        session_id=row["session_id"],
        type=row["type"],
        status=row["status"],
        confidence=row["confidence"],
        subject=row["subject"],
        supersedes=row["supersedes"],
        tags=list(row["tags"] or []),
        evidence=row["evidence"] or [],
    )


class PostgresRowStore:
    """Per-tenant ledger over the row-level schema in ``app.pg_schema``."""

    def __init__(self, dsn: str, tenant_key: str, *, schema: str | None = None):
        if not tenant_key:
            raise ValueError("tenant_key is required")
        self._dsn = dsn
        self._tenant_key = tenant_key
        self._schema = schema
        # Test seam: called after update_memory reads the row and before it writes.
        self._after_read_hook: Callable[[], None] | None = None

    # -- plumbing ---------------------------------------------------------------

    @contextlib.contextmanager
    def _tx(self) -> Iterator[psycopg.Connection]:
        """One transaction as the tenant; every database failure fails closed."""
        pool, key = _pool_for(self._dsn, self._schema)
        try:
            with pool.connection() as conn:
                conn.row_factory = dict_row
                conn.execute("SELECT set_config('jarvis.tenant_key', %s, true)", (self._tenant_key,))
                conn.execute("SELECT set_config('jarvis.actor', %s, true)", (self._tenant_key,))
                if key not in _ready:
                    check_schema_version(conn)
                    _require_rls_enforced(conn)
                    _ready.add(key)
                if (key, self._tenant_key) not in _legacy_ok:
                    _refuse_if_legacy_data_unimported(conn, self._tenant_key)
                    _legacy_ok.add((key, self._tenant_key))
                yield conn
        except StoreUnavailableError:
            raise
        except psycopg.Error as exc:
            _log.error("ledger database error (%s): %s", type(exc).__name__, exc)
            raise StoreUnavailableError("Ledger database error") from exc

    # -- readiness --------------------------------------------------------------

    def readiness(self) -> dict[str, str]:
        """Live readiness checks for /ready (never cached): name -> "ok" | "failed".

        Uses the same bounded pool checkout as requests.  Each check runs in its own savepoint so one
        failure cannot hide the others; details go to the log, never into the result.
        """
        names = ("database", "schema_version", "role", "history_write_denied", "legacy_data")
        checks = {name: "failed" for name in names}
        try:
            pool, _ = _pool_for(self._dsn, self._schema)
            with pool.connection() as conn:
                conn.row_factory = dict_row
                conn.execute("SELECT set_config('jarvis.tenant_key', %s, true)", (self._tenant_key,))

                def run(name: str, check: Callable[[], bool | None]) -> None:
                    try:
                        with conn.transaction():
                            ok = check()
                        checks[name] = "ok" if ok is not False else "failed"
                    except (StoreUnavailableError, psycopg.Error) as exc:
                        _log.error("readiness check %s failed (%s): %s", name, type(exc).__name__, exc)

                run("database", lambda: conn.execute("SELECT 1").fetchone() is not None)
                run("schema_version", lambda: check_schema_version(conn))
                run("role", lambda: _require_rls_enforced(conn))
                run("history_write_denied", lambda: _history_is_unwritable(conn))
                run("legacy_data", lambda: _refuse_if_legacy_data_unimported(conn, self._tenant_key))
                conn.rollback()  # nothing above may leave anything behind
        except (psycopg.Error, ValueError) as exc:
            _log.error("readiness: cannot use the database (%s): %s", type(exc).__name__, exc)
        return checks

    # -- board ------------------------------------------------------------------

    def get_board(self) -> MemoryBoard:
        with self._tx() as conn:
            row = conn.execute(
                "SELECT board FROM boards WHERE tenant_key = %s", (self._tenant_key,)
            ).fetchone()
        return MemoryBoard(**row["board"]) if row else MemoryBoard()

    def set_board(self, board: MemoryBoard) -> MemoryBoard:
        with self._tx() as conn:
            conn.execute(
                "INSERT INTO boards (tenant_key, board) VALUES (%s, %s) "
                "ON CONFLICT (tenant_key) DO UPDATE SET board = EXCLUDED.board",
                (self._tenant_key, Jsonb(board.model_dump())),
            )
        return board

    def patch_board(self, updates: dict[str, Any]) -> MemoryBoard:
        with self._tx() as conn:
            conn.execute(
                "INSERT INTO boards (tenant_key, board) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                (self._tenant_key, Jsonb(MemoryBoard().model_dump())),
            )
            row = conn.execute(
                "SELECT board FROM boards WHERE tenant_key = %s FOR UPDATE", (self._tenant_key,)
            ).fetchone()
            current = dict(row["board"])
            for key, value in updates.items():
                if value is not None:
                    current[key] = value
            board = MemoryBoard(**current)
            conn.execute(
                "UPDATE boards SET board = %s WHERE tenant_key = %s",
                (Jsonb(board.model_dump()), self._tenant_key),
            )
        return board

    # -- reads ------------------------------------------------------------------

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
        where = ["tenant_key = %s"]
        params: list[Any] = [self._tenant_key]
        if truth_scope:
            lower = truth_scope.lower()
            if lower == "live":
                where.append("status <> 'archived'")
            else:
                where.append("(status = %s OR source_agent = %s)")
                params += [lower, lower]
        for column, value in (
            ("type", memory_type), ("status", status), ("session_id", session_id), ("subject", subject)
        ):
            if value:
                where.append(f"{column} = %s")
                params.append(value)
        sql_text = (
            f"SELECT {_COLUMNS} FROM memories WHERE {' AND '.join(where)} "
            'ORDER BY created_at DESC, id COLLATE "C" DESC'
        )
        if not query:
            sql_text += " LIMIT %s"
            params.append(max(0, int(limit)))
        with self._tx() as conn:
            rows = conn.execute(sql_text, params).fetchall()
        records = [_record(r) for r in rows]
        if query:
            q = query.lower()
            records = [m for m in records if memory_matches_query(m, q)][: max(0, int(limit))]
        return records

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
        with self._tx() as conn:
            row = conn.execute(
                f"SELECT {_COLUMNS} FROM memories WHERE tenant_key = %s AND id = %s",
                (self._tenant_key, memory_id),
            ).fetchone()
        return _record(row) if row else None

    def conflicts(self, subject: str | None = None) -> list[ConflictSet]:
        return detect_conflicts(self.list_memories(limit=10**9), subject=subject)

    # -- history ----------------------------------------------------------------

    def history(self, memory_id: str, limit: int = 200) -> list[dict[str, Any]]:
        """Append-only change log for one record (survives deletion), oldest first."""
        with self._tx() as conn:
            rows = conn.execute(
                "SELECT history_id, seq, memory_id, version, op, actor, changed_at, before, after, "
                "prev_hash, row_hash FROM record_history "
                "WHERE tenant_key = %s AND memory_id = %s ORDER BY history_id LIMIT %s",
                (self._tenant_key, memory_id, max(1, min(int(limit), 1000))),
            ).fetchall()
        for row in rows:
            row["changed_at"] = _iso(row["changed_at"])
        return rows

    def verify_history(self, memory_id: str | None = None) -> list[dict[str, Any]]:
        """Recompute the hash chain and compare live rows to it; [] means intact."""
        with self._tx() as conn:
            return conn.execute(
                "SELECT history_id, memory_id, problem FROM jarvis_verify_history(%s, %s)",
                (self._tenant_key, memory_id),
            ).fetchall()

    # -- writes -----------------------------------------------------------------

    def _supersedes_exists(self, conn: psycopg.Connection, target: str) -> bool:
        return (
            conn.execute(
                "SELECT 1 FROM memories WHERE tenant_key = %s AND id = %s", (self._tenant_key, target)
            ).fetchone()
            is not None
        )

    def create_memory(self, data: MemoryCreate) -> MemoryRecord:
        now = datetime.now(timezone.utc)
        for _ in range(3):
            rec_id = _make_id("mem")
            try:
                with self._tx() as conn:
                    if data.supersedes and not self._supersedes_exists(conn, data.supersedes):
                        raise ValueError(f"supersedes target not found: {data.supersedes}")
                    try:
                        row = conn.execute(
                            "INSERT INTO memories (tenant_key, id, content, content_sha256, created_at, "
                            "updated_at, source_agent, session_id, type, status, confidence, subject, "
                            "supersedes, tags, evidence) "
                            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::text[],%s) "
                            f"RETURNING {_COLUMNS}",
                            (
                                self._tenant_key, rec_id, data.content, content_sha256(data.content),
                                now, now, data.source_agent, data.session_id, data.type, data.status,
                                data.confidence, data.subject, data.supersedes or None, list(data.tags),
                                Jsonb([e.model_dump() for e in data.evidence]),
                            ),
                        ).fetchone()
                    except psycopg.errors.ForeignKeyViolation as exc:  # target deleted mid-flight
                        raise ValueError(f"supersedes target not found: {data.supersedes}") from exc
                return _record(row)
            except psycopg.errors.UniqueViolation:  # pragma: no cover - 48-bit id collision
                continue
        raise StoreUnavailableError("Could not allocate a unique memory id")  # pragma: no cover

    def update_memory(self, memory_id: str, data: MemoryUpdate) -> MemoryRecord | None:
        """Read-modify-write guarded by the row version; retried on concurrent change."""
        for attempt in range(MAX_UPDATE_ATTEMPTS):
            with self._tx() as conn:
                row = conn.execute(
                    f"SELECT {_COLUMNS} FROM memories WHERE tenant_key = %s AND id = %s",
                    (self._tenant_key, memory_id),
                ).fetchone()
                if row is None:
                    return None
                existing = _record(row)
                if data.expected_version is not None and data.expected_version != existing.version:
                    raise StoreVersionConflict(
                        f"version conflict: expected {data.expected_version}, current {existing.version}"
                    )
                if self._after_read_hook is not None:
                    self._after_read_hook()
                updates = existing.model_dump()
                for key in (
                    "content", "source_agent", "session_id", "type", "confidence", "evidence",
                    "supersedes", "status", "subject", "tags",
                ):
                    value = getattr(data, key, None)
                    if value is not None:
                        updates[key] = (
                            [e.model_dump() if hasattr(e, "model_dump") else e for e in value]
                            if key == "evidence"
                            else value
                        )
                if updates["supersedes"] == "":
                    updates["supersedes"] = None
                if updates["supersedes"] and not self._supersedes_exists(conn, updates["supersedes"]):
                    raise ValueError(f"supersedes target not found: {updates['supersedes']}")
                if data.content is not None:
                    updates["content_sha256"] = content_sha256(data.content)
                now = datetime.now(timezone.utc)
                new = MemoryRecord(**{**updates, "updated_at": _iso(now)})
                try:
                    changed = conn.execute(
                        "UPDATE memories SET content=%s, content_sha256=%s, updated_at=%s, source_agent=%s, "
                        "session_id=%s, type=%s, status=%s, confidence=%s, subject=%s, supersedes=%s, "
                        "tags=%s::text[], evidence=%s "
                        "WHERE tenant_key=%s AND id=%s AND version=%s "
                        f"RETURNING {_COLUMNS}",
                        (
                            new.content, new.content_sha256, now, new.source_agent, new.session_id,
                            new.type, new.status, new.confidence, new.subject, new.supersedes,
                            list(new.tags), Jsonb([e.model_dump() for e in new.evidence]),
                            self._tenant_key, memory_id, existing.version,
                        ),
                    ).fetchone()
                except psycopg.errors.ForeignKeyViolation as exc:
                    raise ValueError(f"supersedes target not found: {new.supersedes}") from exc
            if changed is not None:
                return _record(changed)
            if data.expected_version is not None:
                raise StoreVersionConflict("version conflict: record changed during update")
            time.sleep(random.uniform(0, min(0.25, 0.005 * 2**attempt)))  # jittered exponential backoff
        raise StoreVersionConflict("version conflict: too much concurrent modification, retry")

    def delete_memory(self, memory_id: str) -> bool:
        with self._tx() as conn:
            row = conn.execute(
                "DELETE FROM memories WHERE tenant_key = %s AND id = %s RETURNING id",
                (self._tenant_key, memory_id),
            ).fetchone()
        return row is not None


def _require_rls_enforced(conn: psycopg.Connection) -> None:
    """Refuse to serve as a role that bypasses row-level security (fail closed, 503)."""
    row = conn.execute(
        "SELECT rolsuper OR rolbypassrls AS bypass FROM pg_roles WHERE rolname = current_user"
    ).fetchone()
    if row is None or row["bypass"]:
        _log.error(
            "ledger database role is a superuser or has BYPASSRLS; refusing to serve. "
            "Connect as an ordinary role (JARVIS_DATABASE_URL); use JARVIS_DATABASE_MIGRATE_URL for DDL."
        )
        raise StoreUnavailableError("Ledger database role must not bypass row-level security")


def _refuse_if_legacy_data_unimported(conn: psycopg.Connection, tenant: str) -> None:
    """Fail closed rather than quietly serve an empty ledger next to an un-imported legacy one.

    The older JSONB store kept one document per tenant in ``jarvis_tenant_ledgers``.  If that
    tenant still has records there while the row store has none, someone switched stores without
    importing; serving (and then writing to) an empty ledger would look like data loss.
    Override with JARVIS_PG_IGNORE_LEGACY_BLOB=1.  A role that cannot read the old table is
    skipped: there is nothing it could be protecting.
    """
    if os.getenv("JARVIS_PG_IGNORE_LEGACY_BLOB", "").strip().lower() in ("1", "true", "yes", "on"):
        return
    blob_schema = validate_schema_name((os.getenv("JARVIS_LEGACY_BLOB_SCHEMA") or "public").strip())
    present = conn.execute(
        "SELECT to_regclass(%s) IS NOT NULL AS present", (f"{blob_schema}.jarvis_tenant_ledgers",)
    ).fetchone()["present"]
    if not present:
        return
    try:
        with conn.transaction():  # savepoint: a permission error must not poison the request's transaction
            row = conn.execute(
                sql.SQL(
                    "SELECT coalesce(jsonb_array_length(CASE WHEN jsonb_typeof(payload->'memories') = 'array' "
                    "THEN payload->'memories' END), 0) AS n FROM {}.jarvis_tenant_ledgers WHERE tenant_key = %s"
                ).format(sql.Identifier(blob_schema)),
                (tenant,),
            ).fetchone()
    except psycopg.errors.InsufficientPrivilege:
        return
    if row is None or row["n"] == 0:
        return
    populated = conn.execute(
        "SELECT EXISTS (SELECT 1 FROM memories) OR EXISTS (SELECT 1 FROM record_history) AS populated"
    ).fetchone()["populated"]
    if populated:
        return
    _log.error(
        "tenant has %s record(s) in the legacy jarvis_tenant_ledgers blob but the row store is empty; "
        "import them (python -m app.pg_import --source-blob <tenant_key> --tenant <tenant_key> --apply), "
        "set JARVIS_PG_STORE=blob to keep the old store, or set JARVIS_PG_IGNORE_LEGACY_BLOB=1",
        row["n"],
    )
    raise StoreUnavailableError("Legacy blob ledger exists but the row store is empty; import it first")

_HISTORY_TABLES = ("record_history", "chain_heads", "history_counters")
_PROBE_INSERTS = {
    "record_history": (
        "INSERT INTO record_history (tenant_key, memory_id, version, op, actor, after, prev_hash, row_hash, seq) "
        "VALUES (current_setting('jarvis.tenant_key'), 'readiness-probe', 1, 'backfill', 'probe', '{}'::jsonb, "
        "repeat('0', 64), repeat('0', 64), 1)"
    ),
    "chain_heads": (
        "INSERT INTO chain_heads (tenant_key, id, last_seq, last_hash, deleted) "
        "VALUES (current_setting('jarvis.tenant_key'), 'readiness-probe', 1, repeat('0', 64), false)"
    ),
    "history_counters": (
        "INSERT INTO history_counters (tenant_key, last_seq) VALUES (current_setting('jarvis.tenant_key'), 1)"
    ),
}


class _ProbeSucceeded(Exception):
    """A write that must have been refused went through; raised only to roll its savepoint back."""


def _history_is_unwritable(conn: psycopg.Connection) -> bool:
    """Prove the connected role cannot write the history tables (True only when proven).

    Two independent proofs per table: the catalog says the role holds none of INSERT/UPDATE/DELETE/
    TRUNCATE, and an actual attempt at each of INSERT/UPDATE/DELETE is refused with *permission
    denied* (inside a savepoint that is rolled back even if the attempt unexpectedly succeeds, so the
    probe never leaves a row behind).  TRUNCATE is checked in the catalog only: attempting it would
    take an ACCESS EXCLUSIVE lock and could stall live traffic.  Any other outcome (success, a different
    error) means "cannot prove" and fails closed.
    """
    for table in _HISTORY_TABLES:
        for privilege in ("INSERT", "UPDATE", "DELETE", "TRUNCATE"):
            held = conn.execute(
                "SELECT has_table_privilege(current_user, %s, %s) AS held", (table, privilege)
            ).fetchone()["held"]
            if held:
                _log.error("ledger role holds %s on %s; it must be read-only there", privilege, table)
                return False
        ident = sql.Identifier(table)
        attempts = (
            sql.SQL(_PROBE_INSERTS[table]),
            sql.SQL("UPDATE {} SET tenant_key = tenant_key WHERE false").format(ident),
            sql.SQL("DELETE FROM {} WHERE false").format(ident),
        )
        for attempt in attempts:
            try:
                with conn.transaction():
                    conn.execute(attempt)
                    raise _ProbeSucceeded()
            except _ProbeSucceeded:
                _log.error("a probe write to %s succeeded; the ledger role must be read-only there", table)
                return False
            except psycopg.errors.InsufficientPrivilege as exc:
                message = getattr(exc.diag, "message_primary", "") or ""
                if not message.startswith("permission denied"):  # e.g. a row-level-security refusal
                    _log.error("probe on %s was refused for another reason: %s", table, message)
                    return False
            except psycopg.Error as exc:
                _log.error("probe on %s failed unexpectedly (%s)", table, type(exc).__name__)
                return False
    return True
