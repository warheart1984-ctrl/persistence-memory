"""The shared inference backend is operator-only (RFC phase 0): key mode keeps working for the operator exactly as before, and a tenant never reaches it.

The tenant side of this is pinned in tests/test_tenant_isolation.py (the route registry classifies both routes operator-only, and the old strict xfails are plain
tests). This file is the OPERATOR side: the gate must not take anything away from the shared-key deployment that runs today.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import amul_llm
from app.main import app
from app.store import reset_store_for_tests

KEY = "inference-operator-key-0123456789"


@pytest.fixture
def operator(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_API_KEY", KEY)
    monkeypatch.delenv("JARVIS_ALLOW_UNAUTHENTICATED", raising=False)
    monkeypatch.delenv("EMR_RECALL_API_KEY", raising=False)
    monkeypatch.delenv("JARVIS_MULTI_TENANT", raising=False)
    monkeypatch.delenv("JARVIS_AUTH_MODE", raising=False)
    monkeypatch.setenv("JARVIS_STORE_PATH", str(tmp_path / "s.json"))
    monkeypatch.setenv("JARVIS_CURSOR_HMAC_KEY", "inference-operator-cursor-key-0123456789")
    monkeypatch.setattr(amul_llm, "LLM_URL", "http://inference.internal.example:13305/api/v1")
    monkeypatch.setattr(amul_llm, "LLM_LOG_PATH", str(tmp_path / "llm-log.jsonl"))
    reset_store_for_tests()
    return TestClient(app), {"X-API-Key": KEY}


def test_the_operator_key_still_reads_the_inference_status_in_full(operator):
    client, headers = operator
    r = client.get("/api/jarvis/llm/status", headers=headers)
    assert r.status_code == 200
    body = r.json()
    assert body["backend"]["url"] == "http://inference.internal.example:13305/api/v1" and body["replay_log"]["path"].endswith("llm-log.jsonl")


def test_the_operator_key_still_generates(operator):
    client, headers = operator
    r = client.post("/api/jarvis/llm/generate", headers=headers, json={"user": "hello", "recall": False})
    assert r.status_code == 200, r.text
    assert r.json()["metadata"]["model_version"]                                    # the replay record, as before (the backend is unreachable here: the echo stub answers)


def test_no_credential_is_still_refused_before_the_gate(operator):
    client, _ = operator
    assert client.get("/api/jarvis/llm/status").status_code == 401
    assert client.post("/api/jarvis/llm/generate", json={"user": "hello"}).status_code == 401


def test_the_operator_still_sees_the_amul_field_directory(operator):
    client, headers = operator
    r = client.get("/api/jarvis/memory/amul/field/status", headers=headers)
    assert r.status_code == 200 and r.json()["path"]
