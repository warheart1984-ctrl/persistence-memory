"""Per-tenant API key authentication for a request (RFC phase 1, slice 2): JARVIS_KEY_TENANTS.

Off by default. With JARVIS_KEY_TENANTS=1 a request that presents a ``jlk_`` token (as ``Authorization: Bearer`` or ``X-API-Key``) is authenticated against the control schema
(``app.tenant_keys.resolve``, through the two SECURITY DEFINER functions the application role may call) and bound to a tenant principal: the tenant is the key's own, never a request
parameter. The operator's ledger key keeps working exactly as before; a request that presents neither is refused as before.

Failure behaviour: every way a token can fail to authenticate is the same 401 the ledger key middleware gives for a wrong key (no oracle between "not a key", "wrong key" and "revoked"); a database
that cannot be reached is a 503, never a fall-back to anything; a valid key of a tenant that is not active is a 503 ``tenant_unavailable`` for that tenant only.
"""

from __future__ import annotations

import os
from typing import Iterable

import psycopg
from psycopg.rows import dict_row

from app import tenant_keys
from app.pg_schema import control_schema_name, validate_schema_name

_TRUTHY = frozenset({"1", "true", "yes", "on"})


def key_tenants_enabled() -> bool:
    return (os.getenv("JARVIS_KEY_TENANTS") or "").strip().lower() in _TRUTHY


def _bearer(authorization: str | None) -> str | None:
    if authorization and authorization.lower().startswith("bearer "):
        return authorization[7:].strip() or None
    return None


def token_candidates(headers) -> list[str]:
    """Values the request offers that LOOK like a tenant key (``jlk_`` prefix): the Bearer token and X-API-Key. Anything else is somebody else's credential, not ours to judge."""
    out: list[str] = []
    for raw in (_bearer(headers.get("authorization")), (headers.get("x-api-key") or "").strip() or None):
        if raw and raw.startswith(tenant_keys.TOKEN_PREFIX) and raw not in out:
            out.append(raw)
    return out


def failure_limit() -> int:
    """Failed key attempts per minute, per (client address, key id), before the key is not even checked (JARVIS_KEY_FAIL_LIMIT_PER_MINUTE)."""
    try:
        return max(1, int(os.getenv("JARVIS_KEY_FAIL_LIMIT_PER_MINUTE", "20")))
    except ValueError:
        return 20


def _failure_subjects(candidates: Iterable[str], client: str) -> list[str]:
    out = []
    for token in candidates:
        parsed = tenant_keys.parse_token(token)
        # a malformed token has no key id to guess against: it is charged to the address alone
        out.append(f"keyfail:{client}:{parsed[0] if parsed else '-'}")
    return out or [f"keyfail:{client}:-"]


def attempts_blocked(candidates: Iterable[str], client: str) -> bool:
    """True when this address has already failed `failure_limit()` times against one of these key ids this minute: the request is refused with 429 BEFORE any HMAC is computed or the
    database consulted, so guessing one key's secret is throttled. Keyed by address as well as key id, so a stranger who sends junk naming your key id cannot lock you out of your own key."""
    from app.public_security import key_failure_limiter

    limit = failure_limit()
    return any(key_failure_limiter.blocked(subject, limit) for subject in _failure_subjects(candidates, client))


def record_failure(candidates: Iterable[str], client: str) -> None:
    from app.public_security import key_failure_limiter

    for subject in _failure_subjects(candidates, client):
        key_failure_limiter.allow(subject, limit=10**9)      # count it; the decision to refuse is made by attempts_blocked


class KeyAuthUnavailable(RuntimeError):
    """The control schema could not be consulted (database down, misconfigured pepper): the request is refused with 503, never admitted."""


def _settings() -> tuple[str, str, str]:
    dsn = (os.getenv("JARVIS_DATABASE_URL") or "").strip()
    if not dsn:
        raise KeyAuthUnavailable("no database configured")
    schema = validate_schema_name((os.getenv("JARVIS_DATABASE_SCHEMA") or "jarvis").strip())
    return dsn, schema, control_schema_name(schema)


def authenticate(candidates: Iterable[str]) -> tenant_keys.ResolvedKey | None:
    """Blocking (call it from a thread): the first candidate that authenticates, else None. Raises KeyAuthUnavailable when the control schema cannot be consulted."""
    from app import pg_store

    try:
        pepper = tenant_keys.pepper()
        dsn, schema, control = _settings()
        pool, _ = pg_store._pool_for(dsn, schema)
        with pool.connection() as conn:
            conn.row_factory = dict_row
            for token in candidates:
                resolved = tenant_keys.resolve(conn, control, token, pepper)
                if resolved is not None:
                    return resolved
            return None
    except (tenant_keys.KeyConfigError, psycopg.Error, OSError, ValueError) as exc:
        raise KeyAuthUnavailable(type(exc).__name__) from None
    except Exception as exc:  # noqa: BLE001 - pool timeouts and the like: refuse, never admit
        if type(exc).__name__ in ("PoolTimeout", "TooManyRequests", "PoolClosed"):
            raise KeyAuthUnavailable(type(exc).__name__) from None
        raise


def config_problems() -> list[str]:
    """What JARVIS_KEY_TENANTS refuses to start with (checked at startup, only when the flag is on)."""
    problems: list[str] = []
    if not (os.getenv("JARVIS_DATABASE_URL") or "").strip():
        problems.append("no PostgreSQL store is configured (JARVIS_DATABASE_URL): per-tenant keys live in the control schema")
    elif (os.getenv("JARVIS_PG_STORE") or "rows").strip().lower() != "rows":
        problems.append("JARVIS_PG_STORE is not 'rows': per-tenant keys need the row store")
    try:
        tenant_keys.pepper()
    except tenant_keys.KeyConfigError as exc:
        problems.append(str(exc))
    return problems


def assert_config() -> None:
    problems = config_problems()
    if problems:
        raise RuntimeError("JARVIS_KEY_TENANTS refuses to start: " + "; ".join(problems))
