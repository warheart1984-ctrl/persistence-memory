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

_V4 = """
-- Imports label their history entries as backfills: the importer sets the transaction-local
-- jarvis.history_op = 'backfill', honoured for INSERTs only.  Ordinary creates stay 'create'.
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
        IF current_setting('jarvis.history_op', true) = 'backfill' THEN
            opn := 'backfill';
        END IF;
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
"""

_V5 = """
-- Evidence Objects: content-addressed, immutable evidence (id = eo:sha256:<hash of the canonical content>).
-- Append-only like record_history: the application role can only SELECT and INSERT, and the triggers
-- refuse UPDATE, DELETE and TRUNCATE for everyone else.
CREATE TABLE evidence_objects (
    tenant_key text        NOT NULL,
    id         text        NOT NULL CHECK (id ~ '^eo:sha256:[0-9a-f]{64}$'),
    schema_id  text        NOT NULL CHECK (char_length(schema_id) BETWEEN 1 AND 128),
    payload    jsonb       NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
    pointer    jsonb       CHECK (pointer IS NULL OR jsonb_typeof(pointer) = 'object'),
    size_bytes integer     NOT NULL CHECK (size_bytes BETWEEN 1 AND 65536),
    created_at timestamptz NOT NULL DEFAULT now(),
    created_by text        NOT NULL CHECK (char_length(created_by) BETWEEN 1 AND 128),
    PRIMARY KEY (tenant_key, id)
);
CREATE FUNCTION jarvis_evidence_immutable() RETURNS trigger LANGUAGE plpgsql AS $fn$
BEGIN
    RAISE EXCEPTION 'evidence_objects is append-only';
END
$fn$;
CREATE TRIGGER evidence_objects_no_update BEFORE UPDATE ON evidence_objects
    FOR EACH ROW EXECUTE FUNCTION jarvis_evidence_immutable();
CREATE TRIGGER evidence_objects_no_delete BEFORE DELETE ON evidence_objects
    FOR EACH ROW EXECUTE FUNCTION jarvis_evidence_immutable();
CREATE TRIGGER evidence_objects_no_truncate BEFORE TRUNCATE ON evidence_objects
    FOR EACH STATEMENT EXECUTE FUNCTION jarvis_evidence_immutable();
ALTER TABLE evidence_objects ENABLE ROW LEVEL SECURITY;
ALTER TABLE evidence_objects FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON evidence_objects
    USING (tenant_key = current_setting('jarvis.tenant_key', true))
    WITH CHECK (tenant_key = current_setting('jarvis.tenant_key', true));
"""

# --- v6: Continuity Blocks ---------------------------------------------------------------------------------------
# Each verification check is a separate fragment so the tests can build a copy of the verifier WITHOUT one check
# and prove that a given tamper is caught by that check (and only by it).  See tests/test_pg_blocks.py.
_BLOCK_CHECKS: dict[str, str] = {
    # a missing block: heights must run 1..max without a hole
    "heights": """
    SELECT max(k.height) INTO maxh FROM blocks k WHERE k.tenant_key = p_tenant;
    IF maxh IS NOT NULL THEN
        FOR gap IN
            SELECT g AS n FROM generate_series(1::bigint, maxh) g
            WHERE NOT EXISTS (SELECT 1 FROM blocks k WHERE k.tenant_key = p_tenant AND k.height = g)
            LIMIT 100
        LOOP
            RETURN QUERY SELECT gap.n, 'missing block (a block was removed)'::text;
        END LOOP;
    END IF;
""",
    # each block must name the hash of the block before it (genesis: 64 zeros)
    "link": """
        IF b.prev_block_hash <> prev_hash THEN
            RETURN QUERY SELECT b.height,
                'prev_block_hash does not match the previous block (a block was removed, reordered or altered)'::text;
        END IF;
""",
    # blocks must tile the history: no gap and no overlap between consecutive ranges
    "tiling": """
        IF b.first_seq <> prev_last + 1 THEN
            RETURN QUERY SELECT b.height,
                format('block does not continue the history (expected first_seq %s, found %s)', prev_last + 1, b.first_seq)::text;
        END IF;
""",
    # a block can never reach past what the history counter says exists
    "counter": """
        IF ctr IS NULL OR b.last_seq > ctr THEN
            RETURN QUERY SELECT b.height, 'block extends past the history counter (counter altered or history removed)'::text;
        END IF;
""",
    # the range must still hold exactly the entries the block says it holds
    "count": """
        IF n <> b.entry_count OR b.entry_count <> b.last_seq - b.first_seq + 1 THEN
            RETURN QUERY SELECT b.height,
                format('entry count does not match the history in the block''s range (block says %s, found %s)', b.entry_count, n)::text;
        END IF;
""",
    # the Merkle root must be reproducible from the entries' row hashes (an entry or the root was altered)
    "root": """
        IF root IS DISTINCT FROM b.entries_root THEN
            RETURN QUERY SELECT b.height,
                'entries_root does not match the history entries in the block''s range (an entry or the block was altered)'::text;
        END IF;
""",
    # the block hash must be reproducible from the block's own fields
    "hash": """
        IF jarvis_block_hash(b.format, b.tenant_key, b.height, b.first_seq, b.last_seq, b.entry_count,
                             b.prev_block_hash, b.entries_root) <> b.block_hash THEN
            RETURN QUERY SELECT b.height, 'block_hash does not match the block''s contents (block was altered)'::text;
        END IF;
""",
}
_BLOCK_CHECK_ORDER = ("link", "tiling", "counter", "count", "root", "hash")


def _v6(skip: frozenset[str] | set[str] = frozenset()) -> str:
    """The v6 migration.  ``skip`` (tests only) leaves named checks out of jarvis_verify_blocks."""
    unknown = set(skip) - set(_BLOCK_CHECKS)
    if unknown:
        raise ValueError(f"unknown block checks: {sorted(unknown)}")
    pre = "" if "heights" in skip else _BLOCK_CHECKS["heights"]
    loop = "".join(_BLOCK_CHECKS[name] for name in _BLOCK_CHECK_ORDER if name not in skip)
    return _V6_HEAD + f"""
CREATE FUNCTION jarvis_verify_blocks(p_tenant text)
RETURNS TABLE (height bigint, problem text) LANGUAGE plpgsql AS $fn$
DECLARE
    b record; gap record; ctr bigint; maxh bigint; n bigint; leaves text[]; root text;
    prev_hash text := repeat('0', 64); prev_last bigint := 0;
BEGIN
    SELECT c.last_seq INTO ctr FROM history_counters c WHERE c.tenant_key = p_tenant;
{pre}
    FOR b IN SELECT * FROM blocks k WHERE k.tenant_key = p_tenant ORDER BY k.height LOOP
        SELECT count(*), array_agg(h.row_hash ORDER BY h.seq) INTO n, leaves
        FROM record_history h WHERE h.tenant_key = p_tenant AND h.seq BETWEEN b.first_seq AND b.last_seq;
        root := CASE WHEN n > 0 THEN jarvis_merkle_root(leaves) END;
{loop}
        prev_hash := b.block_hash; prev_last := b.last_seq;
    END LOOP;
END
$fn$;
"""


_V6_HEAD = """
-- Continuity Blocks: immutable seals over contiguous ranges of the per-tenant history (record_history.seq).
-- A block stores no copy of the entries and nothing in the history tables changes: membership is the seq range,
-- the Merkle root commits to the entries' row_hash values, and prev_block_hash chains block to block.
CREATE TABLE blocks (
    tenant_key      text        NOT NULL CHECK (tenant_key <> ''),
    height          bigint      NOT NULL CHECK (height >= 1),
    first_seq       bigint      NOT NULL CHECK (first_seq >= 1),
    last_seq        bigint      NOT NULL,
    entry_count     bigint      NOT NULL CHECK (entry_count >= 1),
    prev_block_hash text        NOT NULL CHECK (prev_block_hash ~ '^[0-9a-f]{64}$'),
    entries_root    text        NOT NULL CHECK (entries_root ~ '^[0-9a-f]{64}$'),
    block_hash      text        NOT NULL CHECK (block_hash ~ '^[0-9a-f]{64}$'),
    format          integer     NOT NULL CHECK (format = 1),
    sealed_at       timestamptz NOT NULL DEFAULT now(),
    sealed_by       text        NOT NULL CHECK (char_length(sealed_by) BETWEEN 1 AND 128),
    PRIMARY KEY (tenant_key, height),
    UNIQUE (tenant_key, first_seq),
    CHECK (last_seq >= first_seq)
);
CREATE FUNCTION jarvis_blocks_immutable() RETURNS trigger LANGUAGE plpgsql AS $fn$
BEGIN
    RAISE EXCEPTION 'blocks is append-only';
END
$fn$;
CREATE TRIGGER blocks_no_update BEFORE UPDATE ON blocks
    FOR EACH ROW EXECUTE FUNCTION jarvis_blocks_immutable();
CREATE TRIGGER blocks_no_delete BEFORE DELETE ON blocks
    FOR EACH ROW EXECUTE FUNCTION jarvis_blocks_immutable();
CREATE TRIGGER blocks_no_truncate BEFORE TRUNCATE ON blocks
    FOR EACH STATEMENT EXECUTE FUNCTION jarvis_blocks_immutable();
ALTER TABLE blocks ENABLE ROW LEVEL SECURITY;
ALTER TABLE blocks FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON blocks
    USING (tenant_key = current_setting('jarvis.tenant_key', true))
    WITH CHECK (tenant_key = current_setting('jarvis.tenant_key', true));

-- RFC 6962 Merkle tree hash over the entries' row_hash values (hex), in order.  Leaf = sha256(0x00 || raw hash),
-- node = sha256(0x01 || left || right).  Pairing level by level and carrying an unpaired last node up unchanged
-- gives exactly the RFC's tree (split at the largest power of two below n); app/blocks.py implements the
-- RFC's recursive definition separately, and the tests compare the two.
CREATE FUNCTION jarvis_merkle_root(leaves text[]) RETURNS text LANGUAGE plpgsql IMMUTABLE AS $fn$
DECLARE
    lvl bytea[]; nxt bytea[]; n integer; i integer;
BEGIN
    n := coalesce(array_length(leaves, 1), 0);
    IF n = 0 THEN
        RAISE EXCEPTION 'a block must contain at least one entry';
    END IF;
    FOR i IN 1..n LOOP
        lvl[i] := sha256(decode('00', 'hex') || decode(leaves[i], 'hex'));
    END LOOP;
    WHILE n > 1 LOOP
        nxt := ARRAY[]::bytea[];
        FOR i IN 1..(n / 2) LOOP
            nxt[i] := sha256(decode('01', 'hex') || lvl[2 * i - 1] || lvl[2 * i]);
        END LOOP;
        IF n % 2 = 1 THEN
            nxt[n / 2 + 1] := lvl[n];
        END IF;
        lvl := nxt;
        n := array_length(lvl, 1);
    END LOOP;
    RETURN encode(lvl[1], 'hex');
END
$fn$;

CREATE FUNCTION jarvis_block_hash(fmt integer, tenant text, height bigint, first_seq bigint, last_seq bigint,
                                  entry_count bigint, prev_block_hash text, entries_root text)
RETURNS text LANGUAGE sql IMMUTABLE AS $fn$
    SELECT encode(sha256(convert_to(
        'jarvis-block|v' || fmt::text || '|' || octet_length(convert_to(tenant, 'UTF8'))::text || ':' || tenant || '|' ||
        height::text || '|' || first_seq::text || '|' || last_seq::text || '|' || entry_count::text || '|' ||
        prev_block_hash || '|' || entries_root, 'UTF8')), 'hex')
$fn$;

-- Seal the next block of the session tenant's history.  Runs as the table owner so the application role needs
-- no write access to blocks: it can only ask for a seal, and the hashes are computed here, never supplied.
-- Every write holds the tenant's counter row until it commits, so commit order is seq order: any counter value a
-- seal can read is backed by committed entries 1..counter, whatever writers are doing, and nothing here blocks
-- them.  Sealers are serialised by an advisory lock; READ COMMITTED makes the statements after that lock see the
-- block the previous sealer just committed (under REPEATABLE READ they would not, and would collide on the height).
CREATE FUNCTION jarvis_seal_block(
    p_tenant text, p_min_entries integer DEFAULT 500, p_max_age interval DEFAULT interval '1 hour',
    p_force boolean DEFAULT false, p_max_entries integer DEFAULT 10000)
RETURNS TABLE (sealed boolean, height bigint, first_seq bigint, last_seq bigint, entry_count bigint,
               block_hash text, reason text)
LANGUAGE plpgsql SECURITY DEFINER SET search_path FROM CURRENT AS $fn$
DECLARE
    ctr bigint; bh bigint; bl bigint; bprev text; s bigint; e bigint; n bigint; have bigint;
    leaves text[]; root text; hsh text; oldest timestamptz; act text;
BEGIN
    IF p_tenant IS NULL OR p_tenant = '' THEN
        RAISE EXCEPTION 'jarvis_seal_block: a tenant is required';
    END IF;
    IF current_setting('jarvis.tenant_key', true) IS DISTINCT FROM p_tenant THEN
        RAISE EXCEPTION 'jarvis_seal_block: tenant % is not the session tenant', p_tenant;
    END IF;
    IF current_setting('transaction_isolation') <> 'read committed' THEN
        RAISE EXCEPTION 'jarvis_seal_block needs READ COMMITTED (it relies on seeing the previous sealer''s block once it holds the seal lock)';
    END IF;
    IF p_min_entries < 1 OR p_max_entries < 1 THEN
        RAISE EXCEPTION 'jarvis_seal_block: thresholds must be at least 1';
    END IF;
    PERFORM pg_advisory_xact_lock(hashtextextended('jarvis-seal/' || p_tenant, 0));
    SELECT c.last_seq INTO ctr FROM history_counters c WHERE c.tenant_key = p_tenant;
    IF ctr IS NULL THEN
        RETURN QUERY SELECT false, NULL::bigint, NULL::bigint, NULL::bigint, NULL::bigint, NULL::text, 'no history to seal'::text;
        RETURN;
    END IF;
    SELECT k.height, k.last_seq, k.block_hash INTO bh, bl, bprev
        FROM blocks k WHERE k.tenant_key = p_tenant ORDER BY k.height DESC LIMIT 1;
    IF NOT FOUND THEN
        bh := 0; bl := 0; bprev := repeat('0', 64);
    END IF;
    IF bl > ctr THEN
        RAISE EXCEPTION 'jarvis_seal_block: the last block extends past the history counter; run pg_verify before sealing';
    END IF;
    s := bl + 1;
    IF s > ctr THEN
        RETURN QUERY SELECT false, NULL::bigint, NULL::bigint, NULL::bigint, NULL::bigint, NULL::text, 'nothing new to seal'::text;
        RETURN;
    END IF;
    SELECT h.changed_at INTO oldest FROM record_history h WHERE h.tenant_key = p_tenant AND h.seq = s;
    IF oldest IS NULL THEN
        RAISE EXCEPTION 'jarvis_seal_block: history entry % is missing; run pg_verify before sealing', s;
    END IF;
    IF NOT p_force AND (ctr - s + 1) < p_min_entries AND clock_timestamp() - oldest < p_max_age THEN
        RETURN QUERY SELECT false, NULL::bigint, NULL::bigint, NULL::bigint, NULL::bigint, NULL::text,
            format('below threshold: %s unsealed entries (need %s) and the oldest is younger than %s', ctr - s + 1, p_min_entries, p_max_age)::text;
        RETURN;
    END IF;
    e := least(ctr, s + p_max_entries - 1);
    n := e - s + 1;
    SELECT count(*), array_agg(h.row_hash ORDER BY h.seq) INTO have, leaves
        FROM record_history h WHERE h.tenant_key = p_tenant AND h.seq BETWEEN s AND e;
    IF have <> n THEN
        RAISE EXCEPTION 'jarvis_seal_block: history is missing entries between seq % and %; run pg_verify before sealing', s, e;
    END IF;
    root := jarvis_merkle_root(leaves);
    hsh := jarvis_block_hash(1, p_tenant, bh + 1, s, e, n, bprev, root);
    act := coalesce(nullif(current_setting('jarvis.actor', true), ''), 'unknown');
    INSERT INTO blocks (tenant_key, height, first_seq, last_seq, entry_count, prev_block_hash, entries_root, block_hash, format, sealed_by)
        VALUES (p_tenant, bh + 1, s, e, n, bprev, root, hsh, 1, act);
    RETURN QUERY SELECT true, bh + 1, s, e, n, hsh, 'sealed'::text;
END
$fn$;
REVOKE ALL ON FUNCTION jarvis_seal_block(text, integer, interval, boolean, integer) FROM PUBLIC;
"""

_V6 = _v6()

_V7 = """
-- Signatures, verification side: the log of attestations (blocks, replay receipts, checkpoints) and the log of trust statements
-- (which keys may sign).  Both append-only like record_history; the application role can read them and can only add a row
-- through the store functions, which enforce the chain (gapless sequence, previous hash) and compute the hashes.  The signature
-- itself is checked by the application before it calls the function (the database has no Ed25519), and again by every verifier.
-- Nothing here holds a private key.
CREATE FUNCTION jarvis_attestation_message(kind text, tenant text, subject text, subject_hash text, signer_seq bigint,
                                           prev_hash text, signed_at text) RETURNS text LANGUAGE sql IMMUTABLE AS $fn$
    SELECT 'jarvis-attest|v1|' || kind || '|' || octet_length(convert_to(tenant, 'UTF8'))::text || ':' || tenant || '|' ||
           subject || '|' || subject_hash || '|' || signer_seq::text || '|' || prev_hash || '|' || signed_at
$fn$;

CREATE FUNCTION jarvis_trust_message(kind text, tenant text, key_id text, arg bigint, subject_hash text, stmt_seq bigint,
                                     prev_hash text) RETURNS text LANGUAGE sql IMMUTABLE AS $fn$
    SELECT 'jarvis-trust|v1|' || kind || '|' || octet_length(convert_to(tenant, 'UTF8'))::text || ':' || tenant || '|' ||
           key_id || '|' || coalesce(arg::text, '') || '|' || coalesce(subject_hash, '') || '|' || stmt_seq::text || '|' || prev_hash
$fn$;

-- sha256(message || LF || key id || LF || signature): the signature is stored already normalised (LF line ends, no blanks around)
CREATE FUNCTION jarvis_signed_hash(message text, key_id text, signature text) RETURNS text LANGUAGE sql IMMUTABLE AS $fn$
    SELECT encode(sha256(convert_to(message || chr(10) || key_id || chr(10) || signature, 'UTF8')), 'hex')
$fn$;

CREATE TABLE attestations (
    tenant_key       text        NOT NULL CHECK (tenant_key <> ''),
    signer_seq       bigint      NOT NULL CHECK (signer_seq >= 1),
    kind             text        NOT NULL CHECK (kind IN ('block', 'receipt', 'checkpoint')),
    subject          text        NOT NULL CHECK (char_length(subject) BETWEEN 1 AND 300 AND subject !~ '[|[:cntrl:]]'),
    subject_hash     text        NOT NULL CHECK (subject_hash ~ '^[0-9a-f]{64}$'),
    prev_hash        text        NOT NULL CHECK (prev_hash ~ '^[0-9a-f]{64}$'),
    key_id           text        NOT NULL CHECK (key_id ~ '^SHA256:[A-Za-z0-9+/]{43}$'),
    signed_at        text        NOT NULL CHECK (signed_at ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$'),
    signature        text        NOT NULL CHECK (char_length(signature) BETWEEN 100 AND 4000
                                                 AND signature = btrim(signature, ' ' || chr(10) || chr(13) || chr(9))
                                                 AND position(chr(13) in signature) = 0),
    attestation_hash text        NOT NULL CHECK (attestation_hash ~ '^[0-9a-f]{64}$'),
    stored_at        timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_key, signer_seq)
);

CREATE TABLE trust_statements (
    tenant_key     text        NOT NULL CHECK (tenant_key <> ''),
    stmt_seq       bigint      NOT NULL CHECK (stmt_seq >= 1),
    kind           text        NOT NULL CHECK (kind IN ('key', 'revoke', 'root_add', 'cosign', 'void')),
    key_id         text        NOT NULL CHECK (key_id ~ '^SHA256:[A-Za-z0-9+/]{43}$'),
    pubkey         text        CHECK (pubkey IS NULL OR char_length(pubkey) BETWEEN 50 AND 600),
    arg            bigint      CHECK (arg IS NULL OR arg >= 0),
    subject_hash   text        CHECK (subject_hash IS NULL OR subject_hash ~ '^[0-9a-f]{64}$'),
    prev_hash      text        NOT NULL CHECK (prev_hash ~ '^[0-9a-f]{64}$'),
    signed_by      text        NOT NULL CHECK (signed_by ~ '^SHA256:[A-Za-z0-9+/]{43}$'),
    signature      text        NOT NULL CHECK (char_length(signature) BETWEEN 100 AND 4000
                                               AND signature = btrim(signature, ' ' || chr(10) || chr(13) || chr(9))
                                               AND position(chr(13) in signature) = 0),
    statement_hash text        NOT NULL CHECK (statement_hash ~ '^[0-9a-f]{64}$'),
    stored_at      timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_key, stmt_seq)
);

CREATE FUNCTION jarvis_signatures_immutable() RETURNS trigger LANGUAGE plpgsql AS $fn$
BEGIN
    RAISE EXCEPTION '% is append-only', TG_TABLE_NAME;
END
$fn$;
CREATE TRIGGER attestations_no_update BEFORE UPDATE ON attestations FOR EACH ROW EXECUTE FUNCTION jarvis_signatures_immutable();
CREATE TRIGGER attestations_no_delete BEFORE DELETE ON attestations FOR EACH ROW EXECUTE FUNCTION jarvis_signatures_immutable();
CREATE TRIGGER attestations_no_truncate BEFORE TRUNCATE ON attestations FOR EACH STATEMENT EXECUTE FUNCTION jarvis_signatures_immutable();
CREATE TRIGGER trust_statements_no_update BEFORE UPDATE ON trust_statements FOR EACH ROW EXECUTE FUNCTION jarvis_signatures_immutable();
CREATE TRIGGER trust_statements_no_delete BEFORE DELETE ON trust_statements FOR EACH ROW EXECUTE FUNCTION jarvis_signatures_immutable();
CREATE TRIGGER trust_statements_no_truncate BEFORE TRUNCATE ON trust_statements FOR EACH STATEMENT EXECUTE FUNCTION jarvis_signatures_immutable();
ALTER TABLE attestations ENABLE ROW LEVEL SECURITY;
ALTER TABLE attestations FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON attestations USING (tenant_key = current_setting('jarvis.tenant_key', true))
    WITH CHECK (tenant_key = current_setting('jarvis.tenant_key', true));
ALTER TABLE trust_statements ENABLE ROW LEVEL SECURITY;
ALTER TABLE trust_statements FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON trust_statements USING (tenant_key = current_setting('jarvis.tenant_key', true))
    WITH CHECK (tenant_key = current_setting('jarvis.tenant_key', true));

-- Add the next attestation.  Runs as the table owner; enforces the chain (the next signer_seq, the previous attestation's hash)
-- and computes the stored hash.  SQLSTATE JA001 = the sequence or previous hash is wrong (someone else added one first).
CREATE FUNCTION jarvis_store_attestation(p_tenant text, p_kind text, p_subject text, p_subject_hash text, p_signer_seq bigint,
                                         p_prev_hash text, p_key_id text, p_signed_at text, p_signature text)
RETURNS TABLE (signer_seq bigint, attestation_hash text) LANGUAGE plpgsql SECURITY DEFINER SET search_path FROM CURRENT AS $fn$
DECLARE
    last_seq bigint; last_hash text; msg text; h text;
BEGIN
    IF current_setting('jarvis.tenant_key', true) IS DISTINCT FROM p_tenant THEN
        RAISE EXCEPTION 'jarvis_store_attestation: tenant % is not the session tenant', p_tenant;
    END IF;
    PERFORM pg_advisory_xact_lock(hashtextextended('jarvis-attest/' || p_tenant, 0));
    SELECT a.signer_seq, a.attestation_hash INTO last_seq, last_hash
        FROM attestations a WHERE a.tenant_key = p_tenant ORDER BY a.signer_seq DESC LIMIT 1;
    IF NOT FOUND THEN
        last_seq := 0; last_hash := repeat('0', 64);
    END IF;
    IF p_signer_seq <> last_seq + 1 THEN
        RAISE EXCEPTION 'signer_seq must be % (the log head is %)', last_seq + 1, last_seq USING ERRCODE = 'JA001';
    END IF;
    IF p_prev_hash <> last_hash THEN
        RAISE EXCEPTION 'prev_hash is not the hash of attestation %', last_seq USING ERRCODE = 'JA001';
    END IF;
    msg := jarvis_attestation_message(p_kind, p_tenant, p_subject, p_subject_hash, p_signer_seq, p_prev_hash, p_signed_at);
    h := jarvis_signed_hash(msg, p_key_id, p_signature);
    INSERT INTO attestations (tenant_key, signer_seq, kind, subject, subject_hash, prev_hash, key_id, signed_at, signature, attestation_hash)
        VALUES (p_tenant, p_signer_seq, p_kind, p_subject, p_subject_hash, p_prev_hash, p_key_id, p_signed_at, p_signature, h);
    RETURN QUERY SELECT p_signer_seq, h;
END
$fn$;

CREATE FUNCTION jarvis_store_trust_statement(p_tenant text, p_kind text, p_key_id text, p_pubkey text, p_arg bigint,
                                             p_subject_hash text, p_stmt_seq bigint, p_prev_hash text, p_signed_by text, p_signature text)
RETURNS TABLE (stmt_seq bigint, statement_hash text) LANGUAGE plpgsql SECURITY DEFINER SET search_path FROM CURRENT AS $fn$
DECLARE
    last_seq bigint; last_hash text; msg text; h text;
BEGIN
    IF current_setting('jarvis.tenant_key', true) IS DISTINCT FROM p_tenant THEN
        RAISE EXCEPTION 'jarvis_store_trust_statement: tenant % is not the session tenant', p_tenant;
    END IF;
    PERFORM pg_advisory_xact_lock(hashtextextended('jarvis-trust/' || p_tenant, 0));
    SELECT t.stmt_seq, t.statement_hash INTO last_seq, last_hash
        FROM trust_statements t WHERE t.tenant_key = p_tenant ORDER BY t.stmt_seq DESC LIMIT 1;
    IF NOT FOUND THEN
        last_seq := 0; last_hash := repeat('0', 64);
    END IF;
    IF p_stmt_seq <> last_seq + 1 THEN
        RAISE EXCEPTION 'stmt_seq must be % (the log head is %)', last_seq + 1, last_seq USING ERRCODE = 'JA001';
    END IF;
    IF p_prev_hash <> last_hash THEN
        RAISE EXCEPTION 'prev_hash is not the hash of statement %', last_seq USING ERRCODE = 'JA001';
    END IF;
    msg := jarvis_trust_message(p_kind, p_tenant, p_key_id, p_arg, p_subject_hash, p_stmt_seq, p_prev_hash);
    h := jarvis_signed_hash(msg, p_signed_by, p_signature);
    INSERT INTO trust_statements (tenant_key, stmt_seq, kind, key_id, pubkey, arg, subject_hash, prev_hash, signed_by, signature, statement_hash)
        VALUES (p_tenant, p_stmt_seq, p_kind, p_key_id, p_pubkey, p_arg, p_subject_hash, p_prev_hash, p_signed_by, p_signature, h);
    RETURN QUERY SELECT p_stmt_seq, h;
END
$fn$;
REVOKE ALL ON FUNCTION jarvis_store_attestation(text, text, text, text, bigint, text, text, text, text) FROM PUBLIC;
REVOKE ALL ON FUNCTION jarvis_store_trust_statement(text, text, text, text, bigint, text, bigint, text, text, text) FROM PUBLIC;
"""

MIGRATIONS: list[tuple[int, str]] = [(1, _V1), (2, _V2), (3, _V3), (4, _V4), (5, _V5), (6, _V6), (7, _V7)]
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
    if exists("evidence_objects"):
        # v5+: the application can read and add evidence objects, never change or remove one
        conn.execute(sql.SQL("REVOKE ALL ON evidence_objects FROM {}").format(r))
        conn.execute(sql.SQL("GRANT SELECT, INSERT ON evidence_objects TO {}").format(r))
    if exists("blocks"):
        # v6+: the application reads blocks and can only ask for a seal (SECURITY DEFINER function); it never
        # writes a block row itself
        conn.execute(sql.SQL("REVOKE ALL ON blocks FROM {}").format(r))
        conn.execute(sql.SQL("GRANT SELECT ON blocks TO {}").format(r))
        conn.execute(sql.SQL("GRANT EXECUTE ON FUNCTION jarvis_seal_block(text, integer, interval, boolean, integer) TO {}").format(r))
    if exists("attestations"):
        # v7+: the application reads the signing logs and can only add to them through the store functions
        for table in ("attestations", "trust_statements"):
            conn.execute(sql.SQL("REVOKE ALL ON {} FROM {}").format(sql.Identifier(table), r))
            conn.execute(sql.SQL("GRANT SELECT ON {} TO {}").format(sql.Identifier(table), r))
        conn.execute(sql.SQL("GRANT EXECUTE ON FUNCTION jarvis_store_attestation(text, text, text, text, bigint, text, text, text, text) TO {}").format(r))
        conn.execute(sql.SQL("GRANT EXECUTE ON FUNCTION jarvis_store_trust_statement(text, text, text, text, bigint, text, bigint, text, text, text) TO {}").format(r))
    if not exists("chain_heads") and exists("record_history"):
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
