"""Per-tenant API keys (RFC phase 1, slice 1): the tokens, the lookup, and the migrator-role CLI that issues them.

    python -m app.tenant_keys create-tenant --label NAME
    python -m app.tenant_keys create-key    --tenant t_<hex32> [--label L] [--scopes memory.read,memory.write] [--expires-days N] [--token-file PATH]
    python -m app.tenant_keys revoke-key KEY_ID
    python -m app.tenant_keys set-status --tenant t_<hex32> --status active|suspended|...
    python -m app.tenant_keys list-tenants | list-keys [--tenant t_<hex32>]

This module does NOT put anything in the request path: it is the storage and the verification primitive (``resolve``) that the key-authentication middleware will use. The public API
can never create, change or list a key or a tenant: only this CLI can, and it runs as the migrator role on the box.

Token: ``jlk_<key_id>_<secret>``. ``key_id`` is 16 hex characters, public and not secret (it is the lookup handle, so verification is one indexed read and not an enumeration oracle); ``secret`` is
256 bits of CSPRNG output. The database stores only ``HMAC-SHA256(pepper, secret)``; the pepper (``JARVIS_KEY_PEPPER_FILE``, or ``JARVIS_KEY_PEPPER``) is never in the database, so a stolen database alone
cannot verify a guess. Every failure to authenticate (unknown id, wrong secret, revoked, expired, malformed) is the same answer, ``None``, and the comparison is made even when the id is unknown.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import os
import re
import secrets
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import psycopg
from psycopg import sql
from psycopg.rows import dict_row

from app.pg_schema import control_schema_name, validate_schema_name

TOKEN_PREFIX = "jlk_"
_TOKEN_RE = re.compile(r"^jlk_([0-9a-f]{16})_([A-Za-z0-9_-]{43})$")
TENANT_KEY_RE = re.compile(r"^t_[0-9a-f]{32}$")
TENANT_STATUSES = ("active", "suspended", "quiesced", "restoring", "deleting", "deleted")
DEFAULT_SCOPES = ("memory.read", "memory.write")
_ALLOWED_SCOPES = frozenset(DEFAULT_SCOPES)
MIN_PEPPER_BYTES = 32
_DUMMY_HASH = "0" * 64                           # compared against when the key id is unknown, so an unknown id costs the same as a wrong secret


class KeyConfigError(RuntimeError):
    """The pepper is missing or too weak, or the database settings are unusable."""


@dataclass(frozen=True)
class ResolvedKey:
    """A verified key. ``tenant_status`` is the tenant's lifecycle state: the caller decides what a non-active tenant means (503 tenant_unavailable), this module only authenticates."""
    key_id: str
    tenant_key: str
    scopes: frozenset[str]
    tenant_status: str


# ------------------------------------------------------------------------------------------------------------------------------ tokens
def new_key() -> tuple[str, str]:
    """(key_id, secret): fresh, random, independent of each other."""
    return secrets.token_hex(8), secrets.token_urlsafe(32)


def make_token(key_id: str, secret: str) -> str:
    return f"{TOKEN_PREFIX}{key_id}_{secret}"


def parse_token(token: str | None) -> tuple[str, str] | None:
    """(key_id, secret), or None for anything that is not exactly a token (no partial parsing, no tolerance for whitespace or case)."""
    m = _TOKEN_RE.match(token or "")
    return (m.group(1), m.group(2)) if m else None


def pepper() -> bytes:
    """The HMAC key, from JARVIS_KEY_PEPPER_FILE (first line) or JARVIS_KEY_PEPPER; at least 32 bytes, or KeyConfigError (no weak default)."""
    path = (os.getenv("JARVIS_KEY_PEPPER_FILE") or "").strip()
    if path:
        try:
            value = open(path, encoding="utf-8").readline().strip()
        except OSError as exc:
            raise KeyConfigError(f"JARVIS_KEY_PEPPER_FILE names a file that cannot be read ({type(exc).__name__})") from None
    else:
        value = (os.getenv("JARVIS_KEY_PEPPER") or "").strip()
    raw = value.encode("utf-8")
    if len(raw) < MIN_PEPPER_BYTES:
        raise KeyConfigError(f"the key pepper is missing or shorter than {MIN_PEPPER_BYTES} bytes (set JARVIS_KEY_PEPPER_FILE)")
    return raw


def key_hash(secret: str, pepper_bytes: bytes) -> str:
    return hmac.new(pepper_bytes, secret.encode("utf-8"), hashlib.sha256).hexdigest()


# --------------------------------------------------------------------------------------------------- application side: verification
def resolve(conn: psycopg.Connection, control: str, token: str | None, pepper_bytes: bytes) -> ResolvedKey | None:
    """Authenticate a token through the SECURITY DEFINER lookup (the only thing the application role may call). None for every kind of failure, indistinguishably."""
    parsed = parse_token(token)
    if parsed is None:
        hmac.compare_digest(_DUMMY_HASH, _DUMMY_HASH)
        return None
    key_id, secret = parsed
    row = conn.execute(
        sql.SQL("SELECT tenant_key, key_hash, scopes, key_status, expires_at, tenant_status FROM {}.resolve_key(%s)").format(sql.Identifier(control)), (key_id,)).fetchone()
    stored = (row["key_hash"] if isinstance(row, dict) else row[1]) if row else _DUMMY_HASH
    match = hmac.compare_digest(key_hash(secret, pepper_bytes), stored)           # always computed, found or not
    if row is None or not match:
        return None
    r = row if isinstance(row, dict) else dict(zip(("tenant_key", "key_hash", "scopes", "key_status", "expires_at", "tenant_status"), row))
    if r["key_status"] != "active":
        return None
    if r["expires_at"] is not None and r["expires_at"] <= datetime.now(timezone.utc):
        return None
    conn.execute(sql.SQL("SELECT {}.touch_key_last_used(%s)").format(sql.Identifier(control)), (key_id,))
    return ResolvedKey(key_id=key_id, tenant_key=r["tenant_key"], scopes=frozenset(r["scopes"]), tenant_status=r["tenant_status"])


# ------------------------------------------------------------------------------------------------ migrator side: issuing and changing
def create_tenant(conn: psycopg.Connection, control: str, label: str = "") -> str:
    tenant_key = "t_" + secrets.token_hex(16)
    conn.execute(sql.SQL("INSERT INTO {}.tenants (tenant_key, label) VALUES (%s, %s)").format(sql.Identifier(control)), (tenant_key, label))
    return tenant_key


def create_key(conn: psycopg.Connection, control: str, tenant_key: str, pepper_bytes: bytes, *, label: str = "", scopes: tuple[str, ...] = DEFAULT_SCOPES,
               expires_at: datetime | None = None) -> str:
    """Issue a key for an existing tenant; returns the full token, which exists nowhere else (only its HMAC is stored)."""
    if not TENANT_KEY_RE.match(tenant_key):
        raise ValueError("not a key-tenant id (t_ followed by 32 hex characters)")
    bad = [s for s in scopes if s not in _ALLOWED_SCOPES]
    if bad or not scopes:
        raise ValueError(f"scopes must be a non-empty subset of {sorted(_ALLOWED_SCOPES)}")
    key_id, secret = new_key()
    conn.execute(
        sql.SQL("INSERT INTO {}.api_keys (key_id, tenant_key, key_hash, scopes, label, expires_at) VALUES (%s, %s, %s, %s, %s, %s)").format(sql.Identifier(control)),
        (key_id, tenant_key, key_hash(secret, pepper_bytes), list(scopes), label, expires_at))
    return make_token(key_id, secret)


def revoke_key(conn: psycopg.Connection, control: str, key_id: str) -> bool:
    cur = conn.execute(sql.SQL("UPDATE {}.api_keys SET status = 'revoked', revoked_at = now() WHERE key_id = %s AND status = 'active'").format(sql.Identifier(control)), (key_id,))
    return cur.rowcount == 1


def set_tenant_status(conn: psycopg.Connection, control: str, tenant_key: str, status: str) -> bool:
    if status not in TENANT_STATUSES:
        raise ValueError(f"status must be one of {TENANT_STATUSES}")
    cur = conn.execute(
        sql.SQL("UPDATE {}.tenants SET status = %s, deleted_at = CASE WHEN %s = 'deleted' THEN now() ELSE deleted_at END WHERE tenant_key = %s").format(sql.Identifier(control)),
        (status, status, tenant_key))
    return cur.rowcount == 1


def list_tenants(conn: psycopg.Connection, control: str) -> list[dict]:
    conn.row_factory = dict_row
    return conn.execute(sql.SQL("SELECT tenant_key, label, status, created_at, deleted_at FROM {}.tenants ORDER BY created_at, tenant_key").format(sql.Identifier(control))).fetchall()


def list_keys(conn: psycopg.Connection, control: str, tenant_key: str | None = None) -> list[dict]:
    """Never includes the hash."""
    conn.row_factory = dict_row
    q = sql.SQL("SELECT key_id, tenant_key, scopes, status, label, created_at, expires_at, last_used_at, revoked_at FROM {}.api_keys").format(sql.Identifier(control))
    if tenant_key:
        return conn.execute(q + sql.SQL(" WHERE tenant_key = %s ORDER BY created_at, key_id"), (tenant_key,)).fetchall()
    return conn.execute(q + sql.SQL(" ORDER BY created_at, key_id")).fetchall()


# ------------------------------------------------------------------------------------------------------------------------------ CLI
def _connect() -> tuple[psycopg.Connection, str]:
    dsn = (os.getenv("JARVIS_DATABASE_MIGRATE_URL") or os.getenv("JARVIS_DATABASE_URL") or "").strip()
    if not dsn:
        raise KeyConfigError("set JARVIS_DATABASE_MIGRATE_URL (the migrator role issues keys)")
    schema = validate_schema_name((os.getenv("JARVIS_DATABASE_SCHEMA") or "jarvis").strip())
    return psycopg.connect(dsn, connect_timeout=5), control_schema_name(schema)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.tenant_keys", description="Issue and manage per-tenant API keys (migrator role, on the box)")
    sub = parser.add_subparsers(dest="cmd", required=True)
    ct = sub.add_parser("create-tenant")
    ct.add_argument("--label", default="")
    ck = sub.add_parser("create-key")
    ck.add_argument("--tenant", required=True)
    ck.add_argument("--label", default="")
    ck.add_argument("--scopes", default=",".join(DEFAULT_SCOPES))
    ck.add_argument("--expires-days", type=float, default=None)
    ck.add_argument("--token-file", default=None, help="write the token to this file (mode 600) instead of printing it")
    rk = sub.add_parser("revoke-key")
    rk.add_argument("key_id")
    ss = sub.add_parser("set-status")
    ss.add_argument("--tenant", required=True)
    ss.add_argument("--status", required=True, choices=TENANT_STATUSES)
    sub.add_parser("list-tenants")
    lk = sub.add_parser("list-keys")
    lk.add_argument("--tenant", default=None)
    args = parser.parse_args(argv)
    try:
        conn, control = _connect()
        with conn:
            if args.cmd == "create-tenant":
                print(create_tenant(conn, control, args.label))
            elif args.cmd == "create-key":
                scopes = tuple(s.strip() for s in args.scopes.split(",") if s.strip())
                expires = datetime.now(timezone.utc) + timedelta(days=args.expires_days) if args.expires_days else None
                token = create_key(conn, control, args.tenant, pepper(), label=args.label, scopes=scopes, expires_at=expires)
                if args.token_file:
                    fd = os.open(args.token_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                    with os.fdopen(fd, "w") as fh:
                        fh.write(token + "\n")
                    print(f"key {parse_token(token)[0]} issued; token written to {args.token_file}")
                else:
                    print(token)
            elif args.cmd == "revoke-key":
                print("revoked" if revoke_key(conn, control, args.key_id) else "no active key with that id")
            elif args.cmd == "set-status":
                print("updated" if set_tenant_status(conn, control, args.tenant, args.status) else "no such tenant")
            elif args.cmd == "list-tenants":
                for t in list_tenants(conn, control):
                    print(f"{t['tenant_key']}  {t['status']:<10}  {t['label']}")
            elif args.cmd == "list-keys":
                for k in list_keys(conn, control, args.tenant):
                    print(f"{k['key_id']}  {k['tenant_key']}  {k['status']:<7}  {','.join(k['scopes'])}  {k['label']}")
        return 0
    except (KeyConfigError, ValueError, psycopg.Error, OSError) as exc:
        print(f"error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
