"""Row-level PostgreSQL schema for the Continuity Ledger, with a versioned migration runner.

Requires PostgreSQL 15+ (``ON DELETE SET NULL (column)``).

Isolation is layered: every query filters on ``tenant_key`` *and* row-level security
(``FORCE``d, so the table owner is subject to it too) only exposes rows whose tenant
matches the per-transaction setting ``jarvis.tenant_key``.  Superusers bypass RLS in
PostgreSQL, so the application must connect as an ordinary role.
"""

from __future__ import annotations

import re

import psycopg
from psycopg import sql

from app.store_errors import StoreUnavailableError

_SCHEMA_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,62}$")

_V1 = """
CREATE TABLE boards (
    tenant_key text PRIMARY KEY CHECK (tenant_key <> ''),
    board      jsonb  NOT NULL CHECK (jsonb_typeof(board) = 'object'),
    version    bigint NOT NULL DEFAULT 1
);

CREATE TABLE memories (
    tenant_key     text   NOT NULL CHECK (tenant_key <> ''),
    id             text   NOT NULL CHECK (char_length(id) BETWEEN 1 AND 128),
    version        bigint NOT NULL DEFAULT 1,
    content        text   NOT NULL CHECK (char_length(content) BETWEEN 1 AND 2000),
    content_sha256 text   NOT NULL CHECK (content_sha256 ~ '^[0-9a-f]{64}$'),
    created_at     timestamptz NOT NULL,
    updated_at     timestamptz NOT NULL,
    source_agent   text   NOT NULL CHECK (char_length(source_agent) <= 128),
    session_id     text   NOT NULL CHECK (char_length(session_id) BETWEEN 1 AND 128),
    type           text   NOT NULL CHECK (type IN
        ('decision','fact','task','preference','architecture','research','external_context')),
    status         text   NOT NULL CHECK (status IN ('draft','verified','archived')),
    confidence     double precision NOT NULL CHECK (confidence BETWEEN 0 AND 1),
    subject        text   CHECK (subject IS NULL OR char_length(subject) <= 256),
    supersedes     text,
    tags           text[] NOT NULL DEFAULT '{}' CHECK (cardinality(tags) <= 32),
    evidence       jsonb  NOT NULL DEFAULT '[]'
        CHECK (jsonb_typeof(evidence) = 'array' AND jsonb_array_length(evidence) <= 32),
    PRIMARY KEY (tenant_key, id),
    FOREIGN KEY (tenant_key, supersedes) REFERENCES memories (tenant_key, id)
        ON DELETE SET NULL (supersedes)
);
CREATE INDEX memories_status_idx  ON memories (tenant_key, status);
CREATE INDEX memories_subject_idx ON memories (tenant_key, subject) WHERE subject IS NOT NULL;
CREATE INDEX memories_session_idx ON memories (tenant_key, session_id);
CREATE INDEX memories_created_idx ON memories (tenant_key, created_at DESC, id DESC);

-- version is owned by the database: every UPDATE bumps it, clients cannot set it,
-- and a row's identity can never change.
CREATE FUNCTION jarvis_bump_version() RETURNS trigger LANGUAGE plpgsql AS $fn$
BEGIN
    IF NEW.tenant_key IS DISTINCT FROM OLD.tenant_key THEN
        RAISE EXCEPTION 'tenant_key is immutable';
    END IF;
    IF TG_TABLE_NAME = 'memories' THEN
        IF NEW.id IS DISTINCT FROM OLD.id THEN
            RAISE EXCEPTION 'id is immutable';
        END IF;
    END IF;
    NEW.version := OLD.version + 1;
    RETURN NEW;
END
$fn$;
CREATE TRIGGER memories_bump_version BEFORE UPDATE ON memories
    FOR EACH ROW EXECUTE FUNCTION jarvis_bump_version();
CREATE TRIGGER boards_bump_version BEFORE UPDATE ON boards
    FOR EACH ROW EXECUTE FUNCTION jarvis_bump_version();

ALTER TABLE memories ENABLE ROW LEVEL SECURITY;
ALTER TABLE memories FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON memories
    USING (tenant_key = current_setting('jarvis.tenant_key', true))
    WITH CHECK (tenant_key = current_setting('jarvis.tenant_key', true));
ALTER TABLE boards ENABLE ROW LEVEL SECURITY;
ALTER TABLE boards FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON boards
    USING (tenant_key = current_setting('jarvis.tenant_key', true))
    WITH CHECK (tenant_key = current_setting('jarvis.tenant_key', true));
"""

MIGRATIONS: list[tuple[int, str]] = [(1, _V1)]
EXPECTED_SCHEMA_VERSION = MIGRATIONS[-1][0]


def validate_schema_name(schema: str) -> str:
    if not _SCHEMA_RE.match(schema):
        raise ValueError(f"invalid schema name: {schema!r}")
    return schema


def _grant(conn: psycopg.Connection, schema: str, role: str) -> None:
    s, r = sql.Identifier(schema), sql.Identifier(role)
    conn.execute(sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(s, r))
    conn.execute(sql.SQL("GRANT SELECT, INSERT, UPDATE, DELETE ON memories, boards TO {}").format(r))
    conn.execute(sql.SQL("GRANT SELECT ON schema_version TO {}").format(r))


def migrate(
    dsn: str,
    *,
    schema: str | None = None,
    app_role: str | None = None,
) -> int:
    """Apply pending migrations atomically and return the resulting schema version.

    ``dsn`` must belong to a role allowed to run DDL.  ``app_role`` (optional) receives
    only the DML grants the application needs; it never gets DDL or history UPDATE/DELETE.
    """
    if schema is not None:
        validate_schema_name(schema)
    with psycopg.connect(dsn, connect_timeout=5) as conn:
        if schema is not None:
            conn.execute(sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(sql.Identifier(schema)))
            conn.execute(sql.SQL("SET LOCAL search_path TO {}").format(sql.Identifier(schema)))
        conn.execute("SELECT pg_advisory_xact_lock(hashtext('jarvis-ledger-migrate'))")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_version ("
            "version integer PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())"
        )
        row = conn.execute("SELECT coalesce(max(version), 0) FROM schema_version").fetchone()
        current = row[0]
        if current > EXPECTED_SCHEMA_VERSION:
            raise StoreUnavailableError(
                f"database schema version {current} is newer than this code ({EXPECTED_SCHEMA_VERSION})"
            )
        for version, ddl in MIGRATIONS:
            if version > current:
                conn.execute(ddl)
                conn.execute("INSERT INTO schema_version (version) VALUES (%s)", (version,))
                current = version
        if app_role is not None:
            target = schema or conn.execute("SELECT current_schema()").fetchone()[0]
            _grant(conn, target, app_role)
        return current


def check_schema_version(conn: psycopg.Connection) -> None:
    """Fail closed unless the database is migrated to exactly the version this code expects."""
    try:
        row = conn.execute("SELECT max(version) FROM schema_version").fetchone()
    except psycopg.Error as exc:
        raise StoreUnavailableError("Ledger schema is missing; run the migration") from exc
    found = (next(iter(row.values())) if isinstance(row, dict) else row[0]) if row else None
    if found != EXPECTED_SCHEMA_VERSION:
        raise StoreUnavailableError(
            f"Ledger schema version {found} does not match expected {EXPECTED_SCHEMA_VERSION}"
        )
