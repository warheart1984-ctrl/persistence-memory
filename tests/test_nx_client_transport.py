"""NxSearchClient against a fake nx-search: MCP handshake, timeouts, fallbacks, argv, cleanup.

The fake is a few lines of node that speaks just enough of the MCP stdio protocol; no real nx-search is needed.
Skipped where node is missing.
"""
from __future__ import annotations

import concurrent.futures
import json
import shutil
import subprocess
import time

import pytest

import app.nx_search_client as mod
from app.nx_search_client import NxSearchClient

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node is required for the fake nx-search")

FAKE_NX = r"""
const fs = require('fs');
const readline = require('readline');
const log = process.env.FAKE_NX_LOG;
const mode = process.env.FAKE_NX_MODE || 'ok';
if (process.argv[2] === 'search') {          // the CLI fallback path
  console.log(JSON.stringify({ content: [{ path: '/from-cli', snippet: 'cli' }], filenames: [] }));
  process.exit(0);
}
function send(o) { process.stdout.write(JSON.stringify(o) + '\n'); }
const rl = readline.createInterface({ input: process.stdin });
rl.on('line', (line) => {
  const m = JSON.parse(line);
  fs.appendFileSync(log, JSON.stringify({ method: m.method, id: m.id ?? null, enabled: process.env.JARVIS_NX_ENABLED, pid: process.pid }) + '\n');
  if (mode === 'hang') return;
  if (m.method === 'initialize') {
    if (mode === 'disabled') return send({ jsonrpc: '2.0', id: m.id, error: { code: -32001, message: 'NX_DISABLED' } });
    return send({ jsonrpc: '2.0', id: m.id, result: { protocolVersion: '2025-03-26' } });
  }
  if (m.method === 'tools/call') {
    const body = { query: m.params.arguments.query, content: [{ path: '/from-mcp', snippet: 's' }], filenames: [] };
    return send({ jsonrpc: '2.0', id: m.id, result: { content: [{ type: 'text', text: JSON.stringify(body) }] } });
  }
  // notifications get no response
});
"""


@pytest.fixture()
def fake_nx(tmp_path, monkeypatch):
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin" / "nx.js").write_text(FAKE_NX)
    log = tmp_path / "received.log"
    monkeypatch.setenv("FAKE_NX_LOG", str(log))
    monkeypatch.delenv("FAKE_NX_MODE", raising=False)
    monkeypatch.setattr(mod, "MCP_TIMEOUT_S", 1.0)

    def received() -> list[dict]:
        return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []

    return tmp_path, received


def _within(seconds: float, fn):
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(fn)
        try:
            return future.result(timeout=seconds)
        except concurrent.futures.TimeoutError:
            pytest.fail(f"did not finish within {seconds}s (a hang)")


def test_the_initialized_notification_is_sent_but_never_waited_for(fake_nx):
    path, received = fake_nx
    client = NxSearchClient(nx_path=str(path), persistent=True)
    try:
        result = _within(10, lambda: client.search("alpha"))
        assert result["content"][0]["path"] == "/from-mcp", result
        methods = [r["method"] for r in received()]
        assert methods == ["initialize", "notifications/initialized", "tools/call"]
    finally:
        client.close()


def test_the_child_is_started_with_the_bridge_enabled(fake_nx):
    path, received = fake_nx
    with NxSearchClient(nx_path=str(path), persistent=True) as client:
        _within(10, lambda: client.search("alpha"))
    assert {r["enabled"] for r in received()} == {"1"}


def test_a_client_that_is_not_persistent_does_not_leave_a_process_behind(fake_nx):
    path, _ = fake_nx
    client = NxSearchClient(nx_path=str(path))
    _within(10, lambda: client.search("alpha"))
    assert client._mcp_process is None
    _within(10, lambda: client.stats())
    assert client._mcp_process is None


def test_close_ends_the_process_and_is_idempotent(fake_nx):
    path, _ = fake_nx
    client = NxSearchClient(nx_path=str(path), persistent=True)
    _within(10, lambda: client.search("alpha"))
    proc = client._mcp_process
    assert proc is not None and proc.poll() is None
    client.close()
    assert proc.poll() is not None
    client.close()


def test_a_persistent_client_reuses_one_process(fake_nx):
    path, received = fake_nx
    with NxSearchClient(nx_path=str(path), persistent=True) as client:
        _within(10, lambda: (client.search("a"), client.search("b")))
    assert len({r["pid"] for r in received()}) == 1
    assert [r["method"] for r in received()].count("initialize") == 1


def test_a_refused_handshake_falls_back_to_the_cli(fake_nx, monkeypatch):
    path, _ = fake_nx
    monkeypatch.setenv("FAKE_NX_MODE", "disabled")
    client = NxSearchClient(nx_path=str(path))
    result = _within(15, lambda: client.search("alpha"))
    assert result["content"][0]["path"] == "/from-cli", result
    assert client._mcp_process is None


def test_a_silent_server_times_out_and_the_cli_answers(fake_nx, monkeypatch):
    path, _ = fake_nx
    monkeypatch.setenv("FAKE_NX_MODE", "hang")
    monkeypatch.setattr(mod, "MCP_TIMEOUT_S", 0.5)
    client = NxSearchClient(nx_path=str(path), persistent=True)
    started = time.monotonic()
    result = _within(15, lambda: client.search("alpha"))
    assert result["content"][0]["path"] == "/from-cli", result
    assert time.monotonic() - started < 10
    assert client._mcp_process is None, "the unresponsive child was not left running"


def test_an_unavailable_client_answers_without_starting_anything(tmp_path):
    client = NxSearchClient(nx_path=str(tmp_path / "missing"), require_available=False)
    assert client.search("x") == {"error": "nx-search not available", "content": [], "filenames": []}
    assert client.stats() == {"error": "nx-search not available"}
    assert client._mcp_process is None


# --- every CLI wrapper builds a flat argv of strings ------------------------------------------------------------

@pytest.fixture()
def argv_capture(fake_nx, monkeypatch):
    path, _ = fake_nx
    seen: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        seen.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout='{"ok": true}', stderr="")

    monkeypatch.setattr(mod.subprocess, "run", fake_run)
    return NxSearchClient(nx_path=str(path)), seen


def test_cli_wrappers_build_flat_string_argv(argv_capture):
    client, seen = argv_capture
    calls = [
        (lambda: client.ask("what is x", no_stream=True), ["ask", "what is x", "--no-stream"]),
        (lambda: client.ask("plain"), ["ask", "plain"]),
        (lambda: client.remember("k", "v"), ["remember", "k", "v"]),
        (lambda: client.forget("k"), ["remember", "--forget", "k"]),
        (lambda: client.describe("/img.png", question="what", holo=True, native=True, save=True),
         ["describe", "/img.png", "--q", "what", "--holo", "--native", "--save"]),
        (lambda: client.spatialize("/frames", every_nth=2, max_frames=5, tag="t"),
         ["spatialize", "/frames", "--every-nth", "2", "--max-frames", "5", "--tag", "t"]),
        (lambda: client.scan(["/a", "/b"], rebuild=True), ["scan", "--rebuild", "/a", "/b"]),
        (lambda: client.reindex("/a"), ["reindex", "/a"]),
        (lambda: client.prune(["/a", "/b"]), ["prune", "/a", "/b"]),
    ]
    for call, tail in calls:
        seen.clear()
        result = call()
        assert result == {"ok": True}, (tail, result)
        argv = seen[0]
        assert all(isinstance(a, str) for a in argv), argv
        assert argv[2:] == tail


def test_a_nested_list_argument_is_a_loud_error_not_a_silent_failure(argv_capture):
    client, seen = argv_capture
    with pytest.raises(TypeError, match="strings"):
        client._cli_cmd("ask", ["nested"])
    assert seen == []
