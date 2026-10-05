"""Three distinct refusals - ledger unavailable (503), version conflict (409), denied (401/403) -
each with its own machine-readable code, and only 503 tells clients when to come back."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import app.auth as auth
import app.main as main_module
from app.identity import Principal
from app.main import app
from app.refusal import DENIED, LEDGER_UNAVAILABLE, UNAVAILABLE, VERSION_CONFLICT
from app.store_errors import StoreUnavailableError, StoreVersionConflict

_BODY = {"content": "a record for the contract tests", "source_agent": "t", "session_id": "s", "type": "fact"}


class _BrokenStore:
    """A store that fails the way a dead database does; its message must never reach a client."""

    def __getattr__(self, name):
        def fail(*args, **kwargs):
            raise StoreUnavailableError("secret-host:5432 connection refused for user secretuser")

        return fail


@pytest.fixture
def client():
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


def test_the_codes_are_distinct_stable_strings():
    assert (LEDGER_UNAVAILABLE, VERSION_CONFLICT, DENIED, UNAVAILABLE) == (
        "ledger_unavailable", "version_conflict", "denied", "unavailable",
    )


# --- 503: ledger unavailable ---------------------------------------------------------------------


def test_unavailable_is_503_with_code_generic_body_and_retry_after(client, monkeypatch):
    monkeypatch.setattr(main_module, "get_store", lambda *a, **k: _BrokenStore())
    response = client.get("/api/jarvis/memory")
    assert response.status_code == 503
    assert response.json() == {"detail": "Ledger store unavailable", "code": LEDGER_UNAVAILABLE}
    assert response.headers["retry-after"] == "5"
    assert "secret" not in response.text


def test_retry_after_is_configurable_and_bad_values_fall_back(client, monkeypatch):
    monkeypatch.setattr(main_module, "get_store", lambda *a, **k: _BrokenStore())
    monkeypatch.setenv("JARVIS_RETRY_AFTER_SECONDS", "17")
    assert client.get("/api/jarvis/memory").headers["retry-after"] == "17"
    for bad in ("soon", "-3", "0", "999999"):
        monkeypatch.setenv("JARVIS_RETRY_AFTER_SECONDS", bad)
        assert client.get("/api/jarvis/memory").headers["retry-after"] == "5"


def test_other_503s_also_say_when_to_retry_but_are_not_ledger_errors(client, monkeypatch):
    monkeypatch.setenv("JARVIS_PUBLIC_MODE", "true")
    monkeypatch.setenv("JARVIS_TRUSTED_HOSTS", "memory.example")
    monkeypatch.delenv("EMR_RECALL_API_KEY", raising=False)
    response = client.post(
        "/api/jarvis/tools/emr_recall", headers={"host": "memory.example"},
        json={"intent": "code", "query": "test query"},
    )
    assert response.status_code == 503
    assert response.json()["code"] == UNAVAILABLE and "retry-after" in response.headers


# --- 409: version conflict ---------------------------------------------------------------------------


def test_version_conflict_is_409_with_its_own_code_and_no_retry_after(client):
    mem = client.post("/api/jarvis/memory", json=_BODY).json()["memory"]
    client.patch(f"/api/jarvis/memory/{mem['id']}", json={"subject": "first", "expected_version": 1})
    stale = client.patch(f"/api/jarvis/memory/{mem['id']}", json={"subject": "second", "expected_version": 1})
    assert stale.status_code == 409
    assert stale.json()["code"] == VERSION_CONFLICT
    assert "retry-after" not in stale.headers  # re-read and decide; do not blindly retry


def test_a_conflict_raised_anywhere_maps_to_409(client, monkeypatch):
    def boom(*a, **k):
        raise StoreVersionConflict("version conflict: too much concurrent modification, retry")

    monkeypatch.setattr(main_module, "emr_upsert", boom)
    rpc = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
        "name": "emr_upsert", "arguments": {"id": "mem-x", "content": "replacement text here", "user_requested": True}}}
    monkeypatch.setenv("JARVIS_MCP_WRITE_ENABLED", "true")
    result = client.post("/mcp", json=rpc).json()["result"]
    assert result["isError"] is True and result["structuredContent"]["error"]["code"] == VERSION_CONFLICT


# --- 401/403: denied --------------------------------------------------------------------------------------


def test_missing_or_wrong_api_key_is_denied(client, monkeypatch):
    monkeypatch.delenv("JARVIS_ALLOW_UNAUTHENTICATED", raising=False)
    monkeypatch.setenv("JARVIS_API_KEY", "right-key")
    for headers in ({}, {"X-API-Key": "wrong"}):
        response = client.post("/api/jarvis/memory", json=_BODY, headers=headers)
        assert response.status_code == 401
        assert response.json()["code"] == DENIED and "retry-after" not in response.headers


def test_write_gate_refusal_is_403_denied(client, monkeypatch):
    monkeypatch.setenv("JARVIS_MEMORY_WRITE_ENABLED", "false")
    response = client.post("/api/jarvis/memory", json=_BODY)
    assert response.status_code == 403 and response.json()["code"] == DENIED
    assert "retry-after" not in response.headers


def test_oauth_refusals_are_denied_and_keep_their_challenge_header(monkeypatch):
    monkeypatch.setenv("JARVIS_AUTH_MODE", "oauth")
    monkeypatch.setenv("JARVIS_PUBLIC_BASE_URL", "https://memory.example")
    monkeypatch.setenv("JARVIS_MEMORY_WRITE_ENABLED", "true")
    monkeypatch.setattr(
        auth, "validate_access_token",
        lambda token, *, required_scope="memory.read": Principal(
            subject=token, scopes=frozenset({"memory.read"}), issuer="https://issuer.example"),
    )
    with TestClient(app, raise_server_exceptions=False) as c:
        no_token = c.get("/api/jarvis/memory")
        assert no_token.status_code == 401 and no_token.json()["code"] == DENIED
        assert "resource_metadata" in no_token.headers["www-authenticate"]
        read_only = c.post("/api/jarvis/memory", json=_BODY, headers={"Authorization": "Bearer reader"})
        assert read_only.status_code == 403 and read_only.json()["code"] == DENIED


def test_mcp_tool_denials_carry_the_denied_code(monkeypatch):
    monkeypatch.setenv("JARVIS_AUTH_MODE", "oauth")
    monkeypatch.setenv("JARVIS_PUBLIC_BASE_URL", "https://memory.example")
    monkeypatch.setenv("JARVIS_MCP_WRITE_ENABLED", "true")
    monkeypatch.setattr(
        auth, "validate_access_token",
        lambda token, *, required_scope="memory.read": Principal(
            subject=token, scopes=frozenset({"memory.read"}), issuer="https://issuer.example"),
    )
    rpc = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
        "name": "emr_remember", "arguments": {"content": "a read-only token tries to write", "session_id": "s",
                                               "type": "fact", "user_requested": True}}}
    with TestClient(app, raise_server_exceptions=False) as c:
        result = c.post("/mcp", json=rpc, headers={"Authorization": "Bearer reader"}).json()["result"]
    assert result["isError"] is True and result["structuredContent"]["error"]["code"] == DENIED
    assert "memory.write" in result["content"][0]["text"]


def test_mcp_unavailable_has_its_code_and_a_generic_message(client, monkeypatch):
    monkeypatch.setattr(main_module, "get_store", lambda *a, **k: _BrokenStore())
    rpc = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "emr_fetch", "arguments": {"id": "m"}}}
    result = client.post("/mcp", json=rpc).json()["result"]
    assert result["isError"] is True and result["content"][0]["text"] == "Ledger store unavailable"
    assert result["structuredContent"]["error"]["code"] == LEDGER_UNAVAILABLE
    assert "secret" not in str(result)


# --- everything else is left alone ----------------------------------------------------------------------------


def test_ordinary_errors_keep_their_shape(client):
    missing = client.get("/api/jarvis/memory/mem-does-not-exist")
    assert missing.status_code == 404 and "code" not in missing.json()
    invalid = client.post("/api/jarvis/memory", json={"content": ""})
    assert invalid.status_code == 422 and "code" not in invalid.json()
