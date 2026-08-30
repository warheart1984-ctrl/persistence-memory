from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app


def test_public_mode_requires_a_declared_host(monkeypatch):
    monkeypatch.setenv("JARVIS_PUBLIC_MODE", "true")
    monkeypatch.delenv("JARVIS_TRUSTED_HOSTS", raising=False)
    response = TestClient(app).get("/health", headers={"host": "memory.example"})
    assert response.status_code == 421


def test_public_mode_rejects_unconfigured_recall(monkeypatch):
    monkeypatch.setenv("JARVIS_PUBLIC_MODE", "true")
    monkeypatch.setenv("JARVIS_TRUSTED_HOSTS", "memory.example")
    monkeypatch.delenv("EMR_RECALL_API_KEY", raising=False)
    response = TestClient(app).post(
        "/api/jarvis/tools/emr_recall",
        headers={"host": "memory.example"},
        json={"intent": "code", "query": "test query"},
    )
    assert response.status_code == 503


def test_public_mode_sets_safe_response_headers(monkeypatch):
    monkeypatch.setenv("JARVIS_PUBLIC_MODE", "true")
    monkeypatch.setenv("JARVIS_TRUSTED_HOSTS", "memory.example")
    response = TestClient(app).get("/health", headers={"host": "memory.example"})
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
