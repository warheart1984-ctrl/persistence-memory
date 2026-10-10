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
from psycopg.rows import dict_row, tuple_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from app.continuity import detect_conflicts, content_sha256
from app.models import (
    TWIN_AGENT,
    ConflictSet,
    MemoryBoard,
    MemoryCreate,
    MemoryRecord,
    MemoryUpdate,
    SelectionProvenance,
)
from app.pg_schema import check_schema_version, validate_schema_name
from app.store import _make_id, ledger_retrieve, memory_matches_query
from app.store_errors import InvalidInputError, StoreUnavailableError, StoreVersionConflict
from app import attest
from app import clause_v
from app import evidence as evidence_objects

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


def _link_kwargs() -> dict[str, int]:
    """Bound how long a silently dead link (a dropped network, a vanished peer) can hold a request.

    statement_timeout is enforced by the SERVER, so it cannot help when the server is unreachable, and the pool timeout only bounds waiting for a free
    connection.  A request that picks up an established connection whose peer has gone silent therefore hung for as long as TCP kept retrying (the
    chaos partition fault measured 52 s, /ready included).  Keepalives find a peer that has gone quiet while we wait for its answer
    (idle + interval x count = 7 s by default); tcp_user_timeout fails data that is sent and never acknowledged (5 s).  Either way the connection
    errors, the request becomes a 503 (fail closed), and the pool replaces the connection."""
    return {
        "keepalives": 1,
        "keepalives_idle": _env_int("JARVIS_DATABASE_KEEPALIVES_IDLE_S", 3),
        "keepalives_interval": _env_int("JARVIS_DATABASE_KEEPALIVES_INTERVAL_S", 2),
        "keepalives_count": _env_int("JARVIS_DATABASE_KEEPALIVES_COUNT", 2),
        "tcp_user_timeout": _env_int("JARVIS_DATABASE_TCP_USER_TIMEOUT_MS", 5000),
    }


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
                kwargs={"options": " ".join(options), "connect_timeout": int(_connect_timeout()), **_link_kwargs()},
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
        except (StoreUnavailableError, InvalidInputError):
            raise
        except psycopg.DataError as exc:
            if "NUL" in str(exc):  # PostgreSQL text cannot hold 0x00: the input is at fault, the ledger is fine
                raise InvalidInputError("a text value contains a NUL (0x00) byte, which the ledger cannot store") from exc
            _log.error("ledger database error (%s): %s", type(exc).__name__, exc)
            raise StoreUnavailableError("Ledger database error") from exc
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

    def list_latest(
        self,
        *,
        limit: int,
        after: tuple[datetime, str] | None = None,
        memory_type: str | None = None,
        include_superseded: bool = False,
        include_archived: bool = False,
        include_twin: bool = False,
        tokens: list[str] | None = None,
    ) -> list[tuple[MemoryRecord, str | None]]:
        """Newest-first keyset page; same contract as ``JarvisStore.list_latest``."""
        where = ["m.tenant_key = %s"]
        params: list[Any] = [self._tenant_key]
        if memory_type:
            where.append("m.type = %s")
            params.append(memory_type)
        if not include_twin:
            where.append("m.source_agent <> %s")
            params.append(TWIN_AGENT)
        if not include_archived:
            where.append("m.status <> 'archived'")
        if not include_superseded:
            where.append(
                "NOT EXISTS (SELECT 1 FROM memories s WHERE s.tenant_key = m.tenant_key AND s.supersedes = m.id)"
            )
        if after is not None:
            where.append('(m.created_at, m.id COLLATE "C") < (%s, %s COLLATE "C")')
            params += [after[0], after[1]]
        if tokens:
            # Uses memories_search_idx (V9); the same tokens as app/ledger_search.py::record_tokens.
            where.append("jarvis_search_tokens(m.subject, m.content, m.tags) @> %s::text[]")
            params.append(list(tokens))
        sql_text = (
            f"SELECT {', '.join('m.' + c.strip() for c in _COLUMNS.split(','))}, "
            "(SELECT s.id FROM memories s WHERE s.tenant_key = m.tenant_key AND s.supersedes = m.id "
            ' ORDER BY s.created_at DESC, s.id COLLATE "C" DESC LIMIT 1) AS superseded_by '
            f"FROM memories m WHERE {' AND '.join(where)} "
            'ORDER BY m.created_at DESC, m.id COLLATE "C" DESC LIMIT %s'
        )
        params.append(max(0, int(limit)))
        with self._tx() as conn:
            rows = conn.execute(sql_text, params).fetchall()
        return [(_record(r), r["superseded_by"]) for r in rows]

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

    # -- Continuity Blocks ------------------------------------------------------

    _BLOCK_COLUMNS = ("height, first_seq, last_seq, entry_count, prev_block_hash, entries_root, block_hash, "
                      "format, sealed_at, sealed_by")

    @staticmethod
    def _block(row: dict[str, Any]) -> dict[str, Any]:
        out = dict(row)
        out["sealed_at"] = _iso(out["sealed_at"])
        return out

    def seal_blocks(self, *, force: bool = False, min_entries: int = 500, max_age_seconds: int = 3600,
                    max_entries: int = 10000, max_blocks: int = 20) -> dict[str, Any]:
        """Seal the unsealed tail, one block per transaction, until nothing is left that meets the rules.

        Returns {"sealed": [blocks], "reason": why it stopped, "head": block_head()}.  The hashes are computed by
        ``jarvis_seal_block`` in the database; nothing here supplies one."""
        sealed: list[dict[str, Any]] = []
        reason = "nothing new to seal"
        for _ in range(max(1, int(max_blocks))):
            with self._tx() as conn:
                row = conn.execute(
                    "SELECT sealed, height, first_seq, last_seq, entry_count, block_hash, reason "
                    "FROM jarvis_seal_block(%s, %s, make_interval(secs => %s), %s, %s)",
                    (self._tenant_key, int(min_entries), int(max_age_seconds), bool(force), int(max_entries)),
                ).fetchone()
                if row["sealed"]:
                    full = conn.execute(
                        f"SELECT {self._BLOCK_COLUMNS} FROM blocks WHERE tenant_key = %s AND height = %s",
                        (self._tenant_key, row["height"]),
                    ).fetchone()
            reason = row["reason"]
            if not row["sealed"]:
                break
            sealed.append(self._block(full))
        return {"sealed": sealed, "reason": reason, "head": self.block_head()}

    def list_blocks(self, after_height: int = 0, limit: int = 100) -> list[dict[str, Any]]:
        with self._tx() as conn:
            rows = conn.execute(
                f"SELECT {self._BLOCK_COLUMNS} FROM blocks WHERE tenant_key = %s AND height > %s ORDER BY height LIMIT %s",
                (self._tenant_key, max(0, int(after_height)), max(1, min(int(limit), 1000))),
            ).fetchall()
        return [self._block(r) for r in rows]

    def get_block(self, height: int) -> dict[str, Any] | None:
        with self._tx() as conn:
            row = conn.execute(
                f"SELECT {self._BLOCK_COLUMNS} FROM blocks WHERE tenant_key = %s AND height = %s",
                (self._tenant_key, int(height)),
            ).fetchone()
        return self._block(row) if row else None

    def block_head(self) -> dict[str, Any]:
        """The newest block and what is not sealed yet."""
        with self._tx() as conn:
            tip = conn.execute(
                f"SELECT {self._BLOCK_COLUMNS} FROM blocks WHERE tenant_key = %s ORDER BY height DESC LIMIT 1",
                (self._tenant_key,),
            ).fetchone()
            sealed_seq = tip["last_seq"] if tip else 0
            counter = conn.execute(
                "SELECT last_seq FROM history_counters WHERE tenant_key = %s", (self._tenant_key,)
            ).fetchone()
            oldest = conn.execute(
                "SELECT min(changed_at) AS oldest FROM record_history WHERE tenant_key = %s AND seq > %s",
                (self._tenant_key, sealed_seq),
            ).fetchone()
        history_seq = counter["last_seq"] if counter else 0
        return {
            "tip": self._block(tip) if tip else None,
            "sealed_seq": sealed_seq,
            "history_seq": history_seq,
            "unsealed_entries": max(0, history_seq - sealed_seq),
            "oldest_unsealed_at": _iso(oldest["oldest"]) if oldest and oldest["oldest"] else None,
        }

    def history_seq(self) -> int:
        """This tenant's newest history sequence number (0 when nothing has been written); a plain read."""
        with self._tx() as conn:
            counter = conn.execute(
                "SELECT last_seq FROM history_counters WHERE tenant_key = %s", (self._tenant_key,)
            ).fetchone()
        return int(counter["last_seq"]) if counter else 0

    def verify_blocks(self) -> list[dict[str, Any]]:
        """The database's verifier plus the independent recomputation and the cited-evidence check (the same
        code ``python -m app.pg_verify`` runs); [] means intact."""
        from app import pg_verify  # local import: pg_verify is a command-line module that imports the schema code

        with self._tx() as conn:
            conn.row_factory = tuple_row
            problems = pg_verify._block_problems(conn, self._tenant_key) + pg_verify._block_evidence_problems(conn, self._tenant_key)
        return [{"block": what, "problem": problem} for what, problem in problems]

    # -- Replay Contracts (RC.Ledger.v1) ----------------------------------------

    def _resolve_replay_seq(self, conn: psycopg.Connection, at_seq: int | None, at_block: int | None) -> int:
        from app import replay

        if at_seq is not None and at_block is not None:
            raise replay.ReplayError("replay_bound_ambiguous", "give at_seq or at_block, not both")
        counter = conn.execute("SELECT last_seq FROM history_counters WHERE tenant_key = %s", (self._tenant_key,)).fetchone()
        history_seq = counter["last_seq"] if counter else 0
        if at_block is not None:
            row = conn.execute("SELECT last_seq FROM blocks WHERE tenant_key = %s AND height = %s", (self._tenant_key, at_block)).fetchone()
            if row is None:
                raise replay.ReplayError("replay_block_not_found", f"there is no sealed block {at_block}", 404)
            return row["last_seq"]
        if at_seq is None:
            return history_seq
        if at_seq > history_seq:
            raise replay.ReplayError("replay_seq_out_of_range", f"at_seq {at_seq} is beyond the history counter {history_seq}")
        return at_seq

    def replay_state(self, *, at_seq: int | None = None, at_block: int | None = None, after_id: str | None = None,
                     limit: int = 200) -> "replay.ReplayState":
        """RC.Ledger.v1: the ledger's records as of a history seq (or the end of a sealed block), with the state root.

        One transaction, so the counter, the blocks and the history it reads are one consistent picture."""
        from app import replay

        limit = max(1, min(int(limit), replay.MAX_PAGE))
        with self._tx() as conn:
            seq = self._resolve_replay_seq(conn, at_seq, at_block)
            counter = conn.execute("SELECT last_seq FROM history_counters WHERE tenant_key = %s", (self._tenant_key,)).fetchone()
            sealed_seq = conn.execute("SELECT coalesce(max(last_seq), 0) AS s FROM blocks WHERE tenant_key = %s", (self._tenant_key,)).fetchone()["s"]
            block = conn.execute(
                "SELECT height, first_seq, last_seq, block_hash FROM blocks WHERE tenant_key = %s AND first_seq <= %s AND last_seq >= %s",
                (self._tenant_key, seq, seq)).fetchone()
            heads = conn.execute(
                "SELECT DISTINCT ON (memory_id COLLATE \"C\") memory_id, seq, op, row_hash FROM record_history "
                "WHERE tenant_key = %s AND seq <= %s ORDER BY memory_id COLLATE \"C\", seq DESC",
                (self._tenant_key, seq)).fetchall()
            live = sorted((h for h in heads if h["op"] != "delete"), key=lambda h: h["memory_id"].encode("utf-8"))
            page = [h for h in live if after_id is None or h["memory_id"].encode("utf-8") > after_id.encode("utf-8")][: limit + 1]
            more = len(page) > limit
            page = page[:limit]
            images = {}
            if page:
                images = {r["seq"]: r for r in conn.execute(
                    "SELECT seq, version, after FROM record_history WHERE tenant_key = %s AND seq = ANY(%s)",
                    (self._tenant_key, [h["seq"] for h in page])).fetchall()}
        block_ref = replay.BlockRef(**block) if block else None
        if block_ref is not None:
            block_ref.signed, block_ref.signature_level = self._block_signature(block_ref.height, block_ref.block_hash)
        return replay.ReplayState(
            tenant=self._tenant_key, at_seq=seq, history_seq=counter["last_seq"] if counter else 0, sealed_seq=sealed_seq,
            sealed=block is not None, at_block_boundary=block is not None and block["last_seq"] == seq,
            block=block_ref,
            record_count=len(live), deleted_count=len(heads) - len(live),
            state_root=replay.state_root([(h["memory_id"], h["row_hash"]) for h in live]),
            records=[replay.ReplayedRecord(id=h["memory_id"], seq=h["seq"], version=images[h["seq"]]["version"],
                                           row_hash=h["row_hash"], record=images[h["seq"]]["after"]) for h in page],
            next_after_id=page[-1]["memory_id"] if more else None,
        )

    def replay_events(self, *, from_seq: int = 1, to_seq: int | None = None, limit: int = 200) -> "replay.ReplayEvents":
        """RC.Ledger.v1: the ordered history entries from from_seq to to_seq, evidence links classified."""
        from app import replay

        limit = max(1, min(int(limit), replay.MAX_PAGE))
        if from_seq < 1:
            raise replay.ReplayError("replay_seq_out_of_range", "from_seq starts at 1")
        with self._tx() as conn:
            counter = conn.execute("SELECT last_seq FROM history_counters WHERE tenant_key = %s", (self._tenant_key,)).fetchone()
            history_seq = counter["last_seq"] if counter else 0
            end = history_seq if to_seq is None else to_seq
            if end > history_seq:
                raise replay.ReplayError("replay_seq_out_of_range", f"to_seq {end} is beyond the history counter {history_seq}")
            rows = conn.execute(
                "SELECT seq, memory_id, op, version, actor, changed_at, prev_hash, row_hash, before, after FROM record_history "
                "WHERE tenant_key = %s AND seq BETWEEN %s AND %s ORDER BY seq LIMIT %s",
                (self._tenant_key, from_seq, end, limit + 1)).fetchall()
            more = len(rows) > limit
            rows = rows[:limit]
            refs = sorted({link.get("ref") for r in rows for link in
                           replay.evidence_links(r["after"]) + replay.evidence_links(r["before"])
                           if link.get("kind") == "evidence-object" and link.get("ref")})
            objects = {}
            if refs:
                for o in conn.execute(f"SELECT {self._EVIDENCE_COLUMNS} FROM evidence_objects WHERE tenant_key = %s AND id = ANY(%s)",
                                      (self._tenant_key, refs)).fetchall():
                    objects[o["id"]] = self._evidence_from_row(o)

        def status(link: dict[str, Any]) -> str:
            if link.get("kind") != "evidence-object":
                return "not-checked"
            obj = objects.get(link.get("ref"))
            if obj is None:
                return "missing"
            return "tampered" if evidence_objects.verify_stored(obj) else "intact"

        events = []
        for r in rows:
            links = replay.evidence_links(r["after"] if r["after"] is not None else r["before"])
            events.append(replay.ReplayEvent(
                seq=r["seq"], memory_id=r["memory_id"], op=r["op"], version=r["version"], actor=r["actor"],
                changed_at=_iso(r["changed_at"]), prev_hash=r["prev_hash"], row_hash=r["row_hash"],
                before=r["before"], after=r["after"],
                evidence=[replay.EvidenceStatus(kind=str(l.get("kind", "")), ref=str(l.get("ref", "")), note=l.get("note"), status=status(l))
                          for l in links]))
        return replay.ReplayEvents(tenant=self._tenant_key, from_seq=from_seq, to_seq=end, history_seq=history_seq, events=events,
                                   next_from_seq=rows[-1]["seq"] + 1 if more else None)

    # -- Signatures: attestations and trust statements (verification side; nothing here signs) ----------

    _ATT_COLUMNS = "signer_seq, kind, subject, subject_hash, prev_hash, key_id, signed_at, signature, attestation_hash, stored_at"
    _STMT_COLUMNS = "stmt_seq, kind, key_id, pubkey, arg, subject_hash, prev_hash, signed_by, signature, statement_hash, stored_at"

    @staticmethod
    def _plain(row: tuple[Any, ...], names: str) -> dict[str, Any]:
        out = dict(zip([n.strip() for n in names.split(",")], row))
        if "stored_at" in out:
            out["stored_at"] = _iso(out["stored_at"])
        return out

    def list_attestations(self, after_seq: int = 0, limit: int = 100) -> list[dict[str, Any]]:
        with self._tx() as conn:
            conn.row_factory = tuple_row
            rows = conn.execute(
                f"SELECT {self._ATT_COLUMNS} FROM attestations WHERE tenant_key = %s AND signer_seq > %s ORDER BY signer_seq LIMIT %s",
                (self._tenant_key, max(0, int(after_seq)), max(1, min(int(limit), 1000)))).fetchall()
        return [self._plain(r, self._ATT_COLUMNS) for r in rows]

    def attestation_head(self) -> dict[str, Any]:
        """Where the signing log ends: the next signer_seq and the hash the next attestation must name as prev_hash."""
        with self._tx() as conn:
            conn.row_factory = tuple_row
            row = conn.execute("SELECT signer_seq, attestation_hash, kind, subject FROM attestations WHERE tenant_key = %s ORDER BY signer_seq DESC LIMIT 1",
                               (self._tenant_key,)).fetchone()
            stmt = conn.execute("SELECT stmt_seq, statement_hash FROM trust_statements WHERE tenant_key = %s ORDER BY stmt_seq DESC LIMIT 1",
                                (self._tenant_key,)).fetchone()
            tip = conn.execute("SELECT height, block_hash FROM blocks WHERE tenant_key = %s ORDER BY height DESC LIMIT 1", (self._tenant_key,)).fetchone()
        return {
            "tenant": self._tenant_key,
            "head_seq": row[0] if row else 0, "head_hash": row[1] if row else attest.GENESIS,
            "next_signer_seq": (row[0] if row else 0) + 1, "prev_hash": row[1] if row else attest.GENESIS,
            "trust_head_seq": stmt[0] if stmt else 0, "trust_head_hash": stmt[1] if stmt else attest.GENESIS, "next_stmt_seq": (stmt[0] if stmt else 0) + 1,
            "tip_height": tip[0] if tip else 0, "tip_block_hash": tip[1] if tip else attest.GENESIS,
        }

    def list_trust_statements(self, after_seq: int = 0, limit: int = 100) -> list[dict[str, Any]]:
        with self._tx() as conn:
            conn.row_factory = tuple_row
            rows = conn.execute(
                f"SELECT {self._STMT_COLUMNS} FROM trust_statements WHERE tenant_key = %s AND stmt_seq > %s ORDER BY stmt_seq LIMIT %s",
                (self._tenant_key, max(0, int(after_seq)), max(1, min(int(limit), 1000)))).fetchall()
        return [self._plain(r, self._STMT_COLUMNS) for r in rows]

    def trust_state(self) -> dict[str, Any]:
        """Which roots are pinned (outside the database) and added, which signing keys are authorized, revoked and cosigned."""
        with self._tx() as conn:
            conn.row_factory = tuple_row
            roots = attest.load_roots()
            trust = attest.evaluate_trust(self._tenant_key, roots, attest.load_statements(conn, self._tenant_key))
        return {
            "pinned_roots": sorted(roots), "roots": sorted(trust.roots),
            "keys": [{"key_id": k, "from_signer_seq": a.from_seq, "revoked_after_signer_seq": a.cutoff, "authorized_by_statement": a.stmt_seq}
                     for k, a in sorted(trust.keys.items())],
            "cosigns": [{"checkpoint_seq": c.checkpoint_seq, "checkpoint_hash": c.checkpoint_hash, "root": c.root_id} for c in trust.cosigns],
            "voids": [{"signer_seq": k, "attestation_hash": v} for k, v in sorted(trust.voids.items())],
            "statements": trust.head_seq, "problems": trust.problems, "trust_roots_configured": bool(roots),
        }

    def signature_report(self, *, block_height: int | None, block_hash: str | None, receipt_id: str | None = None) -> dict[str, Any]:
        with self._tx() as conn:
            conn.row_factory = tuple_row
            return attest.signature_report(conn, self._tenant_key, block_height=block_height, block_hash=block_hash, receipt_id=receipt_id)

    def _block_signature(self, height: int, block_hash: str) -> tuple[bool, int | None]:
        """(signed, level) for a block: ``signed`` only when a valid attestation was verified against the pinned roots."""
        try:
            rep = self.signature_report(block_height=height, block_hash=block_hash)
        except StoreUnavailableError:
            raise
        except Exception:  # a signature-checking fault must never break replay; the block is then reported as not signed
            return False, None
        if rep["level"] is None:
            return False, None
        return bool(rep["verified"] and rep["block"] and rep["block"]["level"] >= 1), rep["block"]["level"] if rep["block"] else None

    def pending_attestations(self) -> dict[str, Any]:
        """What has no attestation yet (what a signer would sign next), plus where the log ends."""
        with self._tx() as conn:
            conn.row_factory = tuple_row
            done = {a.subject for a in attest.load_attestations(conn, self._tenant_key)}
            _, blocks, receipts = attest.load_truth(conn, self._tenant_key)
        return {
            "blocks": [{"height": h, "block_hash": bh, "sealed_at": _iso(sealed)} for h, bh, sealed in blocks if f"block:{h}" not in done],
            "receipts": [{"id": rid, "created_at": _iso(created)} for rid, created in receipts if rid not in done],
            "head": self.attestation_head(),
        }

    def verify_signatures(self) -> dict[str, Any]:
        with self._tx() as conn:
            conn.row_factory = tuple_row
            return attest.verify_tenant(conn, self._tenant_key)

    def store_attestation(self, body: dict[str, Any]) -> dict[str, Any]:
        """Verify an attestation (position, signature, key authorization, subject) and add it to the log; the database function
        then enforces the chain and computes the stored hash.  The application never makes a signature."""
        sig = attest.normalize_signature(str(body.get("signature", "")))
        roots = attest.load_roots()
        if not roots:
            raise attest.AttestError("no_trust_root", f"no trust root is configured ({attest.ROOTS_ENV}); nothing can be verified, so nothing is stored", 409)
        with self._tx() as conn:
            conn.row_factory = tuple_row
            tenant = self._tenant_key
            trust = attest.evaluate_trust(tenant, roots, attest.load_statements(conn, tenant))
            existing = attest.load_attestations(conn, tenant)
            truth, _, _ = attest.load_truth(conn, tenant)
            last = existing[-1] if existing else None
            seq, prev = int(body["signer_seq"]), str(body["prev_hash"])
            if seq != (last.signer_seq if last else 0) + 1 or prev != (last.attestation_hash if last else attest.GENESIS):
                raise attest.AttestError("attestation_out_of_order", f"the next attestation is {(last.signer_seq if last else 0) + 1} and must name {(last.attestation_hash if last else attest.GENESIS)} as prev_hash", 409)
            message = attest.attestation_message(str(body["kind"]), tenant, str(body["subject"]), str(body["subject_hash"]), seq, prev, str(body["signed_at"]))
            row = attest.Attestation(signer_seq=seq, kind=str(body["kind"]), subject=str(body["subject"]), subject_hash=str(body["subject_hash"]), prev_hash=prev,
                                     key_id=str(body["key_id"]), signed_at=str(body["signed_at"]), signature=sig,
                                     attestation_hash=attest.attestation_hash(message, str(body["key_id"]), sig))
            problems, _ = attest.evaluate_attestations(tenant, trust, existing + [row], truth)
            mine = [p["problem"] for p in problems if p["subject"] == f"attestation {seq}"]
            if mine:
                code = "already_attested" if any("already attested" in m for m in mine) else "attestation_invalid"
                raise attest.AttestError(code, "; ".join(mine), 409 if code == "already_attested" else 422)
            try:
                got = conn.execute("SELECT signer_seq, attestation_hash FROM jarvis_store_attestation(%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                                   (tenant, row.kind, row.subject, row.subject_hash, seq, prev, row.key_id, row.signed_at, sig)).fetchone()
            except psycopg.DatabaseError as exc:
                if getattr(exc, "sqlstate", "") == "JA001":
                    raise attest.AttestError("attestation_out_of_order", str(exc).splitlines()[0], 409) from exc
                raise
        return {"signer_seq": got[0], "attestation_hash": got[1], "key_id": row.key_id}

    def store_trust_statement(self, body: dict[str, Any]) -> dict[str, Any]:
        """Verify a root-signed trust statement against the pinned roots and the log so far, then add it."""
        sig = attest.normalize_signature(str(body.get("signature", "")))
        roots = attest.load_roots()
        if not roots:
            raise attest.AttestError("no_trust_root", f"no trust root is configured ({attest.ROOTS_ENV}); no statement can be verified, so none is stored", 409)
        with self._tx() as conn:
            conn.row_factory = tuple_row
            tenant = self._tenant_key
            existing = attest.load_statements(conn, tenant)
            last = existing[-1] if existing else None
            seq, prev = int(body["stmt_seq"]), str(body["prev_hash"])
            if seq != (last.stmt_seq if last else 0) + 1 or prev != (last.statement_hash if last else attest.GENESIS):
                raise attest.AttestError("statement_out_of_order", f"the next statement is {(last.stmt_seq if last else 0) + 1} and must name {(last.statement_hash if last else attest.GENESIS)} as prev_hash", 409)
            kind, key_id = str(body["kind"]), str(body["key_id"])
            arg = None if body.get("arg") is None else int(body["arg"])
            shash = body.get("subject_hash") or None
            message = attest.trust_message(kind, tenant, key_id, arg, shash, seq, prev)
            row = attest.Statement(stmt_seq=seq, kind=kind, key_id=key_id, pubkey=body.get("pubkey") or None, arg=arg, subject_hash=shash, prev_hash=prev,
                                   signed_by=str(body["signed_by"]), signature=sig, statement_hash=attest.statement_hash(message, str(body["signed_by"]), sig))
            if kind == "cosign":
                target = conn.execute("SELECT attestation_hash FROM attestations WHERE tenant_key = %s AND signer_seq = %s AND kind = 'checkpoint'", (tenant, arg)).fetchone()
                if target is None or target[0] != shash:
                    raise attest.AttestError("cosign_unknown_checkpoint", "there is no checkpoint attestation with that signer_seq and hash to cosign", 422)
            if kind == "void":
                target = conn.execute("SELECT attestation_hash FROM attestations WHERE tenant_key = %s AND signer_seq = %s", (tenant, arg)).fetchone()
                if target is None or target[0] != shash:
                    raise attest.AttestError("void_unknown_attestation", "there is no attestation with that signer_seq and hash to void", 422)
            trust = attest.evaluate_trust(tenant, roots, existing + [row])
            mine = [p["problem"] for p in trust.problems if p["subject"] == f"trust statement {seq}"]
            if mine:
                raise attest.AttestError("statement_invalid", "; ".join(mine), 422)
            try:
                got = conn.execute("SELECT stmt_seq, statement_hash FROM jarvis_store_trust_statement(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                                   (tenant, kind, key_id, row.pubkey, arg, shash, seq, prev, row.signed_by, sig)).fetchone()
            except psycopg.DatabaseError as exc:
                if getattr(exc, "sqlstate", "") == "JA001":
                    raise attest.AttestError("statement_out_of_order", str(exc).splitlines()[0], 409) from exc
                raise
        return {"stmt_seq": got[0], "statement_hash": got[1], "kind": kind, "key_id": key_id}

    # -- Replay receipts (CES.Local.ReplayReceipt.v1 evidence objects) ------------

    def create_replay_receipt(self, *, at_seq: int | None = None, at_block: int | None = None,
                              source_agent: str = "operator") -> "tuple[evidence_objects.EvidenceObject, bool, replay.ReplayState]":
        """Replay at a SEALED point and store what it produced as an evidence object.  Idempotent: the same replay gives the
        same receipt.  Refuses a point that no sealed block covers."""
        from app import replay

        if at_seq is None and at_block is None:
            tip = self.block_head()["tip"]
            if tip is None:
                raise replay.ReplayError("replay_nothing_sealed", "no block is sealed yet, so there is nothing to give a receipt for", 409)
            at_block = tip["height"]
        state = self.replay_state(at_seq=at_seq, at_block=at_block, limit=1)
        payload = replay.receipt_payload(state)
        obj, created = self.put_evidence_object(evidence_objects.EvidenceObjectCreate(
            schema_id=evidence_objects.CES_REPLAY_RECEIPT, payload=payload, source_agent=source_agent))
        return obj, created, state

    def get_replay_receipt(self, receipt_id: str) -> "evidence_objects.EvidenceObject":
        from app import replay

        if not evidence_objects.ID_RE.match(receipt_id or ""):
            raise replay.ReplayError("receipt_id_invalid", "a receipt id looks like eo:sha256:<64 lowercase hex characters>")
        obj = self.get_evidence_object(receipt_id)
        if obj is None:
            raise replay.ReplayError("receipt_not_found", f"there is no evidence object {receipt_id}", 404)
        if obj.schema_id != evidence_objects.CES_REPLAY_RECEIPT:
            raise replay.ReplayError("not_a_replay_receipt", f"{receipt_id} is a {obj.schema_id} object, not a replay receipt")
        return obj

    def list_replay_receipts(self, limit: int = 100) -> "list[evidence_objects.EvidenceObject]":
        with self._tx() as conn:
            rows = conn.execute(
                f"SELECT {self._EVIDENCE_COLUMNS} FROM evidence_objects WHERE tenant_key = %s AND schema_id = %s "
                "ORDER BY created_at DESC, id LIMIT %s",
                (self._tenant_key, evidence_objects.CES_REPLAY_RECEIPT, max(1, min(int(limit), 1000)))).fetchall()
        return [self._evidence_from_row(r) for r in rows]

    def verify_replay_receipt(self, receipt_id: str) -> "replay.ReceiptVerification":
        """Re-derive a receipt: the stored object must be intact, and replaying at its sealed point must give its root,
        counts and block.  (``python -m app.replay verify --receipt`` does the same from the raw rows.)"""
        from app import replay

        obj = self.get_replay_receipt(receipt_id)
        damaged = evidence_objects.verify_stored(obj)
        if damaged:
            return replay.ReceiptVerification(ok=False, receipt_id=receipt_id, problems=[
                {"check": "receipt", "subject": receipt_id, "problem": f"the stored receipt is damaged: {p}"} for p in damaged])
        p = obj.payload
        problems: list[dict[str, str]] = []

        def problem(message: str) -> None:
            problems.append({"check": "receipt", "subject": receipt_id, "problem": message})

        if p["contract"] != replay.CONTRACT_ID or p["contract_version"] != replay.CONTRACT_VERSION:
            problem(f"this service knows {replay.CONTRACT_ID} version {replay.CONTRACT_VERSION}, not {p['contract']} version {p['contract_version']}")
            return replay.ReceiptVerification(ok=False, receipt_id=receipt_id, problems=problems, receipt=replay.ReplayReceiptPayload(**p))
        if p["tenant"] != self._tenant_key:
            problem(f"the receipt is for tenant {p['tenant']}, not {self._tenant_key}")
        replayed = None
        try:
            state = self.replay_state(at_seq=p["at_seq"], limit=1)
        except replay.ReplayError as exc:
            problem(f"the receipt cannot be replayed: {exc.message}")
        else:
            replayed = {"state_root": state.state_root, "record_count": state.record_count, "deleted_count": state.deleted_count,
                        "block": state.block.model_dump() if state.block else None}
            if state.state_root != p["state_root"]:
                problem(f"the state root on replay is {state.state_root}, the receipt says {p['state_root']}")
            for key in ("record_count", "deleted_count"):
                if getattr(state, key) != p[key]:
                    problem(f"{key} is {getattr(state, key)} on replay, the receipt says {p[key]}")
            if state.block is None:
                problem(f"no sealed block covers seq {p['at_seq']} any more")
            else:
                if state.block.height != p["block_height"]:
                    problem(f"the covering block is {state.block.height}, the receipt says {p['block_height']}")
                if state.block.block_hash != p["block_hash"]:
                    problem(f"the covering block's hash is {state.block.block_hash}, the receipt says {p['block_hash']}")
        signatures = None
        if attest.signatures_mode() != "off":
            signatures = self.signature_report(block_height=p["block_height"], block_hash=p["block_hash"], receipt_id=receipt_id)
            problems.extend({"check": x["check"], "subject": x["subject"], "problem": x["problem"]} for x in signatures["problems"])
        return replay.ReceiptVerification(ok=not problems, receipt_id=receipt_id, problems=problems,
                                          receipt=replay.ReplayReceiptPayload(**p), replayed=replayed, signatures=signatures)

    # -- writes -----------------------------------------------------------------

    def _supersedes_exists(self, conn: psycopg.Connection, target: str) -> bool:
        return (
            conn.execute(
                "SELECT 1 FROM memories WHERE tenant_key = %s AND id = %s", (self._tenant_key, target)
            ).fetchone()
            is not None
        )

    def create_memory(self, data: MemoryCreate) -> MemoryRecord:
        evidence_objects.check_links(data.evidence, self._resolve_evidence)
        clause_v.gate_create(data, self._resolve_evidence)
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

    # -- Evidence Objects ------------------------------------------------------------------------

    _EVIDENCE_COLUMNS = "id, schema_id, payload, pointer, size_bytes, created_at, created_by"

    @staticmethod
    def _evidence_from_row(row: dict[str, Any]) -> "evidence_objects.EvidenceObject":
        return evidence_objects.EvidenceObject(
            id=row["id"], schema_id=row["schema_id"], payload=row["payload"], pointer=row["pointer"],
            size_bytes=row["size_bytes"], created_at=_iso(row["created_at"]), created_by=row["created_by"],
        )

    def put_evidence_object(self, req: "evidence_objects.EvidenceObjectCreate") -> "tuple[evidence_objects.EvidenceObject, bool]":
        """Store the object (idempotent: the same content is the same id). Returns (object, created)."""
        oid, data = evidence_objects.build_object(req)
        with self._tx() as conn:
            row = conn.execute(
                "INSERT INTO evidence_objects (tenant_key, id, schema_id, payload, pointer, size_bytes, created_by) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (tenant_key, id) DO NOTHING "
                f"RETURNING {self._EVIDENCE_COLUMNS}",
                (self._tenant_key, oid, req.schema_id, Jsonb(req.payload), Jsonb(req.pointer) if req.pointer is not None else None,
                 len(data), req.source_agent),
            ).fetchone()
            created = row is not None
            if row is None:
                row = conn.execute(
                    f"SELECT {self._EVIDENCE_COLUMNS} FROM evidence_objects WHERE tenant_key = %s AND id = %s",
                    (self._tenant_key, oid),
                ).fetchone()
        return self._evidence_from_row(row), created

    def get_evidence_object(self, oid: str) -> "evidence_objects.EvidenceObject | None":
        with self._tx() as conn:
            row = conn.execute(
                f"SELECT {self._EVIDENCE_COLUMNS} FROM evidence_objects WHERE tenant_key = %s AND id = %s",
                (self._tenant_key, oid),
            ).fetchone()
        return self._evidence_from_row(row) if row else None

    def all_evidence_objects(self) -> "list[evidence_objects.EvidenceObject]":
        with self._tx() as conn:
            rows = conn.execute(
                f"SELECT {self._EVIDENCE_COLUMNS} FROM evidence_objects WHERE tenant_key = %s ORDER BY created_at, id",
                (self._tenant_key,),
            ).fetchall()
        return [self._evidence_from_row(r) for r in rows]

    def _resolve_evidence_conn(self, conn: psycopg.Connection, ref: str) -> "evidence_objects.EvidenceInfo | None":
        row = conn.execute(
            f"SELECT {self._EVIDENCE_COLUMNS} FROM evidence_objects WHERE tenant_key = %s AND id = %s",
            (self._tenant_key, ref),
        ).fetchone()
        if row is None:
            return None
        obj = self._evidence_from_row(row)
        problems = evidence_objects.verify_stored(obj)
        if problems:
            raise evidence_objects.EvidenceError(
                "evidence_object_invalid",
                "the stored evidence object is damaged",
                [{"code": "evidence_object_hash_mismatch", "ref": ref, "message": problems[0]}],
            )
        return evidence_objects.EvidenceInfo(obj.id, obj.schema_id)

    def _resolve_evidence(self, ref: str) -> "evidence_objects.EvidenceInfo | None":
        with self._tx() as conn:
            return self._resolve_evidence_conn(conn, ref)

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
                resolve = lambda ref: self._resolve_evidence_conn(conn, ref)  # noqa: E731 - same transaction as the update
                if data.evidence is not None:
                    evidence_objects.check_links(data.evidence, resolve)
                clause_v.gate_update(existing, data, resolve)
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
