"""Stdio MCP → memoryboard HTTP integration (no mocks on the HTTP layer)."""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.main import app
from app.store import JarvisStore, reset_store_for_tests
from mcp_server.emr_stdio import handle_tools_call


def _fresh_client(monkeypatch) -> TestClient:
    reset_store_for_tests()
    tmp = Path(tempfile.mktemp(suffix=".json"))
    store = JarvisStore(str(tmp))
    monkeypatch.delenv("EMR_RECALL_API_KEY", raising=False)
    monkeypatch.setenv("JARVIS_STORE_PATH", str(tmp))
    patcher = patch("app.main.get_store", return_value=store)
    patcher.start()
    return TestClient(app), patcher


def test_openapi_registers_emr_tool_routes():
    paths = app.openapi()["paths"]
    for route in (
        "/api/jarvis/tools",
        "/api/jarvis/tools/emr_recall",
        "/api/jarvis/tools/search",
        "/api/jarvis/tools/fetch",
        "/api/jarvis/tools/emr_remember",
        "/api/jarvis/tools/emr_upsert",
    ):
        assert route in paths, f"missing route: {route}"


def test_stdio_tools_call_emr_recall_via_http(monkeypatch):
    client, patcher = _fresh_client(monkeypatch)
    try:

        def _http_post(path: str, arguments: dict) -> dict:
            resp = client.post(path, json=arguments)
            if resp.status_code >= 400:
                raise RuntimeError(
                    f"memoryboard HTTP {resp.status_code}: {resp.text}"
                )
            return resp.json()

        with patch("mcp_server.emr_stdio._http_post", _http_post):
            result = handle_tools_call(
                {
                    "name": "emr_recall",
                    "arguments": {"intent": "code", "query": "test recall"},
                }
            )
    finally:
        patcher.stop()

    assert result["isError"] is False
    assert result["structuredContent"]["protocol"] == "emr-recall-v1"
    assert "abstained" in result["structuredContent"]
