"""Continuity Blocks on PostgreSQL: sealing, chaining, immutability, verification, tamper and missing-block proofs,
and mutation checks that show each verification check is the one doing its job."""

from __future__ import annotations

import contextlib
import hashlib
import threading

import psycopg
import pytest
from psycopg import IsolationLevel

from app import blocks, pg_schema, pg_store, pg_verify
from app.evidence import CES_FACT, EvidenceObjectCreate
from app.models import EvidenceLink, MemoryCreate, MemoryUpdate
from app.pg_schema import EXPECTED_SCHEMA_VERSION, StoreUnavailableError, migrate
from app.pg_store import PostgresRowStore

pytestmark = pytest.mark.postgres

H = [hashlib.sha256(f"leaf-{i}".encode()).hexdigest() for i in range(40)]
SEAL_COLUMNS = ("sealed", "height", "first_seq", "last_seq", "entry_count", "block_hash", "reason")
ALL_CHECKS = ("heights", "link", "tiling", "counter", "count", "root", "hash")


# --- fixtures and helpers ------------------------------------------------------------------------------------

@pytest.fixture
def pg(pg_schema):
    assert migrate(pg_schema.admin_dsn, schema=pg_schema.schema, app_role="jarvis_app_test") == EXPECTED_SCHEMA_VERSION
    yield pg_schema
    pg_store.close_pools()


@pytest.fixture
def store(pg):
    return PostgresRowStore(pg.app_dsn, "alice", schema=pg.schema)


@pytest.fixture
def verify_env(pg, monkeypatch):
    monkeypatch.setenv("JARVIS_DATABASE_MIGRATE_URL", pg.admin_dsn)
    monkeypatch.setenv("JARVIS_DATABASE_SCHEMA", pg.schema)


def run_verify(capsys, *args):
    rc = pg_verify.main(list(args) or ["--tenant", "alice"])
    out = capsys.readouterr()
    assert "postgresql://" not in out.out + out.err
    return rc, out.out


def mk(store, n, prefix="record"):
    return [store.create_memory(MemoryCreate(content=f"{prefix} number {i} of the block tests", source_agent="t", session_id="s", type="fact"))
            for i in range(n)]


def seal(pg, tenant="alice", *, min_entries=500, max_age="1 hour", force=False, max_entries=10000):
    with pg.app_conn(tenant) as conn:
        conn.execute("SELECT set_config('jarvis.actor', 'test-sealer', true)")
        row = conn.execute(
            "SELECT sealed, height, first_seq, last_seq, entry_count, block_hash, reason "
            "FROM jarvis_seal_block(%s, %s, %s::interval, %s, %s)", (tenant, min_entries, max_age, force, max_entries)).fetchone()
    return dict(zip(SEAL_COLUMNS, row))


def seal_all(pg, tenant="alice", size=3):
    """Seal everything currently in the history, `size` entries per block; returns the sealed blocks."""
    out = []
    while True:
        r = seal(pg, tenant, force=True, max_entries=size)
        if not r["sealed"]:
            return out
        out.append(r)


def block_rows(pg, tenant="alice"):
    with pg.admin_conn() as conn:
        return conn.execute(
            "SELECT height, first_seq, last_seq, entry_count, prev_block_hash, entries_root, block_hash, format, sealed_by "
            "FROM blocks WHERE tenant_key = %s ORDER BY height", (tenant,)).fetchall()


def verify_blocks_sql(pg, tenant="alice"):
    with pg.admin_conn() as conn:
        return conn.execute("SELECT height, problem FROM jarvis_verify_blocks(%s)", (tenant,)).fetchall()


def verify_history_sql(pg, tenant="alice"):
    with pg.admin_conn() as conn:
        return conn.execute("SELECT history_id, memory_id, problem FROM jarvis_verify_history(%s)", (tenant,)).fetchall()


def history_hashes(pg, first, last, tenant="alice"):
    with pg.admin_conn() as conn:
        return [r[0] for r in conn.execute(
            "SELECT row_hash FROM record_history WHERE tenant_key = %s AND seq BETWEEN %s AND %s ORDER BY seq", (tenant, first, last))]


@contextlib.contextmanager
def tampering(pg, *pairs):
    """A superuser who switches off the guard triggers (table, trigger) for the duration of the tamper."""
    with pg.admin_conn() as conn:
        for table, trig in pairs:
            conn.execute(f"ALTER TABLE {table} DISABLE TRIGGER {trig}")
        try:
            yield conn
        finally:
            for table, trig in pairs:
                conn.execute(f"ALTER TABLE {table} ENABLE TRIGGER {trig}")


BLOCKS_RW = (("blocks", "blocks_no_update"), ("blocks", "blocks_no_delete"))
HISTORY_RW = (("record_history", "record_history_no_update"), ("record_history", "record_history_no_delete"))


def forge(pg, height, tenant="alice", **over):
    """Rewrite a block so that everything NOT overridden stays self-consistent (root, count and hash are recomputed)."""
    cur = {r[0]: r for r in block_rows(pg, tenant)}[height]
    f = dict(first_seq=cur[1], last_seq=cur[2], entry_count=cur[3], prev_block_hash=cur[4], entries_root=cur[5], block_hash=cur[6])
    leaves = history_hashes(pg, over.get("first_seq", f["first_seq"]), over.get("last_seq", f["last_seq"]), tenant)
    recomputed = {}
    if "entries_root" not in over:
        recomputed["entries_root"] = blocks.merkle_root(leaves)
    if "entry_count" not in over:
        recomputed["entry_count"] = len(leaves)
    f.update(over)
    f.update(recomputed)
    if "block_hash" not in over:
        f["block_hash"] = blocks.block_hash(
            tenant=tenant, height=height, first_seq=f["first_seq"], last_seq=f["last_seq"], entry_count=f["entry_count"],
            prev_block_hash=f["prev_block_hash"], entries_root=f["entries_root"])
    with tampering(pg, *BLOCKS_RW) as conn:
        conn.execute(
            "UPDATE blocks SET first_seq=%(first_seq)s, last_seq=%(last_seq)s, entry_count=%(entry_count)s, "
            "prev_block_hash=%(prev_block_hash)s, entries_root=%(entries_root)s, block_hash=%(block_hash)s "
            "WHERE tenant_key=%(t)s AND height=%(h)s", f | {"t": tenant, "h": height})
    return f


def delete_block(pg, height, tenant="alice"):
    with tampering(pg, *BLOCKS_RW) as conn:
        conn.execute("DELETE FROM blocks WHERE tenant_key = %s AND height = %s", (tenant, height))


def install_verifier(pg, skip):
    """Replace jarvis_verify_blocks with a copy that leaves the named checks out (mutation testing)."""
    ddl = pg_schema._v6(set(skip))
    body = ddl[ddl.index("CREATE FUNCTION jarvis_verify_blocks"):].replace("CREATE FUNCTION", "CREATE OR REPLACE FUNCTION", 1)
    with pg.admin_conn() as conn:
        conn.execute(body)


def seven_records_in_three_blocks(pg, store):
    mk(store, 7)
    sealed = seal_all(pg, size=3)
    assert [(b["height"], b["first_seq"], b["last_seq"], b["entry_count"]) for b in sealed] == [(1, 1, 3, 3), (2, 4, 6, 3), (3, 7, 7, 1)]
    assert verify_blocks_sql(pg) == []
    return sealed


# --- the migration and the hashing agree with Python -----------------------------------------------------------

def test_the_schema_is_at_least_version_6(pg):
    assert EXPECTED_SCHEMA_VERSION >= 6
    with pg.admin_conn() as conn:
        assert conn.execute("SELECT to_regclass('blocks') IS NOT NULL").fetchone()[0]
        for fn in ("jarvis_merkle_root", "jarvis_block_hash", "jarvis_seal_block", "jarvis_verify_blocks"):
            assert conn.execute("SELECT count(*) FROM pg_proc WHERE proname = %s", (fn,)).fetchone()[0] == 1


def test_running_the_migration_again_changes_nothing(pg, store):
    mk(store, 4)
    seal_all(pg)
    before = block_rows(pg)
    assert migrate(pg.admin_dsn, schema=pg.schema, app_role="jarvis_app_test") == EXPECTED_SCHEMA_VERSION
    assert block_rows(pg) == before


@pytest.mark.parametrize("n", [1, 2, 3, 4, 5, 6, 7, 8, 9, 13, 16, 17, 33, 40])
def test_the_sql_merkle_root_equals_the_python_one(pg, n):
    with pg.admin_conn() as conn:
        assert conn.execute("SELECT jarvis_merkle_root(%s::text[])", (H[:n],)).fetchone()[0] == blocks.merkle_root(H[:n])


def test_the_sql_merkle_root_refuses_an_empty_block(pg):
    with pg.admin_conn() as conn:
        with pytest.raises(psycopg.errors.RaiseException, match="at least one entry"):
            conn.execute("SELECT jarvis_merkle_root(ARRAY[]::text[])")


@pytest.mark.parametrize("tenant", ["alice", "héllo wörld", "a|1", "日本語"])
def test_the_sql_block_hash_equals_the_python_one(pg, tenant):
    f = dict(height=12, first_seq=31, last_seq=40, entry_count=10, prev_block_hash=H[1], entries_root=H[2])
    with pg.admin_conn() as conn:
        got = conn.execute("SELECT jarvis_block_hash(1, %s, %s, %s, %s, %s, %s, %s)",
                           (tenant, f["height"], f["first_seq"], f["last_seq"], f["entry_count"], f["prev_block_hash"], f["entries_root"])).fetchone()[0]
    assert got == blocks.block_hash(tenant=tenant, **f)


# --- sealing --------------------------------------------------------------------------------------------------

def test_nothing_to_seal_on_an_empty_history(pg):
    r = seal(pg, force=True)
    assert r["sealed"] is False and r["reason"] == "no history to seal"
    assert block_rows(pg) == []


def test_below_threshold_nothing_is_sealed_and_the_reason_says_why(pg, store):
    mk(store, 3)
    r = seal(pg)  # defaults: 500 entries or 1 hour
    assert r["sealed"] is False and r["reason"].startswith("below threshold: 3 unsealed entries (need 500)")
    assert block_rows(pg) == []


def test_the_entry_threshold_seals(pg, store):
    mk(store, 3)
    assert seal(pg, min_entries=4)["sealed"] is False
    r = seal(pg, min_entries=3)
    assert r["sealed"] is True and (r["height"], r["first_seq"], r["last_seq"], r["entry_count"]) == (1, 1, 3, 3)


def test_the_age_threshold_seals_even_a_single_entry(pg, store):
    mk(store, 1)
    assert seal(pg, max_age="1 hour")["sealed"] is False
    r = seal(pg, max_age="0 seconds")
    assert r["sealed"] is True and r["entry_count"] == 1
    assert seal(pg, max_age="0 seconds")["reason"] == "nothing new to seal"  # an empty block is never made


def test_force_seals_whatever_is_there(pg, store):
    mk(store, 2)
    assert seal(pg, force=True)["entry_count"] == 2


def test_a_seal_is_capped_and_the_next_call_continues_where_it_stopped(pg, store):
    mk(store, 7)
    rs = seal_all(pg, size=3)
    assert [r["entry_count"] for r in rs] == [3, 3, 1]
    rows = block_rows(pg)
    assert [r[0] for r in rows] == [1, 2, 3]
    assert rows[0][4] == blocks.GENESIS_HASH
    assert rows[1][4] == rows[0][6] and rows[2][4] == rows[1][6]
    assert all(r[8] == "test-sealer" and r[7] == 1 for r in rows)
    for h, first, last, count, prev, root, bh, fmt, _ in rows:  # the stored values are what Python computes
        assert root == blocks.merkle_root(history_hashes(pg, first, last))
        assert bh == blocks.block_hash(tenant="alice", height=h, first_seq=first, last_seq=last, entry_count=count, prev_block_hash=prev, entries_root=root)


def test_new_history_after_a_seal_goes_into_the_next_block(pg, store):
    mk(store, 2)
    seal(pg, force=True)
    rec = mk(store, 1, "later")[0]
    store.update_memory(rec.id, MemoryUpdate(subject="x"))
    store.delete_memory(rec.id)  # a delete is an appended entry, so sealed history stays append-only
    r = seal(pg, force=True)
    assert (r["height"], r["first_seq"], r["last_seq"]) == (2, 3, 5)
    assert verify_blocks_sql(pg) == [] and verify_history_sql(pg) == []


def test_the_seal_needs_the_session_tenant_and_read_committed(pg, store):
    mk(store, 2)
    with pytest.raises(psycopg.errors.RaiseException, match="not the session tenant"):
        with pg.app_conn("alice") as conn:
            conn.execute("SELECT * FROM jarvis_seal_block('bob', 1, '1 hour', true, 10)")
    with pytest.raises(psycopg.errors.RaiseException, match="not the session tenant"):
        with pg.app_conn(None) as conn:
            conn.execute("SELECT * FROM jarvis_seal_block('alice', 1, '1 hour', true, 10)")
    conn = psycopg.connect(pg.app_dsn, options=f"-c search_path={pg.schema}")
    conn.isolation_level = IsolationLevel.REPEATABLE_READ
    try:
        conn.execute("SELECT set_config('jarvis.tenant_key', 'alice', true)")
        with pytest.raises(psycopg.errors.RaiseException, match="READ COMMITTED"):
            conn.execute("SELECT * FROM jarvis_seal_block('alice', 1, '1 hour', true, 10)")
    finally:
        conn.close()
    assert block_rows(pg) == []


def test_tenants_have_their_own_independent_chains(pg, store):
    bob = PostgresRowStore(pg.app_dsn, "bob", schema=pg.schema)
    mk(store, 3)
    mk(bob, 2, "bobs")
    a, b = seal(pg, "alice", force=True), seal(pg, "bob", force=True)
    assert (a["height"], a["first_seq"], a["last_seq"]) == (1, 1, 3) and (b["height"], b["first_seq"], b["last_seq"]) == (1, 1, 2)
    assert a["block_hash"] != b["block_hash"]
    with pg.app_conn("alice") as conn:
        assert [r[0] for r in conn.execute("SELECT tenant_key FROM blocks")] == ["alice"]
    assert verify_blocks_sql(pg, "alice") == [] and verify_blocks_sql(pg, "bob") == []


# --- immutability and privileges -----------------------------------------------------------------------------

def test_the_application_role_can_read_blocks_but_never_write_them(pg, store):
    mk(store, 2)
    seal(pg, force=True)
    with pg.app_conn("alice") as conn:
        assert conn.execute("SELECT count(*) FROM blocks").fetchone()[0] == 1
    attempts = [
        "UPDATE blocks SET sealed_by = 'x'", "DELETE FROM blocks", "TRUNCATE blocks",
        "INSERT INTO blocks (tenant_key, height, first_seq, last_seq, entry_count, prev_block_hash, entries_root, block_hash, format, sealed_by) "
        f"VALUES ('alice', 2, 3, 3, 1, '{'0' * 64}', '{'0' * 64}', '{'0' * 64}', 1, 'forger')",
    ]
    for statement in attempts:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            with pg.app_conn("alice") as conn:
                conn.execute(statement)
    assert len(block_rows(pg)) == 1


def test_even_the_owner_is_stopped_by_the_triggers(pg, store):
    mk(store, 2)
    seal(pg, force=True)
    for statement in ("UPDATE blocks SET sealed_by = 'x'", "DELETE FROM blocks", "TRUNCATE blocks"):
        with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
            with pg.admin_conn() as conn:
                conn.execute(statement)


def test_only_the_application_role_may_execute_the_seal(pg):
    with pg.admin_conn() as conn:
        conn.execute("DROP ROLE IF EXISTS jarvis_nobody_test")
        conn.execute("CREATE ROLE jarvis_nobody_test NOLOGIN")
        try:
            sig = "jarvis_seal_block(text, integer, interval, boolean, integer)"
            assert conn.execute("SELECT has_function_privilege('jarvis_app_test', %s, 'EXECUTE')", (sig,)).fetchone()[0] is True
            assert conn.execute("SELECT has_function_privilege('jarvis_nobody_test', %s, 'EXECUTE')", (sig,)).fetchone()[0] is False
        finally:
            conn.execute("DROP ROLE jarvis_nobody_test")


def test_the_table_refuses_malformed_rows_whatever_the_caller_says(pg):
    z = "0" * 64
    bad = [
        ("height 0", f"('t', 0, 1, 1, 1, '{z}', '{z}', '{z}', 1, 'x')"),
        ("short hash", f"('t', 1, 1, 1, 1, 'abc', '{z}', '{z}', 1, 'x')"),
        ("format 2", f"('t', 1, 1, 1, 1, '{z}', '{z}', '{z}', 2, 'x')"),
        ("empty range", f"('t', 1, 5, 4, 1, '{z}', '{z}', '{z}', 1, 'x')"),
    ]
    with pg.admin_conn() as conn:
        for _, values in bad:
            with pytest.raises(psycopg.errors.CheckViolation):
                conn.execute("INSERT INTO blocks (tenant_key, height, first_seq, last_seq, entry_count, prev_block_hash, entries_root, block_hash, format, sealed_by) "
                             f"VALUES {values}")


# --- the history is never rewritten ---------------------------------------------------------------------------

def _snapshot(pg):
    with pg.admin_conn() as conn:
        return {t: conn.execute(f"SELECT * FROM {t} ORDER BY 1, 2").fetchall()
                for t in ("memories", "record_history", "chain_heads", "history_counters", "evidence_objects")}


def test_migrating_a_populated_v5_database_leaves_every_existing_table_byte_for_byte_alone(pg_schema):
    migrate(pg_schema.admin_dsn, schema=pg_schema.schema, app_role="jarvis_app_test", up_to=5)
    with pg_schema.admin_conn() as conn:
        with conn.transaction():
            conn.execute("SELECT set_config('jarvis.tenant_key', 'alice', true)")
            conn.execute("SELECT set_config('jarvis.actor', 'v5-writer', true)")
            for i in range(3):
                conn.execute(
                    "INSERT INTO memories (tenant_key, id, content, content_sha256, created_at, updated_at, source_agent, session_id, type, status, confidence) "
                    "VALUES ('alice', %s, %s, %s, now(), now(), 't', 's', 'fact', 'draft', 0.5)", (f"mem-v5-{i}", f"v5 record {i}", H[i]))
            conn.execute("UPDATE memories SET subject = 'edited' WHERE id = 'mem-v5-0'")
            conn.execute("INSERT INTO evidence_objects (tenant_key, id, schema_id, payload, size_bytes, created_by) "
                         "VALUES ('alice', %s, 's', '{}', 2, 'x')", ("eo:sha256:" + "a" * 64,))
        assert conn.execute("SELECT to_regclass('blocks') IS NULL").fetchone()[0]
    before = _snapshot(pg_schema)
    assert len(before["record_history"]) == 4
    assert migrate(pg_schema.admin_dsn, schema=pg_schema.schema, app_role="jarvis_app_test") == EXPECTED_SCHEMA_VERSION
    assert _snapshot(pg_schema) == before
    r = seal(pg_schema, force=True)  # the first seal covers the pre-existing history
    assert (r["height"], r["first_seq"], r["last_seq"], r["entry_count"]) == (1, 1, 4, 4)
    assert _snapshot(pg_schema) == before  # and sealing changes none of it either
    assert verify_blocks_sql(pg_schema) == [] and verify_history_sql(pg_schema) == []


def test_v5_code_refuses_a_v6_database_so_rollback_means_restoring_the_backup(pg, monkeypatch):
    monkeypatch.setattr(pg_schema, "EXPECTED_SCHEMA_VERSION", 5)
    with pg.admin_conn() as conn:
        with pytest.raises(StoreUnavailableError, match="does not match expected 5"):
            pg_schema.check_schema_version(conn)
    with pytest.raises(StoreUnavailableError, match="newer than this code"):
        migrate(pg.admin_dsn, schema=pg.schema)


# --- clean databases verify clean ------------------------------------------------------------------------------

def test_a_normal_database_verifies_clean_and_pg_verify_reports_the_blocks(pg, store, verify_env, capsys):
    mk(store, 7)
    seal_all(pg, size=3)
    mk(store, 2, "tail")
    rc, out = run_verify(capsys)
    assert rc == 0 and "blocks intact: 3 block(s) sealed through seq 7, 2 entries unsealed" in out
    assert "WARNING" not in out  # a fresh tail is normal


def test_a_database_without_blocks_yet_verifies_clean(pg, store, verify_env, capsys):
    mk(store, 2)
    rc, out = run_verify(capsys)
    assert rc == 0 and "0 block(s) sealed through seq 0, 2 entries unsealed" in out


def test_a_stale_unsealed_tail_is_a_warning_not_a_failure(pg, store, verify_env, capsys):
    mk(store, 3)
    with tampering(pg, *HISTORY_RW) as conn:  # backdate the entries; changed_at is not part of any hash
        conn.execute("UPDATE record_history SET changed_at = now() - interval '48 hours'")
    rc, out = run_verify(capsys)
    assert rc == 0 and "WARNING tenant=alice: 3 unsealed entries" in out and "seal timer may have stopped" in out
    seal(pg, force=True)
    rc, out = run_verify(capsys)
    assert rc == 0 and "WARNING" not in out


def test_pg_verify_all_checks_every_tenants_blocks(pg, store, verify_env, capsys):
    bob = PostgresRowStore(pg.app_dsn, "bob", schema=pg.schema)
    mk(store, 3)
    mk(bob, 3, "bobs")
    seal(pg, "alice", force=True)
    seal(pg, "bob", force=True)
    rc, out = run_verify(capsys, "--all")
    assert rc == 0 and "2 tenant(s)" in out
    with tampering(pg, *BLOCKS_RW) as conn:
        conn.execute("UPDATE blocks SET block_hash = %s WHERE tenant_key = 'bob'", ("b" * 64,))
    rc, out = run_verify(capsys, "--all")
    assert rc == 1 and "tenant=bob record=block 1" in out and "tenant=alice" not in out


def test_concurrent_writers_and_sealers_never_leave_a_gap_or_an_overlap(pg, store, verify_env, capsys):
    errors: list[BaseException] = []
    done = threading.Event()

    def writer(w):
        try:
            for i in range(30):
                store.create_memory(MemoryCreate(content=f"writer {w} record {i} under a sealer", source_agent="t", session_id="s", type="fact"))
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    def sealer():
        try:
            while not done.is_set():
                seal(pg, force=True, max_entries=25)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    writers = [threading.Thread(target=writer, args=(w,)) for w in range(4)]
    sealers = [threading.Thread(target=sealer) for _ in range(3)]  # three sealers racing each other and the writers
    [t.start() for t in sealers + writers]
    [t.join() for t in writers]
    done.set()
    [t.join() for t in sealers]
    assert errors == []
    seal_all(pg, size=25)
    rows = block_rows(pg)
    assert sum(r[3] for r in rows) == 120  # 4 writers x 30 creates, each exactly once
    assert rows[0][1] == 1 and rows[-1][2] == 120 and all(b[1] == a[2] + 1 for a, b in zip(rows, rows[1:]))
    assert verify_blocks_sql(pg) == [] and verify_history_sql(pg) == []
    rc, out = run_verify(capsys)
    assert rc == 0 and "0 entries unsealed" in out


def test_a_seal_does_not_wait_for_or_hold_up_a_writer_in_flight(pg, store):
    """An uncommitted write holds the counter row; the seal must neither wait for it nor include it."""
    mk(store, 3)
    writer = psycopg.connect(pg.app_dsn, options=f"-c search_path={pg.schema}")
    try:
        writer.execute("SELECT set_config('jarvis.tenant_key', 'alice', true)")
        writer.execute(
            "INSERT INTO memories (tenant_key, id, content, content_sha256, created_at, updated_at, source_agent, session_id, type, status, confidence) "
            "VALUES ('alice', 'mem-inflight', 'in flight', %s, now(), now(), 't', 's', 'fact', 'draft', 0.5)", (H[0],))
        box: dict = {}
        sealer = threading.Thread(target=lambda: box.update(r=seal(pg, force=True)))
        sealer.start()
        sealer.join(timeout=10)
        assert not sealer.is_alive(), "the seal queued behind an in-flight write"
        assert (box["r"]["sealed"], box["r"]["last_seq"]) == (True, 3)  # sealed what is committed; the in-flight entry is not in it
        writer.commit()
    finally:
        writer.close()
    r2 = seal(pg, force=True)
    assert (r2["first_seq"], r2["last_seq"]) == (4, 4)
    assert verify_blocks_sql(pg) == [] and verify_history_sql(pg) == []


# --- tamper proofs --------------------------------------------------------------------------------------------

def test_a_forged_block_field_is_found_by_the_verifier_and_by_pg_verify(pg, store, verify_env, capsys):
    seven_records_in_three_blocks(pg, store)
    with tampering(pg, *BLOCKS_RW) as conn:
        conn.execute("UPDATE blocks SET entries_root = %s WHERE height = 2", ("c" * 64,))
    problems = verify_blocks_sql(pg)
    assert any(h == 2 and "entries_root" in p for h, p in problems) and any(h == 2 and "block_hash" in p for h, p in problems)
    rc, out = run_verify(capsys)
    assert rc == 1 and "record=block 2" in out


@pytest.mark.parametrize("column,value,keyword", [
    ("block_hash", "d" * 64, "block_hash does not match"),
    ("entry_count", 99, "entry count"),
    ("prev_block_hash", "e" * 64, "prev_block_hash"),
])
def test_each_forged_column_is_named(pg, store, verify_env, capsys, column, value, keyword):
    seven_records_in_three_blocks(pg, store)
    with tampering(pg, *BLOCKS_RW) as conn:
        conn.execute(f"UPDATE blocks SET {column} = %s WHERE height = 2", (value,))
    assert any(keyword in p for _, p in verify_blocks_sql(pg))
    rc, out = run_verify(capsys)
    assert rc == 1 and "block 2" in out


def _rewrite_entry_consistently(pg, memory_id, seq, new_content):
    """The strongest tamper on a record: change what a history entry says AND the live row, recompute the entry's
    row_hash and the chain head, leaving the per-record chain perfectly self-consistent."""
    pairs = (("record_history", "record_history_no_update"), ("memories", "memories_history"), ("memories", "memories_bump_version"))
    with tampering(pg, *pairs) as conn:
        conn.execute("UPDATE memories SET content = %s, content_sha256 = %s WHERE id = %s",
                     (new_content, hashlib.sha256(new_content.encode()).hexdigest(), memory_id))
        conn.execute("UPDATE record_history h SET after = jarvis_memory_json(m) FROM memories m WHERE h.seq = %s AND m.id = %s", (seq, memory_id))
        conn.execute("UPDATE record_history SET row_hash = jarvis_history_hash(prev_hash, op, version, before, after) WHERE seq = %s", (seq,))
        conn.execute("UPDATE chain_heads SET last_hash = (SELECT row_hash FROM record_history WHERE seq = %s) WHERE id = %s", (seq, memory_id))


def test_a_sealed_entry_rewritten_so_the_record_chain_still_passes_is_caught_only_by_the_block(pg, store, verify_env, capsys):
    recs = mk(store, 5)
    store.update_memory(recs[2].id, MemoryUpdate(subject="before the forgery"))  # seq 6, the last entry of recs[2]
    seal_all(pg, size=4)
    assert verify_blocks_sql(pg) == [] and verify_history_sql(pg) == []
    _rewrite_entry_consistently(pg, recs[2].id, 6, "a forged version of this record")
    assert verify_history_sql(pg) == []  # the per-record chain, its head and the live row all agree: undetectable there
    problems = verify_blocks_sql(pg)
    assert [h for h, _ in problems] == [2] and "entries_root" in problems[0][1]
    rc, out = run_verify(capsys)
    assert rc == 1 and "record=block 2" in out and "entries_root" in out


def test_removing_a_history_entry_inside_a_sealed_range_is_found(pg, store, verify_env, capsys):
    seven_records_in_three_blocks(pg, store)
    with tampering(pg, *HISTORY_RW) as conn:
        conn.execute("DELETE FROM record_history WHERE seq = 5")
    problems = [p for h, p in verify_blocks_sql(pg) if h == 2]
    assert any("entry count" in p for p in problems) and any("entries_root" in p for p in problems)
    assert run_verify(capsys)[0] == 1


def test_a_lowered_history_counter_is_found_at_the_block_that_reaches_past_it(pg, store, verify_env, capsys):
    seven_records_in_three_blocks(pg, store)
    with pg.admin_conn() as conn:
        conn.execute("UPDATE history_counters SET last_seq = last_seq - 1 WHERE tenant_key = 'alice'")
    assert any(h == 3 and "past the history counter" in p for h, p in verify_blocks_sql(pg))
    assert run_verify(capsys)[0] == 1


# --- missing-block proofs -------------------------------------------------------------------------------------

def test_a_missing_middle_block_is_found_three_ways(pg, store, verify_env, capsys):
    seven_records_in_three_blocks(pg, store)
    delete_block(pg, 2)
    problems = verify_blocks_sql(pg)
    assert (2, "missing block (a block was removed)") in problems
    assert any(h == 3 and "prev_block_hash" in p for h, p in problems)
    assert any(h == 3 and "does not continue the history" in p for h, p in problems)
    rc, out = run_verify(capsys)
    assert rc == 1 and "record=block 2" in out and "a block is missing" in out


def test_a_missing_first_block_is_found(pg, store, verify_env, capsys):
    seven_records_in_three_blocks(pg, store)
    delete_block(pg, 1)
    problems = verify_blocks_sql(pg)
    assert (1, "missing block (a block was removed)") in problems
    assert any(h == 2 and "prev_block_hash" in p for h, p in problems)
    assert run_verify(capsys)[0] == 1


def test_removing_the_tip_block_is_invisible_to_the_database_alone(pg, store, verify_env, capsys):
    """KNOWN LIMIT, closed in the next PR: with the last block gone nothing in the database refers to it, so the
    chain is still a valid chain, just shorter.  Only an anchor kept outside the database (the block head written
    into the backup anchors) can notice.  This test pins the behaviour so nobody mistakes the database for that guard."""
    seven_records_in_three_blocks(pg, store)
    delete_block(pg, 3)
    assert verify_blocks_sql(pg) == []
    rc, out = run_verify(capsys)
    assert rc == 0 and "2 block(s) sealed through seq 6, 1 entry unsealed" in out


def test_rewriting_the_whole_block_chain_consistently_passes_every_check_in_the_database(pg, store, verify_env, capsys):
    """KNOWN LIMIT, closed in the next PR by the external anchor: someone with full database control can rewrite an
    entry AND re-seal every block after it.  The database cannot tell; the anchor (block hash kept elsewhere) can."""
    recs = mk(store, 5)
    store.update_memory(recs[2].id, MemoryUpdate(subject="before the forgery"))
    seal_all(pg, size=4)
    before_tip = block_rows(pg)[-1][6]
    _rewrite_entry_consistently(pg, recs[2].id, 6, "a forged version of this record")
    prev = blocks.GENESIS_HASH
    for height, *_ in block_rows(pg):
        prev = forge(pg, height, prev_block_hash=prev)["block_hash"]
    assert verify_history_sql(pg) == [] and verify_blocks_sql(pg) == []
    assert run_verify(capsys)[0] == 0
    assert block_rows(pg)[-1][6] != before_tip  # the one thing that changed is the tip hash an anchor would have recorded


# --- evidence cited by sealed history -----------------------------------------------------------------------

def _linked_fact(store):
    obj, _ = store.put_evidence_object(EvidenceObjectCreate(
        schema_id=CES_FACT, payload={"observation": "The ledger listens on 127.0.0.1:8011.", "method": "command", "source": "ss -ltn"}))
    store.create_memory(MemoryCreate(content="The ledger listens on loopback.", source_agent="t", session_id="s", type="fact",
                                     evidence=[EvidenceLink(kind="evidence-object", ref=obj.id)]))
    return obj


def test_a_cited_evidence_object_that_goes_missing_is_a_block_level_error(pg, store, verify_env, capsys):
    obj = _linked_fact(store)
    seal(pg, force=True)
    assert run_verify(capsys)[0] == 0
    with tampering(pg, ("evidence_objects", "evidence_objects_no_delete")) as conn:
        conn.execute("DELETE FROM evidence_objects")
    rc, out = run_verify(capsys)
    assert rc == 1 and "record=block 1" in out and f"cites evidence object {obj.id}, which does not exist" in out


def test_a_cited_evidence_object_that_was_altered_is_a_block_level_error(pg, store, verify_env, capsys):
    obj = _linked_fact(store)
    seal(pg, force=True)
    with tampering(pg, ("evidence_objects", "evidence_objects_no_update")) as conn:
        conn.execute("UPDATE evidence_objects SET payload = jsonb_set(payload, '{source}', '\"forged\"')")
    rc, out = run_verify(capsys)
    assert rc == 1 and f"cites evidence object {obj.id}: " in out and "record=block 1" in out


def test_evidence_cited_by_a_later_unsealed_entry_is_not_a_block_matter(pg, store, verify_env, capsys):
    mk(store, 2)
    seal(pg, force=True)
    _linked_fact(store)  # unsealed
    with tampering(pg, ("evidence_objects", "evidence_objects_no_delete")) as conn:
        conn.execute("DELETE FROM evidence_objects")
    rc, out = run_verify(capsys)
    assert rc == 0 and "PROBLEM" not in out  # nothing sealed cites it, so no block is blamed


# --- mutation checks: each verification check is the one catching its tamper -----------------------------------
# For each check, forge a block chain that is consistent EVERYWHERE except what that one check looks at.  The real
# verifier must flag it; the verifier rebuilt WITHOUT that check must stay silent.  If a check were redundant (or
# the test tamper accidentally tripped another check) the mutated verifier would still report and the test fail.

def _tamper_heights(pg, store):
    seven_records_in_three_blocks(pg, store)
    b1 = block_rows(pg)[0]
    delete_block(pg, 2)
    forge(pg, 3, first_seq=4, last_seq=7, prev_block_hash=b1[6])  # block 3 now covers 4..7 and links to block 1


def _tamper_link(pg, store):
    seven_records_in_three_blocks(pg, store)
    forge(pg, 2, prev_block_hash="f" * 64)


def _tamper_tiling(pg, store):
    mk(store, 6)
    seal_all(pg, size=3)
    forge(pg, 2, first_seq=3)  # overlaps block 1 by one entry; root, count, hash and link all recomputed


def _tamper_counter(pg, store):
    seven_records_in_three_blocks(pg, store)
    with pg.admin_conn() as conn:
        conn.execute("UPDATE history_counters SET last_seq = last_seq - 1 WHERE tenant_key = 'alice'")


def _tamper_count(pg, store):
    mk(store, 6)
    seal_all(pg, size=3)  # two blocks; forging the tip leaves no later block whose prev link would also break
    forge(pg, 2, entry_count=4)  # root and hash computed over the real 3 entries, but the block claims 4


def _tamper_root(pg, store):
    seven_records_in_three_blocks(pg, store)
    with tampering(pg, *HISTORY_RW) as conn:
        conn.execute("UPDATE record_history SET row_hash = %s WHERE seq = 5", ("a" * 64,))


def _tamper_hash(pg, store):
    mk(store, 3)
    seal_all(pg, size=3)
    with tampering(pg, *BLOCKS_RW) as conn:
        conn.execute("UPDATE blocks SET block_hash = %s", ("b" * 64,))


MUTATIONS = [
    ("heights", _tamper_heights, "missing block"),
    ("link", _tamper_link, "prev_block_hash"),
    ("tiling", _tamper_tiling, "does not continue the history"),
    ("counter", _tamper_counter, "past the history counter"),
    ("count", _tamper_count, "entry count"),
    ("root", _tamper_root, "entries_root"),
    ("hash", _tamper_hash, "block_hash does not match"),
]


@pytest.mark.parametrize("check,tamper,keyword", MUTATIONS, ids=[m[0] for m in MUTATIONS])
def test_each_check_is_the_one_that_catches_its_tamper(pg, store, check, tamper, keyword):
    tamper(pg, store)
    real = verify_blocks_sql(pg)
    assert real and all(keyword in p for _, p in real), real  # the full verifier flags it, and only for this reason
    install_verifier(pg, {check})
    assert verify_blocks_sql(pg) == []  # without that one check the same tamper goes unnoticed


@pytest.mark.parametrize("check,tamper,keyword", [m for m in MUTATIONS if m[0] != "counter"], ids=[m[0] for m in MUTATIONS if m[0] != "counter"])
def test_the_python_recomputation_catches_what_a_broken_sql_verifier_would_miss(pg, store, verify_env, capsys, check, tamper, keyword):
    tamper(pg, store)
    install_verifier(pg, set(ALL_CHECKS))  # a verifier that checks nothing (a tampered or buggy function)
    assert verify_blocks_sql(pg) == []
    rc, out = run_verify(capsys)
    assert rc == 1 and "(recompute)" in out


def test_the_cited_evidence_check_is_the_one_that_catches_a_missing_object(pg, store, verify_env, capsys, monkeypatch):
    _linked_fact(store)
    seal(pg, force=True)
    with tampering(pg, ("evidence_objects", "evidence_objects_no_delete")) as conn:
        conn.execute("DELETE FROM evidence_objects")
    assert run_verify(capsys)[0] == 1
    monkeypatch.setattr(pg_verify, "_block_evidence_problems", lambda conn, tenant: [])
    assert run_verify(capsys)[0] == 0  # nothing else in the verifier notices a vanished cited object


def test_the_mutation_helper_refuses_an_unknown_check():
    with pytest.raises(ValueError, match="unknown block checks"):
        pg_schema._v6({"nonsense"})
    assert set(pg_schema._BLOCK_CHECKS) == set(ALL_CHECKS)
