"""The WSL rehearsal (deploy/mint/rehearse/scenario.sh) writes records through the API. Under Clause V those writes must
be gate-valid, or the rehearsal fails for the wrong reason (and the outage check would get a 422, not the 503 it proves).
The rehearsal itself needs WSL and is not run in CI; these tests read its real payloads out of the script."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import clause_v
from app.main import app

SCENARIO = Path(__file__).resolve().parents[1] / "deploy" / "mint" / "rehearse" / "scenario.sh"
TEXT = SCENARIO.read_text("utf-8")


def _mk_template() -> str:
    match = re.search(r"""^mk\(\) \{ api POST /api/jarvis/memory -d "\$\(printf '(\{.*?\})' "\$1" "\$2"\)"; \}$""", TEXT, re.M)
    assert match, "could not find mk() in scenario.sh"
    return match.group(1)


def _outage_payload() -> dict:
    match = re.search(r"""-d '(\{"content":"written during an outage".*?\})' "http""", TEXT)
    assert match, "could not find the outage write in scenario.sh"
    return json.loads(match.group(1))


@pytest.fixture
def enforce(monkeypatch):
    monkeypatch.setenv("JARVIS_CLAUSE_V", "enforce")
    monkeypatch.setenv("JARVIS_CLAUSE_V_SOFT", "warn")


def test_the_fixture_helper_writes_decisions_with_a_user_request_link():
    payload = json.loads(_mk_template() % ("rehearsal record number 1 about gardening and soil", "topic-1"))
    assert payload["type"] == "decision"
    assert payload["evidence"] and payload["evidence"][0]["kind"] == "user-request"


def test_the_fixture_helper_is_accepted_by_the_gate_and_the_rest_of_the_rehearsal_can_edit_it(enforce):
    client = TestClient(app)
    payload = json.loads(_mk_template() % ("second wave record 1 about irrigation", "topic-irrigation"))
    created = client.post("/api/jarvis/memory", json=payload)
    assert created.status_code == 200 and "clause_v_warnings" not in created.json()
    record = created.json()["memory"]
    # the edits the scenario makes to its fixtures (confidence, subject) must still go through
    assert client.patch(f"/api/jarvis/memory/{record['id']}", json={"confidence": 0.9, "expected_version": 1}).status_code == 200
    assert client.patch(f"/api/jarvis/memory/{record['id']}", json={"subject": "revised"}).status_code == 200
    assert client.patch(f"/api/jarvis/memory/{record['id']}", json={"confidence": 0.1, "expected_version": 1}).status_code == 409


def test_the_outage_write_is_gate_valid_so_it_reaches_the_database_and_proves_the_503(enforce):
    payload = _outage_payload()
    assert clause_v.hard_reasons(type=payload["type"], evidence=payload["evidence"]) == []
    assert TestClient(app).post("/api/jarvis/memory", json=payload).status_code == 200  # nothing but a dead database can stop it


def test_the_old_outage_write_would_have_been_refused_by_the_gate_before_reaching_the_database():
    old = {"type": "fact", "evidence": []}
    assert [r.code for r in clause_v.hard_reasons(**old)] == ["clause_v_evidence_required"]


def test_no_other_write_in_the_scenario_uses_a_type_the_gate_refuses():
    assert not re.findall(r'"type":\s*"(fact|preference|task|external_context)"', TEXT)
