"""The Replay Contract endpoints: operator-key only, row store only, errors with codes, shape of the answers."""

from __future__ import annotations

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app import auth, pg_store, replay
from app.main import app
from app.pg_schema import EXPECTED_SCHEMA_VERSION, migrate

KEY = "replay-api-test-key"
HDR = {"X-API-Key": KEY}
ROUTES = [("get", "/api/jarvis/replay/contracts"), ("get", "/api/jarvis/replay/state"), ("get", "/api/jarvis/replay/events")]


@pytest.fixture
def pg(pg_schema):
    assert migrate(pg_schema.admin_dsn, schema=pg_schema.schema, app_role="jarvis_app_test") == EXPECTED_SCHEMA_VERSION
    yield pg_schema
    pg_store.close_pools()


@pytest.fixture
def client(pg, monkeypatch):
    monkeypatch.setenv("JARVIS_DATABASE_URL", pg.app_dsn)
    monkeypatch.setenv("JARVIS_DATABASE_SCHEMA", pg.schema)
    monkeypatch.setenv("JARVIS_PG_STORE", "rows")
    monkeypatch.setenv("JARVIS_API_KEY", KEY)
    monkeypatch.delenv("JARVIS_ALLOW_UNAUTHENTICATED", raising=False)
    monkeypatch.setenv("JARVIS_MEMORY_WRITE_ENABLED", "true")
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


def add(client, n, prefix="replay api record"):
    ids = []
    for i in range(n):
        r = client.post("/api/jarvis/memory", headers=HDR, json={"content": f"{prefix} {i} for the replay endpoints", "source_agent": "t", "session_id": "s", "type": "fact"})
        assert r.status_code == 200, r.text
        ids.append(r.json()["memory"]["id"])
    return ids


@pytest.mark.parametrize("method,path", ROUTES)
def test_every_replay_route_needs_the_operator_key(client, method, path):
    assert getattr(client, method)(path).status_code == 401
    assert getattr(client, method)(path, headers={"X-API-Key": "wrong"}).status_code == 401


def test_the_registry_lists_one_implemented_contract_and_five_declared(client):
    body = client.get("/api/jarvis/replay/contracts", headers=HDR).json()["contracts"]
    assert {c["id"]: c["status"] for c in body} == {
        "RC.Ledger.v1": "implemented", "RC.AIKI.v1": "declared", "RC.ARIS.v1": "declared",
        "RC.SX.v1": "declared", "RC.Lineage.v1": "declared", "RC.Mandala.v1": "declared"}
    ledger = next(c for c in body if c["id"] == "RC.Ledger.v1")
    assert ledger["algorithm"] == "ledger-state-at-seq/v1" and ledger["determinism"] and len(ledger["schemas"]) == 5


def test_state_at_the_end_and_at_a_seq(client):
    ids = add(client, 4)
    now = client.get("/api/jarvis/replay/state", headers=HDR).json()
    assert (now["contract"], now["contract_version"], now["tenant"]) == ("RC.Ledger.v1", 1, "operator")
    assert (now["at_seq"], now["history_seq"], now["record_count"], now["deleted_count"]) == (4, 4, 4, 0)
    assert now["sealed"] is False and now["block"] is None and now["next_after_id"] is None
    assert sorted(r["id"] for r in now["records"]) == sorted(ids) and len(now["state_root"]) == 64
    assert all({"id", "seq", "version", "row_hash", "record"} == set(r) for r in now["records"])
    early = client.get("/api/jarvis/replay/state?at_seq=2", headers=HDR).json()
    assert early["at_seq"] == 2 and early["record_count"] == 2 and early["state_root"] != now["state_root"]
    assert client.get("/api/jarvis/replay/state?at_seq=0", headers=HDR).json()["state_root"] == replay.EMPTY_ROOT


def test_the_state_is_the_same_on_every_request_and_matches_the_live_listing(client):
    add(client, 3)
    a = client.get("/api/jarvis/replay/state", headers=HDR).json()
    b = client.get("/api/jarvis/replay/state", headers=HDR).json()
    assert a == b
    live = {m["id"]: m for m in client.get("/api/jarvis/memory?limit=50&with_provenance=false", headers=HDR).json()["memories"]}
    assert {r["id"]: r["record"]["content"] for r in a["records"]} == {i: m["content"] for i, m in live.items()}


def test_paging_and_blocks(client):
    add(client, 5)
    client.post("/api/jarvis/blocks/seal", headers=HDR, json={"force": True, "max_entries": 3})
    page = client.get("/api/jarvis/replay/state?limit=2", headers=HDR).json()
    assert len(page["records"]) == 2 and page["next_after_id"] == page["records"][-1]["id"] and page["record_count"] == 5
    rest = client.get(f"/api/jarvis/replay/state?limit=10&after_id={page['next_after_id']}", headers=HDR).json()
    assert len(rest["records"]) == 3 and rest["next_after_id"] is None and rest["state_root"] == page["state_root"]
    block = client.get("/api/jarvis/replay/state?at_block=1", headers=HDR).json()
    assert (block["at_seq"], block["sealed"], block["at_block_boundary"], block["block"]["height"]) == (3, True, True, 1)
    assert block["block"]["block_hash"] == client.get("/api/jarvis/blocks/1", headers=HDR).json()["block"]["block_hash"]
    assert client.get("/api/jarvis/replay/state?at_seq=4", headers=HDR).json()["block"]["height"] == 2


def test_events_in_order_with_paging(client):
    ids = add(client, 3)
    client.patch(f"/api/jarvis/memory/{ids[0]}", headers=HDR, json={"subject": "edited"})
    body = client.get("/api/jarvis/replay/events", headers=HDR).json()
    assert [(e["seq"], e["op"]) for e in body["events"]] == [(1, "create"), (2, "create"), (3, "create"), (4, "update")]
    assert body["history_seq"] == 4 and body["next_from_seq"] is None and body["events"][0]["actor"] == "operator"
    assert all({"seq", "memory_id", "op", "version", "actor", "changed_at", "prev_hash", "row_hash", "before", "after", "evidence"} == set(e) for e in body["events"])
    paged = client.get("/api/jarvis/replay/events?from_seq=2&to_seq=4&limit=2", headers=HDR).json()
    assert [e["seq"] for e in paged["events"]] == [2, 3] and paged["next_from_seq"] == 4


def test_events_classify_evidence(client):
    obj = client.post("/api/jarvis/evidence", headers=HDR, json={"schema_id": "CES.Local.FactEvidence.v1", "source_agent": "t",
                                                                  "payload": {"observation": "o", "method": "command", "source": "s"}}).json()["evidence"]
    client.post("/api/jarvis/memory", headers=HDR, json={"content": "cites an object and a file", "source_agent": "t", "session_id": "s", "type": "fact",
                                                          "evidence": [{"kind": "evidence-object", "ref": obj["id"]}, {"kind": "file", "ref": "docs/POSTGRES.md"}]})
    ev = client.get("/api/jarvis/replay/events", headers=HDR).json()["events"][0]["evidence"]
    assert {(x["kind"], x["status"]) for x in ev} == {("evidence-object", "intact"), ("file", "not-checked")}


@pytest.mark.parametrize("query,status,code", [
    ("at_seq=9", 422, "replay_seq_out_of_range"), ("at_block=3", 404, "replay_block_not_found"),
    ("at_seq=1&at_block=1", 422, "replay_bound_ambiguous"),
])
def test_state_errors_carry_a_code(client, query, status, code):
    add(client, 2)
    r = client.get(f"/api/jarvis/replay/state?{query}", headers=HDR)
    assert r.status_code == status and code in r.json()["detail"]


@pytest.mark.parametrize("path", [
    "/api/jarvis/replay/state?at_seq=-1", "/api/jarvis/replay/state?at_block=0", "/api/jarvis/replay/state?limit=0",
    "/api/jarvis/replay/state?limit=1001", "/api/jarvis/replay/events?from_seq=0", "/api/jarvis/replay/events?limit=0", "/api/jarvis/replay/state?at_seq=x",
])
def test_malformed_parameters_are_422(client, path):
    assert client.get(path, headers=HDR).status_code == 422


def test_events_beyond_the_history_are_refused(client):
    add(client, 2)
    r = client.get("/api/jarvis/replay/events?to_seq=5", headers=HDR)
    assert r.status_code == 422 and "replay_seq_out_of_range" in r.json()["detail"]


def test_the_routes_are_guarded_by_the_operator_only_dependency():
    seen = {}
    for route in app.routes:
        if getattr(route, "path", "").startswith("/api/jarvis/replay"):
            seen[route.path] = {d.call.__name__ for d in route.dependant.dependencies}
    assert set(seen) == {"/api/jarvis/replay/contracts", "/api/jarvis/replay/state", "/api/jarvis/replay/events"} | {
        "/api/jarvis/replay/receipts", "/api/jarvis/replay/receipts/{receipt_id}", "/api/jarvis/replay/receipts/{receipt_id}/verify"}
    assert all(names & {"require_operator_read", "require_operator_write"} for names in seen.values())  # the receipt routes are tested in test_replay_receipts_api.py


def test_an_oauth_user_token_is_refused(monkeypatch):
    monkeypatch.setattr(auth, "oauth_enabled", lambda: True)
    with pytest.raises(HTTPException) as exc:
        auth.require_operator_read()
    assert exc.value.status_code == 403


def test_oauth_mode_without_a_token_never_reaches_a_replay_route(monkeypatch):
    monkeypatch.setenv("JARVIS_AUTH_MODE", "oauth")
    with TestClient(app, raise_server_exceptions=False) as c:
        for method, path in ROUTES:
            assert getattr(c, method)(path).status_code == 401


@pytest.mark.json_store_only
@pytest.mark.parametrize("path", ["/api/jarvis/replay/state", "/api/jarvis/replay/events"])
def test_the_json_store_answers_501(path):
    with TestClient(app, raise_server_exceptions=False) as c:
        assert c.get(path).status_code == 501


@pytest.mark.json_store_only
def test_the_registry_needs_no_database():
    with TestClient(app, raise_server_exceptions=False) as c:
        assert c.get("/api/jarvis/replay/contracts").status_code == 200
