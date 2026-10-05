"""Store exceptions shared by the JSON and Postgres ledgers (no heavy imports)."""

from __future__ import annotations


class StoreUnavailableError(RuntimeError):
    """The ledger cannot be read or written safely; callers must fail closed."""


class StoreVersionConflict(Exception):
    """Optimistic-lock failure: the record changed since the caller read it (HTTP 409)."""
