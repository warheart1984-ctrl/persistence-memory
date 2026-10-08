"""Digest-chained turn receipts and session leases in one SQLite file on one host.

What the tests prove (tests/test_twinchat.py, tests/test_twinchat_redteam.py):

* a turn receipt is inserted with its turn index and previous digest allocated
  inside one ``BEGIN IMMEDIATE`` transaction, so concurrent writers, in threads
  or in separate processes, cannot fork or skip a session's chain;
* acquiring a session lease is one ``BEGIN IMMEDIATE`` transaction too: of
  several processes racing for the same free lease, exactly one gets it
  (``test_lease_acquire_is_exclusive_across_processes``);
* an expired lease is taken over and the next turn is marked
  ``context_reset=True``; a stale owner's release cannot evict the new owner's
  lease (the release is bound to the owner token).

What it does NOT give you:

* the chain is tamper-EVIDENT against editing one row, not tamper-PROOF: the
  head is not anchored in the ledger or signed, so someone who can write the
  file can rewrite a whole session consistently or delete its last turns
  undetected;
* a lease is not renewed: a turn that outlives the 120 s expiry loses its
  exclusivity (its receipt append is still serialised, so the chain stays
  linear, but two turns can then run on one session);
* lease expiry uses the wall clock, so a clock step can shorten or stretch it;
* ``JARVIS_TWIN_CHAT_MAX_BYTES`` caps the main database file only (not the WAL),
  is shared by every tenant, and nothing is ever deleted or rotated: one chatty
  tenant can fill it and stop every tenant's turns (503);
* the draft-record write to the ledger and the outcome receipt here are two
  stores with no shared transaction;
* SQLite on a network filesystem, or one file shared by several hosts, is
  unsupported.

Open issues for the items above: #58 persist atomicity, #59 quotas and the WAL-aware
cap, #60 session cache, #61 anchoring heads, #62 lease renewal and clock, #63 session ids.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
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


def _enable_wal(conn: sqlite3.Connection) -> None:
    """Set WAL mode, retrying the transient lock from concurrent startup."""
    deadline = time.monotonic() + 10
    while True:
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            return
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc).lower() and "busy" not in str(exc).lower():
                raise
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.01)


class ReceiptStore:
    """One SQLite database per host; safe across threads and processes."""

    def __init__(self, path: Path | None = None):
        self._path = path or (_dir() / "receipts.sqlite3")
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self._path), check_same_thread=False, timeout=10)
        _enable_wal(self._conn)
        self._conn.execute("PRAGMA busy_timeout=10000")
        # Reads run on their own connection: WAL snapshot isolation means
        # they can never observe another transaction's uncommitted rows —
        # no phantom receipts reachable through the API.
        self._read = sqlite3.connect(str(self._path), check_same_thread=False, timeout=10)
        self._read.execute("PRAGMA busy_timeout=10000")
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
                owner       TEXT,
                PRIMARY KEY (tenant_key, session_id)
            );
            """
        )
        # The first merged version of this table (PR #53) had no owner column.
        # No deployment ever ran that version (checked 2026-10-08: the live
        # image predates twinchat), but a development database created by it
        # would fail on its first INSERT, so the column is still added, once,
        # without touching rows. Serialize the check and ALTER across
        # processes; re-read the schema only after BEGIN IMMEDIATE, because
        # another worker may have completed the migration while this one
        # waited for the write lock.
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            lease_cols = {
                r[1]
                for r in self._conn.execute("PRAGMA table_info(leases)").fetchall()
            }
            if "owner" not in lease_cols:
                self._conn.execute("ALTER TABLE leases ADD COLUMN owner TEXT")
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise

    # --- leases ------------------------------------------------------------
    # lease_until is WALL CLOCK — the row survives process restarts and is
    # shared by every process on the host, so a process-relative monotonic
    # reading is meaningless here (a crashed lease once read as live for the
    # length of the dead process's uptime).

    def acquire_lease(self, tenant_key: str, session_id: str) -> tuple[bool, bool, str | None]:
        """Returns (acquired, stale_takeover, owner_token).

        acquired=False when a live lease is held by another in-flight turn
        (caller → 409). An expired lease is taken over and reported stale so
        the next turn is marked ``context_reset=True``. The owner token must
        be handed back to ``release_lease`` — a stale owner's release must
        never evict a takeover lease.

        The read and the write are one ``BEGIN IMMEDIATE`` transaction: the
        in-process ``_DB_LOCK`` only orders threads, and without the SQLite
        write lock several processes could each see "free" and each take the
        lease. A row with no owner (a development database from before owner
        tokens, holding a process-relative deadline far below the epoch) reads
        as expired and is taken over like any other abandoned lease.
        """
        now = time.time()
        with _DB_LOCK:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._conn.execute(
                    "SELECT lease_until FROM leases WHERE tenant_key=? AND session_id=?",
                    (tenant_key, session_id),
                ).fetchone()
                held = row is not None and row[0] > now
                token = secrets.token_hex(8)
                if not held:
                    self._conn.execute(
                        "INSERT OR REPLACE INTO leases (tenant_key, session_id, lease_until, owner)"
                        " VALUES (?, ?, ?, ?)",
                        (tenant_key, session_id, now + LEASE_TTL_S, token),
                    )
                self._conn.execute("COMMIT" if not held else "ROLLBACK")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
        if held:
            return False, False, None
        return True, row is not None, token

    def release_lease(self, tenant_key: str, session_id: str, token: str) -> None:
        """Release a lease, only if ``token`` still owns it."""
        with _DB_LOCK:
            self._conn.execute(
                "DELETE FROM leases WHERE tenant_key=? AND session_id=? AND owner=?",
                (tenant_key, session_id, token),
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
        cur = self._read.execute(
            "SELECT body_json FROM receipts WHERE tenant_key=? AND receipt_digest=?",
            (tenant_key, digest),
        )
        row = cur.fetchone()
        return json.loads(row[0]) if row else None

    def session_turns(self, tenant_key: str, session_id: str) -> list[dict]:
        cur = self._read.execute(
            "SELECT turn_index, receipt_digest FROM receipts"
            " WHERE tenant_key=? AND session_id=? ORDER BY turn_index",
            (tenant_key, session_id),
        )
        return [{"turn_index": r[0], "receipt_digest": r[1]} for r in cur.fetchall()]

    def verify_chain(self, tenant_key: str, session_id: str) -> bool:
        """Rehash every row and re-link the chain for one session."""
        cur = self._read.execute(
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
