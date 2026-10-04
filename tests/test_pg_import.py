"""JSON (or legacy blob) -> Postgres rows: dry-run by default, verified, never touches the source."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from app import pg_import, pg_store
from app.models import MemoryCreate, MemoryRecord, MemoryUpdate
from app.pg_schema import migrate
from app.pg_store import PostgresRowStore
from app.store import JarvisStore

pytestmark = pytest.mark.postgres

SHA = "a" * 64


@pytest.fixture
def pg(pg_schema):
    migrate(pg_schema.admin_dsn, schema=pg_schema.schema, app_role="jarvis_app_test")
    yield pg_schema
    pg_store.close_pools()


@pytest.fixture
def env(pg, monkeypatch):
    monkeypatch.setenv("JARVIS_DATABASE_MIGRATE_URL", pg.admin_dsn)
    monkeypatch.setenv("JARVIS_DATABASE_SCHEMA", pg.schema)
    return pg


def _rec(i, **over):
    base = dict(
        id=f"mem-{i:03d}", content=f"record number {i} with some text", created_at=f"2026-07-0{i % 9 + 1}T10:00:00.250000+00:00",
        updated_at=f"2026-07-0{i % 9 + 1}T11:00:00+00:00", source_agent="agent", session_id="sess", type="fact",
        confidence=0.5, evidence=[{"kind": "ref", "ref": f"doc-{i}", "note": "n"}], supersedes=None, status="draft",
        subject=None, tags=["t1", "t2"], version=1, content_sha256="",
    )
    base.update(over)
    from app.continuity import content_sha256

    base["content_sha256"] = base["content_sha256"] or content_sha256(base["content"])
    return base


def _write_source(tmp_path, records, board=None, name="source-store.json"):
    doc = {"board": board if board is not None else {"board_id": "imported", "summary": "from json"},
           "schema": "continuity-ledger-v1", "memories": records}
    path = tmp_path / "src" / name
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(doc, indent=2), "utf-8")
    return path


def _standard(tmp_path):
    recs = [
        _rec(1, status="verified", version=4, confidence=0.9),
        _rec(2, supersedes="mem-001", subject="topic"),
        _rec(3, status="archived", type="decision", subject="topic"),
        _rec(4, tags=[], evidence=[]),
        _rec(5, supersedes="mem-002", version=2),
    ]
    return _write_source(tmp_path, recs), recs


def _snapshot(path: Path):
    return (path.read_bytes(), path.stat().st_mtime_ns, hashlib.sha256(path.read_bytes()).hexdigest())


def _counts(pg):
    with pg.admin_conn() as conn:
        return tuple(
            conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
            for t in ("memories", "boards", "record_history", "chain_heads", "history_counters")
        )


def _run(*argv):
    return pg_import.main(list(argv))


def _store(pg, tenant="operator"):
    return PostgresRowStore(pg.app_dsn, tenant, schema=pg.schema)


def test_dry_run_is_the_default_writes_nothing_and_reports(tmp_path, env, capsys):
    src, recs = _standard(tmp_path)
    before = _snapshot(src)
    assert _run("--source", str(src)) == 0
    out = capsys.readouterr().out
    assert "DRY RUN" in out and "5 records" in out and "rolled back" in out
    assert "status verified=1" in out.replace(": ", " ") or "verified" in out
    assert _counts(env) == (0, 0, 0, 0, 0)
    assert _snapshot(src) == before


def test_apply_imports_exactly_and_marks_history_as_backfill(tmp_path, env, capsys):
    src, recs = _standard(tmp_path)
    before = _snapshot(src)
    assert _run("--source", str(src), "--apply") == 0
    assert "APPLIED" in capsys.readouterr().out
    s = _store(env)
    assert len(s.list_memories(limit=100)) == 5
    for raw in recs:
        got = s.get_memory(raw["id"])
        want = MemoryRecord(**raw)
        assert got.model_dump() == want.model_dump()  # ids, timestamps, versions, hashes, pointers preserved
    assert s.get_board().board_id == "imported" and s.get_board().summary == "from json"
    for raw in recs:
        entries = s.history(raw["id"])
        assert [(e["op"], e["actor"]) for e in entries] == [("backfill", "import:backfill")]
        assert entries[0]["after"]["created_at"].startswith(raw["created_at"][:19])  # real creation time kept
    assert s.verify_history() == []
    assert _snapshot(src) == before and src.exists()  # source untouched


def test_records_created_after_import_are_ordinary_creates(tmp_path, env):
    src, _ = _standard(tmp_path)
    assert _run("--source", str(src), "--apply") == 0
    s = _store(env)
    new = s.create_memory(MemoryCreate(content="created after the import", source_agent="t", session_id="s", type="fact"))
    s.update_memory("mem-001", MemoryUpdate(subject="later edit"))
    assert [e["op"] for e in s.history(new.id)] == ["create"]
    assert [e["op"] for e in s.history("mem-001")] == ["backfill", "update"]
    assert s.verify_history() == []


def test_verify_mode_confirms_then_detects_drift(tmp_path, env, capsys):
    src, _ = _standard(tmp_path)
    _run("--source", str(src), "--apply")
    capsys.readouterr()
    assert _run("--source", str(src), "--verify") == 0
    assert "verified" in capsys.readouterr().out
    _store(env).update_memory("mem-003", MemoryUpdate(subject="changed after import"))
    assert _run("--source", str(src), "--verify") == 1
    assert "mem-003" in capsys.readouterr().out


def test_second_apply_is_refused_and_resume_is_a_no_op(tmp_path, env, capsys):
    src, _ = _standard(tmp_path)
    assert _run("--source", str(src), "--apply") == 0
    before = _counts(env)
    assert _run("--source", str(src), "--apply") == 2
    assert "already has" in capsys.readouterr().out
    assert _counts(env) == before
    assert _run("--source", str(src), "--apply", "--resume") == 0
    assert _counts(env) == before  # nothing new, no extra history entries


def test_resume_aborts_when_a_target_row_differs(tmp_path, env, capsys):
    src, _ = _standard(tmp_path)
    _run("--source", str(src), "--apply")
    _store(env).update_memory("mem-002", MemoryUpdate(subject="edited in the target"))
    before = _counts(env)
    assert _run("--source", str(src), "--apply", "--resume") == 2
    assert "mem-002" in capsys.readouterr().out
    assert _counts(env) == before


def test_resume_aborts_when_the_target_has_records_not_in_the_source(tmp_path, env):
    src, _ = _standard(tmp_path)
    _run("--source", str(src), "--apply")
    _store(env).create_memory(MemoryCreate(content="live write after cutover", source_agent="t", session_id="s", type="fact"))
    assert _run("--source", str(src), "--apply", "--resume") == 2


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d["memories"][1].update(type="BOGUS"),
        lambda d: d["memories"][1].update(confidence=5),
        lambda d: d.update(memories="notalist"),
        lambda d: (d.pop("memories"), d.update(memorie=[])) and d.update(memorie=[_rec(9)]),
        lambda d: d["memories"].append(dict(d["memories"][0])),  # duplicate id
        lambda d: d.update(board="nope"),
    ],
    ids=["bad-type", "bad-confidence", "memories-not-list", "unknown-key", "duplicate-id", "bad-board"],
)
def test_invalid_source_aborts_with_nothing_written(tmp_path, env, mutate, capsys):
    src, _ = _standard(tmp_path)
    doc = json.loads(src.read_text("utf-8"))
    mutate(doc)
    src.write_text(json.dumps(doc), "utf-8")
    before = _snapshot(src)
    assert _run("--source", str(src), "--apply") == 2
    assert _counts(env) == (0, 0, 0, 0, 0)
    assert _snapshot(src) == before


@pytest.mark.parametrize(
    "over,needle",
    [
        ({"content": "x" * 2001}, "content"),
        ({"tags": [f"t{i}" for i in range(33)]}, "tags"),
        ({"evidence": [{"kind": "ref", "ref": f"r{i}"} for i in range(33)]}, "evidence"),
        ({"content_sha256": "not-a-hash"}, "content_sha256"),
    ],
)
def test_records_that_violate_database_limits_are_reported_and_block_apply(tmp_path, env, over, needle, capsys):
    src, recs = _standard(tmp_path)
    doc = json.loads(src.read_text("utf-8"))
    doc["memories"][3].update(over)
    src.write_text(json.dumps(doc), "utf-8")
    assert _run("--source", str(src)) == 2  # even the dry run says so
    out = capsys.readouterr().out
    assert "mem-004" in out and needle in out
    assert _run("--source", str(src), "--apply") == 2
    assert _counts(env) == (0, 0, 0, 0, 0)


def test_a_database_failure_midway_rolls_everything_back(tmp_path, env, monkeypatch):
    src, _ = _standard(tmp_path)
    doc = json.loads(src.read_text("utf-8"))
    doc["memories"][4]["content"] = "y" * 2001  # passes nothing locally if checks are skipped
    src.write_text(json.dumps(doc), "utf-8")
    monkeypatch.setattr(pg_import, "local_problems", lambda rec: [])  # let the database be the judge
    assert _run("--source", str(src), "--apply") == 2
    assert _counts(env) == (0, 0, 0, 0, 0)  # four good records were inserted, then rolled back


def test_dangling_supersedes_aborts_unless_explicitly_nulled(tmp_path, env, capsys):
    recs = [_rec(1), _rec(2, supersedes="mem-gone")]
    src = _write_source(tmp_path, recs)
    assert _run("--source", str(src), "--apply") == 2
    assert "mem-gone" in capsys.readouterr().out and _counts(env) == (0, 0, 0, 0, 0)
    assert _run("--source", str(src), "--apply", "--null-dangling-supersedes") == 0
    out = capsys.readouterr().out
    assert "mem-002" in out and "mem-gone" in out  # the nulled pointer is listed for the operator
    assert _store(env).get_memory("mem-002").supersedes is None


def test_supersedes_cycles_abort(tmp_path, env, capsys):
    src = _write_source(tmp_path, [_rec(1, supersedes="mem-002"), _rec(2, supersedes="mem-001")])
    assert _run("--source", str(src), "--apply") == 2
    assert "cycle" in capsys.readouterr().out and _counts(env) == (0, 0, 0, 0, 0)


def test_forward_references_are_ordered_so_the_foreign_key_holds(tmp_path, env):
    src = _write_source(tmp_path, [_rec(3, supersedes="mem-002"), _rec(2, supersedes="mem-001"), _rec(1)])
    assert _run("--source", str(src), "--apply") == 0
    assert _store(env).get_memory("mem-003").supersedes == "mem-002"


def test_legacy_rows_are_migrated_in_memory_and_the_file_is_not_rewritten(tmp_path, env):
    legacy = {"id": "mem-legacy", "content": "Old signal text", "category": "decision", "tags": ["sess-9"],
              "created_at": "2026-07-01T00:00:00+00:00", "updated_at": "2026-07-01T00:00:00+00:00"}
    src = _write_source(tmp_path, [legacy])
    before = _snapshot(src)
    assert _run("--source", str(src), "--apply") == 0
    got = _store(env).get_memory("mem-legacy")
    assert got.type == "decision" and got.session_id == "sess-9" and len(got.content_sha256) == 64
    assert _snapshot(src) == before  # the JSON loader would have re-saved it; the importer must not


def test_naive_timestamps_are_treated_as_utc(tmp_path, env):
    src = _write_source(tmp_path, [_rec(1, created_at="2026-07-01T10:00:00", updated_at="2026-07-01T10:00:00")])
    assert _run("--source", str(src), "--apply") == 0
    assert _store(env).get_memory("mem-001").created_at == "2026-07-01T10:00:00+00:00"


def test_unparseable_timestamps_abort(tmp_path, env):
    src = _write_source(tmp_path, [_rec(1, created_at="yesterday-ish")])
    assert _run("--source", str(src), "--apply") == 2
    assert _counts(env) == (0, 0, 0, 0, 0)


def test_a_source_that_changes_during_the_run_aborts_and_rolls_back(tmp_path, env, monkeypatch):
    src, _ = _standard(tmp_path)
    calls = []
    real = pg_import._sha256_file

    def shifting(path):
        calls.append(1)
        return real(path) if len(calls) == 1 else "0" * 64

    monkeypatch.setattr(pg_import, "_sha256_file", shifting)
    assert _run("--source", str(src), "--apply") == 2
    assert _counts(env) == (0, 0, 0, 0, 0)


def test_manifest_lists_counts_and_hashes_and_refuses_the_source_directory(tmp_path, env):
    src, recs = _standard(tmp_path)
    out = tmp_path / "manifests" / "m.json"
    out.parent.mkdir()
    assert _run("--source", str(src), "--apply", "--manifest", str(out)) == 0
    manifest = json.loads(out.read_text("utf-8"))
    assert manifest["record_count"] == 5 and len(manifest["records"]) == 5
    assert manifest["source_sha256"] == hashlib.sha256(src.read_bytes()).hexdigest()
    assert {r["id"] for r in manifest["records"]} == {r["id"] for r in recs}
    assert all(len(r["sha256"]) == 64 for r in manifest["records"])
    assert _run("--source", str(src), "--manifest", str(src.parent / "m.json")) == 2  # next to the source: refused
    assert not (src.parent / "m.json").exists()


def test_tenants_are_isolated(tmp_path, env):
    src, _ = _standard(tmp_path)
    assert _run("--source", str(src), "--apply", "--tenant", "alice") == 0
    assert len(_store(env, "alice").list_memories(limit=100)) == 5
    assert _store(env, "bob").list_memories() == [] and _store(env).list_memories() == []
    assert _run("--source", str(src), "--apply", "--tenant", "bob") == 0  # same ids, different tenant


def test_import_works_with_the_ordinary_app_role_too(tmp_path, pg, monkeypatch):
    src, _ = _standard(tmp_path)
    monkeypatch.delenv("JARVIS_DATABASE_MIGRATE_URL", raising=False)
    monkeypatch.setenv("JARVIS_DATABASE_URL", pg.app_dsn)
    monkeypatch.setenv("JARVIS_DATABASE_SCHEMA", pg.schema)
    assert _run("--source", str(src), "--apply") == 0
    assert len(_store(pg).list_memories(limit=100)) == 5


def test_never_prints_the_connection_string(tmp_path, env, capsys):
    src, _ = _standard(tmp_path)
    _run("--source", str(src), "--apply")
    out = capsys.readouterr()
    assert "postgresql://" not in out.out + out.err and env.admin_dsn.split("@")[0] not in out.out + out.err


def test_missing_database_url_is_a_usage_error_but_source_analysis_still_runs(tmp_path, monkeypatch, capsys):
    src, _ = _standard(tmp_path)
    monkeypatch.delenv("JARVIS_DATABASE_MIGRATE_URL", raising=False)
    monkeypatch.delenv("JARVIS_DATABASE_URL", raising=False)
    assert _run("--source", str(src)) == 0
    assert "analysis only" in capsys.readouterr().out
    assert _run("--source", str(src), "--apply") == 2


def test_missing_source_file_is_an_error(tmp_path, env):
    assert _run("--source", str(tmp_path / "nope.json")) == 2


def test_import_from_the_legacy_jsonb_blob_table(tmp_path, env, capsys):
    _, recs = _standard(tmp_path)
    doc = {"board": {"board_id": "blobboard"}, "schema": "continuity-ledger-v1", "memories": recs}
    with env.admin_conn() as conn:
        conn.execute("CREATE TABLE jarvis_tenant_ledgers (tenant_key text PRIMARY KEY, payload jsonb NOT NULL, updated_at timestamptz DEFAULT now())")
        conn.execute("INSERT INTO jarvis_tenant_ledgers (tenant_key, payload) VALUES ('legacy-tenant', %s::jsonb)", (json.dumps(doc),))
    assert _run("--source-blob", "legacy-tenant", "--blob-schema", env.schema, "--tenant", "alice", "--apply") == 0
    s = _store(env, "alice")
    assert len(s.list_memories(limit=100)) == 5 and s.get_board().board_id == "blobboard"
    assert s.verify_history() == []
    with env.admin_conn() as conn:  # the blob table is only read
        assert conn.execute("SELECT count(*) FROM jarvis_tenant_ledgers").fetchone()[0] == 1


def test_importing_the_json_store_class_output_round_trips(tmp_path, env):
    """A file written by the real JSON store (not hand-built) imports cleanly."""
    path = tmp_path / "real" / "jarvis-store.json"
    js = JarvisStore(str(path))
    a = js.create_memory(MemoryCreate(content="first real record", source_agent="t", session_id="s", type="fact", tags=["x"]))
    b = js.create_memory(MemoryCreate(content="second real record", source_agent="t", session_id="s", type="decision", supersedes=a.id))
    js.update_memory(b.id, MemoryUpdate(status="verified"))
    before = _snapshot(path)
    assert _run("--source", str(path), "--apply") == 0
    s = _store(env)
    assert s.get_memory(b.id).model_dump() == js.get_memory(b.id).model_dump()
    assert s.get_memory(b.id).version == 2 and s.get_memory(a.id).version == 1
    assert _snapshot(path) == before
