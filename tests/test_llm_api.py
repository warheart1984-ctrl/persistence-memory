"""HTTP routes for AMUL LLM: /api/jarvis/llm/status and /api/jarvis/llm/generate.

conftest clears JARVIS_LLM_URL, so generation answers from the echo stub and
no network is touched; backend adapters are covered in test_amul_llm.py.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import app.amul_llm as llm
from app.main import app

client = TestClient(app)


def test_status_reports_backend_config():
    resp = client.get("/api/jarvis/llm/status")
    assert resp.status_code == 200
    backend = resp.json()["backend"]
    assert {"url", "api", "authenticated", "model_env", "fallback"} <= set(backend)


def test_generate_returns_replay_record_and_logs_it():
    resp = client.post("/api/jarvis/llm/generate", json={"user": "what is a continuity ledger?"})
    assert resp.status_code == 200
    rec = resp.json()
    assert rec["schema_version"] == "amul-llm-replay-v1"
    assert rec["metadata"]["model_version"] == "echo-stub-v0"  # no backend in tests
    assert "[echo-stub]" in rec["final_answer"]
    lines = [ln for ln in open(llm.LLM_LOG_PATH, encoding="utf-8") if ln.strip()]
    assert len(lines) == 1  # R-B: one replay record per generation


def test_generate_rejects_unknown_mode_with_400():
    resp = client.post("/api/jarvis/llm/generate", json={"user": "hi", "mode": "no_such_mode"})
    assert resp.status_code == 400
    assert "unknown mode override" in resp.json()["detail"]


def test_generate_validates_contract():
    assert client.post("/api/jarvis/llm/generate", json={"user": ""}).status_code == 422


@pytest.fixture()
def keyed_client(monkeypatch):
    monkeypatch.setenv("JARVIS_API_KEY", "test-secret-key")
    monkeypatch.delenv("JARVIS_ALLOW_UNAUTHENTICATED", raising=False)
    return TestClient(app)


def test_llm_routes_require_ledger_key(keyed_client):
    body = {"user": "hi"}
    assert keyed_client.get("/api/jarvis/llm/status").status_code == 401
    assert keyed_client.post("/api/jarvis/llm/generate", json=body).status_code == 401
    auth = {"Authorization": "Bearer test-secret-key"}
    assert keyed_client.get("/api/jarvis/llm/status", headers=auth).status_code == 200
    assert keyed_client.post("/api/jarvis/llm/generate", json=body, headers=auth).status_code == 200
