"""EMR_WRITER_API_KEY is a SCOPED credential: the seven read tools plus the two governed DRAFT writes (emr_remember, emr_upsert), and nothing else.

It exists so a second machine (a laptop, another assistant) can write memories without holding the ledger key. Every route the isolation matrix's registry knows is run with only the writer
key: it must reach exactly the read tool routes, the two write tool routes and the MCP transport, and be refused (401/403) everywhere else; a route added later is closed to it by default.
The writes still need JARVIS_MCP_WRITE_ENABLED and an explicit user_requested, and the recall key stays read-only even with a writer key configured. The ledger key keeps working on all of it.
"""

from __future__ import annotations

import json
import os
import pathlib
import secrets

import pytest
from fastapi.testclient import TestClient

from app import auth
from app.main import app
from app.store import get_store
from mcp_server.protocol import MCP_TOOLS, RECALL_READ_TOOLS, WRITER_TOOLS
from tests.test_recall_credential_boundary import (ALL_TOOLS, LEDGER, MCP_TRANSPORT, NO_DATA_MCP, READ_TOOL_ROUTES, READ_TOOLS, RECALL, _body, _concrete, _rpc, _tool_args,
                                                   world as _recall_world)
from tests.test_tenant_isolation import PUBLIC, REGISTRY

WRITER = "writer-key-" + secrets.token_urlsafe(24)
WRITE_TOOLS = {"emr_remember", "emr_upsert"}
EXPECTED = READ_TOOLS | WRITE_TOOLS                                                       # written out: changing WRITER_TOOLS must be a decision
WRITE_TOOL_ROUTES = {("POST", f"/api/jarvis/tools/{t}") for t in WRITE_TOOLS}
WRITER_FORMS = {"bearer": {"Authorization": f"Bearer {WRITER}"}, "x-emr-writer-key": {"X-EMR-Writer-Key": WRITER}}
USER_OK = {"user_requested": True, "user_statement": "the operator asked for this write"}
REMEMBER = {"content": "WRITER PROBE", "source_agent": "writer-probe", "session_id": "s", "type": "decision", **USER_OK}


@pytest.fixture
def world(_recall_world, monkeypatch):
    monkeypatch.setenv("EMR_WRITER_API_KEY", WRITER)
    return _recall_world


def contents():
    return sorted(m.content for m in get_store().list_memories(limit=99))


def test_the_writer_tool_set_is_the_one_written_down():
    assert WRITER_TOOLS == EXPECTED and RECALL_READ_TOOLS == READ_TOOLS
    assert WRITER_TOOLS <= {t["name"] for t in MCP_TOOLS} and "emr_drift_check" not in WRITER_TOOLS


@pytest.mark.parametrize("form", sorted(WRITER_FORMS))
def test_every_route_in_the_registry_refuses_the_writer_key_except_the_tools_it_may_use(world, form):
    client, _ = world
    headers = WRITER_FORMS[form]
    answered, wrong = [], []
    for (method, path), (cls, _) in sorted(REGISTRY.items()):
        if cls == PUBLIC or (method, path) in READ_TOOL_ROUTES | WRITE_TOOL_ROUTES | MCP_TRANSPORT:
            continue
        r = client.request(method, _concrete(path), headers=headers, json=_body(method))
        expected = NO_DATA_MCP.get((method, path))
        if expected is not None:
            if r.status_code != expected:
                wrong.append(f"{method} {path}: {r.status_code} (expected {expected})")
        elif r.status_code not in (401, 403):
            wrong.append(f"{method} {path}: {r.status_code} {r.text[:80]}")
        else:
            answered.append((method, path))
    assert not wrong, "the writer key reached routes it must not:\n" + "\n".join(wrong)
    assert len(answered) >= 80, "suspiciously few routes were checked: the registry did not load"


def test_the_registry_lists_every_route_the_writer_key_may_reach():
    for key in WRITE_TOOL_ROUTES:
        assert key in REGISTRY, f"{key} is allowed to the writer key but not in the isolation matrix's registry"


@pytest.mark.parametrize("form", sorted(WRITER_FORMS))
def test_the_writer_key_answers_each_read_tool_route(world, form):
    client, mem_id = world
    for tool, args in _tool_args(mem_id).items():
        r = client.post(f"/api/jarvis/tools/{tool}", headers=WRITER_FORMS[form], json=args)
        assert r.status_code == 200, (tool, r.status_code, r.text[:100])


def test_the_writer_key_writes_drafts_only_when_mcp_writes_are_on_and_only_with_user_intent(world, monkeypatch):
    client, _ = world
    h = WRITER_FORMS["bearer"]
    off = client.post("/api/jarvis/tools/emr_remember", headers=h, json=REMEMBER)
    assert off.status_code == 200 and off.json()["refused"] is True and "WRITER PROBE" not in contents()          # the flag is the deployment's switch; the writer key does not override it
    monkeypatch.setenv("JARVIS_MCP_WRITE_ENABLED", "true")
    no_intent = client.post("/api/jarvis/tools/emr_remember", headers=h, json={k: v for k, v in REMEMBER.items() if not k.startswith("user_")})
    assert no_intent.json()["refused"] is True and no_intent.json()["refuse_reason"] == "user-intent-required" and "WRITER PROBE" not in contents()
    ok = client.post("/api/jarvis/tools/emr_remember", headers=WRITER_FORMS["x-emr-writer-key"], json=REMEMBER)
    assert ok.status_code == 200 and ok.json()["accepted"] is True and ok.json()["memory"]["status"] == "draft"
    assert "WRITER PROBE" in contents()
    # the record is in the operator's ledger: the ledger key sees it
    assert any(m["content"] == "WRITER PROBE" for m in client.get("/api/jarvis/memory", headers={"X-API-Key": LEDGER}).json()["memories"])


@pytest.mark.parametrize("tool", ["emr_drift_check"])
def test_the_writer_key_is_refused_the_drift_tool_route_even_when_mcp_writes_are_on(world, monkeypatch, tool):
    client, mem_id = world
    monkeypatch.setenv("JARVIS_MCP_WRITE_ENABLED", "true")
    r = client.post(f"/api/jarvis/tools/{tool}", headers=WRITER_FORMS["bearer"], json={"summary": "boundary probe record", "records": [mem_id]})
    assert r.status_code in (401, 403)
    assert not list(pathlib.Path(os.environ["JARVIS_DRIFT_LOG_DIR"]).glob("*")), "a writer-only caller wrote to the drift log"


@pytest.mark.parametrize("form", sorted(WRITER_FORMS))
def test_over_mcp_the_writer_key_lists_exactly_its_tools(world, form):
    client, _ = world
    listed = {t["name"] for t in _rpc(client, WRITER_FORMS[form], "tools/list").json()["result"]["tools"]}
    assert listed == EXPECTED, sorted(listed ^ EXPECTED)
    assert {t["name"] for t in _rpc(client, {"X-API-Key": LEDGER}, "tools/list").json()["result"]["tools"]} == ALL_TOOLS      # the ledger key still lists everything


def test_over_mcp_the_writer_key_calls_its_tools_and_is_refused_everything_else(world, monkeypatch):
    client, mem_id = world
    monkeypatch.setenv("JARVIS_MCP_WRITE_ENABLED", "true")
    h = WRITER_FORMS["bearer"]
    for i, (tool, args) in enumerate(_tool_args(mem_id).items(), start=1):
        assert not _rpc(client, h, "tools/call", {"name": tool, "arguments": args}, i=i).json()["result"].get("isError"), tool
    res = _rpc(client, h, "tools/call", {"name": "emr_remember", "arguments": REMEMBER}).json()["result"]
    assert not res.get("isError") and json.loads(res["content"][0]["text"])["accepted"] is True and "WRITER PROBE" in contents()
    for tool in sorted(ALL_TOOLS - EXPECTED) + ["not_a_tool"]:
        res = _rpc(client, h, "tools/call", {"name": tool, "arguments": {"summary": "x", "records": [mem_id]}}).json()["result"]
        assert res["isError"] is True and res["structuredContent"]["error"]["reason"] == "writer-credential-scope", (tool, res)
    assert not list(pathlib.Path(os.environ["JARVIS_DRIFT_LOG_DIR"]).glob("*")), "a writer-only caller wrote to the drift log"


def test_the_writer_key_is_not_accepted_in_x_api_key_and_wrong_or_missing_keys_are_refused(world):
    client, _ = world
    for headers in ({}, {"X-API-Key": WRITER}, {"Authorization": "Bearer wrong"}, {"X-EMR-Writer-Key": "wrong"}):
        assert client.post("/api/jarvis/tools/emr_latest", headers=headers, json={}).status_code == 401, headers
        assert client.post("/api/jarvis/tools/emr_remember", headers=headers, json=REMEMBER).status_code == 401, headers
        assert _rpc(client, headers, "tools/list").status_code == 401, headers


def test_the_recall_key_stays_read_only_even_with_a_writer_key_configured(world, monkeypatch):
    client, _ = world
    monkeypatch.setenv("JARVIS_MCP_WRITE_ENABLED", "true")
    r = client.post("/api/jarvis/tools/emr_remember", headers={"Authorization": f"Bearer {RECALL}"}, json=REMEMBER)
    assert r.status_code == 401 and "WRITER PROBE" not in contents()
    res = _rpc(client, {"Authorization": f"Bearer {RECALL}"}, "tools/call", {"name": "emr_remember", "arguments": REMEMBER}).json()["result"]
    assert res["isError"] is True and res["structuredContent"]["error"]["reason"] == "recall-credential-read-only" and "WRITER PROBE" not in contents()
    assert {t["name"] for t in _rpc(client, {"Authorization": f"Bearer {RECALL}"}, "tools/list").json()["result"]["tools"]} == READ_TOOLS


def test_the_ledger_key_still_does_everything_and_wins_when_presented_with_the_writer_key(world, monkeypatch):
    client, mem_id = world
    monkeypatch.setenv("JARVIS_MCP_WRITE_ENABLED", "true")
    both = {"X-API-Key": LEDGER, "Authorization": f"Bearer {WRITER}"}
    assert client.get("/api/jarvis/tools/calls/verify", headers=both).status_code == 200
    assert client.post("/api/jarvis/drift/check", headers=both, json={"summary": "boundary probe record", "records": [mem_id]}).status_code == 200
    assert {t["name"] for t in _rpc(client, both, "tools/list").json()["result"]["tools"]} == ALL_TOOLS
    assert client.post("/api/jarvis/tools/emr_remember", headers={"X-API-Key": LEDGER}, json=REMEMBER).json()["accepted"] is True       # as before: installing a writer key breaks no ledger-key client


def test_a_writer_key_does_not_open_protected_ledger_reads(world, monkeypatch):
    client, _ = world
    monkeypatch.setenv("JARVIS_PROTECT_LEDGER_READ", "true")
    assert client.get("/api/jarvis/memory", headers={"X-API-Key": LEDGER}).status_code == 200
    for headers in WRITER_FORMS.values():
        assert client.get("/api/jarvis/memory", headers=headers).status_code in (401, 403)


# ----------------------------------------------------------------------------------------------------------------- configuration
@pytest.mark.parametrize("value,fragment", [("short", "shorter than"), (LEDGER, "JARVIS_API_KEY"), (RECALL, "EMR_RECALL_API_KEY")])
def test_a_weak_or_duplicated_writer_key_refuses_to_start(monkeypatch, value, fragment):
    monkeypatch.setenv("JARVIS_API_KEY", LEDGER)
    monkeypatch.setenv("EMR_RECALL_API_KEY", RECALL)
    monkeypatch.setenv("EMR_WRITER_API_KEY", value)
    assert any(fragment in p for p in auth.writer_key_problems())
    with pytest.raises(RuntimeError, match="EMR_WRITER_API_KEY refuses to start"):
        with TestClient(app):
            pass


def test_a_good_writer_key_and_no_writer_key_both_start(monkeypatch):
    monkeypatch.setenv("JARVIS_API_KEY", LEDGER)
    monkeypatch.setenv("EMR_RECALL_API_KEY", RECALL)
    monkeypatch.delenv("EMR_WRITER_API_KEY", raising=False)
    assert auth.writer_key_problems() == []
    monkeypatch.setenv("EMR_WRITER_API_KEY", WRITER)
    assert auth.writer_key_problems() == []
    with TestClient(app):
        pass


def test_the_writer_key_is_not_a_credential_on_a_public_deployment(world, monkeypatch):
    client, _ = world
    monkeypatch.setenv("JARVIS_PUBLIC_MODE", "1")
    monkeypatch.setenv("JARVIS_TRUSTED_HOSTS", "testserver")
    assert any("public deployment" in p for p in auth.writer_key_problems())
    for headers in WRITER_FORMS.values():
        assert client.post("/api/jarvis/tools/emr_latest", headers=headers, json={}).status_code == 401


# ------------------------------------------------------------------------------------------------------------------ stdio proxy
def test_the_stdio_proxy_picks_the_narrowest_key_per_tool(monkeypatch):
    from mcp_server import emr_stdio

    monkeypatch.delenv("JARVIS_API_KEY_FILE", raising=False)
    monkeypatch.setenv("EMR_RECALL_API_KEY", "the-recall-key")
    monkeypatch.setenv("EMR_WRITER_API_KEY", "the-writer-key")
    monkeypatch.setenv("JARVIS_API_KEY", "the-ledger-key")
    for tool in sorted(READ_TOOLS):
        assert emr_stdio.api_key(tool) == "the-recall-key", tool
    for tool in sorted(WRITE_TOOLS):
        assert emr_stdio.api_key(tool) == "the-writer-key", tool                     # least privilege: the writer key, not the ledger key
    assert emr_stdio.api_key("emr_drift_check") == "the-ledger-key"
    monkeypatch.delenv("JARVIS_API_KEY")
    monkeypatch.delenv("EMR_RECALL_API_KEY")                                            # a machine that holds ONLY the writer key (the laptop)
    for tool in sorted(EXPECTED):
        assert emr_stdio.api_key(tool) == "the-writer-key", tool
    monkeypatch.delenv("EMR_WRITER_API_KEY")
    monkeypatch.setenv("JARVIS_API_KEY", "the-ledger-key")
    assert emr_stdio.api_key("emr_remember") == "the-ledger-key"                    # unchanged without a writer key


def test_in_strict_mode_the_writer_key_is_still_a_request_identity_for_its_tools(world, monkeypatch):
    """Strict mode (JARVIS_MULTI_TENANT) never treats "no principal" as the operator, so a scoped key only works if the identity layer binds it (as it does the recall key).
    Without that binding the writer's tool calls would be refused as unauthenticated even though credential_kind accepts them."""
    client, mem_id = world
    monkeypatch.setenv("JARVIS_MULTI_TENANT", "1")
    monkeypatch.setenv("JARVIS_MCP_WRITE_ENABLED", "true")
    control = client.post("/api/jarvis/tools/emr_latest", headers={"Authorization": f"Bearer {RECALL}"}, json={})
    assert control.status_code == 200, "the recall key (the model for this behaviour) must work in strict mode"
    for form, headers in WRITER_FORMS.items():
        assert client.post("/api/jarvis/tools/emr_latest", headers=headers, json={}).status_code == 200, form
        assert client.post("/api/jarvis/tools/fetch", headers=headers, json={"id": mem_id}).status_code == 200, form
        assert _rpc(client, headers, "tools/list").status_code == 200, form
    w = client.post("/api/jarvis/tools/emr_remember", headers=WRITER_FORMS["bearer"], json={**REMEMBER, "content": "WRITER STRICT PROBE"})
    assert w.status_code == 200 and w.json()["accepted"] is True, w.text[:200]
    # and it is still only its tools
    assert client.get("/api/jarvis/tools/calls/verify", headers=WRITER_FORMS["bearer"]).status_code in (401, 403)
    assert client.get("/api/jarvis/memory", headers=WRITER_FORMS["bearer"]).status_code in (401, 403)
