"""EMR_RECALL_API_KEY is a SCOPED credential: it reads through the governed recall tools and nothing else.

Every route the isolation matrix's registry knows is run with ONLY the recall key. It must reach exactly the seven read tools (and the MCP transport that carries
them) and be refused (401/403) everywhere else, so a route added later is closed to it by default and this list can only grow on purpose.  The ledger key keeps
working on all of it, so installing a recall key breaks no existing client.
"""

from __future__ import annotations

import json
import secrets

import pytest
from fastapi.testclient import TestClient

from app import amul_rag
from app.main import app
from app.store import get_store, reset_store_for_tests
from mcp_server.protocol import MCP_TOOLS, RECALL_READ_TOOLS
from tests.test_tenant_isolation import GHOST, GHOST_ARTIFACT, PUBLIC, REGISTRY

LEDGER = "ledger-key-" + secrets.token_urlsafe(18)
RECALL = "recall-key-" + secrets.token_urlsafe(18)
READ_TOOLS = {"emr_recall", "emr_latest", "emr_search_ledger", "search", "fetch", "emr_search", "emr_fetch"}      # written out: changing RECALL_READ_TOOLS must be a decision
ALL_TOOLS = {t["name"] for t in MCP_TOOLS}
# What a recall-only caller may reach, by method+path. Every other non-public route must answer it 401/403.
READ_TOOL_ROUTES = {("POST", f"/api/jarvis/tools/{t}") for t in READ_TOOLS}
MCP_TRANSPORT = {("POST", "/mcp"), ("POST", "/mcp/")}                           # authenticates itself; what it will run is narrowed per call (tests below)
NO_DATA_MCP = {("GET", "/mcp"): 405, ("GET", "/mcp/"): 405, ("DELETE", "/mcp"): 204, ("DELETE", "/mcp/"): 204,       # answer without auth, carry nothing (405 / end-session)
               ("GET", "/api/jarvis/tools"): 200}                                                                        # the tool catalog: the same for every caller, credential or none (tested below)


@pytest.fixture
def world(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_API_KEY", LEDGER)
    monkeypatch.setenv("EMR_RECALL_API_KEY", RECALL)
    for k in ("JARVIS_ALLOW_UNAUTHENTICATED", "JARVIS_MULTI_TENANT", "JARVIS_AUTH_MODE"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("JARVIS_STORE_BOOTSTRAP", "1")
    monkeypatch.setenv("JARVIS_STORE_PATH", str(tmp_path / "s.json"))
    monkeypatch.setenv("JARVIS_CURSOR_HMAC_KEY", "recall-boundary-cursor-key-0123456789")
    monkeypatch.setenv("JARVIS_CALL_LOG_ENABLED", "1")
    monkeypatch.setenv("JARVIS_CALL_LOG_DIR", str(tmp_path / "calls"))
    monkeypatch.setenv("JARVIS_DRIFT_CHECK_ENABLED", "1")
    monkeypatch.setenv("JARVIS_DRIFT_LOG_DIR", str(tmp_path / "drift"))
    monkeypatch.setenv("JARVIS_AMUL_PATH", str(tmp_path / "amul.jsonl"))
    monkeypatch.setenv("JARVIS_EMR_DYNAMICS_PATH", str(tmp_path / "emr.json"))
    monkeypatch.setenv("JARVIS_MEMORY_WRITE_ENABLED", "true")
    monkeypatch.setenv("JARVIS_MCP_WRITE_ENABLED", "false")
    rag_key = tmp_path / "rag.key"
    rag_key.write_text("rag-key-for-the-boundary-test", encoding="utf-8")        # the RAG routes have a key of their own: the recall key is not it (401, not the 503 of an unconfigured RAG)
    monkeypatch.setattr(amul_rag, "RAG_API_KEY_FILE", str(rag_key))
    reset_store_for_tests()
    client = TestClient(app, raise_server_exceptions=False)
    seed = client.post("/api/jarvis/memory", headers={"X-API-Key": LEDGER}, json={"content": "boundary probe record", "source_agent": "probe", "session_id": "s",
                                                                                   "type": "fact", "evidence": [{"kind": "doc", "ref": "x", "note": "y"}]})
    assert seed.status_code == 200, seed.text
    return client, seed.json()["memory"]["id"]


def _concrete(path: str) -> str:
    return (path.replace("{height}", "1").replace("{receipt_id}", "eo:sha256:" + "0" * 64).replace("{evidence_id}", "eo:sha256:" + "0" * 64)
            .replace("{memory_id}", GHOST).replace("{artifact_id}", GHOST_ARTIFACT))


def _body(method: str):
    return {} if method in ("POST", "PATCH", "PUT") else None


RECALL_FORMS = {"bearer": {"Authorization": f"Bearer {RECALL}"}, "x-emr-recall-key": {"X-EMR-Recall-Key": RECALL}}
LEDGER_FORMS = {"x-api-key": {"X-API-Key": LEDGER}, "bearer": {"Authorization": f"Bearer {LEDGER}"}}


def _tool_args(mem_id: str) -> dict[str, dict]:
    return {"emr_recall": {"intent": "chat", "query": "boundary"}, "emr_latest": {}, "emr_search_ledger": {"query": "boundary"}, "search": {"query": "boundary"},
            "fetch": {"id": mem_id}, "emr_search": {"query": "boundary"}, "emr_fetch": {"id": mem_id}}


def _rpc(client, headers, method, params=None, i=1):
    return client.post("/mcp", headers=headers, json={"jsonrpc": "2.0", "id": i, "method": method, **({"params": params} if params else {})})


def test_the_read_tool_set_is_the_one_written_down():
    assert RECALL_READ_TOOLS == READ_TOOLS
    assert READ_TOOLS <= ALL_TOOLS and {"emr_remember", "emr_upsert", "emr_drift_check"} <= ALL_TOOLS - READ_TOOLS     # the writes and the drift check are NOT in it


@pytest.mark.parametrize("form", sorted(RECALL_FORMS))
def test_every_route_in_the_registry_refuses_the_recall_key_except_the_read_tools(world, form):
    client, _ = world
    headers = RECALL_FORMS[form]
    answered, wrong = [], []
    for (method, path), (cls, _) in sorted(REGISTRY.items()):
        if cls == PUBLIC or (method, path) in READ_TOOL_ROUTES or (method, path) in MCP_TRANSPORT:
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
    assert not wrong, "the recall key reached routes it must not:\n" + "\n".join(wrong)
    assert len(answered) >= 80, "suspiciously few routes were checked: the registry did not load"           # (a vacuous loop would pass)


def test_the_tool_catalog_is_the_same_with_the_recall_key_and_with_no_credential_at_all(world):
    client, _ = world
    assert client.get("/api/jarvis/tools").json() == client.get("/api/jarvis/tools", headers=RECALL_FORMS["bearer"]).json()


def test_the_registry_lists_every_route_the_recall_key_may_reach():
    for key in READ_TOOL_ROUTES | MCP_TRANSPORT | set(NO_DATA_MCP):
        assert key in REGISTRY, f"{key} is allowed to the recall key but not in the isolation matrix's registry"


@pytest.mark.parametrize("form", sorted(RECALL_FORMS))
def test_the_recall_key_answers_each_read_tool_route(world, form):
    client, mem_id = world
    for tool, args in _tool_args(mem_id).items():
        r = client.post(f"/api/jarvis/tools/{tool}", headers=RECALL_FORMS[form], json=args)
        assert r.status_code == 200, (tool, r.status_code, r.text[:100])


@pytest.mark.parametrize("tool", ["emr_remember", "emr_upsert", "emr_drift_check"])
def test_the_recall_key_is_refused_the_write_and_drift_tool_routes_even_when_mcp_writes_are_on(world, monkeypatch, tool):
    client, mem_id = world
    monkeypatch.setenv("JARVIS_MCP_WRITE_ENABLED", "true")                        # the flag being on must not matter to a recall-only caller
    args = {"content": "RECALL WRITE PROBE", "source_agent": "probe", "session_id": "s", "type": "fact", "id": mem_id, "summary": "s", "records": [mem_id]}
    r = client.post(f"/api/jarvis/tools/{tool}", headers=RECALL_FORMS["bearer"], json=args)
    assert r.status_code in (401, 403), (tool, r.status_code, r.text[:100])
    assert [m.content for m in get_store().list_memories(limit=99)] == ["boundary probe record"]


def test_over_mcp_the_recall_key_lists_exactly_the_read_tools_and_the_ledger_key_lists_all(world):
    client, _ = world
    for form, headers in RECALL_FORMS.items():
        listed = {t["name"] for t in _rpc(client, headers, "tools/list").json()["result"]["tools"]}
        assert listed == READ_TOOLS, (form, sorted(listed ^ READ_TOOLS))
    for form, headers in LEDGER_FORMS.items():
        assert {t["name"] for t in _rpc(client, headers, "tools/list").json()["result"]["tools"]} == ALL_TOOLS, form


def test_over_mcp_the_recall_key_calls_the_read_tools_and_is_refused_everything_else(world, monkeypatch):
    client, mem_id = world
    monkeypatch.setenv("JARVIS_MCP_WRITE_ENABLED", "true")
    headers = RECALL_FORMS["bearer"]
    for i, (tool, args) in enumerate(_tool_args(mem_id).items(), start=1):
        res = _rpc(client, headers, "tools/call", {"name": tool, "arguments": args}, i=i).json()["result"]
        assert not res.get("isError"), (tool, res)
    for tool in sorted(ALL_TOOLS - READ_TOOLS) + ["not_a_tool"]:
        res = _rpc(client, headers, "tools/call", {"name": tool, "arguments": {"content": "RECALL WRITE PROBE", "source_agent": "p", "session_id": "s", "type": "fact", "id": mem_id}}).json()["result"]
        assert res["isError"] is True and "not permitted" in res["content"][0]["text"], (tool, res)
    assert [m.content for m in get_store().list_memories(limit=99)] == ["boundary probe record"]
    assert not list((__import__("pathlib").Path(__import__("os").environ["JARVIS_DRIFT_LOG_DIR"])).glob("*")), "a recall-only caller wrote to the drift log"


def test_over_mcp_the_ledger_key_still_calls_a_write_tool_and_the_drift_check(world, monkeypatch):
    """Positive control: the scoping narrows the recall key, it does not take anything from the ledger key."""
    client, mem_id = world
    res = _rpc(client, LEDGER_FORMS["x-api-key"], "tools/call", {"name": "emr_drift_check", "arguments": {"summary": "boundary probe record", "records": [mem_id]}}).json()["result"]
    assert not res.get("isError"), res
    res = _rpc(client, LEDGER_FORMS["bearer"], "tools/call", {"name": "emr_remember", "arguments": {"content": "x", "source_agent": "p", "session_id": "s", "type": "fact"}}).json()["result"]
    assert json.loads(res["content"][0]["text"])["refused"] is True               # (writes are off: refused by the flag, not by the credential scope)


def test_no_credential_or_a_wrong_one_is_refused_on_every_tool_route_and_mcp(world):
    client, _ = world
    for headers in ({}, {"Authorization": "Bearer wrong"}, {"X-API-Key": "wrong"}, {"X-EMR-Recall-Key": "wrong"}):
        for tool in sorted(READ_TOOLS):
            assert client.post(f"/api/jarvis/tools/{tool}", headers=headers, json={}).status_code == 401, (tool, headers)
        assert _rpc(client, headers, "tools/list").status_code == 401, headers


def test_installing_a_recall_key_breaks_no_ledger_key_client(world):
    """Before this change the ledger key was REJECTED on the tool routes, MCP, call log and drift routes as soon as EMR_RECALL_API_KEY was set."""
    client, mem_id = world
    for form, headers in LEDGER_FORMS.items():
        assert client.post("/api/jarvis/tools/emr_latest", headers=headers, json={}).status_code == 200, form
        assert _rpc(client, headers, "initialize", {"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}}).status_code == 200, form
        assert client.get("/api/jarvis/tools/calls", headers=headers).status_code == 200, form
        assert client.get("/api/jarvis/tools/calls/verify", headers=headers).status_code == 200, form
    h = LEDGER_FORMS["x-api-key"]
    assert client.get("/api/jarvis/drift/log", headers=h).status_code == 200
    assert client.get("/api/jarvis/drift/log/verify", headers=h).status_code == 200
    assert client.post("/api/jarvis/drift/check", headers=h, json={"summary": "boundary probe record", "records": [mem_id]}).status_code == 200


def test_when_both_credentials_are_presented_the_ledger_key_wins(world):
    client, _ = world
    both = {"X-API-Key": LEDGER, "Authorization": f"Bearer {RECALL}"}
    assert client.get("/api/jarvis/tools/calls/verify", headers=both).status_code == 200
    assert {t["name"] for t in _rpc(client, both, "tools/list").json()["result"]["tools"]} == ALL_TOOLS


# ----------------------------------------------------------------------------------------------------------- review findings on #101
def test_with_protected_ledger_reads_the_ledger_key_still_reads_and_the_recall_key_still_cannot(world, monkeypatch):
    """JARVIS_PROTECT_LEDGER_READ used to demand the RECALL key for GET /memory/*, so installing one broke a ledger-key reader."""
    client, _ = world
    monkeypatch.setenv("JARVIS_PROTECT_LEDGER_READ", "true")
    for form, headers in LEDGER_FORMS.items():
        assert client.get("/api/jarvis/memory", headers=headers).status_code == 200, form
    for form, headers in RECALL_FORMS.items():
        assert client.get("/api/jarvis/memory", headers=headers).status_code in (401, 403), form
    assert client.get("/api/jarvis/memory").status_code in (401, 403)


def test_the_ledger_key_wins_on_the_ledger_key_routes_too_even_behind_a_recall_bearer(world):
    """Bearer = recall key, X-API-Key = ledger key: the middleware and the identity binding used to look only at the Bearer, so drift/nx/ledger routes said 401."""
    client, mem_id = world
    both = {"Authorization": f"Bearer {RECALL}", "X-API-Key": LEDGER}
    assert client.get("/api/jarvis/drift/log", headers=both).status_code == 200
    assert client.get("/api/jarvis/drift/log/verify", headers=both).status_code == 200
    assert client.get("/api/jarvis/memory", headers=both).status_code == 200
    assert client.post("/api/jarvis/drift/check", headers=both, json={"summary": "boundary probe record", "records": [mem_id]}).status_code == 200
    wrong_ledger = {"Authorization": f"Bearer {RECALL}", "X-API-Key": "wrong"}                 # the recall key alone is still not enough there
    assert client.get("/api/jarvis/drift/log", headers=wrong_ledger).status_code == 401
    assert client.get("/api/jarvis/memory", headers=wrong_ledger).status_code == 401


def test_the_stdio_proxy_sends_the_ledger_key_for_operator_tools_and_the_recall_key_for_reads(monkeypatch):
    from mcp_server import emr_stdio

    monkeypatch.delenv("JARVIS_API_KEY_FILE", raising=False)
    monkeypatch.setenv("EMR_RECALL_API_KEY", "the-recall-key")
    monkeypatch.setenv("JARVIS_API_KEY", "the-ledger-key")
    for tool in sorted(READ_TOOLS):
        assert emr_stdio.api_key(tool) == "the-recall-key", tool                                # least privilege for reads (and the old default)
    for tool in ("emr_remember", "emr_upsert", "emr_drift_check"):
        assert emr_stdio.api_key(tool) == "the-ledger-key", tool                                # the server refuses the recall key there
    assert emr_stdio.api_key() == "the-recall-key"                                              # no tool named: the unchanged order
    monkeypatch.delenv("JARVIS_API_KEY")
    assert emr_stdio.api_key("emr_remember") == "the-recall-key"                                # only a recall key exists: nothing better to send


def test_the_stdio_proxy_posts_a_write_tool_with_the_ledger_key(monkeypatch):
    """End to end through _http_post: the Authorization header the proxy builds for emr_remember carries the ledger key."""
    from mcp_server import emr_stdio

    seen = {}

    class _Resp:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return b"{}"

    monkeypatch.setenv("JARVIS_MEMORYBOARD_URL", "http://127.0.0.1:8011")
    monkeypatch.setenv("EMR_RECALL_API_KEY", "the-recall-key")
    monkeypatch.setenv("JARVIS_API_KEY", "the-ledger-key")
    monkeypatch.delenv("JARVIS_API_KEY_FILE", raising=False)
    monkeypatch.setattr(emr_stdio.urllib.request, "urlopen", lambda req, timeout=0: seen.setdefault("auth", req.get_header("Authorization")) and _Resp())
    emr_stdio._http_post("/api/jarvis/tools/emr_remember", {})
    assert seen["auth"] == "Bearer the-ledger-key"
    seen.clear()
    emr_stdio._http_post("/api/jarvis/tools/emr_latest", {})
    assert seen["auth"] == "Bearer the-recall-key"
