"""Twin state/providers/narration endpoint tests — flags, receipts, tenants."""

from __future__ import annotations

import json
import random
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.models import MemoryCreate, MemoryRecord
from app.narrator.gate import gate_narration
from app.narrator.template import template_narration
from app.store import get_store
from app.twin import FilteredRecords
from app.twin_state import build_twin_state

client = TestClient(app)
_NOW = datetime(2026, 3, 15, tzinfo=timezone.utc)


def _store_write(**kw):
    base = dict(content="x", source_agent="devin", session_id="s1",
                type="fact", confidence=0.5, status="draft")
    base.update(kw)
    return get_store().create_memory(MemoryCreate(**base))


def _flags(monkeypatch, twin="1", narrator_flag=None):
    monkeypatch.setenv("JARVIS_TWIN_ENABLED", twin)
    if narrator_flag is None:
        monkeypatch.delenv("JARVIS_TWIN_NARRATOR_ENABLED", raising=False)
    else:
        monkeypatch.setenv("JARVIS_TWIN_NARRATOR_ENABLED", narrator_flag)


# --- flags: dark by default ---

def test_all_three_404_when_twin_disabled(monkeypatch):
    _flags(monkeypatch, twin="0", narrator_flag="1")
    assert client.get("/api/jarvis/twin/state").status_code == 404
    assert client.get("/api/jarvis/twin/providers").status_code == 404
    assert client.get("/api/jarvis/twin/narration").status_code == 404


def test_narration_needs_both_flags(monkeypatch):
    _flags(monkeypatch, twin="1", narrator_flag="0")
    assert client.get("/api/jarvis/twin/state").status_code == 200
    assert client.get("/api/jarvis/twin/providers").status_code == 200
    assert client.get("/api/jarvis/twin/narration").status_code == 404


# --- /twin/state ---

def test_state_endpoint_returns_state(monkeypatch):
    _flags(monkeypatch)
    _store_write(content="seed fact", status="verified",
                 tags=["g1"], evidence=[{"kind": "r", "ref": "e"}])
    r = client.get("/api/jarvis/twin/state")
    assert r.status_code == 200
    s = r.json()["state"]
    assert s["schema"] == "TwinState.v1"
    assert len(s["state_digest"]) == 64
    assert s["record_count"] >= 1
    assert "g1" in s["active_projects"]


# --- /twin/providers ---

def test_providers_lists_none_and_never_exposes_urls_or_keys(monkeypatch):
    _flags(monkeypatch)
    monkeypatch.setenv("JARVIS_TWIN_PROVIDERS", json.dumps([
        {"name": "gw", "adapter": "llm_gateway",
         "base_url": "http://127.0.0.1:9000", "model": "amul",
         "api_key_env": "SUPER_SECRET_ENV"},
    ]))
    monkeypatch.setenv("SUPER_SECRET_ENV", "the-actual-secret")
    r = client.get("/api/jarvis/twin/providers")
    assert r.status_code == 200
    names = {p["name"] for p in r.json()["providers"]}
    assert {"none", "gw"} <= names
    blob = json.dumps(r.json())
    assert "9000" not in blob and "the-actual-secret" not in blob
    assert "SUPER_SECRET_ENV" not in blob  # not even the env var name


# --- /twin/narration ---

def test_narration_none_provider_returns_sections_and_receipt(monkeypatch):
    _flags(monkeypatch, narrator_flag="1")
    _store_write(content="seed fact", status="verified", tags=["g1"])
    r = client.get("/api/jarvis/twin/narration?provider=none")
    assert r.status_code == 200
    body = r.json()
    assert set(body["narration"]) == {
        "assessment", "opportunity", "risk", "next_action", "explanation"}
    rec = body["receipt"]
    for k in ("schema", "state_digest", "twin_input_digest", "provider",
              "model", "prompt_digest", "raw_output_digest",
              "final_output_digest", "dropped", "fallback_used", "latency_ms"):
        assert k in rec, k
    assert rec["schema"] == "TwinNarrationReceipt.v1"
    assert rec["provider"] == "none" and rec["fallback_used"] is False
    assert rec["state_digest"] == "sha256:" + body["state"]["state_digest"]


def test_narration_unknown_provider_400(monkeypatch):
    _flags(monkeypatch, narrator_flag="1")
    monkeypatch.delenv("JARVIS_TWIN_PROVIDERS", raising=False)
    r = client.get("/api/jarvis/twin/narration?provider=nope")
    assert r.status_code == 400 and r.json()["detail"] == "NARRATOR_UNKNOWN"


def test_narration_writes_nothing(monkeypatch):
    _flags(monkeypatch, narrator_flag="1")
    _store_write(content="seed")
    before = len(get_store().list_memories(limit=9999))
    client.get("/api/jarvis/twin/narration?provider=none")
    assert len(get_store().list_memories(limit=9999)) == before


def test_receipt_never_contains_api_key(monkeypatch):
    _flags(monkeypatch, narrator_flag="1")
    monkeypatch.setenv("JARVIS_TWIN_PROVIDERS", json.dumps([
        {"name": "local", "adapter": "ollama", "model": "llama3.1",
         "api_key_env": "NARR_KEY"},
    ]))
    monkeypatch.setenv("NARR_KEY", "super-secret-value-123")
    r = client.get("/api/jarvis/twin/narration?provider=local")
    assert r.status_code == 200
    assert "super-secret-value-123" not in json.dumps(r.json())


def test_narration_falls_back_when_adapter_fails(monkeypatch):
    """Provider that refuses -> template narration, receipt records fallback."""
    _flags(monkeypatch, narrator_flag="1")
    monkeypatch.setenv("JARVIS_TWIN_PROVIDERS", json.dumps([
        {"name": "dead", "adapter": "openai_compatible",
         "base_url": "http://127.0.0.1:1", "model": "m"},  # port 1: refused
    ]))
    r = client.get("/api/jarvis/twin/narration?provider=dead")
    assert r.status_code == 200
    body = r.json()
    # template sentences still render
    assert body["narration"]["assessment"][0]["template"] is True
    assert body["receipt"]["dropped"][0]["reason"].startswith("NARRATOR")


def test_model_unsupported_claim_dropped_and_marked(monkeypatch):
    """Fake model output with an invented number: gate drops, template fills."""
    _flags(monkeypatch, narrator_flag="1")
    _store_write(content="seed fact", status="verified", tags=["g1"])
    evil = json.dumps({"sections": {
        "assessment": [{"text": "Coverage index is 0.99 — everything is proven.",
                        "cites": ["coverage_index"]}],
        "opportunity": [], "risk": [], "next_action": [], "explanation": []}})

    class FakeAdapter:
        name = "fake"

        def narrate(self, req):
            from app.narrator.base import NarrationResponse
            return NarrationResponse(text=evil, provider="fake",
                                     model="fake-1", latency_ms=1)

    monkeypatch.setenv("JARVIS_TWIN_PROVIDERS", json.dumps([
        {"name": "fake", "adapter": "ollama", "model": "x"},
    ]))
    import app.narrator as narrator_mod
    monkeypatch.setattr(narrator_mod, "get_adapter",
                        lambda name: (type("C", (), {
                            "adapter": "ollama", "model": "x", "name": "fake"})(),
                            FakeAdapter()))
    r = client.get("/api/jarvis/twin/narration?provider=fake")
    assert r.status_code == 200
    body = r.json()
    joined = json.dumps(body["narration"])
    assert "0.99" not in joined and "proven" not in joined
    reasons = {d["reason"] for d in body["receipt"]["dropped"]}
    assert reasons  # the drop was recorded


# --- tenant isolation for state and narration ---

def test_twin_state_tenant_isolation(monkeypatch, tmp_path):
    from app.identity import Principal
    import app.auth as auth
    from app.store import reset_store_for_tests

    reset_store_for_tests()
    monkeypatch.setenv("JARVIS_AUTH_MODE", "oauth")
    monkeypatch.setenv("JARVIS_MEMORY_WRITE_ENABLED", "true")
    monkeypatch.setenv("JARVIS_STORE_PATH", str(tmp_path / "operator.json"))
    monkeypatch.setenv("JARVIS_TENANT_STORE_DIR", str(tmp_path / "tenants"))
    monkeypatch.setenv("JARVIS_TWIN_ENABLED", "1")
    monkeypatch.setenv("JARVIS_TWIN_NARRATOR_ENABLED", "1")

    def fake_validate(token: str, *, required_scope: str = "memory.read") -> Principal:
        return Principal(subject=token, issuer="https://issuer.example",
                         scopes=frozenset({"memory.read", "memory.write"}))

    monkeypatch.setattr(auth, "validate_access_token", fake_validate)
    with TestClient(app) as c:
        assert c.post("/api/jarvis/memory",
                      headers={"Authorization": "Bearer alice"},
                      json={"content": "alice only secret",
                            "source_agent": "devin", "session_id": "s",
                            "type": "fact", "status": "verified",
                            "tags": ["alice-proj"]}).status_code == 200
        a_state = c.get("/api/jarvis/twin/state",
                        headers={"Authorization": "Bearer alice"}).json()["state"]
        b_state = c.get("/api/jarvis/twin/state",
                        headers={"Authorization": "Bearer bob"}).json()["state"]
        a_narr = c.get("/api/jarvis/twin/narration?provider=none",
                       headers={"Authorization": "Bearer alice"}).json()
        b_narr = c.get("/api/jarvis/twin/narration?provider=none",
                       headers={"Authorization": "Bearer bob"}).json()

    assert "alice-proj" in a_state["active_projects"]
    assert b_state["record_count"] == 0
    assert b_state["active_projects"] == []
    assert a_state["state_digest"] != b_state["state_digest"]
    # narration is built over the same tenant-scoped state — bob sees nothing
    assert a_narr["state"]["record_count"] == 1
    assert b_narr["state"]["record_count"] == 0
    assert "alice only secret" not in json.dumps(b_narr)


# --- template narrator passes the gate on 500 random ledgers ---

def _rand_rec(rng: random.Random, i: int) -> MemoryRecord:
    types = ["fact", "task", "preference", "decision", "research"]
    statuses = ["draft", "verified", "archived"]
    return MemoryRecord(
        id=f"r{i}",
        content=rng.choice(["did thing", "prefers x", "todo item",
                            "assume the deploy", "risk: key rotation"]),
        created_at=f"2026-{rng.randint(1,3):02d}-{rng.randint(1,28):02d}T00:00:00Z",
        updated_at="2026-03-01T00:00:00Z",
        source_agent=rng.choice(["devin", "claude", "gpt"]),
        session_id="s", confidence=0.5,
        type=rng.choice(types), status=rng.choice(statuses),
        subject=rng.choice(["db", "deploy", "api", "", "ui"]),
        tags=[rng.choice(["risk", "todo", "security", "gate", "web"])
              for _ in range(rng.randint(0, 3))],
    )


def test_template_passes_gate_on_500_random_ledgers():
    """Property: template narration is gate-clean by construction."""
    rng = random.Random(2026)
    for run in range(500):
        n = rng.randint(0, 12)
        recs = [_rand_rec(rng, i) for i in range(n)]
        state = build_twin_state(FilteredRecords.from_records(recs),
                                 now=_NOW)
        sections = template_narration(state)
        out = gate_narration(json.dumps({"sections": sections}), state)
        assert out["dropped"] == [], f"run {run}: {out['dropped']}\n{sections}"
