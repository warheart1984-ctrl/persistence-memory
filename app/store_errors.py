"""Store exceptions shared by the JSON and Postgres ledgers (no heavy imports)."""

from __future__ import annotations


class StoreUnavailableError(RuntimeError):
    """The ledger cannot be read or written safely; callers must fail closed."""


class InvalidInputError(ValueError):
    """The caller sent something the ledger can never store (a NUL byte in a text field).  A 400, not an outage."""


class StoreVersionConflict(Exception):
    """Optimistic-lock failure: the record changed since the caller read it (HTTP 409)."""
