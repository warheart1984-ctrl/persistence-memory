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

_V3 = """
-- Gapless per-tenant history sequence + one chain head per record (also for deleted records).
CREATE TABLE history_counters (
    tenant_key text PRIMARY KEY CHECK (tenant_key <> ''),
    last_seq   bigint NOT NULL CHECK (last_seq >= 0)
);
CREATE TABLE chain_heads (
    tenant_key text    NOT NULL CHECK (tenant_key <> ''),
    id         text    NOT NULL,
    last_seq   bigint  NOT NULL,
    last_hash  text    NOT NULL CHECK (last_hash ~ '^[0-9a-f]{64}$'),
    deleted    boolean NOT NULL,
    PRIMARY KEY (tenant_key, id)
);
ALTER TABLE record_history ADD COLUMN seq bigint;

-- Backfill existing history in history_id order (NO FORCE: the owner sees every tenant here;
-- the append-only trigger is lifted for this one statement and restored right after).
ALTER TABLE record_history NO FORCE ROW LEVEL SECURITY;
ALTER TABLE record_history DISABLE TRIGGER record_history_no_update;
UPDATE record_history h SET seq = n.rn
FROM (SELECT x.history_id, row_number() OVER (PARTITION BY x.tenant_key ORDER BY x.history_id) AS rn
      FROM record_history x) n
WHERE n.history_id = h.history_id;
ALTER TABLE record_history ENABLE TRIGGER record_history_no_update;
ALTER TABLE record_history ALTER COLUMN seq SET NOT NULL;
CREATE UNIQUE INDEX record_history_seq_idx ON record_history (tenant_key, seq);
INSERT INTO history_counters (tenant_key, last_seq)
    SELECT tenant_key, max(seq) FROM record_history GROUP BY tenant_key;
INSERT INTO chain_heads (tenant_key, id, last_seq, last_hash, deleted)
    SELECT DISTINCT ON (tenant_key, memory_id) tenant_key, memory_id, seq, row_hash, (op = 'delete')
    FROM record_history ORDER BY tenant_key, memory_id, history_id DESC;
ALTER TABLE record_history FORCE ROW LEVEL SECURITY;

ALTER TABLE history_counters ENABLE ROW LEVEL SECURITY;
ALTER TABLE history_counters FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON history_counters
    USING (tenant_key = current_setting('jarvis.tenant_key', true))
    WITH CHECK (tenant_key = current_setting('jarvis.tenant_key', true));
ALTER TABLE chain_heads ENABLE ROW LEVEL SECURITY;
ALTER TABLE chain_heads FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON chain_heads
    USING (tenant_key = current_setting('jarvis.tenant_key', true))
    WITH CHECK (tenant_key = current_setting('jarvis.tenant_key', true));

-- The capture trigger now runs as the table owner (SECURITY DEFINER), so the application role
-- needs no write access to the history tables and cannot forge or alter entries, heads or counters.
CREATE OR REPLACE FUNCTION jarvis_record_history() RETURNS trigger LANGUAGE plpgsql
SECURITY DEFINER SET search_path FROM CURRENT AS $fn$
DECLARE
    t text; mid text; opn text; b jsonb; a jsonb; v bigint; prev text; act text; s bigint; rh text;
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
        WHERE h.tenant_key = t AND h.memory_id = mid ORDER BY h.seq DESC LIMIT 1;
    prev := coalesce(prev, repeat('0', 64));
    act := coalesce(nullif(current_setting('jarvis.actor', true), ''), 'unknown');
    INSERT INTO history_counters (tenant_key, last_seq) VALUES (t, 1)
        ON CONFLICT (tenant_key) DO UPDATE SET last_seq = history_counters.last_seq + 1
        RETURNING last_seq INTO s;
    rh := jarvis_history_hash(prev, opn, v, b, a);
    INSERT INTO record_history (tenant_key, memory_id, version, op, actor, before, after, prev_hash, row_hash, seq)
        VALUES (t, mid, v, opn, act, b, a, prev, rh, s);
    INSERT INTO chain_heads (tenant_key, id, last_seq, last_hash, deleted) VALUES (t, mid, s, rh, opn = 'delete')
        ON CONFLICT (tenant_key, id) DO UPDATE
        SET last_seq = EXCLUDED.last_seq, last_hash = EXCLUDED.last_hash, deleted = EXCLUDED.deleted;
    RETURN NULL;
END
$fn$;

CREATE OR REPLACE FUNCTION jarvis_verify_history(p_tenant text, p_memory_id text DEFAULT NULL)
RETURNS TABLE (history_id bigint, memory_id text, problem text) LANGUAGE plpgsql AS $fn$
DECLARE
    r record; hd record; e record; gap record;
    cur_mem text := NULL; last_hash text := NULL; calc text; ctr bigint; maxseq bigint;
BEGIN
    -- 1. per-record hash chain
    FOR r IN
        SELECT * FROM record_history h
        WHERE h.tenant_key = p_tenant AND (p_memory_id IS NULL OR h.memory_id = p_memory_id)
        ORDER BY h.memory_id COLLATE "C", h.seq
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

    -- 2. gapless tenant sequence (whole-tenant verification only)
    IF p_memory_id IS NULL THEN
        SELECT c.last_seq INTO ctr FROM history_counters c WHERE c.tenant_key = p_tenant;
        SELECT max(h.seq) INTO maxseq FROM record_history h WHERE h.tenant_key = p_tenant;
        IF maxseq IS NOT NULL AND (ctr IS NULL OR ctr < maxseq) THEN
            RETURN QUERY SELECT NULL::bigint, NULL::text,
                'history counter is behind the history (counter altered or missing)'::text;
        END IF;
        IF ctr IS NOT NULL THEN
            FOR gap IN
                SELECT g AS n FROM generate_series(1::bigint, ctr) g
                WHERE NOT EXISTS (SELECT 1 FROM record_history h WHERE h.tenant_key = p_tenant AND h.seq = g)
                LIMIT 100
            LOOP
                RETURN QUERY SELECT NULL::bigint, NULL::text,
                    format('missing history sequence number %s (an entry was removed)', gap.n)::text;
            END LOOP;
        END IF;
    END IF;

    -- 3. every chain head must still point at its record's latest entry (deleted records included)
    FOR hd IN
        SELECT ch.id, ch.last_seq, ch.last_hash, ch.deleted FROM chain_heads ch
        WHERE ch.tenant_key = p_tenant AND (p_memory_id IS NULL OR ch.id = p_memory_id)
    LOOP
        SELECT h.* INTO e FROM record_history h WHERE h.tenant_key = p_tenant AND h.seq = hd.last_seq;
        IF NOT FOUND OR e.memory_id <> hd.id THEN
            RETURN QUERY SELECT NULL::bigint, hd.id,
                format('chain head points at a missing history entry (seq %s)', hd.last_seq)::text;
            CONTINUE;
        END IF;
        IF e.row_hash <> hd.last_hash THEN
            RETURN QUERY SELECT e.history_id, hd.id, 'chain head hash does not match its last entry'::text;
        END IF;
        IF (e.op = 'delete') <> hd.deleted THEN
            RETURN QUERY SELECT e.history_id, hd.id, 'chain head deleted flag disagrees with its last entry'::text;
        END IF;
        IF EXISTS (SELECT 1 FROM record_history h
                   WHERE h.tenant_key = p_tenant AND h.memory_id = hd.id AND h.seq > hd.last_seq) THEN
            RETURN QUERY SELECT e.history_id, hd.id, 'record has history newer than its chain head'::text;
        END IF;
    END LOOP;

    -- 4. records with history must have a head
    RETURN QUERY
    SELECT NULL::bigint, d.mid, 'record has history but no chain head'::text
    FROM (SELECT DISTINCT h.memory_id AS mid FROM record_history h
          WHERE h.tenant_key = p_tenant AND (p_memory_id IS NULL OR h.memory_id = p_memory_id)) d
    WHERE NOT EXISTS (SELECT 1 FROM chain_heads ch WHERE ch.tenant_key = p_tenant AND ch.id = d.mid);

    -- 5. every live row must equal the state recorded by its head's entry
    RETURN QUERY
    SELECT lh.history_id, m.id, 'live record differs from its latest history entry (or has none)'::text
    FROM memories m
    LEFT JOIN chain_heads ch ON ch.tenant_key = m.tenant_key AND ch.id = m.id
    LEFT JOIN record_history lh ON lh.tenant_key = m.tenant_key AND lh.seq = ch.last_seq
    WHERE m.tenant_key = p_tenant AND (p_memory_id IS NULL OR m.id = p_memory_id)
      AND (ch.id IS NULL OR ch.deleted OR lh.history_id IS NULL
           OR lh.after IS DISTINCT FROM jarvis_memory_json(m));
END
$fn$;
"""

MIGRATIONS: list[tuple[int, str]] = [(1, _V1), (2, _V2), (3, _V3)]
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
    exists = lambda name: conn.execute("SELECT to_regclass(%s) IS NOT NULL", (name,)).fetchone()[0]  # noqa: E731
    if exists("chain_heads"):
        # v3+: read-only for the app; the SECURITY DEFINER capture trigger is the only writer
        for table in ("record_history", "chain_heads", "history_counters"):
            conn.execute(sql.SQL("REVOKE ALL ON {} FROM {}").format(sql.Identifier(table), r))
            conn.execute(sql.SQL("GRANT SELECT ON {} TO {}").format(sql.Identifier(table), r))
        conn.execute(sql.SQL("REVOKE ALL ON SEQUENCE record_history_history_id_seq FROM {}").format(r))
    elif exists("record_history"):
        # v2 only (transitional): the invoker-rights trigger inserts as the app role
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
