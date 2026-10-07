"""A NUL byte can never be stored in PostgreSQL text.  It is the caller's mistake: a 400, never a fake "ledger unavailable" 503, and the
ledger stays ready.  (Found by the CL_CHAOS_100x hammer: GET /memory/mem-%00 and a record whose content held \\u0000 both answered 503.)"""

from __future__ import annotations

import pytest

from tests.test_attest_api import HDR, client, pg  # noqa: F401  (fixtures by name)

pytestmark = pytest.mark.postgres

GOOD = {"source_agent": "t", "session_id": "s", "type": "decision", "evidence": [{"kind": "user-request", "ref": "x"}]}


def test_a_nul_in_the_path_is_a_400_not_a_503(client):
    for path in ("/api/jarvis/memory/mem-%00", "/api/jarvis/memory/mem-%00/history", "/api/jarvis/evidence/eo:sha256:%00"):
        r = client.get(path, headers=HDR)
        assert r.status_code in (400, 404, 422), (path, r.status_code, r.text)
    r = client.get("/api/jarvis/memory/mem-%00", headers=HDR)
    assert r.status_code == 400 and r.json()["code"] == "invalid_input" and "NUL" in r.json()["detail"]
    assert client.get("/ready").status_code == 200


def test_a_nul_in_a_body_is_a_400_on_create_and_on_update(client):
    r = client.post("/api/jarvis/memory", headers=HDR, json={"content": "a\u0000b", **GOOD})
    assert r.status_code == 400 and "NUL" in r.json()["detail"]
    ok = client.post("/api/jarvis/memory", headers=HDR, json={"content": "fine", **GOOD}).json()["memory"]
    r = client.patch(f"/api/jarvis/memory/{ok['id']}", headers=HDR, json={"subject": "x\u0000y"})
    assert r.status_code == 400
    assert client.get(f"/api/jarvis/memory/{ok['id']}", headers=HDR).json()["memory"]["subject"] in (None, "")  # nothing half-written
    assert client.get("/ready").status_code == 200


def test_a_nul_in_a_query_string_never_becomes_a_5xx(client):
    for qs in ("query=%00", "subject=%00", "session_id=a%00b", "type=%00"):
        for path in ("/api/jarvis/memory/retrieve", "/api/jarvis/memory"):
            assert client.get(f"{path}?{qs}", headers=HDR).status_code < 500, (path, qs)
    assert client.get("/api/jarvis/memory/retrieve?query=%00", headers=HDR).status_code == 200  # a query that matches nothing is a 200


def test_other_database_errors_are_still_a_503(client, monkeypatch):
    import psycopg

    from app import pg_store

    def boom(*a, **k):
        raise psycopg.errors.DataError("numeric field overflow")
    monkeypatch.setattr(pg_store, "check_schema_version", boom)
    pg_store._ready.clear()
    assert client.get("/api/jarvis/memory", headers=HDR).status_code == 503
