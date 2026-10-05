"""Hooks must send the API key a protected server requires, and must not leak it."""

from __future__ import annotations

import importlib.util
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

_HOOKS = Path(__file__).resolve().parents[1] / "agent-hooks"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(f"hooks_{name}", _HOOKS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _Recorder(BaseHTTPRequestHandler):
    seen: list[dict] = []

    def log_message(self, *args):  # silence
        pass

    def do_GET(self):
        type(self).seen.append({"path": self.path, "key": self.headers.get("X-API-Key")})
        routes = {
            "/health": {"status": "ok", "live": True},
            "/ready": {"status": "ready", "checks": {"store": "ok"}},
            "/api/jarvis/memory": {"memories": [{"id": "mem-1", "content": "hello from the ledger"}]},
            "/api/jarvis/memory/board": {"memory_board": {"summary": "a board"}},
        }
        body = json.dumps(routes.get(self.path, {})).encode()
        self.send_response(200 if self.path in routes else 404)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture
def server(monkeypatch):
    _Recorder.seen = []
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Recorder)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    monkeypatch.setenv("JARVIS_MEMORYBOARD_URL", f"http://127.0.0.1:{httpd.server_address[1]}")
    for var in ("JARVIS_API_KEY", "JARVIS_API_KEY_FILE", "DIRECTOR_MEMORYBOARD_BASE_URL"):
        monkeypatch.delenv(var, raising=False)
    yield _Recorder
    httpd.shutdown()


def test_key_from_the_environment_is_sent_as_x_api_key(server, monkeypatch):
    common = _load("jarvis_common")
    monkeypatch.setenv("JARVIS_API_KEY", "env-key-123")
    common.http_json("GET", "/api/jarvis/memory")
    assert server.seen[-1]["key"] == "env-key-123"


def test_key_from_a_file_is_sent_and_whitespace_is_trimmed(server, monkeypatch, tmp_path):
    common = _load("jarvis_common")
    key_file = tmp_path / "api-key"
    key_file.write_text("file-key-456\n", "utf-8")
    monkeypatch.setenv("JARVIS_API_KEY_FILE", str(key_file))
    common.http_json("GET", "/api/jarvis/memory")
    assert server.seen[-1]["key"] == "file-key-456"


def test_the_environment_key_wins_over_the_file(server, monkeypatch, tmp_path):
    common = _load("jarvis_common")
    key_file = tmp_path / "api-key"
    key_file.write_text("file-key", "utf-8")
    monkeypatch.setenv("JARVIS_API_KEY_FILE", str(key_file))
    monkeypatch.setenv("JARVIS_API_KEY", "env-key")
    common.http_json("GET", "/health")
    assert server.seen[-1]["key"] == "env-key"


def test_an_unreadable_key_file_sends_no_key_and_does_not_crash(server, monkeypatch, tmp_path):
    common = _load("jarvis_common")
    monkeypatch.setenv("JARVIS_API_KEY_FILE", str(tmp_path / "does-not-exist"))
    common.http_json("GET", "/health")
    assert server.seen[-1]["key"] is None


def test_without_a_key_nothing_extra_is_sent(server):
    common = _load("jarvis_common")
    common.http_json("GET", "/health")
    assert server.seen[-1]["key"] is None


def test_hooks_pass_the_key_through_try_http_json(server, monkeypatch):
    common = _load("jarvis_common")
    monkeypatch.setenv("JARVIS_API_KEY", "hook-key")
    payload, error = common.try_http_json("GET", "/api/jarvis/memory/board")
    assert error is None and payload["memory_board"]["summary"] == "a board"
    assert server.seen[-1]["key"] == "hook-key"


def test_the_key_is_never_sent_over_plain_http_to_a_non_loopback_host(monkeypatch):
    common = _load("jarvis_common")
    monkeypatch.setenv("JARVIS_API_KEY", "must-not-leak")
    monkeypatch.setenv("JARVIS_MEMORYBOARD_URL", "http://192.0.2.7:8001")

    attempted = []

    def no_network(*args, **kwargs):
        attempted.append(True)
        raise OSError("network is off in this test")

    monkeypatch.setattr(common.urllib.request, "urlopen", no_network)
    payload, error = common.try_http_json("GET", "/health")
    assert attempted == []  # refused before any request was made
    assert payload is None and "plain http" in error and "must-not-leak" not in error


@pytest.mark.parametrize("base", ["https://ledger.example", "http://127.0.0.1:8001", "http://localhost:8001", "http://[::1]:8001"])
def test_https_and_loopback_are_allowed(monkeypatch, base):
    common = _load("jarvis_common")
    monkeypatch.setenv("JARVIS_API_KEY", "ok-to-send")
    monkeypatch.setenv("JARVIS_MEMORYBOARD_URL", base)
    captured = {}

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b"{}"

    def fake_urlopen(req, timeout=None):
        captured["key"] = req.get_header("X-api-key")
        return _Resp()

    monkeypatch.setattr(common.urllib.request, "urlopen", fake_urlopen)
    common.http_json("GET", "/health")
    assert captured["key"] == "ok-to-send"


def test_ping_script_works_with_the_current_health_shape_and_sends_the_key(server, monkeypatch, capsys):
    ping = _load("ping_memoryboard")
    monkeypatch.setenv("JARVIS_API_KEY", "ping-key")
    ping.main()
    out = capsys.readouterr().out
    assert "Health: ok" in out and "OK: service is live" in out and "Memories stored: 1" in out
    assert {s["key"] for s in server.seen if s["path"].startswith("/api/")} == {"ping-key"}
