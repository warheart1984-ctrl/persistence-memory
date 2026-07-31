"""Optional API-key middleware tests."""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app.store import JarvisStore


@pytest.fixture()
def client_with_key(monkeypatch):
    monkeypatch.setenv("JARVIS_API_KEY", "test-secret-key")
    # Re-import app after env so middleware sees key on each request via getenv
    tmp = Path(tempfile.mktemp(suffix=".json"))
    store = JarvisStore(str(tmp))
    from app.main import app

    with patch("app.main.get_store", return_value=store):
        yield TestClient(app)


def _payload():
    return {
        "content": "Auth gate check",
        "source_agent": "test",
        "session_id": "sess-auth",
        "type": "fact",
        "confidence": 0.5,
        "status": "draft",
    }


def test_health_public_with_key_configured(client_with_key):
    resp = client_with_key.get("/health")
    assert resp.status_code == 200


def test_post_rejected_without_key(client_with_key):
    resp = client_with_key.post("/api/jarvis/memory", json=_payload())
    assert resp.status_code == 401


def test_post_accepted_with_bearer(client_with_key):
    resp = client_with_key.post(
        "/api/jarvis/memory",
        json=_payload(),
        headers={"Authorization": "Bearer test-secret-key"},
    )
    assert resp.status_code == 200
    assert resp.json()["memory"]["id"].startswith("mem-")


def test_get_accepted_with_x_api_key(client_with_key):
    created = client_with_key.post(
        "/api/jarvis/memory",
        json=_payload(),
        headers={"X-API-Key": "test-secret-key"},
    ).json()["memory"]
    resp = client_with_key.get(
        f"/api/jarvis/memory/{created['id']}",
        headers={"X-API-Key": "test-secret-key"},
    )
    assert resp.status_code == 200
