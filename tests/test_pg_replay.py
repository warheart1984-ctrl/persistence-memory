"""RC.Ledger.v1 on PostgreSQL: reconstruction as of any seq, determinism, the offline verifier, tamper proofs, mutations."""

from __future__ import annotations

import hashlib
import random

import psycopg
import pytest

from app import pg_store, replay
from app.evidence import CES_FACT, EvidenceObjectCreate
from app.models import EvidenceLink, MemoryCreate, MemoryUpdate
from app.pg_schema import EXPECTED_SCHEMA_VERSION, migrate
from app.pg_store import PostgresRowStore
from tests.test_pg_blocks import (
    BLOCKS_RW, HISTORY_RW, _rewrite_entry_consistently, block_rows, forge, mk, seal, seal_all, tampering,
)

pytestmark = pytest.mark.postgres


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


# --- helpers -----------------------------------------------------------------------------------------------------

def full(store, **kw) -> replay.ReplayState:
    return store.replay_state(limit=replay.MAX_PAGE, **kw)


def records_of(state: replay.ReplayState) -> dict[str, dict]:
    return {r.id: r.record for r in state.records}


def live_table(pg, tenant="alice") -> dict[str, dict]:
    with pg.admin_conn() as conn:
        return {r[0]: r[1] for r in conn.execute("SELECT m.id, jarvis_memory_json(m) FROM memories m WHERE m.tenant_key = %s", (tenant,))}


def counter(pg, tenant="alice") -> int:
    with pg.admin_conn() as conn:
        row = conn.execute("SELECT last_seq FROM history_counters WHERE tenant_key = %s", (tenant,)).fetchone()
    return row[0] if row else 0


def entries(pg, tenant="alice") -> list[replay.Entry]:
    with pg.admin_conn() as conn:
        return [replay.Entry(*r) for r in conn.execute(
            "SELECT seq, memory_id, op, version, prev_hash, row_hash, before::text, after::text FROM record_history WHERE tenant_key = %s ORDER BY seq", (tenant,))]


def independent_root(pg, at_seq, tenant="alice") -> str:
    """The state root from nothing but the raw entries: a Python fold, no SQL DISTINCT ON."""
    folded = replay.fold_state(entries(pg, tenant), at_seq)
    return replay.state_root([(mid, e.row_hash) for mid, e in folded.live.items()])


def verify(pg, **kw):
    with psycopg.connect(pg.admin_dsn, options=f"-c search_path={pg.schema}") as conn:
        return replay.verify_replay(conn, "alice", **kw)


def checks_of(result) -> set[str]:
    return {p["check"] for p in result["problems"]}


def run_cli(capsys, *args):
    rc = replay.main(["verify", "--tenant", "alice", *args])
    out = capsys.readouterr()
    assert "postgresql://" not in out.out + out.err
    return rc, out.out + out.err


def churn(store, pg, seed=7, steps=60):
    """A reproducible mix of creates, updates, deletes and supersedes; returns [(seq, live table snapshot)] after each step."""
    rng = random.Random(seed)
    ids: list[str] = []
    snaps = []
    for i in range(steps):
        roll = rng.random()
        if roll < 0.45 or not ids:
            sup = rng.choice(ids) if ids and rng.random() < 0.2 else None
            rec = store.create_memory(MemoryCreate(content=f"churn record {i} seed {seed}", source_agent="t", session_id="s", type="fact",
                                                   supersedes=sup, confidence=round(rng.random(), 3), tags=["churn"]))
            ids.append(rec.id)
        elif roll < 0.85:
            store.update_memory(rng.choice(ids), MemoryUpdate(subject=f"subject {i}", confidence=round(rng.random(), 3)))
        else:
            victim = rng.choice(ids)
            store.delete_memory(victim)
            ids.remove(victim)
        snaps.append((counter(pg), live_table(pg)))
    return snaps


# --- reconstruction -------------------------------------------------------------------------------------------------

def test_replay_reproduces_the_ledger_exactly_as_it_was_after_every_single_write(pg, store):
    snaps = churn(store, pg)
    assert len({seq for seq, _ in snaps}) == len(snaps)  # one entry per step, so every step is a distinct point in history
    for seq, snapshot in snaps:
        state = full(store, at_seq=seq)
        assert records_of(state) == snapshot, f"replay at seq {seq} differs from the ledger as it was then"
        assert state.record_count == len(snapshot) and state.at_seq == seq
        assert state.state_root == independent_root(pg, seq)  # the SQL answer equals a plain Python fold of the raw entries


def test_the_state_at_the_current_end_is_the_live_table(pg, store):
    churn(store, pg, seed=3, steps=40)
    state = full(store)
    assert state.at_seq == state.history_seq == counter(pg) and records_of(state) == live_table(pg)


def test_the_state_at_a_past_seq_does_not_change_when_the_ledger_changes_after_it(pg, store):
    churn(store, pg, seed=5, steps=25)
    seq = counter(pg)
    before = full(store, at_seq=seq)
    churn(store, pg, seed=6, steps=25)  # more writes, updates and deletes of other records
    mk(store, 3, "afterwards")
    seal_all(pg, size=7)
    after = full(store, at_seq=seq)
    assert after.state_root == before.state_root and after.records == before.records
    assert (after.record_count, after.deleted_count) == (before.record_count, before.deleted_count)
    assert after.sealed is True and before.sealed is False  # only the flag about sealing moved


def test_a_restored_copy_replays_to_the_identical_state(pg, store):
    """A backup and restore carries the history rows over unchanged; copy them into a fresh schema and replay there."""
    churn(store, pg, seed=9, steps=40)
    seq = counter(pg)
    mine = full(store, at_seq=seq)
    copy = pg.admin_dsn
    with psycopg.connect(copy, autocommit=True) as conn:
        conn.execute('CREATE SCHEMA "restored"')
        try:
            migrate(copy, schema="restored")
            conn.execute('SET search_path TO "restored"')
            conn.execute("ALTER TABLE memories DISABLE TRIGGER USER")
            for table in ("memories", "record_history", "chain_heads", "history_counters"):
                conn.execute(f'INSERT INTO "restored".{table} SELECT * FROM "{pg.schema}".{table}')
            theirs = PostgresRowStore(pg.app_dsn, "alice", schema="restored")
            conn.execute('GRANT USAGE ON SCHEMA "restored" TO jarvis_app_test')
            conn.execute('GRANT SELECT ON ALL TABLES IN SCHEMA "restored" TO jarvis_app_test')
            conn.execute('GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA "restored" TO jarvis_app_test')
            again = theirs.replay_state(at_seq=seq, limit=replay.MAX_PAGE)
            assert again.state_root == mine.state_root and again.records == mine.records
        finally:
            pg_store.close_pools()
            conn.execute('DROP SCHEMA "restored" CASCADE')


def test_deleted_records_are_not_in_the_state_and_are_counted(pg, store):
    a, b, c = mk(store, 3)
    store.delete_memory(b.id)
    state = full(store)
    assert sorted(records_of(state)) == sorted([a.id, c.id]) and (state.record_count, state.deleted_count) == (2, 1)
    before_delete = full(store, at_seq=3)
    assert sorted(records_of(before_delete)) == sorted([a.id, b.id, c.id]) and before_delete.deleted_count == 0
    assert before_delete.state_root != state.state_root


def test_supersedes_edges_survive_the_replay(pg, store):
    old = mk(store, 1)[0]
    new = store.create_memory(MemoryCreate(content="the newer decision", source_agent="t", session_id="s", type="fact", supersedes=old.id))
    state = full(store)
    assert records_of(state)[new.id]["supersedes"] == old.id
    assert records_of(full(store, at_seq=1)) == {old.id: records_of(full(store, at_seq=1))[old.id]}  # before the newer record existed


def test_backfilled_entries_replay_like_any_other(pg, store):
    with pg.admin_conn() as conn:
        with conn.transaction():
            conn.execute("SELECT set_config('jarvis.tenant_key', 'alice', true)")
            conn.execute("SELECT set_config('jarvis.history_op', 'backfill', true)")
            conn.execute(
                "INSERT INTO memories (tenant_key, id, content, content_sha256, created_at, updated_at, source_agent, session_id, type, status, confidence) "
                "VALUES ('alice', 'mem-imported', 'an imported record', %s, now(), now(), 'import', 's', 'fact', 'draft', 0.5)", ("a" * 64,))
    assert [e.op for e in entries(pg)] == ["backfill"]
    state = full(store)
    assert list(records_of(state)) == ["mem-imported"] and state.state_root == independent_root(pg, 1)


def test_an_empty_ledger_replays_to_the_empty_state(pg, store):
    state = full(store)
    assert (state.at_seq, state.record_count, state.history_seq, state.sealed, state.block) == (0, 0, 0, False, None)
    assert state.state_root == replay.EMPTY_ROOT and state.records == []
    mk(store, 2)
    assert full(store, at_seq=0).state_root == replay.EMPTY_ROOT and full(store, at_seq=0).records == []


def test_the_bounds_are_checked(pg, store):
    mk(store, 3)
    with pytest.raises(replay.ReplayError) as exc:
        full(store, at_seq=4)
    assert exc.value.code == "replay_seq_out_of_range" and exc.value.status == 422
    with pytest.raises(replay.ReplayError) as exc:
        full(store, at_block=1)
    assert exc.value.code == "replay_block_not_found" and exc.value.status == 404
    with pytest.raises(replay.ReplayError) as exc:
        full(store, at_seq=1, at_block=1)
    assert exc.value.code == "replay_bound_ambiguous"
    with pytest.raises(replay.ReplayError):
        store.replay_events(from_seq=0)
    with pytest.raises(replay.ReplayError):
        store.replay_events(to_seq=9)


# --- sealing flags ---------------------------------------------------------------------------------------------------

def test_the_sealed_flags_and_the_block_reference(pg, store):
    mk(store, 7)
    assert full(store).sealed is False and full(store).block is None
    seal_all(pg, size=3)  # blocks: 1-3, 4-6, 7-7
    rows = {r[0]: r for r in block_rows(pg)}
    end = full(store, at_seq=6)
    assert (end.sealed, end.at_block_boundary, end.block.height, end.block.block_hash) == (True, True, 2, rows[2][6])
    middle = full(store, at_seq=5)
    assert (middle.sealed, middle.at_block_boundary, middle.block.height) == (True, False, 2)
    by_block = full(store, at_block=2)
    assert by_block.at_seq == 6 and by_block.state_root == end.state_root and by_block.records == end.records
    assert full(store, at_block=3).at_seq == 7 and full(store, at_block=3).at_block_boundary is True
    assert full(store, at_seq=0).sealed is False
    mk(store, 1, "later")
    tail = full(store)
    assert (tail.sealed, tail.block, tail.sealed_seq, tail.history_seq) == (False, None, 7, 8)


def test_sealing_changes_nothing_about_the_state(pg, store):
    churn(store, pg, seed=11, steps=30)
    seq = counter(pg)
    before = full(store, at_seq=seq)
    seal_all(pg, size=8)
    after = full(store, at_seq=seq)
    assert (after.state_root, after.records) == (before.state_root, before.records) and after.sealed is True


# --- paging and tenants -----------------------------------------------------------------------------------------------

def test_pages_join_up_to_the_whole_state_and_the_root_covers_all_of_it(pg, store):
    mk(store, 9)
    whole = full(store)
    got, cursor, pages = [], None, 0
    while True:
        page = store.replay_state(after_id=cursor, limit=4)
        pages += 1
        assert page.state_root == whole.state_root and page.record_count == whole.record_count == 9
        got += page.records
        cursor = page.next_after_id
        if cursor is None:
            break
    assert pages == 3 and got == whole.records
    ids = [r.id.encode() for r in got]
    assert ids == sorted(ids)  # bytewise order


def test_tenants_replay_independently(pg, store):
    bob = PostgresRowStore(pg.app_dsn, "bob", schema=pg.schema)
    a = mk(store, 3)
    b = mk(bob, 2, "bobs")
    sa, sb = full(store), full(bob)
    assert sorted(records_of(sa)) == sorted(r.id for r in a) and sorted(records_of(sb)) == sorted(r.id for r in b)
    assert sa.state_root != sb.state_root and sb.tenant == "bob" and (sb.at_seq, sb.history_seq) == (2, 2)
    with pytest.raises(replay.ReplayError):
        full(bob, at_seq=3)  # alice's counter does not leak into bob's range


# --- events -----------------------------------------------------------------------------------------------------------

def test_events_are_the_ordered_history_with_op_actor_and_images(pg, store):
    rec = mk(store, 1)[0]
    store.update_memory(rec.id, MemoryUpdate(subject="x"))
    store.delete_memory(rec.id)
    ev = store.replay_events()
    assert [(e.seq, e.op, e.version) for e in ev.events] == [(1, "create", 1), (2, "update", 2), (3, "delete", 2)]
    assert all(e.actor == "alice" and e.memory_id == rec.id for e in ev.events)
    assert ev.events[0].before is None and ev.events[0].after["content"].startswith("record number 0")
    assert ev.events[1].before["subject"] is None and ev.events[1].after["subject"] == "x"
    assert ev.events[2].before is not None and ev.events[2].after is None
    for prev, cur in zip(ev.events, ev.events[1:]):
        assert cur.prev_hash == prev.row_hash  # the per-record chain is visible in the events
    assert ev.events[0].prev_hash == replay.GENESIS and ev.next_from_seq is None and ev.history_seq == 3


def test_event_paging_and_ranges(pg, store):
    mk(store, 9)
    pages, nxt = [], 1
    while nxt:
        page = store.replay_events(from_seq=nxt, limit=4)
        pages.append([e.seq for e in page.events])
        nxt = page.next_from_seq
    assert pages == [[1, 2, 3, 4], [5, 6, 7, 8], [9]]
    assert [e.seq for e in store.replay_events(from_seq=3, to_seq=5).events] == [3, 4, 5]
    assert store.replay_events(from_seq=3, to_seq=5).to_seq == 5
    assert [e.seq for e in store.replay_events(from_seq=9).events] == [9]


def test_event_evidence_is_classified_intact_missing_tampered_or_not_checked(pg, store):
    obj, _ = store.put_evidence_object(EvidenceObjectCreate(schema_id=CES_FACT, payload={"observation": "listening on loopback", "method": "command", "source": "ss"}))
    store.create_memory(MemoryCreate(content="a fact with an evidence object", source_agent="t", session_id="s", type="fact",
                                     evidence=[EvidenceLink(kind="evidence-object", ref=obj.id), EvidenceLink(kind="file", ref="docs/POSTGRES.md", note="a pointer")]))
    def statuses():
        return {(x.kind, x.status) for x in store.replay_events().events[0].evidence}
    assert statuses() == {("evidence-object", "intact"), ("file", "not-checked")}
    with tampering(pg, ("evidence_objects", "evidence_objects_no_update")) as conn:
        conn.execute("UPDATE evidence_objects SET payload = jsonb_set(payload, '{source}', '\"forged\"')")
    assert statuses() == {("evidence-object", "tampered"), ("file", "not-checked")}
    with tampering(pg, ("evidence_objects", "evidence_objects_no_delete")) as conn:
        conn.execute("DELETE FROM evidence_objects")
    assert statuses() == {("evidence-object", "missing"), ("file", "not-checked")}


def test_a_delete_event_reports_the_evidence_the_record_had(pg, store):
    obj, _ = store.put_evidence_object(EvidenceObjectCreate(schema_id=CES_FACT, payload={"observation": "x", "method": "command", "source": "y"}))
    rec = store.create_memory(MemoryCreate(content="to be deleted", source_agent="t", session_id="s", type="fact", evidence=[EvidenceLink(kind="evidence-object", ref=obj.id)]))
    store.delete_memory(rec.id)
    assert [x.status for x in store.replay_events().events[1].evidence] == ["intact"]


# --- the offline verifier -----------------------------------------------------------------------------------------------

def test_a_clean_ledger_verifies_at_every_point(pg, store, verify_env, capsys):
    churn(store, pg, seed=2, steps=30)
    seal_all(pg, size=8)
    mk(store, 2, "tail")
    tip = counter(pg)
    for seq in (0, 1, 7, 8, 9, 16, 24, tip - 2, tip):
        result = verify(pg, at_seq=seq)
        assert result["ok"] is True and result["problems"] == [], (seq, result["problems"])
        assert result["state_root"] == full(store, at_seq=seq).state_root == independent_root(pg, seq)
    rc, out = run_cli(capsys)
    assert rc == 0 and out.startswith("ok: replayed") and "not covered by a sealed block" in out
    rc, out = run_cli(capsys, "--at-block", "2")
    assert rc == 0 and "block 2 (" in out and f"state root {full(store, at_block=2).state_root}" in out


def test_the_cli_accepts_the_right_expected_root_and_refuses_a_wrong_one(pg, store, verify_env, capsys):
    mk(store, 5)
    root = full(store).state_root
    assert run_cli(capsys, "--expect-root", root)[0] == 0
    rc, out = run_cli(capsys, "--expect-root", "f" * 64)
    assert rc == 1 and "[state_root]" in out and "differs from the expected root" in out
    rc, out = run_cli(capsys, "--at-seq", "99")
    assert rc == 2 and "replay_seq_out_of_range" in out
    rc, out = run_cli(capsys, "--at-block", "4")
    assert rc == 2 and "replay_block_not_found" in out


def test_the_cli_needs_a_database_url(capsys, monkeypatch):
    monkeypatch.delenv("JARVIS_DATABASE_MIGRATE_URL", raising=False)
    monkeypatch.delenv("JARVIS_DATABASE_URL", raising=False)
    assert replay.main(["verify"]) == 2


# --- tamper proofs: each check, the tamper only it finds, and the same tamper with that check switched off ---------------

def _tamper_entry_hash(pg, store):
    """Alter an OLD entry's content (not the newest of its record): only its own hash no longer matches."""
    rec = mk(store, 1)[0]
    store.update_memory(rec.id, MemoryUpdate(subject="one"))
    store.update_memory(rec.id, MemoryUpdate(subject="two"))
    with tampering(pg, *HISTORY_RW) as conn:
        conn.execute("UPDATE record_history SET after = jsonb_set(after, '{content}', '\"forged\"') WHERE seq = 1")
    return {"at_seq": 3}


def _tamper_chain_link(pg, store):
    """Re-point an old entry at a different predecessor and recompute its hash: the entry is self-consistent but the
    entry after it no longer chains to it."""
    rec = mk(store, 1)[0]
    store.update_memory(rec.id, MemoryUpdate(subject="one"))
    store.update_memory(rec.id, MemoryUpdate(subject="two"))
    with tampering(pg, *HISTORY_RW) as conn:
        conn.execute("UPDATE record_history SET prev_hash = %s WHERE seq = 2", ("c" * 64,))
        conn.execute("UPDATE record_history SET row_hash = jarvis_history_hash(prev_hash, op, version, before, after) WHERE seq = 2")
    return {"at_seq": 3}


def _tamper_seq(pg, store):
    """Remove an entry from the middle of the sequence."""
    mk(store, 4)
    with tampering(pg, *HISTORY_RW) as conn:
        conn.execute("DELETE FROM record_history WHERE seq = 2")
    return {"at_seq": 4}


def _tamper_live(pg, store):
    """Change a live record behind the history's back (with the capture triggers off)."""
    rec = mk(store, 2)[0]
    with tampering(pg, ("memories", "memories_history"), ("memories", "memories_bump_version")) as conn:
        conn.execute("UPDATE memories SET content = 'changed behind the history' WHERE id = %s", (rec.id,))
    return {}


def _tamper_block(pg, store):
    """A sealed block altered in place."""
    mk(store, 4)
    seal_all(pg, size=2)
    with tampering(pg, *BLOCKS_RW) as conn:
        conn.execute("UPDATE blocks SET entries_root = %s WHERE height = 2", ("d" * 64,))
    return {"at_seq": 4}


def _tamper_rewritten_entry(pg, store):
    """The strongest rewrite: an entry's content AND its hash, the record chain and the live row all made consistent."""
    recs = mk(store, 5)
    store.update_memory(recs[2].id, MemoryUpdate(subject="before the forgery"))
    seal_all(pg, size=4)
    _rewrite_entry_consistently(pg, recs[2].id, 6, "a forged version of this record")
    return {"at_seq": 6}


TAMPERS = [
    ("entry_hash", _tamper_entry_hash, "does not match the entry's contents"),
    ("chain_link", _tamper_chain_link, "prev_hash does not match the previous entry"),
    ("seq", _tamper_seq, "is missing"),
    ("live_match", _tamper_live, "differs from its latest history entry"),
    ("blocks", _tamper_block, "entries_root"),
    ("blocks", _tamper_rewritten_entry, "entries_root"),
]


@pytest.mark.parametrize("check,tamper,keyword", TAMPERS, ids=[f"{t[0]}:{t[1].__name__[8:]}" for t in TAMPERS])
def test_each_check_catches_its_tamper_and_a_verifier_without_that_check_misses_it(pg, store, monkeypatch, check, tamper, keyword):
    kw = tamper(pg, store)
    real = verify(pg, **kw)
    assert real["ok"] is False and check in checks_of(real), real["problems"]
    assert any(keyword in p["problem"] for p in real["problems"] if p["check"] == check), real["problems"]
    monkeypatch.setitem(replay.CHECKS, check, lambda ctx: [])
    mutated = verify(pg, **kw)
    assert not any(keyword in p["problem"] for p in mutated["problems"]), f"another check also finds it: {mutated['problems']}"


def test_the_isolated_tampers_are_found_by_exactly_the_check_they_target(pg, store):
    """For the first two (an old entry's content, a re-pointed predecessor) nothing else in the verifier notices."""
    kw = _tamper_entry_hash(pg, store)
    assert checks_of(verify(pg, **kw)) == {"entry_hash"}


def test_a_consistently_rewritten_entry_passes_the_record_checks_and_only_the_block_notices(pg, store):
    kw = _tamper_rewritten_entry(pg, store)
    result = verify(pg, **kw)
    assert checks_of(result) == {"blocks"}  # entry hashes, chain, live row and state fold all agree with the forgery
    with psycopg.connect(pg.admin_dsn, options=f"-c search_path={pg.schema}") as conn:
        assert conn.execute("SELECT count(*) FROM jarvis_verify_history('alice')").fetchone()[0] == 0


def test_a_rewrite_with_every_later_block_resealed_passes_the_database_and_is_caught_by_the_expected_root(pg, store):
    """The limit of the database: a consistent rewrite plus a full re-seal verifies clean.  A receipt or anchor taken
    before (here: the state root) is what exposes it."""
    recs = mk(store, 5)
    store.update_memory(recs[2].id, MemoryUpdate(subject="before the forgery"))
    seal_all(pg, size=4)
    honest = full(store, at_seq=6).state_root
    _rewrite_entry_consistently(pg, recs[2].id, 6, "a forged version of this record")
    prev = replay.GENESIS
    for height, *_ in block_rows(pg):
        prev = forge(pg, height, prev_block_hash=prev)["block_hash"]
    assert verify(pg, at_seq=6)["ok"] is True
    after = verify(pg, at_seq=6, expect_root=honest)
    assert checks_of(after) == {"state_root"} and after["state_root"] != honest


def test_the_expected_root_check_is_what_notices_a_changed_root(pg, store, monkeypatch):
    mk(store, 3)
    assert checks_of(verify(pg, expect_root="e" * 64)) == {"state_root"}
    monkeypatch.setitem(replay.CHECKS, "state_root", lambda ctx: [])
    assert verify(pg, expect_root="e" * 64)["problems"] == []


def test_an_old_state_stays_verifiable_after_a_later_tamper_of_a_newer_record(pg, store):
    recs = mk(store, 4)
    store.update_memory(recs[3].id, MemoryUpdate(subject="newer"))
    with tampering(pg, *HISTORY_RW) as conn:
        conn.execute("UPDATE record_history SET row_hash = %s WHERE seq = 5", ("9" * 64,))
    assert verify(pg, at_seq=4)["ok"] is True  # entries up to seq 4 are untouched
    assert verify(pg, at_seq=5)["ok"] is False
