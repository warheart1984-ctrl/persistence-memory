"""Operator mode: tools and /mcp must not be open just because only JARVIS_API_KEY is set."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.emr_write import EmrUpsertRequest, emr_upsert
from app.main import app
from app.models import MemoryCreate
from app.store import get_store

_FETCH = {"id": "mem-nope"}
_RPC = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "emr_fetch", "arguments": _FETCH}}
_ROUTES = [
    ("/api/jarvis/tools/fetch", _FETCH),
    ("/mcp", _RPC),
    ("/mcp/", _RPC),
]


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture
def locked_down(monkeypatch):
    monkeypatch.delenv("JARVIS_ALLOW_UNAUTHENTICATED", raising=False)
    monkeypatch.delenv("JARVIS_API_KEY", raising=False)
    monkeypatch.delenv("EMR_RECALL_API_KEY", raising=False)
    monkeypatch.delenv("JARVIS_AUTH_MODE", raising=False)


@pytest.mark.parametrize("path,body", _ROUTES)
def test_no_key_configured_and_no_opt_out_is_401(client, locked_down, path, body):
    assert client.post(path, json=body).status_code == 401


@pytest.mark.parametrize("path,body", _ROUTES)
def test_only_jarvis_api_key_set_requires_it(client, locked_down, monkeypatch, path, body):
    monkeypatch.setenv("JARVIS_API_KEY", "ledger-key")
    assert client.post(path, json=body).status_code == 401
    assert client.post(path, json=body, headers={"X-API-Key": "wrong"}).status_code == 401
    ok = client.post(path, json=body, headers={"Authorization": "Bearer ledger-key"})
    assert ok.status_code != 401
    ok = client.post(path, json=body, headers={"X-API-Key": "ledger-key"})
    assert ok.status_code != 401


@pytest.mark.parametrize("path,body", _ROUTES)
def test_explicit_unauthenticated_opt_out_still_works(client, locked_down, monkeypatch, path, body):
    monkeypatch.setenv("JARVIS_ALLOW_UNAUTHENTICATED", "1")
    assert client.post(path, json=body).status_code != 401


def test_recall_key_still_governs_when_set(client, locked_down, monkeypatch):
    monkeypatch.setenv("EMR_RECALL_API_KEY", "recall-key")
    assert client.post("/api/jarvis/tools/fetch", json=_FETCH).status_code == 401
    ok = client.post("/api/jarvis/tools/fetch", json=_FETCH, headers={"Authorization": "Bearer recall-key"})
    assert ok.status_code != 401


def test_tool_catalog_stays_public(client, locked_down):
    assert client.get("/api/jarvis/tools").status_code == 200


def test_upsert_refuses_verified_target(monkeypatch):
    monkeypatch.setenv("JARVIS_MCP_WRITE_ENABLED", "true")
    store = get_store()
    verified = store.create_memory(
        MemoryCreate(
            content="verified ledger truth that a draft write must not archive",
            source_agent="t",
            session_id="s",
            type="fact",
            status="verified",
        )
    )
    resp = emr_upsert(
        store,
        EmrUpsertRequest(id=verified.id, content="a draft replacement of verified truth", user_requested=True),
    )
    assert resp.accepted is False
    assert store.get_memory(verified.id).status == "verified"
    assert [m.id for m in store.list_memories(limit=10)] == [verified.id]
