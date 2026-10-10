"""emr_search_ledger: ranked word search over the ledger, identical on the JSON store and Postgres (schema V9)."""

from __future__ import annotations

import contextlib
import hashlib
import json
import time
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import psycopg
import pytest
from fastapi.testclient import TestClient

from app import ledger_search, pg_store
from app.emr_latest import LatestError
from app.ledger_search import SearchParams, record_tokens, search_ledger, tokens
from app.main import app
from app.models import EvidenceLink, MemoryCreate
from app.pg_schema import EXPECTED_SCHEMA_VERSION, V9_ROLLBACK, migrate
from app.pg_store import PostgresRowStore
from app.store import JarvisStore

KEY = "ledger-search-test-key"
HDR = {"X-API-Key": KEY}
MCP = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
URL = "/api/jarvis/memory/search"

TRICKY = [
    "Plain words, punctuation; and  spaces!",
    "emr_latest uses snake_case and kebab-case-words",
    "See https://example.com/path?q=1#frag and user@mail.example",
    "Ünïcödé ÄBC straße 日本語テキスト mixed with ASCII",
    "TABS\tand\nnewlines\r\nCRLF",
    "numbers 1.5 2026-10-09 v8 V9 0x1F",
    "",
]


# ------------------------------------------------------------------------------------------------ fixtures


@pytest.fixture(params=[pytest.param("json", marks=pytest.mark.json_store_only), "postgres"])
def backend(request, monkeypatch):
    monkeypatch.setenv("JARVIS_API_KEY", KEY)
    monkeypatch.delenv("JARVIS_ALLOW_UNAUTHENTICATED", raising=False)
    monkeypatch.setenv("JARVIS_MEMORY_WRITE_ENABLED", "true")
    if request.param == "postgres":
        pg = request.getfixturevalue("pg_schema")
        migrate(pg.admin_dsn, schema=pg.schema, app_role="jarvis_app_test")
        monkeypatch.setenv("JARVIS_DATABASE_URL", pg.app_dsn)
        monkeypatch.setenv("JARVIS_DATABASE_SCHEMA", pg.schema)
        monkeypatch.setenv("JARVIS_PG_STORE", "rows")
        request.node._pg = pg
    yield request.param
    pg_store.close_pools()


@pytest.fixture
def client(backend):
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


@pytest.fixture
def make_store(backend, tmp_path, request):
    def build(tenant: str):
        if backend == "json":
            return JarvisStore(str(tmp_path / f"tenant-{tenant}.json"))
        pg = request.node._pg
        return PostgresRowStore(pg.app_dsn, tenant, schema=pg.schema)

    return build


@contextlib.contextmanager
def frozen(when: datetime):
    class _Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return when

    with patch("app.store._now_iso", lambda: when.isoformat()), patch("app.pg_store.datetime", _Frozen):
        yield


BASE = datetime(2032, 3, 4, 5, 6, 7, tzinfo=timezone.utc)


def seed(store, rows):
    """Create rows at fixed, distinct timestamps (identical on every backend)."""
    made = []
    for i, row in enumerate(rows):
        body = dict(source_agent="agent", session_id="s", type="fact")
        body.update(row)
        with frozen(BASE + timedelta(seconds=i)):
            made.append(store.create_memory(MemoryCreate(**body)))
    return made


def add(client, content, **extra):
    body = {"content": content, "source_agent": "agent", "session_id": "s", "type": "fact", **extra}
    r = client.post("/api/jarvis/memory", headers=HDR, json=body)
    assert r.status_code == 200, r.text
    time.sleep(0.003)
    return r.json()["memory"]


def search(client, query, **params):
    return client.get(URL, headers=HDR, params={"query": query, **params})


def mcp_call(client, name, arguments, headers=HDR):
    h = {**MCP, **headers}
    client.post("/mcp", headers=h, json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    r = client.post("/mcp", headers=h, json={"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": name, "arguments": arguments}})
    assert r.status_code == 200, r.text
    return r.json()["result"]


CORPUS = [
    {"content": "the deploy runbook for the mint box", "subject": "mint deploy"},
    {"content": "notes about deploy keys and the backup timer", "tags": ["backup"]},
    {"content": "backup restore drill passed, deploy deploy deploy"},
    {"content": "unrelated grocery list"},
    {"content": "an old deploy note", "status": "archived"},
    {"content": "twin observation about deploy", "source_agent": "ai-twin", "type": "research"},
    {"content": "Mint DEPLOY checklist", "tags": ["deploy", "mint"]},
]


# ------------------------------------------------------------------------------------------------ tokenizer parity


@pytest.mark.postgres  # builds its own schema; the whole-suite Postgres mode must not pre-migrate it
def test_python_tokens_equal_the_database_function_character_for_character(pg_schema):
    migrate(pg_schema.admin_dsn, schema=pg_schema.schema, app_role="jarvis_app_test")
    with pg_schema.app_conn(None) as conn:
        for subject in (None, "Subject Line", "ÄBC-Déf"):
            for text in TRICKY:
                for tags in ([], ["Tag-One", "tag_two"], ["日本"]):
                    (db,) = conn.execute("SELECT jarvis_search_tokens(%s, %s, %s)", (subject, text, tags)).fetchone()
                    assert [t for t in db if t] == record_tokens(subject, text, tags), (subject, text, tags)


def test_tokens_are_simple_ascii_lowercase_without_stemming():
    assert tokens("Running RUNS run") == ["running", "runs", "run"]
    assert tokens("emr_latest kebab-case") == ["emr", "latest", "kebab", "case"]
    assert tokens("ÄBC") == ["Äbc"]  # only ASCII letters are folded, so the database locale never matters
    assert tokens("  !!! ") == []


# ------------------------------------------------------------------------------------------------ parity and ranking


QUERIES = ["deploy", "mint deploy", "backup", "deploy backup", "grocery", "nothing-matches", "DEPLOY"]


def _signature(body):
    return [(r["summary"], r["status"], r["score"], r["created_at"]) for r in body["records"]]


@pytest.mark.postgres  # builds its own schema; the whole-suite Postgres mode must not pre-migrate it
def test_both_backends_return_the_same_records_in_the_same_order(pg_schema, tmp_path, monkeypatch):
    migrate(pg_schema.admin_dsn, schema=pg_schema.schema, app_role="jarvis_app_test")
    stores = {"json": JarvisStore(str(tmp_path / "parity.json")), "pg": PostgresRowStore(pg_schema.app_dsn, "parity", schema=pg_schema.schema)}
    try:
        for store in stores.values():
            seed(store, CORPUS)
        for q in QUERIES:
            for flags in ({}, {"include_archived": True, "include_twin": True}):
                out = {name: search_ledger(s, tenant="parity", params=SearchParams(query=q, limit=50, **flags), operator=False) for name, s in stores.items()}
                assert _signature(out["json"]) == _signature(out["pg"]), (q, flags)
                assert out["json"]["tokens"] == out["pg"]["tokens"]
    finally:
        pg_store.close_pools()


def test_ranking_subject_beats_tags_beats_content_and_ties_go_to_the_newest(make_store):
    store = make_store("rank")
    made = seed(store, [
        {"content": "deploy deploy deploy"},  # content tf capped at 3 -> 3
        {"content": "x", "tags": ["deploy"]},  # tag -> 4
        {"content": "y", "subject": "deploy"},  # subject -> 6
        {"content": "deploy once"},  # 1
        {"content": "deploy again"},  # 1 (newer than the one above)
    ])
    body = search_ledger(store, tenant="rank", params=SearchParams(query="deploy", limit=50), operator=False)
    assert [r["id"] for r in body["records"]] == [made[2].id, made[1].id, made[0].id, made[4].id, made[3].id]
    assert [r["score"] for r in body["records"]] == [6, 4, 3, 1, 1]


def test_every_query_word_is_required_and_an_exact_phrase_earns_a_bonus(make_store):
    store = make_store("phrase")
    made = seed(store, [
        {"content": "backup then later the restore"},
        {"content": "restore backup"},
        {"content": "backup restore"},
        {"content": "only backup here"},
    ])
    body = search_ledger(store, tenant="phrase", params=SearchParams(query="backup restore", limit=50), operator=False)
    ids = [r["id"] for r in body["records"]]
    assert made[3].id not in ids and set(ids) == {made[0].id, made[1].id, made[2].id}
    assert ids[0] == made[2].id and body["records"][0]["score"] == 2 + ledger_search.PHRASE_BONUS


def test_default_exclusions_and_flags_match_emr_latest(client):
    plain = add(client, "deploy plain")["id"]
    old = add(client, "deploy old")["id"]
    new = add(client, "deploy new", supersedes=old)["id"]
    archived = add(client, "deploy archived", status="archived")["id"]
    twin = add(client, "deploy twin", source_agent="ai-twin", type="research")["id"]
    decision = add(client, "deploy decision", type="decision")["id"]
    ids = lambda **p: {r["id"] for r in search(client, "deploy", limit=50, **p).json()["records"]}  # noqa: E731
    assert ids() == {plain, new, decision}
    assert ids(include_superseded="true") == {plain, old, new, decision}
    assert ids(include_archived="true") == {plain, new, decision, archived}
    assert ids(include_twin="true") == {plain, new, decision, twin}
    assert ids(type="decision") == {decision}


def test_the_candidate_cap_is_reported_and_identical_on_both_backends(make_store, monkeypatch):
    monkeypatch.setattr(ledger_search, "MAX_CANDIDATES", 3)
    store = make_store("cap")
    made = seed(store, [{"content": f"deploy number {i}"} for i in range(5)])
    body = search_ledger(store, tenant="cap", params=SearchParams(query="deploy", limit=50), operator=False)
    assert body["candidates_capped"] is True
    assert [r["id"] for r in body["records"]] == [made[4].id, made[3].id, made[2].id]  # the newest three matches


# ------------------------------------------------------------------------------------------------ refusals, auth, tenancy


@pytest.mark.parametrize("query,reason", [("", "QUERY_EMPTY"), ("   ", "QUERY_EMPTY"), ("!!! ...", "QUERY_EMPTY"),
                                          ("x" * 501, "QUERY_TOO_LONG"), (" ".join(f"w{i}" for i in range(17)), "QUERY_TOO_LONG")])
def test_bad_queries_are_refused_with_a_reason(client, query, reason):
    r = search(client, query)
    assert r.status_code == 422 and r.json()["reason"] == reason and r.json()["code"] == "invalid_request"


def test_a_missing_query_and_a_bad_limit_are_refused(client):
    r = client.get(URL, headers=HDR)
    assert r.status_code == 422 and r.json()["reason"] == "QUERY_EMPTY"
    for bad in ("0", "51", "-1", "abc"):
        r = search(client, "deploy", limit=bad)
        assert r.status_code == 422 and r.json()["reason"] == "LIMIT_OUT_OF_RANGE"
    out = mcp_call(client, "emr_search_ledger", {"query": "deploy", "limit": 0})
    assert out["isError"] and out["structuredContent"]["error"] == {"code": "invalid_request", "reason": "LIMIT_OUT_OF_RANGE"}


@pytest.mark.parametrize("headers", [{}, {"X-API-Key": "wrong"}])
def test_no_or_wrong_credentials_get_authority_denied(client, headers):
    r = client.get(URL, headers=headers, params={"query": "deploy"})
    assert r.status_code == 401 and r.json()["reason"] == "AUTHORITY_DENIED"
    r = client.post("/api/jarvis/tools/emr_search_ledger", headers=headers, json={"query": "deploy"})
    assert r.status_code == 401 and r.json()["reason"] == "AUTHORITY_DENIED"


def test_an_unresolved_tenant_is_refused(client):
    add(client, "deploy")
    with patch("app.main.oauth_enabled", return_value=True), patch("app.main.current_tenant_key", return_value=None):
        r = search(client, "deploy")
    assert r.status_code == 403 and r.json()["reason"] == "TENANT_UNRESOLVED" and "records" not in r.json()


def test_one_tenants_records_never_match_for_another(make_store):
    x, y = make_store("tenant-x"), make_store("tenant-y")
    seed(x, [{"content": "secret deploy plan of x"}])
    seed(y, [{"content": "y has its own deploy"}])
    body = search_ledger(y, tenant="tenant-y", params=SearchParams(query="deploy", limit=50), operator=False)
    assert [r["summary"] for r in body["records"]] == ["y has its own deploy"]
    assert search_ledger(y, tenant="tenant-y", params=SearchParams(query="secret", limit=50), operator=False)["records"] == []


# ------------------------------------------------------------------------------------------------ surfaces and read-only


def test_mcp_tool_tool_route_and_http_agree(client):
    add(client, "mint deploy runbook", subject="deploy")
    add(client, "deploy keys", tags=["deploy"])
    add(client, "a deploy note")
    for args in ({"query": "deploy"}, {"query": "deploy", "limit": 2}, {"query": "mint deploy"}):
        http = search(client, **{k: v for k, v in args.items()}).json()
        tool = mcp_call(client, "emr_search_ledger", args)
        assert tool["isError"] is False
        assert tool["structuredContent"]["records"] == http["records"]
        assert tool["structuredContent"]["result_digest"] == http["result_digest"]
        posted = client.post("/api/jarvis/tools/emr_search_ledger", headers=HDR, json=args).json()
        assert posted["result_digest"] == http["result_digest"]


def test_the_digest_covers_id_created_at_status_and_score_in_order(client):
    add(client, "deploy one")
    add(client, "deploy two", subject="deploy")
    body = search(client, "deploy").json()
    blob = json.dumps([[r["id"], r["created_at"], r["status"], r["score"]] for r in body["records"]], separators=(",", ":"), ensure_ascii=False)
    assert body["result_digest"] == hashlib.sha256(blob.encode("utf-8")).hexdigest()
    assert search(client, "nothing").json()["result_digest"] == hashlib.sha256(b"[]").hexdigest()


def test_fifty_searches_change_nothing(client, backend, tmp_path):
    for i in range(4):
        add(client, f"deploy {i}")

    def snapshot():
        if backend == "postgres":
            from app.store import get_store

            s = get_store()
            return (s.history_seq(), s.block_head(), [m.model_dump() for m in s.list_memories(limit=50)])
        data = tmp_path / "jarvis-store.json"
        return (hashlib.sha256(data.read_bytes()).hexdigest(), data.stat().st_mtime_ns)

    before = snapshot()
    for i in range(50):
        assert search(client, "deploy" if i % 2 else "deploy 1").status_code == 200
    assert snapshot() == before


# ------------------------------------------------------------------------------------------------ V9 migration on a populated ledger


def _jsonb_rows(pg):
    with pg.admin_conn() as conn:
        return conn.execute("SELECT id, jarvis_memory_json(m)::text FROM memories m ORDER BY tenant_key, id").fetchall()


@pytest.mark.postgres  # builds its own schema; the whole-suite Postgres mode must not pre-migrate it
def test_v9_on_a_populated_ledger_changes_no_row_hash_or_root_and_rolls_back_cleanly(pg_schema):
    assert migrate(pg_schema.admin_dsn, schema=pg_schema.schema, app_role="jarvis_app_test", up_to=8) == 8
    with patch("app.pg_schema.EXPECTED_SCHEMA_VERSION", 8), patch("app.pg_store.check_schema_version", lambda conn: None):
        store = PostgresRowStore(pg_schema.app_dsn, "migr", schema=pg_schema.schema)
        made = seed(store, [{"content": f"populated record {i} about deploy", "tags": ["t"], "evidence": [EvidenceLink(kind="ref", ref=f"doc#{i}")]} for i in range(12)])
        store.update_memory(made[0].id, __import__("app.models", fromlist=["MemoryUpdate"]).MemoryUpdate(subject="changed"))
        store.delete_memory(made[1].id)
        before_root = store.replay_state().state_root
        before_problems = store.verify_history()
        before_rows = _jsonb_rows(pg_schema)
        before_seq = store.history_seq()
        pg_store.close_pools()
    assert before_problems == []

    assert migrate(pg_schema.admin_dsn, schema=pg_schema.schema, app_role="jarvis_app_test") == EXPECTED_SCHEMA_VERSION == 9
    store = PostgresRowStore(pg_schema.app_dsn, "migr", schema=pg_schema.schema)
    try:
        assert store.verify_history() == []
        assert store.replay_state().state_root == before_root
        assert store.history_seq() == before_seq
        assert _jsonb_rows(pg_schema) == before_rows  # no snapshot changed: there is no new column
        with pg_schema.admin_conn() as conn:
            cols = [r[0] for r in conn.execute("SELECT column_name FROM information_schema.columns WHERE table_schema = %s AND table_name = 'memories'", (pg_schema.schema,))]
            assert "search_tsv" not in cols
            conn.execute("SET enable_seqscan = off")
            plan = "\n".join(r[0] for r in conn.execute(
                "EXPLAIN SELECT id FROM memories m WHERE jarvis_search_tokens(m.subject, m.content, m.tags) @> ARRAY['deploy']"))
            assert "memories_search_idx" in plan
        hits = search_ledger(store, tenant="migr", params=SearchParams(query="populated deploy", limit=50), operator=False)
        assert len(hits["records"]) == 11  # 12 seeded, one deleted
    finally:
        pg_store.close_pools()

    with psycopg.connect(pg_schema.admin_dsn, options=f"-c search_path={pg_schema.schema}") as conn:  # one transaction
        conn.execute(V9_ROLLBACK)
    with pg_schema.admin_conn() as conn:
        assert conn.execute("SELECT max(version) FROM schema_version").fetchone()[0] == 8
        assert conn.execute("SELECT to_regclass('memories_search_idx')").fetchone()[0] is None
        assert conn.execute("SELECT count(*) FROM pg_proc WHERE proname = 'jarvis_search_tokens' AND pronamespace = %s::regnamespace", (pg_schema.schema,)).fetchone()[0] == 0
    assert _jsonb_rows(pg_schema) == before_rows
    with patch("app.pg_schema.EXPECTED_SCHEMA_VERSION", 8), patch("app.pg_store.check_schema_version", lambda conn: None):
        store = PostgresRowStore(pg_schema.app_dsn, "migr", schema=pg_schema.schema)
        try:
            assert store.verify_history() == [] and store.replay_state().state_root == before_root
        finally:
            pg_store.close_pools()
    assert migrate(pg_schema.admin_dsn, schema=pg_schema.schema, app_role="jarvis_app_test") == 9  # and forward again


# ------------------------------------------------------------------------------------------------ /memory/unified label


def test_unified_labels_nx_results_as_file_hits(monkeypatch):
    monkeypatch.setenv("JARVIS_NX_ENABLED", "true")

    class FakeNx:
        def search(self, query, limit=25):
            return {"filenames": [{"path": "C:/x.txt"}], "content": []}

    with patch("app.main._nx_client", return_value=FakeNx()), TestClient(app) as c:
        body = c.get("/api/jarvis/memory/unified", params={"query": "x"}).json()
    assert body["file_hits"] == {"filenames": [{"path": "C:/x.txt"}], "content": []}
    assert "long_term_memory" not in body


def test_search_is_refused_when_unexpected_types_are_passed(client):
    out = mcp_call(client, "emr_search_ledger", {"query": 5})
    assert out["isError"] and out["structuredContent"]["error"]["reason"] == "QUERY_EMPTY"
    with pytest.raises(LatestError):
        ledger_search.query_tokens(None)


# --- review follow-ups: repeated query words, exported catalog -------------------------------------------------------

def test_phrase_bonus_needs_the_whole_query_including_repeated_words():
    from types import SimpleNamespace

    from app import ledger_search as ls

    rec = lambda text: SimpleNamespace(subject=None, tags=[], content=text)
    qtoks = ls.query_tokens("foo bar foo")
    assert qtoks == ["foo", "bar"]  # distinct words still drive candidate selection
    phrase = ls.tokens("foo bar foo")
    without = ls.score(rec("foo bar"), qtoks, phrase)  # lacks the whole phrase: no bonus
    with_phrase = ls.score(rec("foo bar foo"), qtoks, phrase)
    assert with_phrase >= without + ls.PHRASE_BONUS
    # without repeats nothing changes: the default phrase is the distinct tokens
    assert ls.score(rec("foo bar"), ["foo", "bar"]) == ls.score(rec("foo bar"), ["foo", "bar"], ["foo", "bar"])


def test_exported_tool_catalog_carries_the_ledger_search_and_latest_schemas():
    from app.emr_tool import tool_catalog
    from mcp_server.protocol import EMR_LATEST_TOOL, EMR_SEARCH_LEDGER_TOOL

    cat = tool_catalog()
    by_name = {t["function"]["name"]: t["function"] for t in cat["tools"]}
    for mcp in (EMR_SEARCH_LEDGER_TOOL, EMR_LATEST_TOOL):
        assert mcp["name"] in by_name, f"{mcp['name']} is in write_policy but not discoverable"
        assert by_name[mcp["name"]]["parameters"] == mcp["inputSchema"]
    # every tool the policy names as callable here has a schema
    assert set(cat["write_policy"]) - {"emr_recall"} >= {"emr_latest", "emr_search_ledger"}
    assert {"emr_latest", "emr_search_ledger"} <= set(by_name)


def test_unified_guide_uses_the_file_hits_key():
    from pathlib import Path

    guide = (Path(__file__).resolve().parent.parent / "UNIFIED_MEMORY_SYSTEM.md").read_text(encoding="utf-8")
    assert "long_term_memory" not in guide and '"file_hits"' in guide


def test_phrase_search_is_linear_and_agrees_with_the_naive_definition():
    import random
    import time
    from types import SimpleNamespace

    from app import ledger_search as ls

    rng = random.Random(7)
    for _ in range(500):
        seq = [rng.choice("ab") for _ in range(rng.randint(0, 12))]
        run = [rng.choice("ab") for _ in range(rng.randint(1, 5))]
        naive = any(seq[i:i + len(run)] == run for i in range(len(seq) - len(run) + 1))
        assert ls._contains_run(seq, run) is naive, (seq, run)

    # worst case for the old per-position slicing: a 124-word phrase that almost matches everywhere in a 2000-word record
    seq, run = ["the"] * 1999 + ["x"], ["the"] * 123 + ["y"]
    n = len(run)

    def timed(fn):
        start = time.perf_counter()
        for _ in range(300):
            assert fn() is False
        return time.perf_counter() - start

    linear = timed(lambda: ls._contains_run(seq, run))
    slicing = timed(lambda: any(seq[i:i + n] == run for i in range(len(seq) - n + 1)))
    assert linear < slicing / 2, (linear, slicing)  # relative, so a slow runner does not flake it
