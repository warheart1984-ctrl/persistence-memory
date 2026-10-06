"""jarvisctl smoke writes one test record. It must satisfy the Clause V gate, or the smoke fails on a healthy ledger."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import app

SMOKE = Path(__file__).resolve().parents[1] / "deploy" / "mint" / "bin" / "smoke.sh"


def _smoke_payload() -> dict:
    text = SMOKE.read_text("utf-8")
    match = re.search(r"-d '(\{\"content\":\"smoke test record.*?\})' \"\$base/api/jarvis/memory\"", text)
    assert match, "could not find the smoke test's write payload in smoke.sh"
    return json.loads(match.group(1))


@pytest.fixture
def enforce(monkeypatch):
    monkeypatch.setenv("JARVIS_CLAUSE_V", "enforce")
    monkeypatch.setenv("JARVIS_CLAUSE_V_SOFT", "warn")


def test_the_smoke_write_is_a_decision_with_a_user_request_link():
    payload = _smoke_payload()
    assert payload["type"] == "decision"
    assert payload["evidence"] and payload["evidence"][0]["kind"] == "user-request"


def test_the_smoke_write_passes_the_clause_v_gate_and_can_be_deleted(enforce):
    client = TestClient(app)
    created = client.post("/api/jarvis/memory", json=_smoke_payload())
    assert created.status_code == 200 and "clause_v_warnings" not in created.json()
    memory_id = created.json()["memory"]["id"]
    assert client.delete(f"/api/jarvis/memory/{memory_id}").status_code == 200


def test_the_old_payload_would_have_been_refused(enforce):
    old = _smoke_payload() | {"type": "fact"}
    old.pop("evidence", None)
    resp = TestClient(app).post("/api/jarvis/memory", json=old)
    assert resp.status_code == 422 and resp.json()["code"] == "clause_v_violation"
