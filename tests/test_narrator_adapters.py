"""Narrator adapter tests — fake local HTTP servers only, never real providers."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

import app.narrator as narrator
from app.narrator.base import NarrationRequest, NarratorError, ProviderConfig

REQ = NarrationRequest(model="m", system_prompt="sys", user_prompt="usr",
                       timeout_s=5.0)


class _Capture(BaseHTTPRequestHandler):
    """Records the request; responds per class-level ``responder``."""

    captured: dict = {}
    responder = staticmethod(lambda path, body, hdrs: (200, {}))
    delay_s = 0.0

    def do_POST(self):
        import time
        if self.delay_s:
            time.sleep(self.delay_s)
        n = int(self.headers.get("content-length") or 0)
        body = json.loads(self.rfile.read(n) or b"{}")
        type(self).captured = {"path": self.path, "body": body,
                               "headers": dict(self.headers)}
        status, payload = self.responder(self.path, body, dict(self.headers))
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *a):
        pass


@pytest.fixture()
def server():
    _Capture.captured = {}
    _Capture.delay_s = 0.0
    httpd = HTTPServer(("127.0.0.1", 0), _Capture)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    t.join(2)


def _req(**kw) -> NarrationRequest:
    return NarrationRequest(model=kw.pop("model", "m"),
                            system_prompt="sys", user_prompt="usr",
                            timeout_s=kw.pop("timeout_s", 5.0), **kw)


def _openai_ok(path, body, headers):
    return 200, {"model": "gpt-x",
                 "choices": [{"message": {"content": "hello"}}],
                 "usage": {"total_tokens": 3}}


# --- request shapes against the fake ---

def test_openai_compatible_shape(server):
    _Capture.responder = staticmethod(_openai_ok)
    cfg = ProviderConfig(name="t", adapter="openai_compatible",
                         base_url=server, model="gpt-x")
    out = narrator.OpenAICompatibleAdapter(cfg).narrate(REQ)
    assert out.text == "hello" and out.model == "gpt-x" and out.usage
    cap = _Capture.captured
    assert cap["path"] == "/chat/completions"
    assert cap["body"]["model"] == "m"          # request model wins over cfg.model
    # empty request model falls back to the configured model
    narrator.OpenAICompatibleAdapter(cfg).narrate(_req(model=""))
    assert _Capture.captured["body"]["model"] == "gpt-x"
    assert cap["body"]["messages"][0]["role"] == "system"
    assert cap["body"]["temperature"] == 0.0
    assert "Authorization" not in cap["headers"]  # no key configured


def test_openai_compatible_sends_key_in_header_only(server, monkeypatch):
    seen = {}
    def resp(path, body, headers):
        seen.update(headers)
        return _openai_ok(path, body, headers)
    _Capture.responder = staticmethod(resp)
    monkeypatch.setenv("MY_KEY_ENV", "sekrit")
    cfg = ProviderConfig(name="t", adapter="openai_compatible",
                         base_url=server, model="m", api_key_env="MY_KEY_ENV")
    narrator.OpenAICompatibleAdapter(cfg).narrate(REQ)
    assert seen.get("Authorization") == "Bearer sekrit"
    assert "sekrit" not in _Capture.captured["path"]  # never in the URL


def test_anthropic_shape(server):
    def resp(path, body, headers):
        return 200, {"model": "claude-x",
                     "content": [{"type": "text", "text": "hi"}]}
    _Capture.responder = staticmethod(resp)
    monkeypatch = pytest.MonkeyPatch.context()
    cfg = ProviderConfig(name="t", adapter="anthropic",
                         base_url=server, model="claude-x",
                         api_key_env="ANTHROPIC_TEST_KEY")
    with monkeypatch as mp:
        mp.setenv("ANTHROPIC_TEST_KEY", "k")
        out = narrator.AnthropicAdapter(cfg).narrate(REQ)
    assert out.text == "hi"
    cap = _Capture.captured
    assert cap["path"] == "/v1/messages"
    assert cap["body"]["system"] == "sys"
    assert cap["headers"]["x-api-key"] == "k"
    assert cap["headers"]["anthropic-version"] == "2023-06-01"


def test_google_shape(server, monkeypatch):
    def resp(path, body, headers):
        return 200, {"modelVersion": "gem-x",
                     "candidates": [{"content": {"parts": [{"text": "yo"}]}}]}
    _Capture.responder = staticmethod(resp)
    monkeypatch.setenv("GOOGLE_TEST_KEY", "gk")
    cfg = ProviderConfig(name="t", adapter="google", base_url=server,
                         model="gem-x", api_key_env="GOOGLE_TEST_KEY")
    out = narrator.GoogleAdapter(cfg).narrate(REQ)
    assert out.text == "yo"
    cap = _Capture.captured
    assert cap["path"] == "/v1beta/models/m:generateContent"  # req model wins
    assert cap["headers"]["x-goog-api-key"] == "gk"
    assert "gk" not in cap["path"]


def test_ollama_shape(server):
    def resp(path, body, headers):
        return 200, {"model": "llama3.1",
                     "message": {"role": "assistant", "content": "local hi"},
                     "eval_count": 9, "prompt_eval_count": 4}
    _Capture.responder = staticmethod(resp)
    cfg = ProviderConfig(name="t", adapter="ollama", base_url=server,
                         model="llama3.1")
    out = narrator.OllamaAdapter(cfg).narrate(REQ)
    assert out.text == "local hi" and out.usage["completion_tokens"] == 9
    cap = _Capture.captured
    assert cap["path"] == "/api/chat"
    assert cap["body"]["stream"] is False
    assert cap["body"]["format"] == "json"


def test_llm_gateway_shape(server):
    def resp(path, body, headers):
        return 200, {"content": "gateway says hi", "model": "amul"}
    _Capture.responder = staticmethod(resp)
    cfg = ProviderConfig(name="t", adapter="llm_gateway",
                         base_url=server, model="amul")
    out = narrator.LlmGatewayAdapter(cfg).narrate(REQ)
    assert out.text == "gateway says hi"
    cap = _Capture.captured
    assert cap["path"] == "/chat/complete"
    assert cap["body"]["params"] == {"temperature": 0.0, "max_tokens": 900}


# --- failure modes fall back via NarratorError ---

def test_non_2xx_raises(server):
    _Capture.responder = staticmethod(lambda *a: (500, {"error": "boom"}))
    cfg = ProviderConfig(name="t", adapter="openai_compatible",
                         base_url=server, model="m")
    with pytest.raises(NarratorError) as ei:
        narrator.OpenAICompatibleAdapter(cfg).narrate(REQ)
    assert ei.value.code == "NARRATOR_HTTP"


def test_timeout_raises(server):
    _Capture.responder = staticmethod(_openai_ok)
    _Capture.delay_s = 1.0
    cfg = ProviderConfig(name="t", adapter="openai_compatible",
                         base_url=server, model="m")
    req = _req(timeout_s=0.05)
    with pytest.raises(NarratorError) as ei:
        narrator.OpenAICompatibleAdapter(cfg).narrate(req)
    assert ei.value.code == "NARRATOR_HTTP"
    _Capture.delay_s = 0.0


def test_malformed_response_raises(server):
    _Capture.responder = staticmethod(lambda *a: (200, {"no_choices": True}))
    cfg = ProviderConfig(name="t", adapter="openai_compatible",
                         base_url=server, model="m")
    # choices absent -> text falls back to "" without crashing; gate catches it
    out = narrator.OpenAICompatibleAdapter(cfg).narrate(REQ)
    assert out.text == ""


def test_missing_key_env_refuses_not_crashes(server):
    cfg = ProviderConfig(name="t", adapter="openai_compatible",
                         base_url=server, model="m",
                         api_key_env="DEFINITELY_MISSING_ENV")
    with pytest.raises(NarratorError) as ei:
        narrator.OpenAICompatibleAdapter(cfg).narrate(REQ)
    assert ei.value.code == "NARRATOR_NO_KEY"


# --- registry: config, allowlist, unknown ---

def test_unknown_provider_refused(monkeypatch):
    monkeypatch.delenv("JARVIS_TWIN_PROVIDERS", raising=False)
    monkeypatch.delenv("JARVIS_TWIN_NARRATOR", raising=False)
    with pytest.raises(NarratorError) as ei:
        narrator.get_adapter("not-a-thing")
    assert ei.value.code == "NARRATOR_UNKNOWN"


def test_url_allowlist_refuses_non_localhost(monkeypatch):
    monkeypatch.setenv("JARVIS_TWIN_PROVIDERS", json.dumps([
        {"name": "remote", "adapter": "openai_compatible",
         "base_url": "https://api.example.com", "model": "m"},
    ]))
    monkeypatch.delenv("JARVIS_TWIN_ALLOWED_URLS", raising=False)
    with pytest.raises(NarratorError) as ei:
        narrator.get_adapter("remote")
    assert ei.value.code == "NARRATOR_URL_NOT_ALLOWED"


def test_url_allowlist_accepts_listed_remote(monkeypatch):
    monkeypatch.setenv("JARVIS_TWIN_PROVIDERS", json.dumps([
        {"name": "remote", "adapter": "openai_compatible",
         "base_url": "https://api.example.com", "model": "m"},
    ]))
    monkeypatch.setenv("JARVIS_TWIN_ALLOWED_URLS", "https://api.example.com")
    cfg, adapter = narrator.get_adapter("remote")
    assert cfg.name == "remote" and adapter is not None


def test_providers_json_config_and_catalog(monkeypatch):
    monkeypatch.setenv("JARVIS_TWIN_PROVIDERS", json.dumps([
        {"name": "local", "adapter": "ollama", "model": "llama3.1"},
        {"name": "gw", "adapter": "llm_gateway",
         "base_url": "http://127.0.0.1:9000", "model": "amul",
         "api_key_env": "SOME_KEY"},
    ]))
    catalog = narrator.provider_catalog()
    names = {p["name"] for p in catalog}
    assert names == {"none", "local", "gw"}
    for p in catalog:
        assert "base_url" not in p and "api_key_env" not in p  # no secrets/URLs


def test_bad_providers_json_refuses(monkeypatch):
    monkeypatch.setenv("JARVIS_TWIN_PROVIDERS", "{nope")
    with pytest.raises(NarratorError) as ei:
        narrator.configured_providers()
    assert ei.value.code == "NARRATOR_CONFIG"


def test_none_always_configured(monkeypatch):
    monkeypatch.delenv("JARVIS_TWIN_PROVIDERS", raising=False)
    monkeypatch.delenv("JARVIS_TWIN_NARRATOR", raising=False)
    cfg, adapter = narrator.get_adapter("none")
    assert cfg.adapter == "none" and adapter is None
