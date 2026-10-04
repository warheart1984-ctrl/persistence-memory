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
