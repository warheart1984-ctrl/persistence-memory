"""MCP clients get a generic message when the store is unavailable; details stay in the server log."""

from __future__ import annotations

import json
import logging

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.models import MemoryCreate
from app.store import JarvisStore

_TOOLS = [
    ("emr_recall", {"intent": "recall", "query": "anything at all"}),
    ("emr_fetch", {"id": "mem-x"}),
    ("search", {"query": "anything at all"}),
]


@pytest.mark.parametrize("path", ["/mcp", "/mcp/"])
@pytest.mark.parametrize("name,arguments", _TOOLS)
def test_mcp_tool_error_for_bad_store_is_generic(tmp_path, caplog, path, name, arguments):
    store_path = tmp_path / "jarvis-store.json"  # conftest points JARVIS_STORE_PATH here
    seeded = JarvisStore(str(store_path))
    record = seeded.create_memory(MemoryCreate(content="a record", source_agent="t", session_id="s", type="fact"))
    data = json.loads(store_path.read_text("utf-8"))
    data["memories"][0]["type"] = "BOGUS"
    store_path.write_text(json.dumps(data), "utf-8")

    rpc = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": arguments}}
    with caplog.at_level(logging.ERROR):
        with TestClient(app) as client:
            response = client.post(path, json=rpc)
    result = response.json()["result"]
    assert result["isError"] is True
    assert result["content"][0]["text"] == "Ledger store unavailable"
    assert record.id not in response.text
    assert record.id in caplog.text  # the detail is kept server-side


# The Postgres counterparts of these properties live in tests/test_pg_*.py (CHECK constraints,
# fail-closed on a database outage, generic MCP errors, history verification).
import pytest as _pytest_marker  # noqa: E402

pytestmark = _pytest_marker.mark.json_store_only
