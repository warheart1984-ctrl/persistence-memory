"""The ledger MCP server: fails closed, never leaks the key, reads freely, writes only when switched on."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

from mcp_server import ledger_stdio as mcp

KEY = "Zebra-Quartz-Lantern-91"
_REPO = Path(__file__).resolve().parents[1]


def _record(i: int, content: str | None = None) -> dict:
    return {
        "id": f"mem-{i:012x}",
        "content": content if content is not None else f"memory number {i}",
        "type": "fact",
        "status": "draft",
        "source_agent": "test",
        "session_id": "s",
        "subject": None,
        "tags": [],
        "confidence": 0.5,
        "created_at": "2026-10-05T00:00:00+00:00",
        "version": 1,
        "content_sha256": "0" * 64,
    }


class _Ledger(BaseHTTPRequestHandler):
    seen: list[dict] = []
    total = 60
    ready = True
    fail_body: str | None = None
    long_first = False
    archived: set[int] = set()

    def log_message(self, *args):
        pass

    def _reply(self, code: int, payload: dict):
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _note(self, body=None):
        type(self).seen.append({"method": self.command, "path": self.path, "key": self.headers.get("X-API-Key"), "body": body})

    def do_GET(self):
        self._note()
        url = urlparse(self.path)
        cls = type(self)
        if cls.fail_body is not None:
            return self._reply(500, {"detail": cls.fail_body})
        if url.path == "/health":
            return self._reply(200, {"status": "ok", "memory_write_enabled": True})
        if url.path == "/ready":
            return self._reply(200, {"status": "ready", "checks": {"database": "ok"}}) if cls.ready else self._reply(503, {"status": "unavailable", "checks": {"database": "failed"}})
        if url.path == "/api/jarvis/memory":
            q = parse_qs(url.query)
            limit = int(q.get("limit", ["50"])[0])
            scope = q.get("truth_scope", [None])[0]
            records = [
                _record(i, "x" * 1500 if (cls.long_first and i == 0) else None) | {"status": "archived" if i in cls.archived else "draft"}
                for i in range(cls.total)
            ]
            if scope == "live":
                records = [r for r in records if r["status"] != "archived"]
            elif scope == "archived":
                records = [r for r in records if r["status"] == "archived"]
            return self._reply(200, {"memories": records[:limit]})
        if url.path.startswith("/api/jarvis/memory/"):
            memory_id = url.path.rsplit("/", 1)[1]
            if memory_id == _record(7)["id"]:
                return self._reply(200, {"memory": _record(7), "selection": {}})
            return self._reply(404, {"detail": "Memory not found"})
        return self._reply(404, {})

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length) or b"{}")
        self._note(body)
        if urlparse(self.path).path == "/api/jarvis/memory":
            record = _record(99, body["content"]) | {"source_agent": body["source_agent"], "type": body["type"], "status": body["status"]}
            return self._reply(200, {"memory": record})
        return self._reply(404, {})


@pytest.fixture
def ledger(monkeypatch, tmp_path):
    _Ledger.seen = []
    _Ledger.total = 60
    _Ledger.ready = True
    _Ledger.fail_body = None
    _Ledger.long_first = False
    _Ledger.archived = set()
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Ledger)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    key_file = tmp_path / "api-key"
    key_file.write_text(KEY + "\n", "utf-8")
    monkeypatch.setenv("JARVIS_MEMORYBOARD_URL", f"http://127.0.0.1:{httpd.server_address[1]}")
    monkeypatch.setenv("JARVIS_API_KEY_FILE", str(key_file))
    for var in ("JARVIS_API_KEY", "JARVIS_LEDGER_MCP_WRITE", "JARVIS_LEDGER_MCP_SOURCE", "EMR_RECALL_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    yield _Ledger
    httpd.shutdown()


def call(name: str, arguments: dict | None = None) -> dict:
    return mcp.handle_tools_call({"name": name, "arguments": arguments or {}})


def code_of(result: dict) -> str:
    assert result["isError"] is True
    return result["structuredContent"]["error"]["code"]


# --- fail closed --------------------------------------------------------------------------------------------

@pytest.mark.parametrize("tool,args", [("health", {}), ("recall", {}), ("get", {"id": "mem-000000000007"})])
def test_without_a_url_every_tool_refuses_and_nothing_is_sent(ledger, monkeypatch, tool, args):
    monkeypatch.delenv("JARVIS_MEMORYBOARD_URL")
    result = call(tool, args)
    assert code_of(result) == "no_url" and "JARVIS_MEMORYBOARD_URL is not set" in result["content"][0]["text"]
    assert ledger.seen == []


def test_a_blank_url_counts_as_not_set(ledger, monkeypatch):
    monkeypatch.setenv("JARVIS_MEMORYBOARD_URL", "   ")
    assert code_of(call("health")) == "no_url" and ledger.seen == []


@pytest.mark.parametrize("tool,args", [("health", {}), ("recall", {}), ("get", {"id": "mem-000000000007"})])
def test_without_a_key_every_tool_refuses_and_nothing_is_sent(ledger, monkeypatch, tool, args):
    monkeypatch.delenv("JARVIS_API_KEY_FILE")
    result = call(tool, args)
    assert code_of(result) == "no_key" and "No API key" in result["content"][0]["text"]
    assert ledger.seen == []


def test_an_unreadable_or_empty_key_file_counts_as_no_key(ledger, monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_API_KEY_FILE", str(tmp_path / "does-not-exist"))
    assert code_of(call("health")) == "no_key"
    empty = tmp_path / "empty"
    empty.write_text("\n", "utf-8")
    monkeypatch.setenv("JARVIS_API_KEY_FILE", str(empty))
    assert code_of(call("health")) == "no_key" and ledger.seen == []


def test_the_key_is_never_sent_over_plain_http_to_a_non_loopback_host(ledger, monkeypatch):
    monkeypatch.setenv("JARVIS_MEMORYBOARD_URL", "http://192.0.2.7:8011")
    attempted = []
    monkeypatch.setattr(mcp.urllib.request, "urlopen", lambda *a, **k: attempted.append(True))
    result = call("health")
    assert code_of(result) == "key_not_allowed" and attempted == [] and KEY not in json.dumps(result)


def test_there_is_no_default_address_in_the_source():
    text = (_REPO / "mcp_server" / "ledger_stdio.py").read_text("utf-8")
    assert "8001" not in text and "8002" not in text and "DEFAULT_BASE" not in text


def test_the_key_header_is_sent_but_never_returned_or_echoed_in_errors(ledger):
    results = [call("health"), call("recall"), call("get", {"id": _record(7)["id"]})]
    assert all(r["isError"] is False for r in results)
    assert {s["key"] for s in ledger.seen} == {KEY}
    ledger.fail_body = f"upstream exploded while holding {KEY}"
    failed = call("recall")
    assert failed["isError"] is True
    everything = json.dumps(results) + json.dumps(failed)
    assert KEY not in everything and "<key>" in failed["content"][0]["text"]


# --- health / recall / get ----------------------------------------------------------------------------------

def test_health_reports_ready(ledger):
    result = call("health")["structuredContent"]
    assert result["health"] == "ok" and result["ready"] is True


def test_health_reports_not_ready_without_raising(ledger):
    ledger.ready = False
    result = call("health")
    assert result["isError"] is False and result["structuredContent"]["ready"] is False


def test_recall_defaults_to_50_without_provenance(ledger):
    result = call("recall")["structuredContent"]
    assert result["count"] == 50 and result["limit"] == 50 and result["capped"] is True
    assert ledger.seen[-1]["path"] == "/api/jarvis/memory?limit=50&with_provenance=false&truth_scope=live"


def test_recall_up_to_200_returns_every_record_and_is_not_capped_below_the_limit(ledger):
    result = call("recall", {"limit": 200})["structuredContent"]
    assert result["count"] == 60 and result["capped"] is False
    assert len({m["id"] for m in result["memories"]}) == 60


@pytest.mark.parametrize("limit", [0, 201, -1, "50", 5.5, True, None.__class__])
def test_recall_rejects_a_bad_limit_without_a_request(ledger, limit):
    result = call("recall", {"limit": limit})
    assert code_of(result) == "bad_argument" and ledger.seen == []


def test_recall_passes_filters_and_shortens_long_content(ledger):
    ledger.long_first = True
    result = call("recall", {"limit": 3, "type": "fact", "query": "retry loop"})["structuredContent"]
    assert "type=fact" in ledger.seen[-1]["path"] and "query=retry+loop" in ledger.seen[-1]["path"]
    first = result["memories"][0]
    assert len(first["content"]) <= 400 and first["truncated"] is True
    full = call("recall", {"limit": 1, "content_chars": 0})["structuredContent"]["memories"][0]
    assert len(full["content"]) == 1500 and full["truncated"] is False


def test_get_returns_one_memory(ledger):
    result = call("get", {"id": _record(7)["id"]})["structuredContent"]
    assert result["memory"]["id"] == _record(7)["id"]


@pytest.mark.parametrize("bad", ["../etc/passwd", "mem 1", "mem-1/../2", "", "x" * 65, None, 7])
def test_get_rejects_a_malformed_id_without_a_request(ledger, bad):
    assert code_of(call("get", {"id": bad})) == "bad_argument" and ledger.seen == []


def test_get_of_an_unknown_id_says_not_found(ledger):
    assert code_of(call("get", {"id": "mem-doesnotexist"})) == "not_found"


# --- writing is off unless switched on ----------------------------------------------------------------------

_GOOD = {"content": "Grok test fact", "type": "fact", "user_requested": "please store this test fact"}


def test_write_is_not_offered_and_does_nothing_by_default(ledger):
    names = [t["name"] for t in mcp._listed_tools()]
    assert names == ["health", "recall", "get"]
    result = call("write", _GOOD)
    assert result["isError"] is True and "disabled" in result["content"][0]["text"]
    assert ledger.seen == []


@pytest.fixture
def writable(ledger, monkeypatch):
    monkeypatch.setenv("JARVIS_LEDGER_MCP_WRITE", "1")
    return ledger


def test_write_is_offered_when_switched_on(writable):
    assert [t["name"] for t in mcp._listed_tools()] == ["health", "recall", "get", "write"]


def test_write_posts_one_draft_with_the_fixed_source_agent(writable):
    result = call("write", _GOOD | {"session_id": "chat-1"})
    assert result["isError"] is False and result["structuredContent"]["stored"] is True
    post = [s for s in writable.seen if s["method"] == "POST"]
    assert len(post) == 1 and post[0]["key"] == KEY
    body = post[0]["body"]
    assert body["source_agent"] == "grok-bot" and body["type"] == "fact" and body["status"] == "draft"
    assert body["session_id"] == "chat-1" and body["tags"] == ["grok-bot", "user-requested"]
    assert body["evidence"][0]["note"] == "please store this test fact"


def test_write_accepts_a_decision_and_a_custom_source_name(writable, monkeypatch):
    monkeypatch.setenv("JARVIS_LEDGER_MCP_SOURCE", "my-agent")
    assert call("write", _GOOD | {"type": "decision"})["isError"] is False
    assert writable.seen[-1]["body"]["source_agent"] == "my-agent" and writable.seen[-1]["body"]["type"] == "decision"


@pytest.mark.parametrize(
    "bad",
    [
        {"type": "task"},
        {"type": "preference"},
        {"content": ""},
        {"content": "x" * 1901},
        {"user_requested": ""},
        {"user_requested": "yes"},
        {"session_id": "s" * 129},
    ],
)
def test_write_refuses_bad_arguments_without_a_request(writable, bad):
    assert code_of(call("write", _GOOD | bad)) in ("bad_argument", "user_request_required") and writable.seen == []


def test_write_requires_the_users_words(writable):
    args = {k: v for k, v in _GOOD.items() if k != "user_requested"}
    assert code_of(call("write", args)) == "user_request_required" and writable.seen == []


@pytest.mark.parametrize(
    "secret",
    ["sk-" + "a1B2c3D4" * 5, "ghp_" + "A1b2C3d4E5" * 4, "-----BEGIN " + "RSA PRIVATE KEY-----", "password = hunter22!", KEY],
)
def test_write_refuses_anything_that_looks_like_a_credential(writable, secret):
    result = call("write", _GOOD | {"content": f"note: {secret}"})
    assert code_of(result) == "secret_refused" and writable.seen == []
    assert secret not in json.dumps(result)


def test_a_secret_hidden_in_the_users_wording_is_refused_too(writable):
    result = call("write", _GOOD | {"user_requested": "store it, my password = hunter22!"})
    assert code_of(result) == "secret_refused" and writable.seen == []


# --- protocol -----------------------------------------------------------------------------------------------

def test_initialize_ping_unknown_and_notifications():
    init = mcp.dispatch({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2024-11-05"}})
    assert init["result"]["protocolVersion"] == "2024-11-05" and init["result"]["serverInfo"]["name"] == "jarvis-ledger"
    assert mcp.dispatch({"jsonrpc": "2.0", "id": 2, "method": "ping"})["result"] == {}
    assert mcp.dispatch({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None
    assert mcp.dispatch({"jsonrpc": "2.0", "id": 3, "method": "nope"})["error"]["code"] == -32601
    assert code_of(mcp.dispatch({"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "nope"}})["result"] | {"structuredContent": {"error": {"code": "x"}}}) == "x"


def test_tools_have_read_only_hints_except_write():
    tools = {t["name"]: t for t in mcp._listed_tools()}
    assert all(tools[n]["annotations"]["readOnlyHint"] is True for n in ("health", "recall", "get"))
    assert mcp.TOOLS["write"]["annotations"]["readOnlyHint"] is False


def _stdio(env_extra: dict, lines: list[dict]) -> tuple[list[dict], str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("JARVIS_")}
    env.update(env_extra)
    proc = subprocess.run(
        [sys.executable, "-B", str(_REPO / "mcp_server" / "ledger_stdio.py")],
        input="\n".join(json.dumps(m) for m in lines) + "\n",
        capture_output=True,
        text=True,
        env=env,
        cwd=str(_REPO),
        timeout=60,
    )
    return [json.loads(line) for line in proc.stdout.splitlines() if line.strip()], proc.stderr


def test_the_real_stdio_process_refuses_without_a_url_and_works_with_one(ledger, tmp_path):
    msgs = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "health", "arguments": {}}},
    ]
    out, err = _stdio({}, msgs)  # no URL, no key
    assert [m["id"] for m in out] == [1, 2, 3]  # the notification got no reply
    assert out[2]["result"]["isError"] is True and "JARVIS_MEMORYBOARD_URL is not set" in err and ledger.seen == []

    key_file = tmp_path / "k"
    key_file.write_text(KEY, "utf-8")
    out, err = _stdio({"JARVIS_MEMORYBOARD_URL": os.environ["JARVIS_MEMORYBOARD_URL"], "JARVIS_API_KEY_FILE": str(key_file)}, msgs)
    assert out[2]["result"]["isError"] is False and out[2]["result"]["structuredContent"]["ready"] is True
    assert KEY not in json.dumps(out) and KEY not in err


# --- truth_scope: archived records stay out of recall unless asked for -----------------------------------

def test_recall_leaves_archived_records_out_by_default(ledger):
    ledger.archived = {3, 4, 5}
    result = call("recall", {"limit": 200})["structuredContent"]
    assert result["truth_scope"] == "live" and result["count"] == 57
    assert not any(m["status"] == "archived" for m in result["memories"])
    assert "truth_scope=live" in ledger.seen[-1]["path"]


def test_recall_can_ask_for_everything_or_only_archived(ledger):
    ledger.archived = {3, 4, 5}
    everything = call("recall", {"limit": 200, "truth_scope": "all"})["structuredContent"]
    assert everything["count"] == 60 and "truth_scope=" not in ledger.seen[-1]["path"]
    only = call("recall", {"limit": 200, "truth_scope": "archived"})["structuredContent"]
    assert only["count"] == 3 and all(m["status"] == "archived" for m in only["memories"])
    assert "truth_scope=archived" in ledger.seen[-1]["path"]


@pytest.mark.parametrize("scope", ["draft", "verified", "grok-bot", "", "LIVE", None.__class__, 5, ["live"]])
def test_recall_rejects_any_other_truth_scope_without_a_request(ledger, scope):
    result = call("recall", {"truth_scope": scope})
    assert code_of(result) == "bad_argument" and ledger.seen == []


def test_the_recall_schema_offers_the_three_scopes():
    schema = {t["name"]: t for t in mcp._listed_tools()}["recall"]["inputSchema"]["properties"]["truth_scope"]
    assert schema["enum"] == ["live", "all", "archived"] and schema["default"] == "live"
