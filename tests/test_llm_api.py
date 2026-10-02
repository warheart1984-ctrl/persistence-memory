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


# --- Memory recall into context ---------------------------------------------------


@pytest.fixture()
def recall_env(monkeypatch, tmp_path):
    """A fresh ledger behind the app, and a captured backend instead of the stub."""
    from unittest.mock import patch

    from app.models import MemoryCreate
    from app.store import JarvisStore

    store = JarvisStore(str(tmp_path / "store.json"))
    sent: list[list[dict]] = []

    def fake_backend(messages, temperature, max_tokens):
        sent.append(messages)
        return llm.GenerationResult(text="answer", model="m", backend="llm-gateway", tokens_used=5)

    monkeypatch.setattr(llm, "core_model_generate", fake_backend)

    def remember(content: str, subject: str | None = None) -> str:
        return store.create_memory(MemoryCreate(
            content=content, source_agent="test", session_id="s", type="fact",
            confidence=0.9, subject=subject,
        )).id

    with patch("app.main.get_store", return_value=store):
        yield remember, sent


def _context(messages: list[dict]) -> str:
    return "\n".join(m["content"] for m in messages if m["content"].startswith("Context:"))


def test_generate_recalls_matching_memory_into_context(recall_env):
    remember, sent = recall_env
    mem_id = remember("The gateway listens on port 8080 and routes to Groq.", subject="gateway")
    resp = client.post("/api/jarvis/llm/generate", json={
        "user": "Which port does the gateway listen on?", "context": "caller note",
    })
    assert resp.status_code == 200
    rec = resp.json()
    assert rec["recall"]["memory_ids"] == [mem_id]
    assert rec["recall"]["abstained"] is False
    context = _context(sent[0])
    assert f"[{mem_id}]" in context and "port 8080" in context
    assert context.index("port 8080") < context.index("caller note")  # recall goes first
    # R-B: the replay log names the recalled memories too.
    logged = [ln for ln in open(llm.LLM_LOG_PATH, encoding="utf-8") if ln.strip()]
    assert mem_id in logged[-1]


def test_generate_abstains_on_unrelated_query(recall_env):
    remember, sent = recall_env
    remember("The gateway listens on port 8080 and routes to Groq.")
    rec = client.post("/api/jarvis/llm/generate", json={"user": "favourite colour of bananas"}).json()
    assert rec["recall"]["memory_ids"] == []
    assert rec["recall"]["abstained"] is True
    assert _context(sent[0]) == ""


def test_generate_without_recall_leaves_context_alone(recall_env):
    remember, sent = recall_env
    remember("The gateway listens on port 8080 and routes to Groq.")
    rec = client.post("/api/jarvis/llm/generate", json={
        "user": "Which port does the gateway listen on?", "recall": False,
    }).json()
    assert "recall" not in rec
    assert _context(sent[0]) == ""


def test_generate_flags_unresolved_conflicts_without_choosing(recall_env):
    remember, sent = recall_env
    remember("The gateway listens on port 8080.", subject="gateway-port")
    remember("The gateway listens on port 9000.", subject="gateway-port")
    rec = client.post("/api/jarvis/llm/generate", json={
        "user": "Which port does the gateway listen on?", "subjects": ["gateway-port"],
    }).json()
    assert rec["recall"]["conflict_subjects"] == ["gateway-port"]
    assert "Unresolved conflicts are recorded for: gateway-port" in _context(sent[0])


def test_gateway_answer_with_context_counts_as_grounded(recall_env):
    remember, _ = recall_env
    remember("The gateway listens on port 8080 and routes to Groq.")
    rec = client.post("/api/jarvis/llm/generate", json={"user": "Which port does the gateway listen on?"}).json()
    # Same confidence an OpenAI-compatible backend gets for a grounded answer.
    assert rec["metadata"]["backend"] == "llm-gateway"
    assert rec["metadata"]["confidence"] == 0.85
