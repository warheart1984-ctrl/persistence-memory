from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import app

_CALL = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "tools/call",
    "params": {"name": "emr_fetch", "arguments": {"id": "x"}},
}


@pytest.mark.parametrize("path", ["/mcp", "/mcp/"])
def test_oauth_mode_mcp_requires_token_on_every_path(monkeypatch, path):
    monkeypatch.setenv("JARVIS_AUTH_MODE", "oauth")
    monkeypatch.setenv("JARVIS_PUBLIC_BASE_URL", "https://memory.example")
    with TestClient(app) as client:
        response = client.post(path, json=_CALL)
    assert response.status_code == 401
    assert "resource_metadata" in response.headers["www-authenticate"]
