"""The replay receipt endpoints: operator-key only, sealed points only, verified by re-derivation."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import pg_store
from app.main import app
from app.pg_schema import EXPECTED_SCHEMA_VERSION, migrate

KEY = "receipt-api-test-key"
HDR = {"X-API-Key": KEY}


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


def add(client, n):
    for i in range(n):
        r = client.post("/api/jarvis/memory", headers=HDR, json={"content": f"receipt api record {i}", "source_agent": "t", "session_id": "s", "type": "fact"})
        assert r.status_code == 200, r.text


def sealed(client, n=6, size=3):
    add(client, n)
    assert client.post("/api/jarvis/blocks/seal", headers=HDR, json={"force": True, "max_entries": size}).status_code == 200


RID = "eo:sha256:" + "0" * 64
ROUTES = [("post", "/api/jarvis/replay/receipts"), ("get", "/api/jarvis/replay/receipts"), ("get", f"/api/jarvis/replay/receipts/{RID}"),
          ("get", f"/api/jarvis/replay/receipts/{RID}/verify")]


@pytest.mark.parametrize("method,path", ROUTES)
def test_every_receipt_route_needs_the_operator_key(client, method, path):
    assert getattr(client, method)(path).status_code == 401
    assert getattr(client, method)(path, headers={"X-API-Key": "wrong"}).status_code == 401


def test_issue_read_list_and_verify_a_receipt(client):
    sealed(client)
    r = client.post("/api/jarvis/replay/receipts", headers=HDR, json={"at_block": 2})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["created"] is True and "records" not in body["state"]
    rid, payload = body["receipt"]["id"], body["receipt"]["payload"]
    assert body["receipt"]["schema_id"] == "CES.Local.ReplayReceipt.v1" and rid.startswith("eo:sha256:")
    assert (payload["at_seq"], payload["block_height"], payload["record_count"]) == (6, 2, 6) and payload["state_root"] == body["state"]["state_root"]
    assert payload["state_root"] == client.get("/api/jarvis/replay/state?at_seq=6", headers=HDR).json()["state_root"]
    again = client.post("/api/jarvis/replay/receipts", headers=HDR, json={"at_block": 2}).json()
    assert again["created"] is False and again["receipt"]["id"] == rid
    assert client.get(f"/api/jarvis/replay/receipts/{rid}", headers=HDR).json()["receipt"] == body["receipt"]
    listed = client.get("/api/jarvis/replay/receipts", headers=HDR).json()
    assert listed["count"] == 1 and listed["receipts"][0]["id"] == rid
    v = client.get(f"/api/jarvis/replay/receipts/{rid}/verify", headers=HDR).json()
    assert v["ok"] is True and v["problems"] == [] and v["receipt_id"] == rid and v["replayed"]["state_root"] == payload["state_root"]


def test_the_default_is_the_end_of_the_newest_sealed_block_and_an_empty_body_is_fine(client):
    sealed(client, n=5, size=3)  # blocks 1-3 and 4-5
    r = client.post("/api/jarvis/replay/receipts", headers=HDR).json()
    assert (r["receipt"]["payload"]["at_seq"], r["receipt"]["payload"]["block_height"]) == (5, 2)
    assert client.post("/api/jarvis/replay/receipts", headers=HDR, json={}).json()["receipt"]["id"] == r["receipt"]["id"]
    assert client.post("/api/jarvis/replay/receipts", headers=HDR, json={"at_seq": 2}).json()["receipt"]["payload"]["block_height"] == 1


@pytest.mark.parametrize("body,status,code", [
    ({}, 409, "replay_nothing_sealed"),
    ({"at_seq": 2}, 422, "replay_not_sealed"),
    ({"at_block": 1}, 404, "replay_block_not_found"),
    ({"at_seq": 1, "at_block": 1}, 422, "replay_bound_ambiguous"),
])
def test_nothing_sealed_yet_refuses_every_receipt(client, body, status, code):
    add(client, 3)
    r = client.post("/api/jarvis/replay/receipts", headers=HDR, json=body)
    assert r.status_code == status and code in r.json()["detail"]
    assert client.get("/api/jarvis/replay/receipts", headers=HDR).json()["count"] == 0


def test_a_point_beyond_the_sealed_tail_is_refused(client):
    sealed(client, n=3, size=3)
    add(client, 2)
    r = client.post("/api/jarvis/replay/receipts", headers=HDR, json={"at_seq": 4})
    assert r.status_code == 422 and "replay_not_sealed" in r.json()["detail"] and "sealed through seq 3" in r.json()["detail"]
    assert client.post("/api/jarvis/replay/receipts", headers=HDR, json={"at_seq": 99}).status_code == 422


@pytest.mark.parametrize("body", [{"at_seq": 0}, {"at_block": 0}, {"at_seq": -1}, {"at_seq": "x"}])
def test_malformed_receipt_requests_are_422(client, body):
    sealed(client, n=3)
    assert client.post("/api/jarvis/replay/receipts", headers=HDR, json=body).status_code == 422


def test_unknown_and_malformed_receipt_ids(client):
    assert client.get(f"/api/jarvis/replay/receipts/{RID}", headers=HDR).status_code == 404
    assert client.get(f"/api/jarvis/replay/receipts/{RID}/verify", headers=HDR).status_code == 404
    r = client.get("/api/jarvis/replay/receipts/not-an-id", headers=HDR)
    assert r.status_code == 422 and "receipt_id_invalid" in r.json()["detail"]


def test_a_fact_evidence_object_is_not_a_receipt(client):
    fact = client.post("/api/jarvis/evidence", headers=HDR, json={"schema_id": "CES.Local.FactEvidence.v1", "source_agent": "t",
                                                                   "payload": {"observation": "o", "method": "command", "source": "s"}}).json()["evidence"]
    r = client.get(f"/api/jarvis/replay/receipts/{fact['id']}/verify", headers=HDR)
    assert r.status_code == 422 and "not_a_replay_receipt" in r.json()["detail"]


def test_the_generic_evidence_route_refuses_to_create_a_receipt(client):
    """Receipts are derived from a replay, never typed in: otherwise anyone with the key could write a convincing one."""
    payload = {"contract": "RC.Ledger.v1", "contract_version": 1, "tenant": "operator", "at_seq": 1, "block_height": 1,
               "block_hash": "a" * 64, "state_root": "b" * 64, "record_count": 1, "deleted_count": 0}
    r = client.post("/api/jarvis/evidence", headers=HDR, json={"schema_id": "CES.Local.ReplayReceipt.v1", "source_agent": "t", "payload": payload})
    assert r.status_code == 422 and r.json()["code"] == "evidence_schema_reserved"
    assert client.get("/api/jarvis/replay/receipts", headers=HDR).json()["count"] == 0


def test_a_forged_receipt_is_exposed_by_the_verify_route(client, pg):
    import json

    from app import evidence

    sealed(client)
    real = client.post("/api/jarvis/replay/receipts", headers=HDR, json={"at_block": 2}).json()["receipt"]
    payload = real["payload"] | {"state_root": "f" * 64}
    fake_id = evidence.object_id(evidence.CES_REPLAY_RECEIPT, payload)
    with pg.admin_conn() as conn:
        conn.execute("INSERT INTO evidence_objects (tenant_key, id, schema_id, payload, size_bytes, created_by) VALUES ('operator', %s, %s, %s::jsonb, 100, 'forger')",
                     (fake_id, evidence.CES_REPLAY_RECEIPT, json.dumps(payload)))
    v = client.get(f"/api/jarvis/replay/receipts/{fake_id}/verify", headers=HDR).json()
    assert v["ok"] is False and any("state root on replay" in p["problem"] for p in v["problems"])


def test_writes_disabled_stops_issuing_but_not_reading_or_verifying(client, monkeypatch):
    sealed(client)
    rid = client.post("/api/jarvis/replay/receipts", headers=HDR, json={"at_block": 1}).json()["receipt"]["id"]
    monkeypatch.setenv("JARVIS_MEMORY_WRITE_ENABLED", "false")
    assert client.post("/api/jarvis/replay/receipts", headers=HDR, json={"at_block": 2}).status_code == 403
    assert client.get(f"/api/jarvis/replay/receipts/{rid}/verify", headers=HDR).json()["ok"] is True


def test_the_routes_are_guarded_by_the_operator_only_dependencies():
    seen = {}
    for route in app.routes:
        path = getattr(route, "path", "")
        if path.startswith("/api/jarvis/replay/receipts"):
            seen[(tuple(sorted(route.methods)), path)] = {d.call.__name__ for d in route.dependant.dependencies}
    assert len(seen) == 4
    for key, names in seen.items():
        assert names & {"require_operator_read", "require_operator_write"}, key
    assert "require_operator_write" in seen[(("POST",), "/api/jarvis/replay/receipts")]


@pytest.mark.json_store_only
@pytest.mark.parametrize("method,path", ROUTES)
def test_the_json_store_answers_501(method, path):
    with TestClient(app, raise_server_exceptions=False) as c:
        assert getattr(c, method)(path).status_code == 501
