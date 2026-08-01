"""API-key middleware tests (required-by-default + local opt-out)."""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app.store import JarvisStore


def _payload():
    return {
        "content": "Auth gate check",
        "source_agent": "test",
        "session_id": "sess-auth",
        "type": "fact",
        "confidence": 0.5,
        "status": "draft",
    }


@pytest.fixture()
def client_with_key(monkeypatch):
    monkeypatch.setenv("JARVIS_API_KEY", "test-secret-key")
    monkeypatch.delenv("JARVIS_ALLOW_UNAUTHENTICATED", raising=False)
    tmp = Path(tempfile.mktemp(suffix=".json"))
    store = JarvisStore(str(tmp))
    from app.main import app

    with patch("app.main.get_store", return_value=store):
        yield TestClient(app)


@pytest.fixture()
def client_locked(monkeypatch):
    """Neither key nor opt-out — protected routes must 401."""
    monkeypatch.delenv("JARVIS_API_KEY", raising=False)
    monkeypatch.delenv("JARVIS_ALLOW_UNAUTHENTICATED", raising=False)
    tmp = Path(tempfile.mktemp(suffix=".json"))
    store = JarvisStore(str(tmp))
    from app.main import app

    with patch("app.main.get_store", return_value=store):
        yield TestClient(app)


@pytest.fixture()
def client_opt_out(monkeypatch):
    monkeypatch.delenv("JARVIS_API_KEY", raising=False)
    monkeypatch.setenv("JARVIS_ALLOW_UNAUTHENTICATED", "1")
    tmp = Path(tempfile.mktemp(suffix=".json"))
    store = JarvisStore(str(tmp))
    from app.main import app

    with patch("app.main.get_store", return_value=store):
        yield TestClient(app)


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


def test_default_rejects_without_key_or_opt_out(client_locked):
    resp = client_locked.post("/api/jarvis/memory", json=_payload())
    assert resp.status_code == 401
    detail = resp.json()["detail"]
    assert "JARVIS_API_KEY" in detail
    assert "JARVIS_ALLOW_UNAUTHENTICATED" in detail
    # Health remains public
    assert client_locked.get("/health").status_code == 200


def test_opt_out_allows_unauthenticated_local_dev(client_opt_out):
    resp = client_opt_out.post("/api/jarvis/memory", json=_payload())
    assert resp.status_code == 200
    assert resp.json()["memory"]["id"].startswith("mem-")
