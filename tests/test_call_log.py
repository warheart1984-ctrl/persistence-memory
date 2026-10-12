"""The server-side call log ("call witness"): every tool call is written by the server, tamper-evidently, without touching the ledger.

Backend-parametrized tests run on the JSON store and on Postgres (CI's test-postgres job sets JARVIS_TEST_PG_DSN; locally the
Postgres variants skip only when no throwaway server is configured).  See docs/call_log.md.
"""

from __future__ import annotations

import ast
import hashlib
import json
import math
import multiprocessing
import os
import socket
import stat
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import call_log, pg_store
from app.main import app
from app.pg_schema import migrate

KEY = "call-log-test-KEY-0123456789"
HDR = {"X-API-Key": KEY}
MCP = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
MARKER = "UNIQUE-ARGUMENT-MARKER-7f3a9c"
ROOT = Path(__file__).resolve().parents[1]


# ----------------------------------------------------------------------------------------------------- fixtures


@pytest.fixture(params=[pytest.param("json", marks=pytest.mark.json_store_only), "postgres"])
def backend(request, monkeypatch):
    monkeypatch.setenv("JARVIS_API_KEY", KEY)
    monkeypatch.delenv("JARVIS_ALLOW_UNAUTHENTICATED", raising=False)
    monkeypatch.delenv("JARVIS_CURSOR_HMAC_KEY", raising=False)
    monkeypatch.setenv("JARVIS_MEMORY_WRITE_ENABLED", "true")
    if request.param == "postgres":
        pg = request.getfixturevalue("pg_schema")
        migrate(pg.admin_dsn, schema=pg.schema, app_role="jarvis_app_test")
        monkeypatch.setenv("JARVIS_DATABASE_URL", pg.app_dsn)
        monkeypatch.setenv("JARVIS_DATABASE_SCHEMA", pg.schema)
        monkeypatch.setenv("JARVIS_PG_STORE", "rows")
    yield request.param
    pg_store.close_pools()


@pytest.fixture
def client(backend):
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


@pytest.fixture
def jclient(monkeypatch):
    """JSON store only: for tests that are about the log, not the backend."""
    monkeypatch.setenv("JARVIS_API_KEY", KEY)
    monkeypatch.delenv("JARVIS_ALLOW_UNAUTHENTICATED", raising=False)
    monkeypatch.setenv("JARVIS_MEMORY_WRITE_ENABLED", "true")
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


@pytest.fixture
def fixed_minute(monkeypatch):
    """A call log whose clock does not move: the per-minute caps and windows cannot straddle a real minute boundary mid-test."""
    fixed = datetime.now(timezone.utc).replace(second=30, microsecond=0)
    real = call_log.CallLog.__init__

    def init(self, directory, *, clock=None, **kw):
        real(self, directory, clock=clock or (lambda: fixed), **kw)

    monkeypatch.setattr(call_log.CallLog, "__init__", init)
    call_log.reset_for_tests()


def log_dir() -> Path:
    return call_log.log_dir()


def entries(client=None) -> list[dict]:
    """Oldest first, straight from the files (not through the API)."""
    out = []
    for path in sorted(log_dir().glob("calls-*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                out.append(json.loads(line))
            except ValueError:
                pass  # a torn line (see the torn-tail test)
    return out


def add(client, content="a record", **extra):
    body = {"content": content, "source_agent": "agent", "session_id": "s", "type": "fact", **extra}
    r = client.post("/api/jarvis/memory", headers=HDR, json=body)
    assert r.status_code == 200, r.text
    return r.json()["memory"]


def tool(client, name, args=None, headers=None):
    return client.post(f"/api/jarvis/tools/{name}", headers={**HDR, **(headers or {})}, json=args or {})


# ------------------------------------------------------------------------- 1. every path writes exactly one entry


@pytest.mark.parametrize(
    "name,args",
    [
        ("emr_latest", {}),
        ("emr_search_ledger", {"query": "record"}),
        ("emr_recall", {"intent": "constitutional", "query": "record"}),
        ("search", {"query": "record"}),
        ("emr_search", {"query": "record"}),
        ("fetch", {"id": "mem-000000000000"}),
        ("emr_fetch", {"id": "mem-000000000000"}),
    ],
)
def test_each_http_tool_route_writes_exactly_one_entry(client, name, args):
    add(client)
    before = len(entries())  # the add is an http-api write: counted, then we measure the tool call alone
    r = tool(client, name, args)
    assert r.status_code in (200, 400, 404, 422), r.text
    got = entries()[before:]
    assert [e["tool"] for e in got] == [name] and got[0]["transport"] == "http-tool"
    assert r.headers.get("x-jarvis-call-seq") == str(got[0]["seq"])


def test_mcp_http_calls_write_one_entry_each_including_a_batch(client):
    h = {**MCP, **HDR}
    init = client.post("/mcp", headers=h, json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"clientInfo": {"name": "cursor", "version": "9.1"}}})
    sid = init.headers["mcp-session-id"]
    h = {**h, "mcp-session-id": sid}
    before = len(entries())
    r = client.post("/mcp", headers=h, json={"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "emr_latest", "arguments": {}}})
    assert r.status_code == 200 and r.json()["result"]["isError"] is False
    got = entries()[before:]
    assert len(got) == 1 and got[0]["transport"] == "mcp-http" and got[0]["tool"] == "emr_latest"
    assert (got[0]["client_name"], got[0]["client_version"], got[0]["client_self_reported"]) == ("cursor", "9.1", True)
    before = len(entries())
    batch = [{"jsonrpc": "2.0", "id": i, "method": "tools/call", "params": {"name": n, "arguments": a}} for i, (n, a) in enumerate(
        [("emr_latest", {}), ("emr_search_ledger", {"query": "x"}), ("no_such_tool", {})], start=10)]
    r = client.post("/mcp", headers=h, json=batch)
    assert r.status_code == 200
    got = entries()[before:]
    assert [e["tool"] for e in got] == ["emr_latest", "emr_search_ledger", "no_such_tool"]
    assert [e["outcome"] for e in got] == ["ok", "ok", "error"]  # the unknown tool is a failed call and is logged too


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_the_stdio_proxy_is_witnessed_as_mcp_stdio_with_its_self_reported_client(jclient, monkeypatch, capsys):
    import uvicorn

    from mcp_server import emr_stdio

    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    try:
        monkeypatch.setenv("JARVIS_MEMORYBOARD_URL", f"http://127.0.0.1:{port}")
        monkeypatch.setenv("JARVIS_API_KEY", KEY)
        emr_stdio.handle_message({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"clientInfo": {"name": "opencode", "version": "0.9"}}})
        capsys.readouterr()
        before = len(entries())
        emr_stdio.handle_message({"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "emr_latest", "arguments": {}}})
        got = entries()[before:]
        assert len(got) == 1, got
        assert (got[0]["transport"], got[0]["client_name"], got[0]["client_version"], got[0]["tool"]) == ("mcp-stdio", "opencode", "0.9", "emr_latest")
        assert got[0]["client_self_reported"] is True
    finally:
        server.should_exit = True
        thread.join(timeout=5)


def test_writes_under_api_jarvis_are_logged_as_http_api(jclient):
    before = len(entries())
    rec = add(jclient)
    assert jclient.patch(f"/api/jarvis/memory/{rec['id']}", headers=HDR, json={"subject": "s", "expected_version": rec["version"]}).status_code == 200
    assert jclient.delete(f"/api/jarvis/memory/{rec['id']}", headers=HDR).status_code == 200
    got = entries()[before:]
    assert [(e["transport"], e["tool"]) for e in got] == [
        ("http-api", "POST /api/jarvis/memory"), ("http-api", "PATCH /api/jarvis/memory/{id}"), ("http-api", "DELETE /api/jarvis/memory/{id}")]
    assert [e["target"] for e in got][1:] == [rec["id"], rec["id"]] and all(e["outcome"] == "ok" for e in got)


# ------------------------------------------------------------ 1b. GET reads of /api/jarvis/memory/* (issue #73)

READ_PATHS = ["/api/jarvis/memory", "/api/jarvis/memory/{id}", "/api/jarvis/memory/latest?limit=3", "/api/jarvis/memory/search?query=record",
              "/api/jarvis/memory/board", "/api/jarvis/memory/retrieve?query=record"]


def test_every_memory_get_read_is_witnessed_with_its_route_target_and_client(jclient):
    rec = add(jclient)
    before = len(entries())
    seen = []
    for path in READ_PATHS:
        r = jclient.get(path.replace("{id}", rec["id"]), headers={**HDR, "X-Jarvis-MCP-Client": "grok/1.2", "X-Jarvis-MCP-Transport": "stdio"})
        assert r.status_code == 200, (path, r.text)
        seen.append(r.headers.get("x-jarvis-call-seq"))
    got = entries()[before:]
    assert len(got) == len(READ_PATHS) and [str(e["seq"]) for e in got] == seen
    assert all(e["method"] == "GET" and e["outcome"] == "ok" and e["transport"] == "mcp-stdio" and e["client_name"] == "grok" for e in got)
    assert [e["tool"] for e in got] == ["GET /api/jarvis/memory", "GET /api/jarvis/memory/{id}", "GET /api/jarvis/memory/latest", "GET /api/jarvis/memory/search",
                                        "GET /api/jarvis/memory/board", "GET /api/jarvis/memory/retrieve"]
    assert got[1]["target"] == rec["id"] and got[0]["target"] is None


def test_a_plain_http_read_is_http_api_and_its_query_is_hashed_never_stored(jclient):
    add(jclient)
    before = len(entries())
    jclient.get("/api/jarvis/memory/search?query=UNIQUE-READ-MARKER-5d1", headers=HDR)
    (e,) = entries()[before:]
    assert e["transport"] == "http-api" and e["args_sha256"] and "UNIQUE-READ-MARKER-5d1" not in json.dumps(e)
    assert call_log.args_hash({"query": "UNIQUE-READ-MARKER-5d1"}) == e["args_sha256"]


def test_a_read_that_is_denied_or_missing_is_witnessed_with_its_outcome(jclient):
    jclient.get("/api/jarvis/memory", headers={"X-API-Key": "wrong"})
    jclient.get("/api/jarvis/memory/mem-000000000000", headers=HDR)
    got = entries()
    assert [(e["outcome"], e["status_code"]) for e in got] == [("denied", 401), ("error", 404)]


def test_reads_outside_the_memory_routes_are_still_not_witnessed(jclient):
    for path in ("/ready", "/api/jarvis/replay/state?limit=1", "/api/jarvis/tools", "/api/jarvis/tools/calls", "/api/jarvis/tools/calls/verify"):
        jclient.get(path, headers=HDR)
    assert entries() == []


def test_turning_read_witnessing_off_leaves_writes_and_tools_witnessed(jclient, monkeypatch):
    monkeypatch.setenv("JARVIS_CALL_LOG_READS", "0")
    rec = add(jclient)
    assert jclient.get("/api/jarvis/memory", headers=HDR).status_code == 200
    assert jclient.get(f"/api/jarvis/memory/{rec['id']}", headers=HDR).status_code == 200
    tool(jclient, "emr_latest", {})
    assert [e["tool"] for e in entries()] == ["POST /api/jarvis/memory", "emr_latest"]


def test_a_flood_of_reads_is_capped_and_rolled_into_one_read_suppressed_entry(fixed_minute, jclient, monkeypatch):
    monkeypatch.setenv("JARVIS_CALL_LOG_READ_CAP_PER_MIN", "5")
    for _ in range(20):
        assert jclient.get("/api/jarvis/memory/latest?limit=1", headers=HDR).status_code == 200
    page = jclient.get("/api/jarvis/tools/calls?limit=200", headers=HDR).json()   # reading the log flushes the open summary
    reads = [e for e in page["entries"] if e["outcome"] == "ok"]
    summary = [e for e in page["entries"] if e["outcome"] == "read_suppressed"]
    assert len(reads) == 5 and len(summary) == 1
    assert summary[0]["count"] == 15 and summary[0]["error_code"] == "SUPPRESSED:15" and summary[0]["window_start"] and summary[0]["window_end"]
    assert jclient.get("/api/jarvis/tools/calls/verify", headers=HDR).json()["ok"] is True


def test_a_write_closes_the_open_read_summary_first_so_the_order_shows_what_preceded_it(fixed_minute, jclient, monkeypatch):
    monkeypatch.setenv("JARVIS_CALL_LOG_READ_CAP_PER_MIN", "2")
    for _ in range(6):
        jclient.get("/api/jarvis/memory", headers=HDR)
    add(jclient)
    got = entries()
    assert [e["outcome"] for e in got] == ["ok", "ok", "read_suppressed", "ok"] and got[2]["count"] == 4 and got[3]["tool"] == "POST /api/jarvis/memory"


def _stdio(tool, method, route):
    return call_log.make_fields(transport="mcp-stdio", tool=tool, args={}, outcome="ok", started=time.perf_counter(), route=route, method=method, client_name="grok")


def test_a_stdio_rest_write_closes_the_read_summary_but_a_stdio_tool_call_does_not(fixed_minute, tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_CALL_LOG_READ_CAP_PER_MIN", "1")
    log = call_log.CallLog(tmp_path / "stdio")
    for i in range(4):
        log.record(call_log.make_fields(transport="mcp-stdio", tool="GET /api/jarvis/memory", args={"i": i}, outcome="ok", started=time.perf_counter(), route="/api/jarvis/memory", method="GET"))
    log.record(_stdio("emr_latest", "POST", "/api/jarvis/tools/emr_latest"))      # a read-only tool through the same adapter: the summary stays open
    assert [e["outcome"] for e in log.entries_desc()] == ["ok", "ok"]            # (entries_desc does not flush; query() would close the summary itself)
    log.record(_stdio("POST /api/jarvis/memory", "POST", "/api/jarvis/memory"))   # a REST write through the same adapter: the boundary
    got = [(e["outcome"], e["tool"]) for e in list(log.entries_desc())[::-1]]
    assert got == [("ok", "GET /api/jarvis/memory"), ("ok", "emr_latest"), ("read_suppressed", "(read calls)"), ("ok", "POST /api/jarvis/memory")]


def test_a_read_only_post_endpoint_does_not_close_the_read_summary(fixed_minute, tmp_path, monkeypatch):
    """graph/bfs, rag/query and the like are POST but only read: alternating them with reads must not write a summary per read."""
    monkeypatch.setenv("JARVIS_CALL_LOG_READ_CAP_PER_MIN", "1")
    log = call_log.CallLog(tmp_path / "ro")
    for i in range(8):
        log.record(call_log.make_fields(transport="http-api", tool="GET /api/jarvis/memory", args={"i": i}, outcome="ok", started=time.perf_counter(), route="/api/jarvis/memory", method="GET"))
        log.record(_stdio("POST /api/jarvis/memory/graph/bfs", "POST", "/api/jarvis/memory/graph/bfs"))
        log.record(_stdio("POST /api/jarvis/rag/query", "POST", "/api/jarvis/rag/query"))
    assert [e["outcome"] for e in log.entries_desc()].count("read_suppressed") == 0       # nothing closed the summary yet
    log.record(_stdio("DELETE /api/jarvis/memory/{id}", "DELETE", "/api/jarvis/memory/{id}"))
    got = [e for e in log.entries_desc() if e["outcome"] == "read_suppressed"]
    assert len(got) == 1 and got[0]["count"] == 7


@pytest.mark.parametrize("method,route,closes", [("POST", "/api/jarvis/memory/promote", True), ("POST", "/api/jarvis/memory/pipeline", True), ("POST", "/api/jarvis/memory", True),
                                                  ("PUT", "/api/jarvis/memory/{id}", True), ("POST", "/api/jarvis/memory/external-search", False),
                                                  ("POST", "/api/jarvis/memory/graph/bfs", False), ("POST", "/api/jarvis/rag/query", False)])
def test_which_rest_routes_close_the_read_summary(fixed_minute, tmp_path, monkeypatch, method, route, closes):
    monkeypatch.setenv("JARVIS_CALL_LOG_READ_CAP_PER_MIN", "1")
    log = call_log.CallLog(tmp_path / "which")
    for i in range(3):
        log.record(_read_fields(i))                                             # 1 written, 2 suppressed
    log.record(call_log.make_fields(transport="http-api", tool=f"{method} {route}", args={}, outcome="ok", started=time.perf_counter(), route=route, method=method))
    assert [e["outcome"] for e in log.entries_desc()].count("read_suppressed") == (1 if closes else 0)


def test_a_failed_write_is_not_an_ordering_boundary(fixed_minute, tmp_path, monkeypatch):
    """A PATCH that 404s or a write tool that errors changed nothing; alternating them with reads must not write a summary per read."""
    monkeypatch.setenv("JARVIS_CALL_LOG_READ_CAP_PER_MIN", "1")
    log = call_log.CallLog(tmp_path / "fw")
    for i in range(6):
        log.record(call_log.make_fields(transport="http-api", tool="GET /api/jarvis/memory", args={"i": i}, outcome="ok", started=time.perf_counter(), route="/api/jarvis/memory", method="GET"))
        log.record(call_log.make_fields(transport="http-api", tool="PATCH /api/jarvis/memory/{id}", args={}, outcome="error", started=time.perf_counter(), status_code=404,
                                        route="/api/jarvis/memory/{id}", method="PATCH"))
        log.record(call_log.make_fields(transport="http-tool", tool="emr_remember", args={}, outcome="error", started=time.perf_counter(), status_code=422,
                                        route="/api/jarvis/tools/emr_remember", method="POST"))
    assert [e["outcome"] for e in log.entries_desc()].count("read_suppressed") == 0
    log.record(call_log.make_fields(transport="http-api", tool="PATCH /api/jarvis/memory/{id}", args={}, outcome="ok", started=time.perf_counter(), route="/api/jarvis/memory/{id}", method="PATCH"))
    assert [e["count"] for e in log.entries_desc() if e["outcome"] == "read_suppressed"] == [5]


def test_the_query_result_says_when_the_log_is_degraded_by_a_failed_summary_flush(fixed_minute, tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_CALL_LOG_READ_CAP_PER_MIN", "1")
    log = call_log.CallLog(tmp_path / "qd")
    for i in range(3):
        log.record(_read_fields(i))
    assert log.query(limit=10)["degraded"] is None                               # the first query flushes the summary: healthy
    for i in range(3):
        log.record(_read_fields(i))
    real = call_log.CallLog._append_locked
    monkeypatch.setattr(call_log.CallLog, "_append_locked", lambda self, *a, **k: (_ for _ in ()).throw(OSError("disk full")))
    page = log.query(limit=10)
    assert page["degraded"] and "summaries" in page["degraded"]["last_error"]
    monkeypatch.setattr(call_log.CallLog, "_append_locked", real)


def test_the_calls_endpoint_passes_the_degraded_state_through(jclient):
    add(jclient)
    body = jclient.get("/api/jarvis/tools/calls?limit=10", headers=HDR).json()
    assert "degraded" in body and body["degraded"] is None


def test_the_read_cap_does_not_throttle_tool_calls_or_writes(jclient, monkeypatch):
    monkeypatch.setenv("JARVIS_CALL_LOG_READ_CAP_PER_MIN", "1")
    for _ in range(4):
        tool(jclient, "emr_latest", {})
    for _ in range(3):
        add(jclient)
    assert [e["outcome"] for e in entries()] == ["ok"] * 7


def test_a_failing_log_never_blocks_a_read(jclient, monkeypatch):
    monkeypatch.setattr(call_log.CallLog, "_append_locked", lambda self, *a, **k: (_ for _ in ()).throw(OSError("disk full")))
    r = jclient.get("/api/jarvis/memory", headers=HDR)
    assert r.status_code == 200 and r.headers.get("x-jarvis-call-log") == "degraded"


def test_the_logs_own_endpoints_and_the_catalog_are_not_logged(jclient):
    jclient.get("/api/jarvis/tools", headers=HDR)
    jclient.get("/api/jarvis/tools/calls", headers=HDR)
    jclient.get("/api/jarvis/tools/calls/verify", headers=HDR)
    assert entries() == []


# -------------------------------------------------------------------------------------------- 2. no secrets in the log


def test_no_key_header_token_or_raw_argument_is_ever_stored(jclient):
    tool(jclient, "emr_search_ledger", {"query": MARKER}, headers={"Authorization": "Bearer bearer-secret-123", "X-EMR-Recall-Key": "recall-secret-456",
                                                                   "X-Jarvis-MCP-Client": "agent/1.0\x00\x1b[31mred"})
    add(jclient, MARKER + " body")
    jclient.get(f"/api/jarvis/tools/emr_latest?api_key={KEY}", headers=HDR)  # wrong method on a tools route: still must not store the query
    blob = b"".join(p.read_bytes() for p in log_dir().rglob("*") if p.is_file())
    for secret in (KEY, "bearer-secret-123", "recall-secret-456", MARKER, "\x1b"):
        assert secret.encode() not in blob, secret
    got = entries()
    assert all(len(e["args_sha256"]) == 64 for e in got) and "args" not in got[0]
    assert got[0]["client_name"] == "agent" and "\x00" not in json.dumps(got)  # control characters stripped from self-reported text


def test_args_hash_is_over_canonical_json_so_key_order_does_not_matter(jclient):
    tool(jclient, "emr_search_ledger", {"query": "a", "limit": 3})
    tool(jclient, "emr_search_ledger", {"limit": 3, "query": "a"})
    a, b = entries()
    assert a["args_sha256"] == b["args_sha256"] == hashlib.sha256(json.dumps({"limit": 3, "query": "a"}, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


# ----------------------------------------------------------------------------------- 3. the chain and tampering


def seed(client, n=6):
    for i in range(n):
        tool(client, "emr_latest", {"limit": 1 + i % 3})


def test_a_clean_log_verifies_and_reports_its_head(jclient):
    seed(jclient)
    r = jclient.get("/api/jarvis/tools/calls/verify", headers=HDR).json()
    assert r["ok"] is True and r["entries"] == 6 and r["head"]["seq"] == 6 and r["head"]["entry_hash"] == entries()[-1]["entry_hash"] and r["problems"] == []
    assert [e["seq"] for e in entries()] == [1, 2, 3, 4, 5, 6] and entries()[0]["prev_hash"] == call_log.GENESIS


def _lines():
    path = sorted(log_dir().glob("calls-*.jsonl"))[0]
    return path, path.read_text(encoding="utf-8").splitlines()


def test_editing_a_line_fails_verification(jclient):
    seed(jclient)
    path, lines = _lines()
    doctored = json.loads(lines[2])
    doctored["tool"] = "emr_remember"
    lines[2] = json.dumps(doctored, sort_keys=True, separators=(",", ":"))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    r = jclient.get("/api/jarvis/tools/calls/verify", headers=HDR).json()
    assert r["ok"] is False and any("edited" in p["problem"] for p in r["problems"])


def test_deleting_a_line_fails_verification(jclient):
    seed(jclient)
    path, lines = _lines()
    del lines[3]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    r = jclient.get("/api/jarvis/tools/calls/verify", headers=HDR).json()
    assert r["ok"] is False and any("deleted" in p["problem"] for p in r["problems"])


def test_reordering_lines_fails_verification(jclient):
    seed(jclient)
    path, lines = _lines()
    lines[1], lines[2] = lines[2], lines[1]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    assert jclient.get("/api/jarvis/tools/calls/verify", headers=HDR).json()["ok"] is False


def test_a_torn_last_line_is_reported_and_the_chain_carries_on(jclient):
    seed(jclient, 3)
    path = sorted(log_dir().glob("calls-*.jsonl"))[0]
    with open(path, "ab") as fh:
        fh.write(b'{"seq": 4, "ts": "2026-10-1')  # a crash mid-write: no newline
    tool(jclient, "emr_latest")  # the writer must cope: end the torn line, link to the last VALID entry
    r = jclient.get("/api/jarvis/tools/calls/verify", headers=HDR).json()
    assert [p["problem"] for p in r["problems"]] and all("valid entry" in p["problem"] or "torn" in p["problem"] for p in r["problems"])
    assert r["head"]["seq"] == 4 and entries()[-1]["prev_hash"] == entries()[-2]["entry_hash"]


# ---------------------------------------------------------------------------------- 4. concurrency across processes


def _worker(directory: str, count: int) -> None:
    log = call_log.CallLog(Path(directory))
    for i in range(count):
        started = time.perf_counter()
        log.record(call_log.make_fields(transport="http-tool", tool="emr_latest", args={"i": i}, outcome="ok", started=started))


def test_four_processes_get_unique_seq_and_a_valid_chain(tmp_path):
    d = str(tmp_path / "multi")
    ctx = multiprocessing.get_context("spawn")
    procs = [ctx.Process(target=_worker, args=(d, 25)) for _ in range(4)]
    for p in procs:
        p.start()
    for p in procs:
        p.join(120)
        assert p.exitcode == 0
    log = call_log.CallLog(Path(d))
    seqs = [e["seq"] for e in reversed(list(log.entries_desc()))]
    assert seqs == list(range(1, 101))  # unique, contiguous, in order
    assert log.verify()["ok"] is True


# ------------------------------------------------------------------------------- 5. denied and error calls are logged


def test_denied_and_error_calls_are_logged(jclient):
    r = jclient.post("/api/jarvis/tools/emr_latest", headers={"X-API-Key": "wrong"}, json={})
    assert r.status_code == 401
    r = tool(jclient, "emr_latest", {"limit": 0})
    assert r.status_code == 422
    r = jclient.post("/mcp", headers={**MCP, "X-API-Key": "wrong"}, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert r.status_code in (401, 403)
    got = entries()
    assert [(e["tool"], e["outcome"]) for e in got] == [("emr_latest", "denied"), ("emr_latest", "error"), ("(unauthenticated)", "denied")]
    assert got[0]["status_code"] == 401 and got[1]["error_code"] == "LIMIT_OUT_OF_RANGE" and got[2]["transport"] == "mcp-http"


def test_a_flood_of_denied_calls_is_capped_and_rolled_into_one_suppressed_entry(fixed_minute, jclient, monkeypatch):
    monkeypatch.setenv("JARVIS_CALL_LOG_DENIED_CAP_PER_MIN", "5")
    for _ in range(20):
        jclient.post("/api/jarvis/tools/emr_latest", headers={"X-API-Key": "wrong"}, json={})
    page = jclient.get("/api/jarvis/tools/calls?limit=200", headers=HDR).json()  # reading flushes the open summary
    denied = [e for e in page["entries"] if e["outcome"] == "denied"]
    summary = [e for e in page["entries"] if e["outcome"] == "denied_suppressed"]
    assert len(denied) == 5 and len(summary) == 1
    assert summary[0]["count"] == 15 and summary[0]["window_start"] and summary[0]["window_end"] and summary[0]["error_code"] == "SUPPRESSED:15"
    assert jclient.get("/api/jarvis/tools/calls/verify", headers=HDR).json()["ok"] is True


# --------------------------------------------------------------------- 6. the ledger is untouched by logging


def _ledger_fingerprint(client, backend):
    if backend == "postgres":
        s = client.get("/api/jarvis/replay/state?limit=1", headers=HDR).json()
        return {"state_root": s["state_root"], "history_seq": s["history_seq"], "records": s["record_count"],
                "receipts": client.get("/api/jarvis/replay/receipts", headers=HDR).json()["receipts"]}
    recs = client.get("/api/jarvis/memory?limit=200", headers=HDR).json()["memories"]
    hist = client.get("/api/jarvis/memory/history/verify", headers=HDR)
    return {"records": json.dumps(recs, sort_keys=True), "history": (hist.status_code, json.dumps(hist.json(), sort_keys=True))}


def test_a_hundred_logged_calls_leave_the_ledger_exactly_as_it_was(client, backend):
    add(client, "one")
    add(client, "two")
    before = _ledger_fingerprint(client, backend)
    n0 = len(entries())
    for i in range(100):
        tool(client, ("emr_latest", "emr_search_ledger", "search")[i % 3], {"query": "one"} if i % 3 else {})
    assert len(entries()) - n0 == 100
    assert _ledger_fingerprint(client, backend) == before


def test_the_log_modules_do_not_import_the_store():
    banned = {"app.store", "app.pg_store", "app.store_errors", "app.replay", "app.blocks"}
    for name in ("call_log.py", "call_witness.py"):
        tree = ast.parse((ROOT / "app" / name).read_text(encoding="utf-8"))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
                imported |= {f"{node.module}.{a.name}" for a in node.names}
            elif isinstance(node, ast.Import):
                imported |= {a.name for a in node.names}
        assert not (imported & banned), (name, imported & banned)


# ------------------------------------------------------------------------- 7. who may read the log


def test_a_tenant_cannot_read_the_log_and_its_calls_carry_its_tenant_key(monkeypatch, tmp_path):
    import app.auth as auth
    from app.identity import Principal

    monkeypatch.setenv("JARVIS_AUTH_MODE", "oauth")
    monkeypatch.setenv("JARVIS_CURSOR_HMAC_KEY", "k" * 32)  # emr_latest signs its cursors
    monkeypatch.setenv("JARVIS_MEMORY_WRITE_ENABLED", "true")
    monkeypatch.setenv("JARVIS_STORE_PATH", str(tmp_path / "operator.json"))
    monkeypatch.setenv("JARVIS_TENANT_STORE_DIR", str(tmp_path / "tenants"))
    monkeypatch.setattr(auth, "validate_access_token", lambda token, *, required_scope="memory.read": Principal(subject=token, scopes=frozenset({"memory.read", "memory.write"}), issuer="https://issuer.example"))
    bearer = {"Authorization": "Bearer alice"}
    with TestClient(app, raise_server_exceptions=False) as c:
        r = c.post("/api/jarvis/tools/emr_latest", headers=bearer, json={})
        assert r.status_code == 200, r.text
        for path in ("/api/jarvis/tools/calls", "/api/jarvis/tools/calls/verify"):
            denied = c.get(path, headers=bearer)
            assert denied.status_code == 403 and denied.json()["reason"] == "AUTHORITY_DENIED" and denied.json()["code"] == "denied"
    e = entries()[0]
    assert e["tool"] == "emr_latest" and len(e["tenant"]) == 64 and e["tenant"] != "operator" and "alice" not in json.dumps(entries())


def test_the_log_endpoints_need_the_operator_key(jclient):
    for path in ("/api/jarvis/tools/calls", "/api/jarvis/tools/calls/verify"):
        assert jclient.get(path).status_code == 401
        assert jclient.get(path, headers={"X-API-Key": "wrong"}).status_code == 401


# ------------------------------------------------------------------------------------------ 8. result digests


def test_the_logged_result_digest_is_the_one_the_tool_returned(client):
    add(client, "findable record")
    for name, args in (("emr_latest", {}), ("emr_search_ledger", {"query": "findable"})):
        r = tool(client, name, args).json()
        assert entries()[-1]["tool"] == name and entries()[-1]["result_digest"] == r["result_digest"] and len(r["result_digest"]) == 64
    h = {**MCP, **HDR}
    client.post("/mcp", headers=h, json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    out = client.post("/mcp", headers=h, json={"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "emr_latest", "arguments": {}}}).json()
    assert entries()[-1]["transport"] == "mcp-http" and entries()[-1]["result_digest"] == out["result"]["structuredContent"]["result_digest"]


# ------------------------------------------------------------------------- 9. the endpoints: filters, paging, limits


def test_filters_paging_and_limits(jclient):
    for i in range(7):
        tool(jclient, "emr_latest", {"limit": 1 + i}, headers={"X-Jarvis-MCP-Client": "devin/1.0" if i % 2 else "cursor/2.0"})
    tool(jclient, "emr_search_ledger", {"query": "x"})
    get = lambda q="": jclient.get(f"/api/jarvis/tools/calls?{q}", headers=HDR)  # noqa: E731
    page = get("limit=3").json()
    assert [e["seq"] for e in page["entries"]] == [8, 7, 6] and page["next_cursor"] == "6" and page["provenance"] == "server-witnessed"
    seen = [e["seq"] for e in page["entries"]]
    while page["next_cursor"]:
        page = get(f"limit=3&cursor={page['next_cursor']}").json()
        seen += [e["seq"] for e in page["entries"]]
    assert seen == [8, 7, 6, 5, 4, 3, 2, 1]
    assert {e["tool"] for e in get("tool=emr_search_ledger").json()["entries"]} == {"emr_search_ledger"}
    assert {e["client_name"] for e in get("client=dev").json()["entries"]} == {"devin"}
    assert get("since=2999-01-01T00:00:00Z").json()["entries"] == []
    for bad in ("limit=0", "limit=201", "cursor=abc"):
        assert get(bad).status_code == 422, bad
    assert len(get("limit=200").json()["entries"]) == 8


# ------------------------------------------------------------------ 10. rotation, retention, the flag, the file mode


def _fields(i=0):
    return call_log.make_fields(transport="http-tool", tool="emr_latest", args={"i": i}, outcome="ok", started=time.perf_counter())


def test_daily_files_chain_across_midnight(tmp_path):
    now = [datetime(2026, 10, 10, 23, 59, 58, tzinfo=timezone.utc)]
    log = call_log.CallLog(tmp_path / "rot", clock=lambda: now[0])
    log.append(_fields(1))
    now[0] = datetime(2026, 10, 11, 0, 0, 2, tzinfo=timezone.utc)
    log.append(_fields(2))
    files = sorted(p.name for p in (tmp_path / "rot").glob("calls-*.jsonl"))
    assert files == ["calls-20261010.jsonl", "calls-20261011.jsonl"]
    first = json.loads((tmp_path / "rot" / files[0]).read_text().splitlines()[-1])
    second = json.loads((tmp_path / "rot" / files[1]).read_text().splitlines()[0])
    assert second["prev_hash"] == first["entry_hash"] and second["seq"] == first["seq"] + 1
    assert log.verify()["ok"] is True


def test_retention_deletes_old_files_keeps_the_chain_verifiable_via_an_anchor(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_CALL_LOG_RETAIN_DAYS", "2")
    day = [datetime(2026, 10, 1, 12, tzinfo=timezone.utc)]
    log = call_log.CallLog(tmp_path / "ret", clock=lambda: day[0])
    for d in range(6):
        day[0] = datetime(2026, 10, 1 + d, 12, tzinfo=timezone.utc)
        log.append(_fields(d))
        log.append(_fields(d + 100))
    names = sorted(p.name for p in (tmp_path / "ret").glob("calls-*.jsonl"))
    assert names[0] >= "calls-20261003.jsonl" and len(names) <= 3
    v = log.verify()
    assert v["ok"] is True and v["anchor"] and v["anchor"]["seq"] >= 2 and v["entries"] < 12 and v["head"]["seq"] == 12
    assert (tmp_path / "ret" / "anchor.json").stat().st_mode & 0o077 == 0 or sys.platform == "win32"


def test_the_flag_defaults_on_in_dev_and_off_in_production_and_off_means_no_files(jclient, monkeypatch, tmp_path):
    monkeypatch.delenv("JARVIS_CALL_LOG_ENABLED", raising=False)
    monkeypatch.delenv("JARVIS_ENV", raising=False)
    assert call_log.enabled() is True
    monkeypatch.setenv("JARVIS_ENV", "production")
    assert call_log.enabled() is False
    monkeypatch.setenv("JARVIS_CALL_LOG_ENABLED", "1")
    assert call_log.enabled() is True
    monkeypatch.setenv("JARVIS_CALL_LOG_ENABLED", "0")
    assert call_log.enabled() is False
    tool(jclient, "emr_latest")
    add(jclient)
    assert not log_dir().exists() or not list(log_dir().glob("calls-*.jsonl"))
    assert jclient.get("/api/jarvis/tools/calls", headers=HDR).status_code == 404


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")
def test_files_are_owner_only(jclient):
    tool(jclient, "emr_latest")
    for path in log_dir().iterdir():
        assert stat.S_IMODE(path.stat().st_mode) & 0o077 == 0, path
    assert stat.S_IMODE(log_dir().stat().st_mode) & 0o077 == 0


def test_a_large_request_body_is_replayed_to_the_route_intact(jclient):
    big = "x" * 300_000
    r = tool(jclient, "emr_search_ledger", {"query": "needle", "padding": big})
    assert r.status_code in (200, 422) and entries()[-1]["args_sha256"] == call_log.args_hash({"query": "needle", "padding": big})


# ---------------------------------------------------------------------------------- 11. the failure policy


def test_a_read_fails_open_with_a_degraded_flag_and_a_write_fails_closed_then_recovery_records_the_gap(jclient, monkeypatch):
    real_append = call_log.CallLog._append_locked

    def failing(self, *a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(call_log.CallLog, "_append_locked", failing)
    r = tool(jclient, "emr_latest")  # a read: served anyway
    assert r.status_code == 200 and r.headers.get("x-jarvis-call-log") == "degraded"
    state = call_log.get_call_log().degraded()
    assert state and state["unlogged"] == 1
    before = jclient.get("/api/jarvis/memory?limit=200", headers=HDR).json()["memories"]
    w = jclient.post("/api/jarvis/memory", headers=HDR, json={"content": "must not land", "source_agent": "a", "session_id": "s", "type": "fact"})
    assert w.status_code == 503 and w.json()["reason"] == "CALL_LOG_UNAVAILABLE" and "Retry-After" in w.headers
    assert jclient.get("/api/jarvis/memory?limit=200", headers=HDR).json()["memories"] == before  # nothing was written
    mcp = {**MCP, **HDR}
    out = jclient.post("/mcp", headers=mcp, json={"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "emr_remember", "arguments": {}}}).json()
    assert out["result"]["isError"] is True and out["result"]["structuredContent"]["error"]["reason"] == "CALL_LOG_UNAVAILABLE"

    monkeypatch.setattr(call_log.CallLog, "_append_locked", real_append)  # the disk is back
    ok = add(jclient, "now it lands")
    assert ok["id"]
    got = entries()
    gaps = [e for e in got if e["outcome"] == "gap"]
    assert len(gaps) == 1 and gaps[0]["count"] >= 1 and gaps[0]["window_start"] and call_log.get_call_log().degraded() is None
    assert call_log.get_call_log().verify()["ok"] is True and call_log.get_call_log().verify()["gaps_recorded"] == 1


def test_an_append_that_fails_after_a_write_committed_keeps_the_real_result_and_blocks_further_writes(jclient, monkeypatch):
    monkeypatch.setenv("JARVIS_CALL_LOG_READS", "0")  # this test counts unlogged WRITES; its probe reads are not part of that count
    log = call_log.get_call_log()
    original = call_log.CallLog.append

    def fail_append(self, fields):
        raise call_log.CallLogError("simulated failure after the write")

    monkeypatch.setattr(call_log.CallLog, "append", fail_append)  # preflight still passes: the failure happens after the write
    w = jclient.post("/api/jarvis/memory", headers=HDR, json={"content": "committed", "source_agent": "a", "session_id": "s", "type": "fact"})
    assert w.status_code == 200 and w.headers.get("x-jarvis-call-log") == "degraded"  # the write happened and is reported as it was
    assert any(m["content"] == "committed" for m in jclient.get("/api/jarvis/memory?limit=200", headers=HDR).json()["memories"])
    assert log.degraded() and log.degraded()["unlogged"] == 1
    monkeypatch.setattr(call_log.CallLog, "_append_locked", lambda self, *a, **k: (_ for _ in ()).throw(OSError("still down")))
    assert jclient.post("/api/jarvis/memory", headers=HDR, json={"content": "no", "source_agent": "a", "session_id": "s", "type": "fact"}).status_code == 503
    monkeypatch.undo()
    monkeypatch.setenv("JARVIS_API_KEY", KEY)
    assert call_log.CallLog.append is original


# ------------------------------------------------------------- 12. the relay evidence script reads the real endpoints


def test_the_relay_report_section_reads_the_real_log_and_labels_client_names_self_reported(jclient):
    import importlib.util

    spec = importlib.util.spec_from_file_location("relay_evidence_cl", ROOT / "scripts" / "relay_evidence.py")
    rel = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = rel
    spec.loader.exec_module(rel)

    for client_name in ("devin/1.0", "opencode/0.9", "devin/1.1"):
        tool(jclient, "emr_latest", {}, headers={"X-Jarvis-MCP-Client": client_name})

    def transport(method, url, headers, body):
        path_and_query = url.replace("http://testserver", "")
        r = jclient.request(method, path_and_query, headers={"X-API-Key": headers["X-API-Key"]}, content=body)
        return r.status_code, r.content

    client = rel.ReadOnlyClient("http://testserver", KEY, transport)
    raw = {}
    for label, status_body in (("calls_emr_latest", client.get("/api/jarvis/tools/calls", tool="emr_latest", limit=200)), ("calls_verify", client.get("/api/jarvis/tools/calls/verify"))):
        raw[label] = {"http_status": status_body[0], "body": status_body[1]}
    section = rel.call_log_section(raw, [{"agent": "Devin"}, {"agent": "OpenCode"}, {"agent": "Cursor"}])
    by = {a["agent"]: a for a in section["per_agent"]}
    assert section["available"] and section["chain_ok"] is True and section["head"]["seq"] == 3
    assert by["Devin"]["witnessed_emr_latest_calls"] == 2 and by["Devin"]["latest"]["client_version"] == "1.1"
    assert by["OpenCode"]["witnessed_emr_latest_calls"] == 1 and by["Cursor"]["witnessed_emr_latest_calls"] == 0
    assert all(k["method"] == "GET" for k in client.calls) and KEY not in json.dumps(client.calls)


# ------------------------------------------------- 13. outage scenarios found by the throwaway drill (permission failures)

posix_user_only = pytest.mark.skipif(sys.platform == "win32" or (hasattr(os, "geteuid") and os.geteuid() == 0), reason="needs POSIX permissions enforced on a non-root user")


def _write_body(tag):
    return {"content": f"probe {tag}", "source_agent": "t", "session_id": "s", "type": "decision", "subject": f"probe-{tag}",
            "evidence": [{"kind": "deploy-report", "ref": "drill", "note": "outage scenario"}]}


def _record_count(client):
    return len(client.get("/api/jarvis/memory?limit=200", headers=HDR).json()["memories"])


@pytest.fixture
def lockable_log_dir(jclient):
    """A log with one entry in it, and a way to make the directory (A) or only today's file (B) unwritable, then restore it."""
    tool(jclient, "emr_latest")  # creates the directory, today's file and the lock file
    d = log_dir()
    today = next(d.glob("calls-*.jsonl"))

    def lock_dir():
        for p in d.iterdir():
            os.chmod(p, 0o400)
        os.chmod(d, 0o500)

    def lock_file():
        os.chmod(today, 0o400)

    def unlock():
        os.chmod(d, 0o700)
        for p in d.iterdir():
            os.chmod(p, 0o600)

    yield type("L", (), {"lock_dir": staticmethod(lock_dir), "lock_file": staticmethod(lock_file), "unlock": staticmethod(unlock)})
    unlock()


# ------------------------------------------------------ the page and the head come from one locked snapshot (issue #80)


def _race_appends(monkeypatch, directory: Path):
    """While the page is read, another writer tries to append: once before the entries are read and once after. An unlocked reader lets
    both finish (`join` returns because they are done); a locked snapshot makes them wait (`join` times out, they finish afterwards)."""
    real = call_log.CallLog.entries_desc
    threads: list[threading.Thread] = []

    def racing(self, snapshot=None):
        def writer():
            call_log.CallLog(directory).append(_fields(99))
        t = threading.Thread(target=writer)
        t.start()
        threads.append(t)
        t.join(timeout=0.4)
        items = list(real(self) if snapshot is None else real(self, snapshot))
        t = threading.Thread(target=writer)
        t.start()
        threads.append(t)
        t.join(timeout=0.4)
        yield from items

    monkeypatch.setattr(call_log.CallLog, "entries_desc", racing)

    def finish():
        for t in threads:
            t.join(timeout=5)
        monkeypatch.setattr(call_log.CallLog, "entries_desc", real)
    return finish


def test_an_append_while_the_page_is_read_cannot_split_it_from_its_head(tmp_path, monkeypatch):
    log = call_log.CallLog(tmp_path / "snap")
    for i in range(5):
        log.append(_fields(i))
    finish = _race_appends(monkeypatch, tmp_path / "snap")
    page = log.query_with_head(limit=50)
    finish()
    assert page["entries"][0]["seq"] == page["head"]["seq"] == 5 and page["head"]["entry_hash"] == page["entries"][0]["entry_hash"]
    assert log.head()["seq"] == 7                        # both appends did happen, just after the snapshot


@pytest.mark.skipif(not call_log._pin_by_handle(), reason="Windows reads the page under the lock by design; the Windows test below pins that")
def test_reading_a_page_does_not_hold_the_writers_lock(tmp_path, monkeypatch):
    """A big log is parsed AFTER the lock is released: a writer that arrives during the read finishes at once (it used to wait for the whole scan),
    and its entry is simply not in the page, which is exactly the log as of the snapshot."""
    log = call_log.CallLog(tmp_path / "nolock")
    for i in range(5):
        log.append(_fields(i))
    real = call_log.CallLog.entries_desc
    outcome = {}

    def slow_read(self, snapshot=None):
        t = threading.Thread(target=lambda: call_log.CallLog(tmp_path / "nolock").append(_fields(99)))
        t.start()
        t.join(timeout=3)
        outcome["writer_blocked"] = t.is_alive()
        yield from (real(self) if snapshot is None else real(self, snapshot))

    monkeypatch.setattr(call_log.CallLog, "entries_desc", slow_read)
    page = log.query_with_head(limit=50)
    monkeypatch.setattr(call_log.CallLog, "entries_desc", real)
    assert outcome["writer_blocked"] is False                                   # the lock was free while the page was read
    assert page["entries"][0]["seq"] == page["head"]["seq"] == 5                # and the page still matches its head exactly
    assert log.head()["seq"] == 6


def test_a_snapshot_that_cannot_open_a_file_fails_instead_of_dropping_it(tmp_path, monkeypatch):
    """Descriptor exhaustion (EMFILE) or a permission error must not look like a missing file: the page would lack that file and every later one."""
    log = call_log.CallLog(tmp_path / "emfile")
    for i in range(3):
        log.append(_fields(i))
    real_open = open

    def failing_open(path, *a, **k):
        if str(path).endswith(".jsonl"):
            raise OSError(24, "Too many open files")
        return real_open(path, *a, **k)

    monkeypatch.setattr("builtins.open", failing_open)
    with pytest.raises(call_log.CallLogError, match="could not snapshot"):
        with log._locked():
            log._snapshot()


def test_on_windows_the_page_is_read_under_the_lock_with_no_pinned_handles_and_no_copies(tmp_path, monkeypatch):
    """Windows cannot unlink an open file and copying every retained file could exhaust memory, so there the page is read lazily while the lock is held: a
    writer that arrives during the read waits, nothing is open afterwards, nothing is copied, and the page still matches its head exactly."""
    monkeypatch.setattr(call_log, "_pin_by_handle", lambda: False)
    log = call_log.CallLog(tmp_path / "winlike")
    for i in range(5):
        log.append(_fields(i))
    taken = []
    real_snapshot = call_log.CallLog._snapshot
    monkeypatch.setattr(call_log.CallLog, "_snapshot", lambda self: taken.append(1) or real_snapshot(self))   # must never be called on this path
    real = call_log.CallLog.entries_desc
    state = {}

    def racing(self, snapshot=None):
        t = threading.Thread(target=lambda: call_log.CallLog(tmp_path / "winlike").append(_fields(99)))
        t.start()
        state["writer"] = t
        t.join(timeout=0.4)
        state["writer_waited"] = t.is_alive()                  # the lock is held for the whole page: the writer cannot get in
        yield from (real(self) if snapshot is None else real(self, snapshot))

    monkeypatch.setattr(call_log.CallLog, "entries_desc", racing)
    page = log.query_with_head(limit=50)
    monkeypatch.setattr(call_log.CallLog, "entries_desc", real)
    assert state["writer_waited"] is True and taken == []
    assert page["entries"][0]["seq"] == page["head"]["seq"] == 5
    state["writer"].join(timeout=30)       # the waiting writer gets in once the lock is released (on Windows its lock retry can take a second to notice)
    assert not state["writer"].is_alive()
    assert log.head()["seq"] == 6


def test_close_tolerates_a_snapshot_entry_with_no_handle(tmp_path):
    snap = call_log.Snapshot()
    snap.files.append((tmp_path / "x.jsonl", 0, None))
    snap.close()
    assert snap.files == []


def test_the_calls_endpoint_answers_503_retry_when_the_snapshot_cannot_be_read(jclient, monkeypatch):
    add(jclient)
    monkeypatch.setattr(call_log.CallLog, "query_with_head", lambda self, **k: (_ for _ in ()).throw(call_log.CallLogError("a file of the snapshot could not be read")))
    r = jclient.get("/api/jarvis/tools/calls", headers=HDR)
    assert r.status_code == 503 and "retry" in r.json()["detail"]


def test_the_degraded_state_belongs_to_the_same_snapshot_as_the_page_and_head(tmp_path, monkeypatch):
    """A writer that recovers while the page is being read appends the gap entry and clears the latch. The page and head do not contain that gap, so the
    response must still say `degraded`: reporting a healthy log beside an incomplete page would present incomplete evidence as complete."""
    log = call_log.CallLog(tmp_path / "degsnap")
    for i in range(3):
        log.append(_fields(i))
    log._mark_degraded("simulated outage", unlogged=2)
    assert log.degraded() is not None
    real = call_log.CallLog.entries_desc

    lazy = not call_log._pin_by_handle()     # Windows reads the page under the lock: another worker cannot recover meanwhile, it waits (and must not be run inline, it would wait for us)
    writers = []

    def recovering(self, snapshot=None):
        if lazy:
            t = threading.Thread(target=lambda: call_log.CallLog(tmp_path / "degsnap").append(_fields(9)))
            t.start()
            writers.append(t)
        else:
            call_log.CallLog(tmp_path / "degsnap").append(_fields(9))      # another worker: writes the gap entry first, then its own, and clears the latch
        yield from (real(self) if snapshot is None else real(self, snapshot))

    monkeypatch.setattr(call_log.CallLog, "entries_desc", recovering)
    page = log.query_with_head(limit=50)
    monkeypatch.setattr(call_log.CallLog, "entries_desc", real)
    for t in writers:
        t.join(timeout=30)
        assert not t.is_alive()
    assert page["degraded"] and page["degraded"]["unlogged"] == 2                       # as of the snapshot
    assert [e["outcome"] for e in page["entries"]].count("gap") == 0 and page["head"]["seq"] == 3   # the page and the head predate the recovery
    assert log.degraded() is None                                                       # the latch was cleared meanwhile, and the next read will say so


def _log_with_days(tmp_path, name, days, monkeypatch):
    monkeypatch.setenv("JARVIS_CALL_LOG_RETAIN_DAYS", "400")
    day = [1]
    log = call_log.CallLog(tmp_path / name, clock=lambda: datetime(2026, 1, day[0], 12, tzinfo=timezone.utc))
    for d in range(1, days + 1):
        day[0] = d
        log.append(_fields(d))
    return log, day


@posix_user_only
def test_a_snapshot_pins_only_the_newest_files_and_still_reads_them_all(tmp_path, monkeypatch):
    """The log keeps ~90 daily files: one descriptor per file per request would let a handful of concurrent reads exhaust the process's descriptors."""
    monkeypatch.setenv("JARVIS_CALL_LOG_SNAPSHOT_PINNED", "3")
    log, _ = _log_with_days(tmp_path, "pinned", 12, monkeypatch)
    with log._locked():
        snap = log._snapshot()
    assert len(snap.files) == 12 and sum(1 for _, _, fh in snap.files if fh is not None) == 3
    assert [fh is not None for _, _, fh in snap.files] == [False] * 9 + [True] * 3       # exactly the newest three
    assert [e["seq"] for e in log.entries_desc(snap)] == list(range(12, 0, -1))            # every entry, newest first
    snap.close()


@posix_user_only
def test_an_unpinned_file_that_disappears_makes_the_read_fail_with_retry_never_a_shorter_page(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_CALL_LOG_SNAPSHOT_PINNED", "2")
    log, _ = _log_with_days(tmp_path, "gone", 6, monkeypatch)
    with log._locked():
        snap = log._snapshot()
    oldest = snap.files[0][0]
    newest = snap.files[-1][0]
    oldest.unlink()                                                    # retention removed an old, unpinned file after the snapshot
    newest.unlink()                                                    # (and, for contrast, a pinned one: its handle still reads)
    with pytest.raises(call_log.CallLogError, match="retry"):
        list(log.entries_desc(snap))
    snap.close()


@posix_user_only
def test_a_page_read_holds_at_most_the_pinned_number_of_descriptors(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_CALL_LOG_SNAPSHOT_PINNED", "2")
    log, _ = _log_with_days(tmp_path, "fds", 15, monkeypatch)
    real = call_log.CallLog.entries_desc
    during = {}

    def counting(self, snapshot=None):
        during["fds"] = len(os.listdir("/proc/self/fd"))
        yield from (real(self) if snapshot is None else real(self, snapshot))

    baseline = len(os.listdir("/proc/self/fd"))
    monkeypatch.setattr(call_log.CallLog, "entries_desc", counting)
    log.query_with_head(limit=50)
    monkeypatch.setattr(call_log.CallLog, "entries_desc", real)
    assert during["fds"] - baseline <= 2 + 1                           # the 2 pinned files (+1 of slack for the interpreter), not 15
    assert len(os.listdir("/proc/self/fd")) <= baseline + 1            # and all of them closed afterwards


def test_concurrent_page_reads_are_bounded_and_the_rest_are_told_to_retry(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_CALL_LOG_SNAPSHOT_READERS", "1")
    monkeypatch.setenv("JARVIS_CALL_LOG_SNAPSHOT_WAIT_SECONDS", "0.2")
    log = call_log.CallLog(tmp_path / "slots")
    for i in range(3):
        log.append(_fields(i))
    real = call_log.CallLog.entries_desc
    inside, release = threading.Event(), threading.Event()

    def slow(self, snapshot=None):
        inside.set()
        release.wait(timeout=5)
        yield from (real(self) if snapshot is None else real(self, snapshot))

    monkeypatch.setattr(call_log.CallLog, "entries_desc", slow)
    first = threading.Thread(target=lambda: log.query_with_head(limit=10))
    first.start()
    assert inside.wait(timeout=5)
    waiting = threading.Thread(target=lambda: pytest.raises(call_log.CallLogError, log.query_with_head, limit=10))
    waiting.start()                                                    # a second reader, waiting for the only slot
    time.sleep(0.05)
    started = time.perf_counter()
    assert log.head()["seq"] == 3                                      # the log's own lock is NOT held while a reader waits for a slot: a writer-side call is not blocked
    assert time.perf_counter() - started < 0.1
    waiting.join(timeout=5)
    with pytest.raises(call_log.CallLogError, match="too many"):
        log.query_with_head(limit=10)                                  # one reader is in flight and only one is allowed: this one is told to retry
    release.set()
    first.join(timeout=5)
    monkeypatch.setattr(call_log.CallLog, "entries_desc", real)
    assert [e["seq"] for e in log.query_with_head(limit=10)["entries"]] == [3, 2, 1]     # the slot was released: reads work again


@pytest.mark.parametrize("raw,expected", [("nan", 2.0), ("NaN", 2.0), ("inf", 2.0), ("Infinity", 2.0), ("-inf", 2.0), ("abc", 2.0), ("", 2.0),
                                          ("-5", 0.0), ("0", 0.0), ("0.25", 0.25), ("5", 5.0), ("1e9", 60.0)])
def test_the_snapshot_wait_is_always_a_finite_bounded_number(monkeypatch, raw, expected):
    """acquire(timeout=nan) can block forever and acquire(timeout=inf) raises: a bad setting must fall back, never hang or 500."""
    monkeypatch.setenv("JARVIS_CALL_LOG_SNAPSHOT_WAIT_SECONDS", raw)
    value = call_log.snapshot_wait_seconds()
    assert value == expected and math.isfinite(value)


class CountingFile(__import__("io").BytesIO):
    """A file that counts how many bytes were read from it."""
    read_bytes = 0

    def read(self, n=-1):
        data = super().read(n)
        self.read_bytes += len(data)
        return data


@pytest.mark.parametrize("chunk", [1, 2, 3, 7, 16, 64, 4096])
@pytest.mark.parametrize("content", [b"", b"\n", b"a", b"a\n", b"a\nb", b"a\nb\n", b"\n\na\n\n", b"line-one\nline-two\nline-three\n", b"x" * 100 + b"\n" + b"y" * 3 + b"\nz",
                                     b"no newline at all", b"\r\nwith cr\r\n"])
def test_reverse_lines_is_exactly_the_lines_backwards_at_any_block_size(content, chunk):
    expected = list(reversed(content.split(b"\n"))) if content else []
    if content.endswith(b"\n"):
        expected = expected[1:]                                      # the empty text after a final newline is not a line
    got = list(call_log.reverse_lines(__import__("io").BytesIO(content), len(content), chunk))
    assert [g for g in got if g] == [e for e in expected if e]       # (empty lines are skipped by the reader anyway: compare the non-empty ones, in order)
    assert b"".join(sorted(got)) == b"".join(sorted(e for e in expected if e))   # and nothing is lost or duplicated


def test_reverse_lines_honours_the_size_and_ignores_what_came_after_it():
    data = b"one\ntwo\nthree\nfour\n"
    assert list(call_log.reverse_lines(__import__("io").BytesIO(data), len(b"one\ntwo\nthree\n"), 4)) == [b"three", b"two", b"one"]


def test_a_page_of_one_entry_reads_one_block_not_the_whole_file():
    """The newest daily file is unbounded: a limit=1 read, repeated by several concurrent readers, must not each hold a full decoded copy of it."""
    lines = [json.dumps({"seq": i, "pad": "p" * 80}).encode() for i in range(1, 5001)]
    data = b"\n".join(lines) + b"\n"                                  # ~450 KB
    fh = CountingFile(data)
    first = next(call_log.reverse_lines(fh, len(data), 8192))
    assert json.loads(first)["seq"] == 5000
    assert fh.read_bytes <= 2 * 8192 < len(data) / 20                  # one block (two at most), not 450 KB
    fh2 = CountingFile(data)
    taken = [json.loads(x)["seq"] for _, x in zip(range(300), call_log.reverse_lines(fh2, len(data), 8192))]
    assert taken == list(range(5000, 4700, -1)) and fh2.read_bytes < len(data) / 4


def test_entries_desc_stops_reading_the_file_once_the_page_is_full(tmp_path, monkeypatch):
    log = call_log.CallLog(tmp_path / "bigfile")
    for i in range(400):
        log.append(_fields(i))
    size = next(log._files()[0].parent.glob("calls-*.jsonl")).stat().st_size
    seen = []
    real = call_log.reverse_lines

    class Spy:
        def __init__(self, fh):
            self.fh = fh
        def seek(self, n):
            return self.fh.seek(n)
        def read(self, n=-1):
            data = self.fh.read(n)
            seen.append(len(data))
            return data

    monkeypatch.setattr(call_log, "reverse_lines", lambda fh, sz, chunk=None: real(Spy(fh), sz, 2048))
    page = log.query(limit=1)
    assert [e["seq"] for e in page["entries"]] == [400] and sum(seen) < size / 4


def test_a_snapshot_page_ignores_what_was_appended_after_it(tmp_path):
    log = call_log.CallLog(tmp_path / "snapbytes")
    for i in range(3):
        log.append(_fields(i))
    with log._locked():
        snap = log._snapshot()
    for i in range(4):
        log.append(_fields(10 + i))
    assert [e["seq"] for e in log.entries_desc(snap)] == [3, 2, 1]
    assert [e["seq"] for e in log.entries_desc()][0] == 7
    snap.close()


def test_a_snapshot_still_reads_a_file_that_retention_deleted_after_it_was_taken(tmp_path, monkeypatch):
    """The page must not be missing a file the snapshot advertised: an append that rotates the log can unlink an older file while a page is being read."""
    monkeypatch.setenv("JARVIS_CALL_LOG_RETAIN_DAYS", "2")
    day = [1]
    log = call_log.CallLog(tmp_path / "ret-snap", clock=lambda: datetime(2026, 10, day[0], 12, tzinfo=timezone.utc))
    log.append(_fields(0))
    day[0] = 2
    log.append(_fields(1))
    day[0] = 8
    log.append(_fields(2))
    with log._locked():
        snap = log._snapshot()
    assert len(snap.files) == 3
    day[0] = 9
    log.append(_fields(3))                                                  # rotates, and retention unlinks the day-1 and day-2 files
    assert [p.name for p in log._files()] == ["calls-20261008.jsonl", "calls-20261009.jsonl"]
    assert [e["seq"] for e in log.entries_desc(snap)] == [3, 2, 1]          # the snapshot still has all of its files, in full (seq 3 is day 8)
    snap.close()
    assert snap.files == []


def test_the_old_two_step_read_would_have_split_the_page_from_its_head(tmp_path, monkeypatch):
    """The same race against query() then head(): proves the test above can fail (the head runs ahead of the page)."""
    log = call_log.CallLog(tmp_path / "split")
    for i in range(5):
        log.append(_fields(i))
    finish = _race_appends(monkeypatch, tmp_path / "split")
    page = log.query()
    head = log.head()
    finish()
    assert head["seq"] > page["entries"][0]["seq"]


def test_the_calls_endpoint_is_not_split_by_an_append_either(jclient, monkeypatch):
    add(jclient)
    tool(jclient, "emr_latest", {})
    finish = _race_appends(monkeypatch, log_dir())
    body = jclient.get("/api/jarvis/tools/calls?limit=200", headers=HDR).json()
    finish()
    assert body["entries"][0]["seq"] == body["head"]["seq"] == 2


def test_under_concurrent_writers_the_head_is_always_the_first_entry_of_an_unfiltered_page(tmp_path):
    log = call_log.CallLog(tmp_path / "stress")
    stop = threading.Event()

    def write():
        w = call_log.CallLog(tmp_path / "stress")
        i = 0
        while not stop.is_set():
            w.append(_fields(i))
            i += 1

    threads = [threading.Thread(target=write) for _ in range(3)]
    for t in threads:
        t.start()
    try:
        bad = []
        for _ in range(60):
            page = log.query_with_head(limit=200)
            if page["entries"] and (page["entries"][0]["seq"] != page["head"]["seq"] or page["entries"][0]["entry_hash"] != page["head"]["entry_hash"]):
                bad.append((page["entries"][0]["seq"], page["head"]["seq"]))
    finally:
        stop.set()
        for t in threads:
            t.join(timeout=10)
    assert not bad, bad
    assert log.verify()["ok"] is True


def test_the_calls_endpoint_returns_the_head_with_the_page_and_keeps_its_shape(jclient):
    add(jclient)
    tool(jclient, "emr_latest", {})
    body = jclient.get("/api/jarvis/tools/calls?limit=200", headers=HDR).json()
    assert body["entries"][0]["seq"] == body["head"]["seq"] == len(body["entries"])
    assert set(body["head"]) == {"seq", "entry_hash", "file", "ts"} and body["client_names"] == "self-reported" and body["provenance"] == "server-witnessed"
    older = jclient.get(f"/api/jarvis/tools/calls?limit=1&cursor={body['head']['seq']}", headers=HDR).json()
    assert older["entries"][0]["seq"] == body["head"]["seq"] - 1 and older["head"]["seq"] == body["head"]["seq"]   # a later page: the head is still the log's head


def test_the_empty_log_has_the_genesis_head(tmp_path):
    page = call_log.CallLog(tmp_path / "none").query_with_head(limit=10)
    assert page["entries"] == [] and page["head"]["seq"] == 0 and page["head"]["entry_hash"] == call_log.GENESIS


def _read_fields(i=0):
    f = call_log.make_fields(transport="http-api", tool="GET /api/jarvis/memory", args={"i": i}, outcome="ok", started=time.perf_counter(), route="/api/jarvis/memory", method="GET")
    return f


def _denied_fields(i=0):
    return call_log.make_fields(transport="http-api", tool="GET /api/jarvis/memory", args={"i": i}, outcome="denied", started=time.perf_counter(), status_code=401, route="/api/jarvis/memory", method="GET")


@pytest.mark.parametrize("kind,make,summary_outcome", [("reads", _read_fields, "read_suppressed"), ("denied", _denied_fields, "denied_suppressed")])
def test_a_summary_whose_append_failed_keeps_its_count_and_is_written_later(fixed_minute, tmp_path, monkeypatch, kind, make, summary_outcome):
    monkeypatch.setenv("JARVIS_CALL_LOG_READ_CAP_PER_MIN", "1")
    monkeypatch.setenv("JARVIS_CALL_LOG_DENIED_CAP_PER_MIN", "1")
    log = call_log.CallLog(tmp_path / "keep")
    for i in range(5):
        log.record(make(i))                                              # one entry, four suppressed
    real = call_log.CallLog._append_locked
    monkeypatch.setattr(call_log.CallLog, "_append_locked", lambda self, *a, **k: (_ for _ in ()).throw(OSError("disk full")))
    log.flush()                                                          # the volume is full: nothing can be written (flush swallows it)
    log.flush()
    monkeypatch.setattr(call_log.CallLog, "_append_locked", real)
    log.flush()                                                          # the volume is back: the very same count is written now
    summary = [e for e in log.query(limit=50)["entries"] if e["outcome"] == summary_outcome]
    assert len(summary) == 1 and summary[0]["count"] == 4
    assert log.verify()["ok"] is True


def test_a_shutdown_flush_that_fails_leaves_the_counts_as_unlogged_for_the_next_process(fixed_minute, tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_CALL_LOG_READ_CAP_PER_MIN", "1")
    monkeypatch.setenv("JARVIS_CALL_LOG_DENIED_CAP_PER_MIN", "1")
    d = tmp_path / "shut"
    log = call_log.CallLog(d)
    for i in range(4):
        log.record(_read_fields(i))                                      # 1 written, 3 suppressed
    for i in range(3):
        log.record(_denied_fields(i))                                    # 1 written, 2 suppressed
    real = call_log.CallLog._append_locked
    monkeypatch.setattr(call_log.CallLog, "_append_locked", lambda self, *a, **k: (_ for _ in ()).throw(OSError("disk full")))
    log.flush_for_shutdown()                                             # the volume is full as the app exits
    monkeypatch.setattr(call_log.CallLog, "_append_locked", real)
    assert log.degraded()["unlogged"] == 5 and "shutdown" in log.degraded()["last_error"]
    nxt = call_log.CallLog(d)                                            # the next process
    nxt.record(_read_fields(99))
    gaps = [e for e in nxt.query(limit=50)["entries"] if e["outcome"] == "gap"]
    assert len(gaps) == 1 and gaps[0]["count"] == 5 and nxt.verify()["ok"] is True


def test_a_failed_summary_flush_shows_up_as_degraded_in_verify_and_query_and_is_written_later(fixed_minute, tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_CALL_LOG_READ_CAP_PER_MIN", "1")
    log = call_log.CallLog(tmp_path / "fl")
    for i in range(4):
        log.record(_read_fields(i))                                      # 3 suppressed
    real = call_log.CallLog._append_locked
    monkeypatch.setattr(call_log.CallLog, "_append_locked", lambda self, *a, **k: (_ for _ in ()).throw(OSError("disk full")))
    v = log.verify()                                                     # verify flushes first, and the flush fails
    assert v["degraded"] and "summaries" in v["degraded"]["last_error"] and v["degraded"]["unlogged"] == 0
    monkeypatch.setattr(call_log.CallLog, "_append_locked", real)
    page = log.query(limit=50)                                           # the volume is back: the same count is written now
    summary = [e for e in page["entries"] if e["outcome"] == "read_suppressed"]
    assert len(summary) == 1 and summary[0]["count"] == 3
    log.record(_fields(1))                                               # the next ordinary call writes the recovery gap entry and clears the latch
    gaps = [e for e in log.query(limit=50)["entries"] if e["outcome"] == "gap"]
    assert len(gaps) == 1 and gaps[0]["count"] == 0 and log.degraded() is None and log.verify()["ok"] is True


def test_a_shutdown_with_an_unwritable_log_volume_leaves_the_counts_in_the_app_log(fixed_minute, tmp_path, monkeypatch, caplog):
    """When even the DEGRADED file cannot be written, nothing on the volume can hold the count: the only trace left is an ERROR in the app's own log."""
    monkeypatch.setenv("JARVIS_CALL_LOG_READ_CAP_PER_MIN", "1")
    log = call_log.CallLog(tmp_path / "gone")
    for i in range(4):
        log.record(_read_fields(i))                                      # 3 suppressed
    monkeypatch.setattr(call_log.CallLog, "_append_locked", lambda self, *a, **k: (_ for _ in ()).throw(OSError("read-only volume")))
    monkeypatch.setattr(call_log.CallLog, "_locked", lambda self: (_ for _ in ()).throw(OSError("read-only volume")))
    with caplog.at_level("ERROR", logger="jarvis.call_log"):
        log.flush_for_shutdown()
    assert any("3 calls" in r.getMessage() and "only trace" in r.getMessage() for r in caplog.records)


def test_open_summaries_are_written_when_the_app_shuts_down(fixed_minute, monkeypatch):
    monkeypatch.setenv("JARVIS_API_KEY", KEY)
    monkeypatch.delenv("JARVIS_ALLOW_UNAUTHENTICATED", raising=False)
    monkeypatch.setenv("JARVIS_MEMORY_WRITE_ENABLED", "true")
    monkeypatch.setenv("JARVIS_CALL_LOG_READ_CAP_PER_MIN", "2")
    with TestClient(app, raise_server_exceptions=False) as c:
        for _ in range(7):
            assert c.get("/api/jarvis/memory", headers=HDR).status_code == 200
        assert [e["outcome"] for e in entries()] == ["ok", "ok"]          # the other five exist only in memory until something closes them
    got = entries()                                                       # leaving the client ran the app's shutdown
    assert [e["outcome"] for e in got] == ["ok", "ok", "read_suppressed"] and got[2]["count"] == 5


def test_a_repeated_query_parameter_changes_the_witness_hash(jclient):
    add(jclient)
    before = len(entries())
    jclient.get("/api/jarvis/memory/active?trajectory=a&trajectory=b", headers=HDR)
    jclient.get("/api/jarvis/memory/active?trajectory=b", headers=HDR)
    jclient.get("/api/jarvis/memory/active?trajectory=b&trajectory=a", headers=HDR)
    two, one, swapped = entries()[before:]
    assert len({two["args_sha256"], one["args_sha256"], swapped["args_sha256"]}) == 3                  # values and their order are part of the fingerprint
    assert two["args_sha256"] == call_log.args_hash({"trajectory": ["a", "b"]})
    assert one["args_sha256"] == call_log.args_hash({"trajectory": "b"})                               # a single value hashes exactly as before


@pytest.mark.parametrize("method,path", [("GET", "/api/jarvis/memory/bearer-secret-123"), ("GET", "/api/jarvis/memory/amul/artifacts/token-xyz-987"),
                                          ("PATCH", "/api/jarvis/memory/bearer-secret-123"), ("DELETE", "/api/jarvis/memory/bearer-secret-123"),
                                          ("GET", "/api/jarvis/memory/watch"), ("GET", "/api/jarvis/memory/amul/artifacts/latest"), ("GET", "/api/jarvis/memory/serve")])
def test_a_path_parameter_that_is_not_a_record_id_never_reaches_the_log(jclient, method, path):
    before = len(entries())
    jclient.request(method, path, headers=HDR, json={"subject": "x", "expected_version": 1} if method == "PATCH" else None)
    (e,) = entries()[before:]
    secret = path.rsplit("/", 1)[1]
    assert secret not in json.dumps(e) and "{id}" in e["route"] and e["route"] == e["tool"].split(" ", 1)[1] and e["target"] is None
    assert secret not in "".join(p.read_text() for p in log_dir().glob("calls-*.jsonl"))


def test_a_literal_from_one_route_is_not_kept_in_another_routes_parameter_slot(jclient):
    """`watch` is a literal of /external/watch only; as a path parameter of /memory/{id} it must be redacted. Position decides, not the word."""
    before = len(entries())
    jclient.get("/api/jarvis/memory/watch", headers=HDR)
    jclient.get("/api/jarvis/memory/external/stats", headers=HDR)           # a real GET route with two literals
    jclient.get("/api/jarvis/memory/not/a/route/at/all-secret-77", headers=HDR)
    got = entries()[before:]
    assert [e["route"] for e in got] == ["/api/jarvis/memory/{id}", "/api/jarvis/memory/external/stats", "/api/jarvis/memory/{other}"]
    assert "secret-77" not in json.dumps(got)


def _tenant_read(tenant, transport, i=0):
    f = call_log.make_fields(transport=transport, tool="GET /api/jarvis/memory", args={"i": i}, outcome="ok", started=time.perf_counter(), route="/api/jarvis/memory", method="GET", tenant=tenant)
    return f


def test_read_summaries_keep_their_tenant_and_transport_and_each_tenant_has_its_own_cap(fixed_minute, tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_CALL_LOG_READ_CAP_PER_MIN", "2")
    log = call_log.CallLog(tmp_path / "tenants")
    for i in range(6):
        log.record(_tenant_read("operator", "http-api", i))                  # a busy caller: 2 written, 4 suppressed
    for i in range(3):
        log.record(_tenant_read("tenant-b", "mcp-stdio", i))                 # another tenant: its own cap, so 2 written, 1 suppressed
    log.record(_tenant_read("tenant-c", "http-api"))                         # and a quiet one is never summarised
    got = log.query(limit=50)["entries"][::-1]
    written = [(e["tenant"], e["transport"]) for e in got if e["outcome"] == "ok"]
    assert written.count(("operator", "http-api")) == 2 and written.count(("tenant-b", "mcp-stdio")) == 2 and written.count(("tenant-c", "http-api")) == 1
    summaries = {(e["tenant"], e["transport"]): e["count"] for e in log.query(limit=50)["entries"] if e["outcome"] == "read_suppressed"}
    assert summaries == {("operator", "http-api"): 4, ("tenant-b", "mcp-stdio"): 1}                     # attributed to who made the calls, not to the operator
    assert log.verify()["ok"] is True


def test_reads_of_different_records_or_artifacts_have_different_witness_hashes(jclient):
    a, b = add(jclient, "first record"), add(jclient, "second record")
    before = len(entries())
    for path in (f"/api/jarvis/memory/{a['id']}", f"/api/jarvis/memory/{b['id']}", "/api/jarvis/memory/amul/artifacts/art-0a1b2c3d4e5f-s1", "/api/jarvis/memory/amul/artifacts/art-3d4e5f6a7b8c-e12",
                 "/api/jarvis/memory/bearer-secret-123", "/api/jarvis/memory/bearer-secret-456"):
        jclient.get(path, headers=HDR)
    got = entries()[before:]
    assert len({e["args_sha256"] for e in got}) == 6                                                   # every path argument is in the fingerprint
    assert [e["target"] for e in got] == [a["id"], b["id"], "art-0a1b2c3d4e5f-s1", "art-3d4e5f6a7b8c-e12", None, None]  # artifact ids are recognised as ids; typed text is not
    assert "bearer-secret" not in json.dumps(got)
    assert got[0]["args_sha256"] == call_log.args_hash({"args": {}, "path": [a["id"]]})                 # hashed, never stored


def test_only_the_generated_artifact_id_grammar_is_recorded_as_a_target(jclient):
    before = len(entries())
    for path in ("/api/jarvis/memory/amul/artifacts/art-0a1b2c3d4e5f-s1", "/api/jarvis/memory/amul/artifacts/art-deadbeef-bearersecret",
                 "/api/jarvis/memory/amul/artifacts/art-0a1b2c3d4e5f-bearersecret", "/api/jarvis/memory/amul/artifacts/art-0a1b2c3d4e5fabcdef-s1",
                 "/api/jarvis/memory/amul/artifacts/art-0a1b2c3d4e5f-Z123", "/api/jarvis/memory/amul/artifacts/art-0a1b2c3d4e5f-r1"):
        jclient.get(path, headers=HDR)
    got = entries()[before:]
    assert [e["target"] for e in got] == ["art-0a1b2c3d4e5f-s1", None, None, None, None, None]
    assert "bearersecret" not in json.dumps(got) and "abcdef-s1" not in json.dumps(got) and "Z123" not in json.dumps(got)


def test_failed_reads_are_never_swallowed_by_the_read_cap(fixed_minute, jclient, monkeypatch):
    """After the cap, successful reads are summarised, but a failed one (404) keeps its route, status and error code."""
    monkeypatch.setenv("JARVIS_CALL_LOG_READ_CAP_PER_MIN", "2")
    for _ in range(5):
        jclient.get("/api/jarvis/memory", headers=HDR)
    r = jclient.get("/api/jarvis/memory/mem-000000000000", headers=HDR)
    assert r.status_code == 404
    jclient.get("/api/jarvis/tools/calls?limit=1", headers=HDR)                        # closes the open summary
    got = entries()
    failed = [e for e in got if e["outcome"] == "error"]
    assert len(failed) == 1 and failed[0]["status_code"] == 404 and failed[0]["route"] == "/api/jarvis/memory/{id}" and failed[0]["error_code"]
    assert [e["count"] for e in got if e["outcome"] == "read_suppressed"] == [3]


def _drive(app_calls, method, path, chunks, headers=(), response=(b"{}",), info=None):
    """Run the real witness middleware over a dummy app and a scripted body. The dummy app reads the whole body (as a real app would) and
    records it in `info`. Returns (status, receive calls the middleware made, response headers)."""
    import asyncio
    from app.call_witness import CallWitnessMiddleware

    info = {} if info is None else info
    received = []
    sent = []

    async def receive():
        received.append(1)
        if not chunks:
            return {"type": "http.request", "body": b"", "more_body": False}
        c = chunks.pop(0)
        return {"type": "http.request", "body": c, "more_body": bool(chunks)}

    async def send(m):
        sent.append(m)

    async def dummy(scope, rcv, snd):
        app_calls.append(1)
        info["receives_before_app_read"] = len(received)
        seen = bytearray()
        if method not in ("GET", "HEAD", "OPTIONS"):
            while True:
                m = await rcv()
                seen += m.get("body", b"")
                if not m.get("more_body"):
                    break
        info["app_saw"] = bytes(seen)
        await snd({"type": "http.response.start", "status": 200, "headers": []})
        for i, piece in enumerate(response):
            await snd({"type": "http.response.body", "body": piece, "more_body": i < len(response) - 1})

    scope = {"type": "http", "method": method, "path": path, "query_string": b"", "headers": [(k.encode(), v.encode()) for k, v in headers]}
    asyncio.run(CallWitnessMiddleware(dummy)(scope, receive, send))
    start = next(m for m in sent if m["type"] == "http.response.start")
    return start["status"], len(received), {k.decode(): v.decode() for k, v in start["headers"]}


def test_a_read_is_witnessed_without_its_body_ever_being_buffered(jclient):
    app_calls = []
    big = [b"x" * 65536 for _ in range(40)]                                  # 2.5 MiB of body on a GET
    status, receives, _ = _drive(app_calls, "GET", "/api/jarvis/memory", big)
    assert status == 200 and app_calls == [1] and receives == 0              # the middleware never read it; the app got the request untouched
    assert [e["tool"] for e in entries()] == ["GET /api/jarvis/memory"]


def test_a_response_too_big_to_buffer_is_witnessed_before_its_headers_are_released(jclient):
    big = [b"y" * (1024 * 1024) for _ in range(3)]                           # 3 MiB, past the 2 MiB buffer
    status, _, hdrs = _drive([], "GET", "/api/jarvis/memory", [], response=big)
    (e,) = entries()
    assert status == 200 and hdrs["x-jarvis-call-seq"] == str(e["seq"]) and e["outcome"] == "ok" and e["result_digest"] is None


def test_one_huge_response_chunk_is_never_copied_into_the_witness_buffer(jclient, monkeypatch):
    """The size is checked before a chunk is copied: a 3 MiB single-chunk body must add nothing to the buffer (and a small one still does)."""
    from app import call_witness
    copied = []

    class Spy(bytearray):
        def extend(self, data):
            copied.append(len(data))
            super().extend(data)

    monkeypatch.setattr(call_witness, "bytearray", Spy, raising=False)
    _drive([], "GET", "/api/jarvis/memory", [], response=[b"z" * (3 * 1024 * 1024)])
    assert copied == []
    _drive([], "GET", "/api/jarvis/memory", [], response=[b"{}"])
    assert copied == [2]


def test_only_the_exact_generated_id_grammars_are_recorded_as_a_target(jclient):
    rec = add(jclient)
    before = len(entries())
    for path in ("/api/jarvis/memory/123456789", "/api/jarvis/memory/mem-" + "a" * 64, "/api/jarvis/memory/mem-abc123", "/api/jarvis/memory/mem-0123456789abcdef",
                 f"/api/jarvis/memory/{rec['id']}"):
        jclient.get(path, headers=HDR)
    got = entries()[before:]
    assert [e["target"] for e in got] == [None, None, None, None, rec["id"]]
    assert "123456789" not in json.dumps(got[:4]) and "a" * 64 not in json.dumps(got) and "0123456789abcdef" not in json.dumps(got)


def test_a_big_response_whose_witness_failed_says_degraded_in_its_headers(jclient, monkeypatch):
    monkeypatch.setattr(call_log.CallLog, "_append_locked", lambda self, *a, **k: (_ for _ in ()).throw(OSError("disk full")))
    big = [b"y" * (1024 * 1024) for _ in range(3)]
    status, _, hdrs = _drive([], "GET", "/api/jarvis/memory", [], response=big)
    assert status == 200 and hdrs.get("x-jarvis-call-log") == "degraded"


def test_a_body_over_the_limit_is_not_refused_it_streams_through_to_the_app_and_is_hashed(jclient, monkeypatch):
    """No smaller API contract: a big body (say a bulk document import) reaches the app complete; the witness buffers only up to its bound
    and records the sha256 and size of what the app read."""
    import hashlib
    monkeypatch.setenv("JARVIS_CALL_LOG_MAX_BODY_BYTES", "2048")
    body = [bytes([65 + i % 26]) * 1000 for i in range(50)]                    # 50 KB in 1 KB chunks
    whole = b"".join(body)
    info = {}
    status, _, _ = _drive([], "POST", "/api/jarvis/memory", list(body), headers=[("content-length", str(len(whole)))], info=info)
    assert status == 200 and info["app_saw"] == whole                          # the app got every byte, in order
    assert info["receives_before_app_read"] <= 4                               # the witness read only a little past its bound before handing over
    (e,) = entries()
    assert e["outcome"] == "ok" and e["args_sha256"] == call_log.args_hash({"_streamed_body_sha256": hashlib.sha256(whole).hexdigest(), "_streamed_body_bytes": len(whole)})


def test_one_giant_final_message_is_streamed_not_retained_either(jclient, monkeypatch):
    """A server may deliver the whole body in one last http.request: it is over the bound, so it takes the streaming path (hashed, not retained)."""
    import hashlib
    monkeypatch.setenv("JARVIS_CALL_LOG_MAX_BODY_BYTES", "2048")
    giant = b"g" * (3 * 1024 * 1024)
    info = {}
    status, receives, _ = _drive([], "POST", "/api/jarvis/memory", [giant], info=info)
    assert status == 200 and info["app_saw"] == giant and receives == 1
    (e,) = entries()
    assert e["args_sha256"] == call_log.args_hash({"_streamed_body_sha256": hashlib.sha256(giant).hexdigest(), "_streamed_body_bytes": len(giant)})


def test_a_failure_while_a_big_response_is_still_streaming_is_recorded(jclient):
    import asyncio
    from app.call_witness import CallWitnessMiddleware

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(m):
        pass

    async def breaks_midway(scope, rcv, snd):
        await snd({"type": "http.response.start", "status": 200, "headers": []})
        await snd({"type": "http.response.body", "body": b"y" * (3 * 1024 * 1024), "more_body": True})   # past the buffer: witnessed now, as 200 ok
        raise RuntimeError("the database went away")

    scope = {"type": "http", "method": "GET", "path": "/api/jarvis/memory", "query_string": b"", "headers": []}
    with pytest.raises(RuntimeError):
        asyncio.run(CallWitnessMiddleware(breaks_midway)(scope, receive, send))
    got = entries()
    assert [(e["outcome"], e["error_code"]) for e in got] == [("ok", None), ("error", "STREAMING_FAILED")]


def test_the_streamed_hash_covers_only_what_the_app_read(jclient, monkeypatch):
    """An app that answers without reading the rest (an early auth failure, say) leaves a hash of what it did read."""
    import hashlib
    monkeypatch.setenv("JARVIS_CALL_LOG_MAX_BODY_BYTES", "2048")
    from app.call_witness import CallWitnessMiddleware
    import asyncio
    chunks = [b"x" * 1000 for _ in range(20)]
    sent = []

    async def receive():
        c = chunks.pop(0)
        return {"type": "http.request", "body": c, "more_body": bool(chunks)}

    async def send(m):
        sent.append(m)

    async def impatient(scope, rcv, snd):
        await snd({"type": "http.response.start", "status": 401, "headers": []})
        await snd({"type": "http.response.body", "body": b"{}"})

    scope = {"type": "http", "method": "POST", "path": "/api/jarvis/memory", "query_string": b"", "headers": []}
    asyncio.run(CallWitnessMiddleware(impatient)(scope, receive, send))
    (e,) = entries()
    assert e["outcome"] == "denied" and e["status_code"] == 401
    assert e["args_sha256"] == call_log.args_hash({"_streamed_body_sha256": hashlib.sha256(b"x" * 3000).hexdigest(), "_streamed_body_bytes": 3000})  # the 3 chunks the witness had read


def test_a_body_under_the_limit_is_still_replayed_to_the_app(jclient, monkeypatch):
    monkeypatch.setenv("JARVIS_CALL_LOG_MAX_BODY_BYTES", "2048")
    rec = add(jclient, "a record well under the limit")
    assert rec["content"] == "a record well under the limit"
    assert [e["tool"] for e in entries()] == ["POST /api/jarvis/memory"] and entries()[0]["outcome"] == "ok"


def test_a_route_with_no_path_parameter_hashes_exactly_as_before(jclient):
    add(jclient)
    before = len(entries())
    jclient.get("/api/jarvis/memory/latest?limit=2", headers=HDR)
    (e,) = entries()[before:]
    assert e["args_sha256"] == call_log.args_hash({"limit": "2"})


def test_the_route_template_depends_on_the_method_not_only_the_path(jclient):
    """`promote`, `pipeline` and `external-search` are POST-only: a GET of the same path is served as a record lookup and must be templated as one."""
    rec = add(jclient)
    before = len(entries())
    for path in ("/api/jarvis/memory/promote", "/api/jarvis/memory/pipeline", "/api/jarvis/memory/external-search"):
        jclient.get(path, headers=HDR)                                      # 404 record lookups
    jclient.post("/api/jarvis/memory/promote", headers=HDR, json={})
    jclient.post(f"/api/jarvis/memory/{rec['id']}", headers=HDR, json={})  # not a route at all for POST
    got = entries()[before:]
    assert [e["route"] for e in got[:3]] == ["/api/jarvis/memory/{id}"] * 3 and all(e["target"] is None and e["method"] == "GET" for e in got[:3])
    assert got[3]["route"] == "/api/jarvis/memory/promote" and got[3]["method"] == "POST"
    assert got[4]["route"] == "/api/jarvis/memory/{other}"
    assert not any(w in json.dumps(got[:3]) for w in ("promote", "pipeline", "external-search"))


def test_mixed_traffic_does_not_write_a_summary_per_read(fixed_minute, jclient, monkeypatch):
    """Alternating reads with a read-only tool call must not close the summary every time, or the cap bounds nothing."""
    monkeypatch.setenv("JARVIS_CALL_LOG_READ_CAP_PER_MIN", "2")
    for _ in range(15):
        jclient.get("/api/jarvis/memory", headers=HDR)
        tool(jclient, "emr_latest", {})
    jclient.get("/api/jarvis/tools/calls?limit=1", headers=HDR)             # reading the log closes the open summary
    got = entries()
    assert [e["outcome"] for e in got].count("read_suppressed") == 1
    assert sum(1 for e in got if e["method"] == "GET" and e["outcome"] == "ok") == 2 and len(got) == 2 + 15 + 1
    assert [e for e in got if e["outcome"] == "read_suppressed"][0]["count"] == 13


def test_real_record_ids_and_known_routes_keep_their_templates(jclient):
    rec = add(jclient)
    before = len(entries())
    for path in (f"/api/jarvis/memory/{rec['id']}", f"/api/jarvis/memory/{rec['id']}/history", "/api/jarvis/memory/latest", "/api/jarvis/memory/amul/field/status"):
        jclient.get(path, headers=HDR)
    got = entries()[before:]
    assert [e["route"] for e in got] == ["/api/jarvis/memory/{id}", "/api/jarvis/memory/{id}/history", "/api/jarvis/memory/latest", "/api/jarvis/memory/amul/field/status"]
    assert [e["target"] for e in got] == [rec["id"], rec["id"], None, None]


@posix_user_only
def test_reads_served_during_an_outage_are_counted_in_the_gap_entry(jclient, lockable_log_dir):
    lockable_log_dir.lock_dir()
    for _ in range(3):
        r = jclient.get("/api/jarvis/memory", headers=HDR)
        assert r.status_code == 200 and r.headers.get("x-jarvis-call-log") == "degraded"      # fail open: the read is served
    lockable_log_dir.unlock()
    add(jclient)
    gaps = [e for e in entries() if e["outcome"] == "gap"]
    assert len(gaps) == 1 and gaps[0]["count"] == 3


@posix_user_only
def test_scenario_a_unwritable_directory_still_leaves_a_gap_entry_after_recovery(jclient, lockable_log_dir, monkeypatch):
    """The DEGRADED file lives in the directory that cannot be written, so the outage must also be remembered in memory."""
    monkeypatch.setenv("JARVIS_CALL_LOG_READS", "0")  # the probe reads are not what this counts (see the reads-during-an-outage test)
    n0 = _record_count(jclient)
    lockable_log_dir.lock_dir()
    r = tool(jclient, "emr_latest")  # a read: served anyway, but it cannot be logged
    assert r.status_code == 200 and r.headers.get("x-jarvis-call-log") == "degraded"
    w = jclient.post("/api/jarvis/memory", headers=HDR, json=_write_body("a1"))
    assert w.status_code == 503 and w.json()["reason"] == "CALL_LOG_UNAVAILABLE" and _record_count(jclient) == n0
    lockable_log_dir.unlock()
    assert jclient.post("/api/jarvis/memory", headers=HDR, json=_write_body("a2")).status_code == 200
    gaps = [e for e in entries() if e["outcome"] == "gap"]
    assert len(gaps) == 1, "the calls made during the outage left no trace in the log"
    assert gaps[0]["count"] == 1 and gaps[0]["refused"] == 1 and gaps[0]["window_start"] and "REFUSED_WRITES:1" in gaps[0]["error_code"]
    assert call_log.get_call_log().verify()["ok"] is True and call_log.get_call_log().degraded() is None


@posix_user_only
def test_scenario_b_an_unwritable_log_file_refuses_the_first_write_instead_of_committing_it_unlogged(jclient, lockable_log_dir, monkeypatch):
    """The directory looks writable, but today's file is not: the preflight must notice, so the write is refused, not committed."""
    monkeypatch.setenv("JARVIS_CALL_LOG_READS", "0")
    n0 = _record_count(jclient)
    lockable_log_dir.lock_file()
    w = jclient.post("/api/jarvis/memory", headers=HDR, json=_write_body("b1"))
    assert w.status_code == 503, "the first write committed without being logged"
    assert w.json()["reason"] == "CALL_LOG_UNAVAILABLE" and _record_count(jclient) == n0
    lockable_log_dir.unlock()
    assert jclient.post("/api/jarvis/memory", headers=HDR, json=_write_body("b2")).status_code == 200
    gaps = [e for e in entries() if e["outcome"] == "gap"]
    assert len(gaps) == 1 and gaps[0]["count"] == 0 and gaps[0]["refused"] == 1  # an outage with nothing served unlogged is still recorded
    assert call_log.get_call_log().verify()["ok"] is True


def test_the_gap_counts_served_but_unlogged_calls_separately_from_refused_writes(tmp_path):
    log = call_log.CallLog(tmp_path / "gapcount")
    log._mark_degraded("disk full", unlogged=1)
    log._mark_degraded("disk full", unlogged=0, refused=1)
    log._mark_degraded("disk full", unlogged=0, refused=1)
    state = log.degraded()
    assert (state["unlogged"], state["refused"]) == (1, 2)
    log.append(_fields())  # recovery: the gap entry comes first, then the call
    first, second = entries_in(log)
    assert first["outcome"] == "gap" and (first["count"], first["refused"]) == (1, 2) and second["tool"] == "emr_latest"
    assert log.degraded() is None and log.verify()["ok"] is True


def entries_in(log):
    return [e for e in reversed(list(log.entries_desc()))]


def test_workers_sharing_a_log_record_one_gap_not_one_per_worker(tmp_path):
    """Two workers refuse writes during the same outage; after recovery the shared record produces exactly one gap with the summed counts."""
    shared = tmp_path / "shared"
    w1, w2 = call_log.CallLog(shared), call_log.CallLog(shared)
    w1._mark_degraded("today's file is read-only", unlogged=0, refused=1)
    w2._mark_degraded("today's file is read-only", unlogged=0, refused=1)
    w2._mark_degraded("today's file is read-only", unlogged=1, refused=0)
    w1.append(_fields(1))  # recovery on worker 1: it writes the gap and clears the shared record
    w2.append(_fields(2))  # worker 2 must not replay a stale copy of the same outage
    gaps = [e for e in reversed(list(w1.entries_desc())) if e["outcome"] == "gap"]
    assert len(gaps) == 1, f"{len(gaps)} gap entries for one outage"
    assert (gaps[0]["count"], gaps[0]["refused"]) == (1, 2)
    assert w1.degraded() is None and w2.degraded() is None and w1.verify()["ok"] is True


def test_unpersisted_increments_are_added_to_the_shared_count_not_max_merged(tmp_path, monkeypatch):
    """Worker A fails to persist one call; worker B (which can write the file) records more.  Three calls happened, so the total is three."""
    shared = tmp_path / "disjoint"
    a, b = call_log.CallLog(shared), call_log.CallLog(shared)
    b._mark_degraded("file unwritable for B? no: B persists", unlogged=1)  # file: unlogged=1
    monkeypatch.setattr(a, "_locked", lambda: (_ for _ in ()).throw(OSError("A cannot persist")))
    a._mark_degraded("A cannot write the directory", unlogged=1)  # A keeps a delta of 1 in memory
    assert a.degraded()["unlogged"] == 2  # file 1 + A's delta 1
    b._mark_degraded("B persists another", unlogged=1)  # file: unlogged=2
    assert a.degraded()["unlogged"] == 3, "max() would have reported 2 for three calls"
    monkeypatch.undo()
    a.append(_fields())
    gap = [e for e in reversed(list(a.entries_desc())) if e["outcome"] == "gap"]
    assert len(gap) == 1 and gap[0]["count"] == 3 and a.degraded() is None


def test_concurrent_threads_do_not_lose_counts_when_the_outage_cannot_be_persisted(tmp_path, monkeypatch):
    log = call_log.CallLog(tmp_path / "threads")
    monkeypatch.setattr(log, "_locked", lambda: (_ for _ in ()).throw(OSError("directory unwritable")))
    for name in ("_combine", "_merge"):  # widen the read-modify-write window whichever name the implementation uses
        original = getattr(call_log.CallLog, name, None)
        if original is not None:
            monkeypatch.setattr(call_log.CallLog, name, staticmethod(lambda *a, _o=original, **k: (time.sleep(0.002), _o(*a, **k))[1]), raising=False)

    def work():
        for _ in range(10):
            log._mark_degraded("down", unlogged=1, refused=1)

    threads = [threading.Thread(target=work) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    state = log.degraded()
    assert (state["unlogged"], state["refused"]) == (80, 80), state
