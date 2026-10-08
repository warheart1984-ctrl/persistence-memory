"""Append-only, digest-chained turn receipts — transactional SQLite on one host.

Tables enforce unique ``(tenant_key, session_id, turn_index)`` and
``(tenant_key, receipt_digest)``; a ``BEGIN IMMEDIATE`` transaction reads
the session head, allocates the next index/previous digest, and inserts
the immutable row atomically across processes on one host. A tenant/session
lease allows only one in-flight request per session (409 when busy,
abandoned leases expire after 120s and the next turn is marked
``context_reset=True``). WAL + busy timeout are on.

No deletion or rotation in v1: a configured byte cap stops new turns
(503) before the store is exhausted. SQLite on a shared network
filesystem or multi-host deployment is unsupported.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
import time
from pathlib import Path

LEASE_TTL_S = 120.0
DEFAULT_MAX_BYTES = 64 * 1024 * 1024  # 64 MiB conservative cap


class ReceiptError(Exception):
    def __init__(self, code: str, detail: str = ""):
        self.code = code
        super().__init__(detail or code)


def canonical_json(body: dict) -> str:
    return json.dumps(
        body,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def receipt_digest(body: dict) -> str:
    """sha256 of canonical JSON with every digest field removed."""
    stripped = {k: v for k, v in body.items() if k not in ("receipt_digest", "persist_digest")}
    return "sha256:" + hashlib.sha256(canonical_json(stripped).encode("utf-8")).hexdigest()


def _dir() -> Path:
    return Path(os.getenv("JARVIS_TWIN_CHAT_DIR", "data/twin-chat"))


def _max_bytes() -> int:
    try:
        return int(os.getenv("JARVIS_TWIN_CHAT_MAX_BYTES", "") or DEFAULT_MAX_BYTES)
    except ValueError:
        return DEFAULT_MAX_BYTES


_DB_LOCK = threading.Lock()


class ReceiptStore:
    """One SQLite database per host; safe across threads and processes."""

    def __init__(self, path: Path | None = None):
        self._path = path or (_dir() / "receipts.sqlite3")
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self._path), check_same_thread=False, timeout=10)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=10000")
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS receipts (
                tenant_key     TEXT NOT NULL,
                session_id     TEXT NOT NULL,
                turn_index     INTEGER NOT NULL,
                receipt_digest TEXT NOT NULL,
                prev_digest    TEXT NOT NULL,
                at             TEXT NOT NULL,
                body_json      TEXT NOT NULL,
                PRIMARY KEY (tenant_key, receipt_digest),
                UNIQUE (tenant_key, session_id, turn_index)
            );
            CREATE TABLE IF NOT EXISTS persist_receipts (
                tenant_key           TEXT NOT NULL,
                persist_digest       TEXT NOT NULL,
                turn_receipt_digest  TEXT NOT NULL,
                session_id           TEXT NOT NULL,
                turn_index           INTEGER NOT NULL,
                body_json            TEXT NOT NULL,
                PRIMARY KEY (tenant_key, persist_digest)
            );
            CREATE TABLE IF NOT EXISTS leases (
                tenant_key  TEXT NOT NULL,
                session_id  TEXT NOT NULL,
                lease_until REAL NOT NULL,
                PRIMARY KEY (tenant_key, session_id)
            );
            """
        )

    # --- leases ------------------------------------------------------------

    def acquire_lease(self, tenant_key: str, session_id: str) -> tuple[bool, bool]:
        """Returns (acquired, stale_takeover).

        acquired=False when a live lease is held by another in-flight turn
        (caller → 409). An expired lease is taken over and reported stale so
        the next turn is marked ``context_reset=True``.
        """
        now = time.monotonic()
        with _DB_LOCK:
            cur = self._conn.execute(
                "SELECT lease_until FROM leases WHERE tenant_key=? AND session_id=?",
                (tenant_key, session_id),
            )
            row = cur.fetchone()
            if row is not None and row[0] > now:
                return False, False
            stale = row is not None
            self._conn.execute(
                "INSERT OR REPLACE INTO leases (tenant_key, session_id, lease_until)"
                " VALUES (?, ?, ?)",
                (tenant_key, session_id, now + LEASE_TTL_S),
            )
            self._conn.commit()
            return True, stale

    def release_lease(self, tenant_key: str, session_id: str) -> None:
        with _DB_LOCK:
            self._conn.execute(
                "DELETE FROM leases WHERE tenant_key=? AND session_id=?",
                (tenant_key, session_id),
            )
            self._conn.commit()

    # --- turn receipts -----------------------------------------------------

    def _check_cap(self) -> None:
        try:
            size = self._path.stat().st_size
        except OSError:
            size = 0
        if size >= _max_bytes():
            raise ReceiptError("RECEIPT_STORE_FULL", "receipt store reached configured cap")

    def head(self, tenant_key: str, session_id: str) -> tuple[int, str] | None:
        cur = self._conn.execute(
            "SELECT turn_index, receipt_digest FROM receipts"
            " WHERE tenant_key=? AND session_id=? ORDER BY turn_index DESC LIMIT 1",
            (tenant_key, session_id),
        )
        row = cur.fetchone()
        return (row[0], row[1]) if row else None

    def append_turn_receipt(self, tenant_key: str, body: dict) -> dict:
        """Allocate (turn_index, prev_digest) + insert atomically. Returns body
        with those fields plus receipt_digest filled in."""
        self._check_cap()
        session_id = body["session_id"]
        with _DB_LOCK:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                head = self.head(tenant_key, session_id)
                turn_index = head[0] + 1 if head else 0
                prev = head[1] if head else "sha256:genesis"
                body = dict(body, turn_index=turn_index, prev_digest=prev)
                body["receipt_digest"] = receipt_digest(body)
                self._conn.execute(
                    "INSERT INTO receipts (tenant_key, session_id, turn_index,"
                    " receipt_digest, prev_digest, at, body_json)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        tenant_key,
                        session_id,
                        turn_index,
                        body["receipt_digest"],
                        prev,
                        body["at"],
                        canonical_json(body),
                    ),
                )
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
        return body

    def append_persist_receipt(self, tenant_key: str, body: dict) -> dict:
        """Insert a linked outcome receipt. Digests cover only itself."""
        self._check_cap()
        body = dict(body)
        body["persist_digest"] = receipt_digest(body)
        with _DB_LOCK:
            self._conn.execute(
                "INSERT INTO persist_receipts (tenant_key, persist_digest,"
                " turn_receipt_digest, session_id, turn_index, body_json)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (
                    tenant_key,
                    body["persist_digest"],
                    body["turn_receipt_digest"],
                    body["session_id"],
                    body["turn_index"],
                    canonical_json(body),
                ),
            )
            self._conn.commit()
        return body

    # --- reads --------------------------------------------------------------

    def get_receipt(self, tenant_key: str, digest: str) -> dict | None:
        cur = self._conn.execute(
            "SELECT body_json FROM receipts WHERE tenant_key=? AND receipt_digest=?",
            (tenant_key, digest),
        )
        row = cur.fetchone()
        return json.loads(row[0]) if row else None

    def session_turns(self, tenant_key: str, session_id: str) -> list[dict]:
        cur = self._conn.execute(
            "SELECT turn_index, receipt_digest FROM receipts"
            " WHERE tenant_key=? AND session_id=? ORDER BY turn_index",
            (tenant_key, session_id),
        )
        return [{"turn_index": r[0], "receipt_digest": r[1]} for r in cur.fetchall()]

    def verify_chain(self, tenant_key: str, session_id: str) -> bool:
        """Rehash every row and re-link the chain for one session."""
        cur = self._conn.execute(
            "SELECT turn_index, body_json FROM receipts"
            " WHERE tenant_key=? AND session_id=? ORDER BY turn_index",
            (tenant_key, session_id),
        )
        prev = "sha256:genesis"
        expected_index = 0
        for turn_index, body_json in cur.fetchall():
            body = json.loads(body_json)
            if turn_index != expected_index or body.get("prev_digest") != prev:
                return False
            if receipt_digest(body) != body.get("receipt_digest"):
                return False
            prev = body["receipt_digest"]
            expected_index += 1
        return True


_store: ReceiptStore | None = None


def get_receipt_store() -> ReceiptStore:
    global _store
    if _store is None:
        _store = ReceiptStore()
    return _store


def reset_store() -> None:
    """Tests only — drop the process-global store."""
    global _store
    _store = None
