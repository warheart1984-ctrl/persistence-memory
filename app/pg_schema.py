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

_V2 = """
-- Deterministic JSON snapshot of a record: timestamps rendered in UTC, independent of
-- the session TimeZone, so hashes recompute identically anywhere.
CREATE FUNCTION jarvis_memory_json(m memories) RETURNS jsonb LANGUAGE sql STABLE AS $fn$
    SELECT to_jsonb(m) || jsonb_build_object(
        'created_at', to_char(m.created_at AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US"+00:00"'),
        'updated_at', to_char(m.updated_at AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US"+00:00"'))
$fn$;

CREATE FUNCTION jarvis_history_hash(prev text, op text, version bigint, before jsonb, after jsonb)
RETURNS text LANGUAGE sql IMMUTABLE AS $fn$
    SELECT encode(sha256(convert_to(
        prev || '|' || op || '|' || version::text || '|' ||
        coalesce(before::text, '') || '|' || coalesce(after::text, ''), 'UTF8')), 'hex')
$fn$;

CREATE TABLE record_history (
    history_id bigserial PRIMARY KEY,
    tenant_key text   NOT NULL CHECK (tenant_key <> ''),
    memory_id  text   NOT NULL,
    version    bigint NOT NULL,
    op         text   NOT NULL CHECK (op IN ('create', 'update', 'delete', 'backfill')),
    actor      text   NOT NULL,
    changed_at timestamptz NOT NULL DEFAULT now(),
    before     jsonb,
    after      jsonb,
    prev_hash  text   NOT NULL CHECK (prev_hash ~ '^[0-9a-f]{64}$'),
    row_hash   text   NOT NULL CHECK (row_hash ~ '^[0-9a-f]{64}$'),
    CHECK ((op IN ('create', 'backfill') AND before IS NULL AND after IS NOT NULL)
        OR (op = 'update' AND before IS NOT NULL AND after IS NOT NULL)
        OR (op = 'delete' AND before IS NOT NULL AND after IS NULL))
);
CREATE INDEX record_history_record_idx ON record_history (tenant_key, memory_id, history_id);

-- Backfill: rows that exist before history capture get a genesis entry with op='backfill'
-- and actor='migration:backfill'.  It records the row's state at upgrade time (its real
-- created_at is inside 'after'); it is NOT a claim that a create happened at that moment.
-- (NO FORCE lets the owner see every tenant for this one statement.)
ALTER TABLE memories NO FORCE ROW LEVEL SECURITY;
INSERT INTO record_history (tenant_key, memory_id, version, op, actor, before, after, prev_hash, row_hash)
SELECT m.tenant_key, m.id, m.version, 'backfill', 'migration:backfill', NULL, jarvis_memory_json(m),
       repeat('0', 64),
       jarvis_history_hash(repeat('0', 64), 'backfill', m.version, NULL, jarvis_memory_json(m))
FROM memories m ORDER BY m.tenant_key, m.id;
ALTER TABLE memories FORCE ROW LEVEL SECURITY;

CREATE FUNCTION jarvis_history_immutable() RETURNS trigger LANGUAGE plpgsql AS $fn$
BEGIN
    RAISE EXCEPTION 'record_history is append-only';
END
$fn$;
CREATE TRIGGER record_history_no_update BEFORE UPDATE ON record_history
    FOR EACH ROW EXECUTE FUNCTION jarvis_history_immutable();
CREATE TRIGGER record_history_no_delete BEFORE DELETE ON record_history
    FOR EACH ROW EXECUTE FUNCTION jarvis_history_immutable();
CREATE TRIGGER record_history_no_truncate BEFORE TRUNCATE ON record_history
    FOR EACH STATEMENT EXECUTE FUNCTION jarvis_history_immutable();

ALTER TABLE record_history ENABLE ROW LEVEL SECURITY;
ALTER TABLE record_history FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON record_history
    USING (tenant_key = current_setting('jarvis.tenant_key', true))
    WITH CHECK (tenant_key = current_setting('jarvis.tenant_key', true));

-- Capture every create/update/delete in the same transaction as the change, chained per record.
CREATE FUNCTION jarvis_record_history() RETURNS trigger LANGUAGE plpgsql AS $fn$
DECLARE
    t text; mid text; opn text; b jsonb; a jsonb; v bigint; prev text; act text;
BEGIN
    IF TG_OP = 'DELETE' THEN
        t := OLD.tenant_key; mid := OLD.id; opn := 'delete'; b := jarvis_memory_json(OLD); a := NULL; v := OLD.version;
    ELSIF TG_OP = 'UPDATE' THEN
        t := NEW.tenant_key; mid := NEW.id; opn := 'update'; b := jarvis_memory_json(OLD); a := jarvis_memory_json(NEW); v := NEW.version;
    ELSE
        t := NEW.tenant_key; mid := NEW.id; opn := 'create'; b := NULL; a := jarvis_memory_json(NEW); v := NEW.version;
    END IF;
    PERFORM pg_advisory_xact_lock(hashtextextended(t || '/' || mid, 0));
    SELECT h.row_hash INTO prev FROM record_history h
        WHERE h.tenant_key = t AND h.memory_id = mid ORDER BY h.history_id DESC LIMIT 1;
    prev := coalesce(prev, repeat('0', 64));
    act := coalesce(nullif(current_setting('jarvis.actor', true), ''), 'unknown');
    INSERT INTO record_history (tenant_key, memory_id, version, op, actor, before, after, prev_hash, row_hash)
    VALUES (t, mid, v, opn, act, b, a, prev, jarvis_history_hash(prev, opn, v, b, a));
    RETURN NULL;
END
$fn$;
CREATE TRIGGER memories_history AFTER INSERT OR UPDATE OR DELETE ON memories
    FOR EACH ROW EXECUTE FUNCTION jarvis_record_history();

-- Verifier: per-record chain integrity, plus every live row must equal its last history entry
-- (catches truncated tails and writes that bypassed the triggers).
CREATE FUNCTION jarvis_verify_history(p_tenant text, p_memory_id text DEFAULT NULL)
RETURNS TABLE (history_id bigint, memory_id text, problem text) LANGUAGE plpgsql AS $fn$
DECLARE
    r record; cur_mem text := NULL; last_hash text := NULL; calc text;
BEGIN
    FOR r IN
        SELECT * FROM record_history h
        WHERE h.tenant_key = p_tenant AND (p_memory_id IS NULL OR h.memory_id = p_memory_id)
        ORDER BY h.memory_id COLLATE "C", h.history_id
    LOOP
        IF cur_mem IS DISTINCT FROM r.memory_id THEN
            cur_mem := r.memory_id; last_hash := repeat('0', 64);
        END IF;
        IF r.prev_hash <> last_hash THEN
            RETURN QUERY SELECT r.history_id, r.memory_id,
                'prev_hash does not match the previous entry (an entry was removed or reordered)'::text;
        END IF;
        calc := jarvis_history_hash(r.prev_hash, r.op, r.version, r.before, r.after);
        IF calc <> r.row_hash THEN
            RETURN QUERY SELECT r.history_id, r.memory_id, 'row_hash mismatch (entry was altered)'::text;
        END IF;
        last_hash := r.row_hash;
    END LOOP;

    -- Head consistency: live rows vs. the latest entry of their chain.
    RETURN QUERY
    SELECT lh.history_id, m.id, 'live record differs from its latest history entry (or has none)'::text
    FROM memories m
    LEFT JOIN LATERAL (
        SELECT h.history_id, h.op, h.after FROM record_history h
        WHERE h.tenant_key = m.tenant_key AND h.memory_id = m.id ORDER BY h.history_id DESC LIMIT 1
    ) lh ON true
    WHERE m.tenant_key = p_tenant AND (p_memory_id IS NULL OR m.id = p_memory_id)
      AND (lh.history_id IS NULL OR lh.op = 'delete' OR lh.after IS DISTINCT FROM jarvis_memory_json(m));
END
$fn$;
"""

MIGRATIONS: list[tuple[int, str]] = [(1, _V1), (2, _V2)]
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
    if conn.execute("SELECT to_regclass('record_history') IS NOT NULL").fetchone()[0]:
        # append-only for the app: no UPDATE/DELETE/TRUNCATE is ever granted
        conn.execute(sql.SQL("GRANT SELECT, INSERT ON record_history TO {}").format(r))
        conn.execute(sql.SQL("GRANT USAGE, SELECT ON SEQUENCE record_history_history_id_seq TO {}").format(r))


def migrate(
    dsn: str,
    *,
    schema: str | None = None,
    app_role: str | None = None,
    up_to: int | None = None,
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
            if version > current and (up_to is None or version <= up_to):
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
