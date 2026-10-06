"""Block anchors: the hash of every sealed block is kept outside the database, in the backup anchors.

These tests prove the two tampers the database cannot see on its own (see tests/test_pg_blocks.py): removing the newest
block, and rewriting an entry and re-sealing every later block consistently."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import types
from pathlib import Path

import pytest

from app import pg_store
from app.pg_schema import EXPECTED_SCHEMA_VERSION, migrate
from app.pg_store import PostgresRowStore
from tests.test_pg_blocks import (
    _rewrite_entry_consistently, block_rows, delete_block, forge, mk, run_verify, seal, seal_all, verify_blocks_sql,
    verify_env, verify_history_sql,
)

ROOT = Path(__file__).resolve().parents[1]
ANCHORS = ROOT / "deploy" / "mint" / "bin" / "anchors.sh"
SHIM = ROOT / "tests" / "fixtures" / "psql_shim.py"
Z = "0" * 64


def bash(script: str, *, env: dict[str, str] | None = None, anchors: Path = ANCHORS) -> subprocess.CompletedProcess:
    return subprocess.run(["bash", "-c", f'set -u; source "{anchors}"; {script}'], capture_output=True, text=True,
                          env={**os.environ, **(env or {})}, timeout=60)


def write(tmp_path: Path, name: str, lines: list[str]) -> Path:
    p = tmp_path / name
    p.write_text("\n".join(lines) + "\n")
    return p


def check(prev: Path, new: Path, anchors: Path = ANCHORS) -> subprocess.CompletedProcess:
    return bash(f'anchors_check "{prev}" "{new}"', anchors=anchors)


# --- anchors_check on plain files: no database -------------------------------------------------------------------

BASE = ["counter|t|5", f"head|t|mem-a|5|{'a' * 64}|f", f"block|t|1|3|{'1' * 64}", f"block|t|2|5|{'2' * 64}"]


def test_a_new_block_and_new_history_is_a_legitimate_successor(tmp_path):
    new = BASE[:1] + [f"head|t|mem-a|7|{'b' * 64}|f"] + BASE[2:] + [f"block|t|3|7|{'3' * 64}"]
    new[0] = "counter|t|7"
    r = check(write(tmp_path, "a", BASE), write(tmp_path, "b", new))
    assert r.returncode == 0 and r.stdout == ""


def test_an_identical_set_is_a_legitimate_successor(tmp_path):
    assert check(write(tmp_path, "a", BASE), write(tmp_path, "b", BASE)).returncode == 0


def test_a_removed_newest_block_is_refused(tmp_path):
    r = check(write(tmp_path, "a", BASE), write(tmp_path, "b", BASE[:-1]))
    assert r.returncode == 1 and "block t|2 vanished (a block was removed)" in r.stdout


def test_a_removed_middle_block_is_refused(tmp_path):
    r = check(write(tmp_path, "a", BASE), write(tmp_path, "b", [BASE[0], BASE[1], BASE[2], BASE[3].replace("|2|", "|2|")][:3]))
    assert r.returncode == 1 and "block t|2 vanished" in r.stdout


def test_a_resealed_block_with_a_new_hash_is_refused(tmp_path):
    new = BASE[:3] + [f"block|t|2|5|{'9' * 64}"]
    r = check(write(tmp_path, "a", BASE), write(tmp_path, "b", new))
    assert r.returncode == 1 and "block t|2 changed its hash (the blocks were re-sealed)" in r.stdout


def test_losing_every_block_at_once_is_refused(tmp_path):
    r = check(write(tmp_path, "a", BASE), write(tmp_path, "b", BASE[:2]))
    assert r.returncode == 1 and r.stdout.count("vanished") == 2


def test_the_first_set_with_blocks_after_a_set_without_any_is_fine(tmp_path):
    assert check(write(tmp_path, "a", BASE[:2]), write(tmp_path, "b", BASE)).returncode == 0


def test_the_older_rules_still_apply(tmp_path):
    r = check(write(tmp_path, "a", BASE), write(tmp_path, "b", ["counter|t|3"] + BASE[2:]))
    assert r.returncode == 1 and "counter went BACKWARDS" in r.stdout and "vanished from the chain heads" in r.stdout


def test_blocks_of_different_tenants_are_kept_apart(tmp_path):
    a = BASE + [f"block|u|1|2|{'7' * 64}"]
    r = check(write(tmp_path, "a", a), write(tmp_path, "b", BASE))
    assert r.returncode == 1 and "block u|1 vanished" in r.stdout and "t|" not in r.stdout.replace("block u|1", "")


def tips(tmp_path, lines):
    r = bash(f'anchors_tip_blocks "{write(tmp_path, "tips", lines)}"')
    assert r.returncode == 0, r.stderr
    return r.stdout.splitlines()


def test_the_tip_blocks_are_each_tenants_newest_numerically(tmp_path):
    lines = ["counter|t|90", f"head|t|mem-a|5|{'a' * 64}|f", f"block|t|2|5|{'2' * 64}", f"block|t|10|9|{'9' * 64}", f"block|t|9|8|{'8' * 64}",
             f"block|u|1|3|{'1' * 64}"]
    assert tips(tmp_path, lines) == [f"t|10|{'9' * 64}", f"u|1|{'1' * 64}"]  # height 10 beats height 9 and 2 (not a string comparison)


def test_a_set_without_blocks_has_no_tip_blocks(tmp_path):
    assert tips(tmp_path, ["counter|t|5", f"head|t|mem-a|5|{'a' * 64}|f"]) == []


# --- against a real database ---------------------------------------------------------------------------------------

@pytest.fixture
def pg(pg_schema):
    assert migrate(pg_schema.admin_dsn, schema=pg_schema.schema, app_role="jarvis_app_test") == EXPECTED_SCHEMA_VERSION
    yield pg_schema
    pg_store.close_pools()


@pytest.fixture
def store(pg):
    return PostgresRowStore(pg.app_dsn, "alice", schema=pg.schema)


def collect(pg, tmp_path: Path, name: str) -> Path:
    """The anchor lines of the database, exactly as backup.sh would write them (through the real anchors_collect)."""
    runner = f'anchors_test_psql() {{ "{sys.executable}" "{SHIM}" "$@"; }}; anchors_collect anchors_test_psql'
    r = bash(runner, env={"SHIM_DSN": pg.admin_dsn, "SHIM_SCHEMA": pg.schema})
    assert r.returncode == 0, r.stderr
    p = tmp_path / name
    p.write_text(r.stdout)
    return p


@pytest.mark.postgres
def test_every_sealed_block_is_written_to_the_anchors(pg, store, tmp_path):
    mk(store, 7)
    seal_all(pg, size=3)
    lines = collect(pg, tmp_path, "a").read_text().splitlines()
    blocks = [ln for ln in lines if ln.startswith("block|")]
    assert blocks == sorted(f"block|alice|{h}|{last}|{bh}" for h, _, last, _, _, _, bh, _, _ in block_rows(pg))
    assert len(blocks) == 3 and any(ln.startswith("head|alice|") for ln in lines) and "counter|alice|7" in lines


@pytest.mark.postgres
def test_a_database_without_blocks_yet_contributes_no_block_lines(pg_schema, tmp_path):
    migrate(pg_schema.admin_dsn, schema=pg_schema.schema, app_role="jarvis_app_test", up_to=5)
    lines = collect(pg_schema, tmp_path, "a").read_text().splitlines()
    assert not any(ln.startswith("block|") for ln in lines)


@pytest.mark.postgres
def test_sealing_more_history_is_a_legitimate_successor(pg, store, tmp_path):
    mk(store, 4)
    seal_all(pg, size=3)
    before = collect(pg, tmp_path, "a")
    mk(store, 3, "later")
    seal_all(pg, size=3)
    assert check(before, collect(pg, tmp_path, "b")).returncode == 0


@pytest.mark.postgres
def test_the_anchor_catches_the_removal_of_the_newest_block_which_the_database_cannot(pg, store, verify_env, capsys, tmp_path):
    mk(store, 7)
    seal_all(pg, size=3)
    before = collect(pg, tmp_path, "before")
    delete_block(pg, 3)
    # inside the database everything still agrees: a shorter chain is a valid chain
    assert verify_blocks_sql(pg) == [] and verify_history_sql(pg) == []
    assert run_verify(capsys)[0] == 0
    # the anchor taken before the removal notices
    r = check(before, collect(pg, tmp_path, "after"))
    assert r.returncode == 1 and "block alice|3 vanished (a block was removed)" in r.stdout


@pytest.mark.postgres
def test_the_anchor_catches_a_rewritten_entry_with_every_block_consistently_resealed(pg, store, verify_env, capsys, tmp_path):
    from app import blocks as hashing
    from app.models import MemoryUpdate

    recs = mk(store, 5)
    store.update_memory(recs[2].id, MemoryUpdate(subject="before the forgery"))
    seal_all(pg, size=4)
    before = collect(pg, tmp_path, "before")
    _rewrite_entry_consistently(pg, recs[2].id, 6, "a forged version of this record")
    prev = hashing.GENESIS_HASH
    for height, *_ in block_rows(pg):
        prev = forge(pg, height, prev_block_hash=prev)["block_hash"]
    # the database cannot tell: the record chain, its head, the live row, every block root and every block link agree
    assert verify_history_sql(pg) == [] and verify_blocks_sql(pg) == []
    assert run_verify(capsys)[0] == 0
    r = check(before, collect(pg, tmp_path, "after"))
    assert r.returncode == 1 and "block alice|2 changed its hash (the blocks were re-sealed)" in r.stdout


@pytest.mark.postgres
def test_resealing_only_the_tip_is_caught_too(pg, store, tmp_path):
    mk(store, 6)
    seal_all(pg, size=3)
    before = collect(pg, tmp_path, "before")
    forge(pg, 2, prev_block_hash=block_rows(pg)[0][6], last_seq=5)  # a different range, consistently sealed
    r = check(before, collect(pg, tmp_path, "after"))
    assert r.returncode == 1 and "block alice|2" in r.stdout


def _mutant(tmp_path: Path, name: str, old: str, new: str) -> Path:
    text = ANCHORS.read_text()
    assert text.count(old) == 1, old
    p = tmp_path / name
    p.write_text(text.replace(old, new))
    return p


@pytest.fixture
def mutants(tmp_path):
    return types.SimpleNamespace(
        no_block_rule=_mutant(tmp_path, "no-block-rule.sh", "      for (k in pb) {", "      for (k in pb) { continue }\n      for (k in pb_unused) {"),
        no_changed_branch=_mutant(tmp_path, "no-changed-branch.sh", "else if (nb[k] != pb[k])", "else if (0)"),
    )


@pytest.mark.postgres
def test_mutation_a_removed_block_needs_the_block_rule(pg, store, tmp_path, mutants):
    mk(store, 7)
    seal_all(pg, size=3)
    before = collect(pg, tmp_path, "before")
    delete_block(pg, 3)
    removed = collect(pg, tmp_path, "removed")
    assert check(before, removed).returncode == 1
    assert check(before, removed, anchors=mutants.no_block_rule).returncode == 0  # blind without the block rule
    assert check(before, removed, anchors=mutants.no_changed_branch).returncode == 1  # the vanished branch still sees it


@pytest.mark.postgres
def test_mutation_a_resealed_block_needs_the_changed_hash_branch(pg, store, tmp_path, mutants):
    mk(store, 7)
    seal_all(pg, size=3)
    before = collect(pg, tmp_path, "before")
    forge(pg, 2, prev_block_hash=block_rows(pg)[0][6], last_seq=5)  # block 2 re-sealed over a different range
    resealed = collect(pg, tmp_path, "resealed")
    assert check(before, resealed).returncode == 1
    assert check(before, resealed, anchors=mutants.no_block_rule).returncode == 0
    assert check(before, resealed, anchors=mutants.no_changed_branch).returncode == 0  # only that branch sees a changed hash


@pytest.mark.postgres
def test_the_dump_reader_and_the_live_reader_agree_including_blocks(tmp_path):
    """anchors_from_dump (what backup.sh uses) and anchors_collect (what restore and the drill use) must produce the
    same lines.  Needs pg_dump/pg_restore on the PATH and a throwaway server; skipped otherwise."""
    import psycopg

    dsn = os.environ.get("JARVIS_TEST_PG_DSN", "").strip()
    if not dsn:
        pytest.skip("JARVIS_TEST_PG_DSN not set")
    if not (shutil.which("pg_dump") and shutil.which("pg_restore")):
        pytest.skip("pg_dump/pg_restore not installed")
    with psycopg.connect(dsn, autocommit=True) as conn:
        if conn.execute("SELECT 1 FROM pg_namespace WHERE nspname = 'jarvis'").fetchone():
            pytest.skip("a schema named jarvis already exists on this server")
        conn.execute("CREATE SCHEMA jarvis")
    try:
        migrate(dsn, schema="jarvis")
        with psycopg.connect(dsn, autocommit=True, options="-c search_path=jarvis") as conn:
            conn.execute("SELECT set_config('jarvis.tenant_key', 'alice', false)")
            conn.execute("SELECT set_config('jarvis.actor', 'alice', false)")
            for i in range(5):
                conn.execute(
                    "INSERT INTO memories (tenant_key, id, content, content_sha256, created_at, updated_at, source_agent, session_id, type, status, confidence) "
                    "VALUES ('alice', %s, %s, %s, now(), now(), 't', 's', 'fact', 'draft', 0.5)", (f"mem-{i}", f"dump record {i}", Z))
            conn.execute("SELECT * FROM jarvis_seal_block('alice', 1, '1 hour', true, 3)")
            conn.execute("SELECT * FROM jarvis_seal_block('alice', 1, '1 hour', true, 3)")
        dump = tmp_path / "x.dump"
        r = subprocess.run(["pg_dump", "-Fc", "-n", "jarvis", "-f", str(dump), dsn], capture_output=True, text=True)
        if r.returncode != 0:
            pytest.skip(f"pg_dump unusable here: {r.stderr.strip()[:200]}")
        script = (f'pg_exec() {{ "$@"; }}; anchors_from_dump "{dump}" > "{tmp_path}/from_dump"; '
                  f'anchors_test_psql() {{ "{sys.executable}" "{SHIM}" "$@"; }}; anchors_collect anchors_test_psql > "{tmp_path}/live"')
        r = bash(script, env={"SHIM_DSN": dsn, "SHIM_SCHEMA": "jarvis"})
        assert r.returncode == 0, r.stderr
        assert (tmp_path / "from_dump").read_text() == (tmp_path / "live").read_text()
        assert "block|alice|2|5|" in (tmp_path / "live").read_text()
    finally:
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute("DROP SCHEMA jarvis CASCADE")
