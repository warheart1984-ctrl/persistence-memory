"""The stdio proxy must present a key the server will accept."""

from __future__ import annotations

import io
import json
from unittest.mock import patch

import pytest

from mcp_server.emr_stdio import _http_post


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _sent_headers(monkeypatch, *, recall=None, jarvis=None) -> dict[str, str]:
    monkeypatch.setenv("JARVIS_MEMORYBOARD_URL", "http://127.0.0.1:8011")
    for name, value in (("EMR_RECALL_API_KEY", recall), ("JARVIS_API_KEY", jarvis)):
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured.update({k.lower(): v for k, v in req.header_items()})
        return _Resp(json.dumps({"ok": True}).encode())

    with patch("urllib.request.urlopen", fake_urlopen):
        _http_post("/api/jarvis/tools/emr_recall", {"query": "x"})
    return captured


def test_falls_back_to_jarvis_api_key(monkeypatch):
    assert _sent_headers(monkeypatch, jarvis="ledger-key")["authorization"] == "Bearer ledger-key"


def test_recall_key_takes_precedence(monkeypatch):
    headers = _sent_headers(monkeypatch, recall="recall-key", jarvis="ledger-key")
    assert headers["authorization"] == "Bearer recall-key"


def test_no_key_sends_no_authorization(monkeypatch):
    assert "authorization" not in _sent_headers(monkeypatch)


def test_without_a_url_nothing_is_sent_and_the_key_is_not_used(monkeypatch):
    monkeypatch.delenv("JARVIS_MEMORYBOARD_URL", raising=False)
    monkeypatch.setenv("JARVIS_API_KEY", "must-not-leak")
    attempted = []
    with patch("urllib.request.urlopen", lambda *a, **k: attempted.append(True)):
        with pytest.raises(RuntimeError, match="JARVIS_MEMORYBOARD_URL is not set") as exc:
            _http_post("/api/jarvis/tools/emr_recall", {"query": "x"})
    assert attempted == [] and "must-not-leak" not in str(exc.value)


def test_the_key_is_never_sent_over_plain_http_to_a_non_loopback_host(monkeypatch):
    monkeypatch.setenv("JARVIS_MEMORYBOARD_URL", "http://192.0.2.7:8011")
    monkeypatch.setenv("JARVIS_API_KEY", "must-not-leak")
    attempted = []
    with patch("urllib.request.urlopen", lambda *a, **k: attempted.append(True)):
        with pytest.raises(RuntimeError, match="plain http"):
            _http_post("/api/jarvis/tools/emr_recall", {"query": "x"})
    assert attempted == []
