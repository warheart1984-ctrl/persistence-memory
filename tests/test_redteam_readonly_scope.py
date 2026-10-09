"""A memory.read-only OAuth token must not reach any write path."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import app.auth as auth
from app.identity import Principal
from app.main import app
from app.store import get_store

_REMEMBER = {
    "content": "read-only token must not write this",
    "session_id": "s1",
    "type": "fact",
    "user_requested": True,
}
_UPSERT = {"id": "mem_does_not_matter", "content": "read-only token must not upsert", "user_requested": True}


@pytest.fixture
def readonly_client(monkeypatch):
    monkeypatch.setenv("JARVIS_AUTH_MODE", "oauth")
    monkeypatch.setenv("JARVIS_PUBLIC_BASE_URL", "https://memory.example")
    monkeypatch.setenv("JARVIS_MEMORY_WRITE_ENABLED", "true")
    monkeypatch.setenv("JARVIS_MCP_WRITE_ENABLED", "true")

    def fake_validate(token: str, *, required_scope: str = "memory.read") -> Principal:
        return Principal(subject=token, scopes=frozenset({"memory.read"}), issuer="https://issuer.example")

    monkeypatch.setattr(auth, "validate_access_token", fake_validate)
    with TestClient(app) as client:
        client.headers["Authorization"] = "Bearer reader"
        yield client


@pytest.mark.parametrize(
    "path,body",
    [
        ("/api/jarvis/tools/emr_remember", _REMEMBER),
        ("/api/jarvis/tools/emr_upsert", _UPSERT),
        ("/api/jarvis/memory/amul/anchor", {"anchor_all": True}),
    ],
)
def test_readonly_token_cannot_post_write_routes(readonly_client, path, body):
    response = readonly_client.post(path, json=body)
    assert response.status_code == 403
    assert "memory.write" in response.json()["detail"]


def test_readonly_token_cannot_use_active_stm_get(readonly_client):
    response = readonly_client.get("/api/jarvis/memory/active", params={"query": "anything"})
    assert response.status_code == 403


@pytest.mark.parametrize("name,arguments", [("emr_remember", _REMEMBER), ("emr_upsert", _UPSERT)])
@pytest.mark.parametrize("path", ["/mcp", "/mcp/"])
def test_readonly_token_mcp_write_tools_are_refused(readonly_client, path, name, arguments):
    rpc = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": arguments}}
    response = readonly_client.post(path, json=rpc)
    assert response.status_code == 200
    result = response.json()["result"]
    assert result["isError"] is True
    assert "memory.write" in result["content"][0]["text"]
    # nothing was written for this tenant
    from app.identity import set_principal, reset_principal

    token = set_principal(Principal(subject="reader", scopes=frozenset({"memory.read"}), issuer="https://issuer.example"))
    try:
        assert get_store().list_memories(limit=10) == []
    finally:
        reset_principal(token)


def test_write_scope_token_still_allowed(monkeypatch, readonly_client):
    def fake_validate(token: str, *, required_scope: str = "memory.read") -> Principal:
        return Principal(subject=token, scopes=frozenset({"memory.read", "memory.write"}), issuer="https://issuer.example")

    monkeypatch.setattr(auth, "validate_access_token", fake_validate)
    response = readonly_client.post("/api/jarvis/tools/emr_remember", json=_REMEMBER)
    assert response.status_code == 200
    assert response.json().get("refused") is not True


def test_readonly_token_cannot_auto_promote_external_search(readonly_client, monkeypatch):
    import app.main as main_module

    calls: list[str] = []

    class FakeClient:
        def search(self, *args, **kwargs):
            calls.append("search")
            return {"content": [{"path": "p", "snippet": "s"}]}

        def promote_to_memory(self, item, source_agent, session_id, *rest):
            return {
                "content": "promoted by a read-only token",
                "source_agent": source_agent,
                "session_id": session_id,
                "type": "fact",
            }

    monkeypatch.setattr(main_module, "NxSearchClient", FakeClient)
    body = {"query": "anything", "auto_promote": True, "source_agent": "t", "session_id": "s"}
    response = readonly_client.post("/api/jarvis/memory/external-search", json=body)
    assert response.status_code == 403
    assert calls == []  # refused before any outbound search or write

    plain = readonly_client.post(
        "/api/jarvis/memory/external-search", json={**body, "auto_promote": False}
    )
    # nx-search reads the host filesystem with no per-tenant scoping, so even plain search is operator-only now
    assert plain.status_code == 403 and "operator-only" in plain.text
    assert calls == []
