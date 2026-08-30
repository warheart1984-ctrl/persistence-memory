from __future__ import annotations

from fastapi.testclient import TestClient

import app.auth as auth
from app.identity import Principal
from app.main import app
from app.store import reset_store_for_tests


def _principal(subject: str) -> Principal:
    return Principal(subject=subject, scopes=frozenset({"memory.read", "memory.write"}), issuer="https://issuer.example")


def _memory(content: str) -> dict[str, object]:
    return {
        "content": content,
        "source_agent": "test",
        "session_id": "session-1",
        "type": "fact",
    }


def test_oauth_metadata_advertises_canonical_resource(monkeypatch):
    monkeypatch.setenv("JARVIS_PUBLIC_BASE_URL", "https://memory.example")
    monkeypatch.setenv("JARVIS_OIDC_ISSUER", "https://issuer.example")
    with TestClient(app) as client:
        response = client.get("/.well-known/oauth-protected-resource/mcp")
    assert response.status_code == 200
    assert response.json()["resource"] == "https://memory.example"
    assert response.json()["authorization_servers"] == ["https://issuer.example"]
    assert response.json()["scopes_supported"] == ["memory.read", "memory.write"]


def test_oauth_middleware_challenges_missing_token(monkeypatch):
    monkeypatch.setenv("JARVIS_AUTH_MODE", "oauth")
    monkeypatch.setenv("JARVIS_PUBLIC_BASE_URL", "https://memory.example")
    with TestClient(app) as client:
        response = client.get("/api/jarvis/memory")
    assert response.status_code == 401
    assert "resource_metadata" in response.headers["www-authenticate"]


def test_oauth_subjects_use_isolated_ledgers(monkeypatch, tmp_path):
    reset_store_for_tests()
    monkeypatch.setenv("JARVIS_AUTH_MODE", "oauth")
    monkeypatch.setenv("JARVIS_MEMORY_WRITE_ENABLED", "true")
    monkeypatch.setenv("JARVIS_STORE_PATH", str(tmp_path / "operator.json"))
    monkeypatch.setenv("JARVIS_TENANT_STORE_DIR", str(tmp_path / "tenants"))

    def fake_validate(token: str, *, required_scope: str = "memory.read") -> Principal:
        return _principal(token)

    monkeypatch.setattr(auth, "validate_access_token", fake_validate)
    with TestClient(app) as client:
        first = client.post("/api/jarvis/memory", headers={"Authorization": "Bearer alice"}, json=_memory("alice only"))
        assert first.status_code == 200
        alice = client.get("/api/jarvis/memory", headers={"Authorization": "Bearer alice"})
        bob = client.get("/api/jarvis/memory", headers={"Authorization": "Bearer bob"})
    assert [row["content"] for row in alice.json()["memories"]] == ["alice only"]
    assert bob.json()["memories"] == []
