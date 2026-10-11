"""The server-side call log ("call witness"): every tool call is written by the server, tamper-evidently, without touching the ledger.

Backend-parametrized tests run on the JSON store and on Postgres (CI's test-postgres job sets JARVIS_TEST_PG_DSN; locally the
Postgres variants skip only when no throwaway server is configured).  See docs/call_log.md.
"""

from __future__ import annotations

import ast
import hashlib
import json
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


def test_non_get_calls_under_api_jarvis_are_logged_as_http_api_and_reads_are_not(jclient):
    before = len(entries())
    rec = add(jclient)
    assert jclient.get("/api/jarvis/memory", headers=HDR).status_code == 200  # a GET read: not witnessed
    assert jclient.patch(f"/api/jarvis/memory/{rec['id']}", headers=HDR, json={"subject": "s", "expected_version": rec["version"]}).status_code == 200
    assert jclient.delete(f"/api/jarvis/memory/{rec['id']}", headers=HDR).status_code == 200
    got = entries()[before:]
    assert [(e["transport"], e["tool"]) for e in got] == [
        ("http-api", "POST /api/jarvis/memory"), ("http-api", "PATCH /api/jarvis/memory/{id}"), ("http-api", "DELETE /api/jarvis/memory/{id}")]
    assert [e["target"] for e in got][1:] == [rec["id"], rec["id"]] and all(e["outcome"] == "ok" for e in got)


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


def test_a_flood_of_denied_calls_is_capped_and_rolled_into_one_suppressed_entry(jclient, monkeypatch):
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


@posix_user_only
def test_scenario_a_unwritable_directory_still_leaves_a_gap_entry_after_recovery(jclient, lockable_log_dir):
    """The DEGRADED file lives in the directory that cannot be written, so the outage must also be remembered in memory."""
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
def test_scenario_b_an_unwritable_log_file_refuses_the_first_write_instead_of_committing_it_unlogged(jclient, lockable_log_dir):
    """The directory looks writable, but today's file is not: the preflight must notice, so the write is refused, not committed."""
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
