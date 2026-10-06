"""The Continuity Block endpoints: operator-key only, row store only, hashes computed by the database."""

from __future__ import annotations

import hashlib

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app import auth, blocks, pg_store
from app.main import app
from app.pg_schema import EXPECTED_SCHEMA_VERSION, migrate

KEY = "block-api-test-key"
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


def add(client, n, prefix="api record"):
    ids = []
    for i in range(n):
        r = client.post("/api/jarvis/memory", headers=HDR, json={"content": f"{prefix} {i} for the block endpoints", "source_agent": "t", "session_id": "s", "type": "fact"})
        assert r.status_code == 200, r.text
        ids.append(r.json()["memory"]["id"])
    return ids


ROUTES = [
    ("get", "/api/jarvis/blocks"), ("get", "/api/jarvis/blocks/head"), ("get", "/api/jarvis/blocks/verify"),
    ("get", "/api/jarvis/blocks/1"), ("post", "/api/jarvis/blocks/seal"),
]


@pytest.mark.parametrize("method,path", ROUTES)
def test_every_block_route_needs_the_operator_key(client, method, path):
    assert getattr(client, method)(path).status_code == 401
    assert getattr(client, method)(path, headers={"X-API-Key": "wrong"}).status_code == 401


def test_the_seal_is_a_no_op_below_the_thresholds_and_says_why(client):
    add(client, 3)
    r = client.post("/api/jarvis/blocks/seal", headers=HDR)
    assert r.status_code == 200
    body = r.json()
    assert body["sealed"] == [] and body["reason"].startswith("below threshold: 3 unsealed entries (need 500)")
    assert body["head"]["tip"] is None and body["head"]["unsealed_entries"] == 3 and body["head"]["history_seq"] == 3
    assert client.get("/api/jarvis/blocks", headers=HDR).json() == {"blocks": [], "count": 0}


def test_seal_read_head_list_and_verify_round_trip(client):
    add(client, 7)
    r = client.post("/api/jarvis/blocks/seal", headers=HDR, json={"force": True, "max_entries": 3})
    assert r.status_code == 200
    body = r.json()
    assert [(b["height"], b["first_seq"], b["last_seq"], b["entry_count"]) for b in body["sealed"]] == [(1, 1, 3, 3), (2, 4, 6, 3), (3, 7, 7, 1)]
    assert body["reason"] == "nothing new to seal"
    assert body["head"]["tip"]["height"] == 3 and body["head"]["unsealed_entries"] == 0 and body["head"]["sealed_seq"] == 7
    listed = client.get("/api/jarvis/blocks", headers=HDR).json()
    assert listed["count"] == 3 and [b["height"] for b in listed["blocks"]] == [1, 2, 3]
    assert [b["height"] for b in client.get("/api/jarvis/blocks?after_height=1&limit=1", headers=HDR).json()["blocks"]] == [2]
    one = client.get("/api/jarvis/blocks/2", headers=HDR).json()["block"]
    assert one == listed["blocks"][1] and one["sealed_by"] == "operator" and one["format"] == 1
    assert client.get("/api/jarvis/blocks/head", headers=HDR).json() == body["head"]
    assert client.get("/api/jarvis/blocks/verify", headers=HDR).json() == {"ok": True, "problems": []}
    # sealing again changes nothing
    again = client.post("/api/jarvis/blocks/seal", headers=HDR, json={"force": True}).json()
    assert again["sealed"] == [] and again["reason"] == "nothing new to seal"
    assert client.get("/api/jarvis/blocks", headers=HDR).json() == listed


def test_the_hashes_come_from_the_database_never_from_the_caller(client):
    add(client, 3)
    forged = {"force": True, "block_hash": "f" * 64, "entries_root": "e" * 64, "prev_block_hash": "d" * 64, "height": 99}
    block = client.post("/api/jarvis/blocks/seal", headers=HDR, json=forged).json()["sealed"][0]
    assert block["height"] == 1 and block["prev_block_hash"] == blocks.GENESIS_HASH
    assert block["block_hash"] != "f" * 64 and block["entries_root"] != "e" * 64
    assert block["block_hash"] == blocks.block_hash(
        tenant="operator", height=1, first_seq=1, last_seq=3, entry_count=3,
        prev_block_hash=blocks.GENESIS_HASH, entries_root=block["entries_root"])


def test_the_seal_rules_can_be_set_per_call(client):
    add(client, 3)
    assert client.post("/api/jarvis/blocks/seal", headers=HDR, json={"min_entries": 4}).json()["sealed"] == []
    assert len(client.post("/api/jarvis/blocks/seal", headers=HDR, json={"min_entries": 3}).json()["sealed"]) == 1
    add(client, 1, "later")
    assert len(client.post("/api/jarvis/blocks/seal", headers=HDR, json={"max_age_seconds": 0}).json()["sealed"]) == 1


@pytest.mark.parametrize("bad", [{"min_entries": 0}, {"max_entries": 0}, {"max_age_seconds": -1}, {"force": "maybe"}])
def test_bad_seal_rules_are_refused_and_nothing_is_sealed(client, bad):
    add(client, 2)
    assert client.post("/api/jarvis/blocks/seal", headers=HDR, json=bad).status_code == 422
    assert client.get("/api/jarvis/blocks", headers=HDR).json()["count"] == 0


def test_unknown_and_malformed_heights(client):
    assert client.get("/api/jarvis/blocks/5", headers=HDR).status_code == 404
    assert client.get("/api/jarvis/blocks/0", headers=HDR).status_code == 422
    assert client.get("/api/jarvis/blocks/nope", headers=HDR).status_code == 422
    assert client.get("/api/jarvis/blocks?limit=0", headers=HDR).status_code == 422


def test_verify_reports_a_tampered_block_by_name(client, pg):
    add(client, 4)
    client.post("/api/jarvis/blocks/seal", headers=HDR, json={"force": True, "max_entries": 2})
    with pg.admin_conn() as conn:
        conn.execute("ALTER TABLE blocks DISABLE TRIGGER blocks_no_update")
        conn.execute("UPDATE blocks SET entries_root = %s WHERE height = 2", ("c" * 64,))
        conn.execute("ALTER TABLE blocks ENABLE TRIGGER blocks_no_update")
    body = client.get("/api/jarvis/blocks/verify", headers=HDR).json()
    assert body["ok"] is False
    assert {p["block"] for p in body["problems"]} == {"block 2"}
    assert any("entries_root" in p["problem"] for p in body["problems"])
    assert any(p["problem"].startswith("(recompute)") for p in body["problems"])  # the second implementation agrees


def test_verify_reports_a_vanished_cited_evidence_object(client, pg):
    obj = client.post("/api/jarvis/evidence", headers=HDR, json={
        "schema_id": "CES.Local.FactEvidence.v1", "source_agent": "t",
        "payload": {"observation": "Blocks cite this.", "method": "command", "source": "test"}}).json()["evidence"]
    r = client.post("/api/jarvis/memory", headers=HDR, json={
        "content": "A fact that cites an evidence object.", "source_agent": "t", "session_id": "s", "type": "fact",
        "evidence": [{"kind": "evidence-object", "ref": obj["id"]}]})
    assert r.status_code == 200
    client.post("/api/jarvis/blocks/seal", headers=HDR, json={"force": True})
    assert client.get("/api/jarvis/blocks/verify", headers=HDR).json()["ok"] is True
    with pg.admin_conn() as conn:
        conn.execute("ALTER TABLE evidence_objects DISABLE TRIGGER evidence_objects_no_delete")
        conn.execute("DELETE FROM evidence_objects")
        conn.execute("ALTER TABLE evidence_objects ENABLE TRIGGER evidence_objects_no_delete")
    body = client.get("/api/jarvis/blocks/verify", headers=HDR).json()
    assert body["ok"] is False and body["problems"][0]["block"] == "block 1" and "does not exist" in body["problems"][0]["problem"]


def test_writes_disabled_stops_the_seal_but_not_the_reads(client, monkeypatch):
    add(client, 2)
    monkeypatch.setenv("JARVIS_MEMORY_WRITE_ENABLED", "false")
    assert client.post("/api/jarvis/blocks/seal", headers=HDR, json={"force": True}).status_code == 403
    assert client.get("/api/jarvis/blocks/head", headers=HDR).status_code == 200
    assert client.get("/api/jarvis/blocks", headers=HDR).json()["count"] == 0


def test_the_block_routes_are_guarded_by_the_operator_only_dependencies():
    guards = {}
    for route in app.routes:
        if getattr(route, "path", "").startswith("/api/jarvis/blocks"):
            guards[(tuple(sorted(route.methods)), route.path)] = {d.call.__name__ for d in route.dependant.dependencies}
    assert len(guards) == 5
    for (methods, path), names in guards.items():
        assert names & {"require_operator_read", "require_operator_write"}, (methods, path)
    assert "require_operator_write" in guards[(("POST",), "/api/jarvis/blocks/seal")]


def test_an_oauth_user_token_is_refused_on_every_block_route(monkeypatch):
    monkeypatch.setattr(auth, "oauth_enabled", lambda: True)
    with pytest.raises(HTTPException) as exc:
        auth.require_operator_read()
    assert exc.value.status_code == 403 and "operator key only" in exc.value.detail
    from app.evidence import require_operator_write
    with pytest.raises(HTTPException) as exc:
        require_operator_write()
    assert exc.value.status_code == 403


def test_oauth_mode_without_a_token_never_reaches_a_block_route(monkeypatch):
    monkeypatch.setenv("JARVIS_AUTH_MODE", "oauth")
    with TestClient(app, raise_server_exceptions=False) as c:
        for method, path in ROUTES:
            assert getattr(c, method)(path).status_code == 401


@pytest.mark.json_store_only
@pytest.mark.parametrize("method,path", ROUTES)
def test_the_json_store_answers_501(method, path):
    with TestClient(app, raise_server_exceptions=False) as c:  # the default test store is the JSON file
        assert getattr(c, method)(path).status_code == 501
