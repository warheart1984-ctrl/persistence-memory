"""Alice's STM and AMUL lineage must be invisible to Bob."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import app.auth as auth
from app.identity import Principal
from app.main import app


@pytest.fixture
def oauth_client(monkeypatch):
    monkeypatch.setenv("JARVIS_AUTH_MODE", "oauth")
    monkeypatch.setenv("JARVIS_PUBLIC_BASE_URL", "https://memory.example")
    monkeypatch.setenv("JARVIS_MEMORY_WRITE_ENABLED", "true")

    def fake_validate(token: str, *, required_scope: str = "memory.read") -> Principal:
        return Principal(
            subject=token,
            scopes=frozenset({"memory.read", "memory.write"}),
            issuer="https://issuer.example",
        )

    monkeypatch.setattr(auth, "validate_access_token", fake_validate)
    with TestClient(app) as client:
        yield client


def _h(who: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {who}"}


def _alice_setup(client) -> str:
    created = client.post(
        "/api/jarvis/memory",
        headers=_h("alice"),
        json={
            "content": "alice private launch codename is bluebird",
            "source_agent": "t",
            "session_id": "s",
            "type": "fact",
        },
    )
    assert created.status_code == 200, created.text
    memory_id = created.json()["memory"]["id"]
    excite = client.post(
        "/api/jarvis/memory/emr/excite",
        headers=_h("alice"),
        json={"query": "bluebird launch codename", "session_key": "default", "theta_promote": 0.0},
    )
    assert excite.status_code == 200, excite.text
    return memory_id


def test_alice_stm_is_not_visible_to_bob(oauth_client):
    memory_id = _alice_setup(oauth_client)
    own = oauth_client.get("/api/jarvis/memory/stm/context", headers=_h("alice"))
    assert memory_id in own.text  # sanity: alice does see her own STM

    ctx = oauth_client.get("/api/jarvis/memory/stm/context", headers=_h("bob"))
    stm = oauth_client.get("/api/jarvis/memory/stm", headers=_h("bob"))
    status = oauth_client.get("/api/jarvis/memory/emr/status", headers=_h("bob"))
    assert memory_id not in ctx.text and "bluebird" not in ctx.text
    assert stm.json()["count"] == 0
    assert status.json()["sessions"] == []


def test_bob_stm_clear_does_not_wipe_alice(oauth_client):
    memory_id = _alice_setup(oauth_client)
    assert oauth_client.delete("/api/jarvis/memory/stm", headers=_h("bob")).status_code == 200
    own = oauth_client.get("/api/jarvis/memory/stm/context", headers=_h("alice"))
    assert memory_id in own.text


def test_alice_amul_lineage_is_not_visible_to_bob(oauth_client):
    memory_id = _alice_setup(oauth_client)
    anchored = oauth_client.post(
        "/api/jarvis/memory/amul/anchor", headers=_h("alice"), json={"memory_id": memory_id}
    )
    assert anchored.status_code == 200, anchored.text
    assert oauth_client.get(f"/api/jarvis/memory/amul/lineage/{memory_id}", headers=_h("alice")).status_code == 200

    lineage = oauth_client.get(f"/api/jarvis/memory/amul/lineage/{memory_id}", headers=_h("bob"))
    field = oauth_client.get("/api/jarvis/memory/amul/field/status", headers=_h("bob"))
    assert lineage.status_code == 404
    assert field.json()["artifact_count"] == 0
