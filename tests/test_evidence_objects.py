"""Evidence Objects: content-addressed, immutable, hashes only, operator-created, linkable from records."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app import evidence as ev
from app.evidence import CES_DECISION, CES_FACT, EvidenceError, EvidenceObjectCreate
from app.main import app
from app.store import PostgresJarvisStore

client = TestClient(app)

FACT_PAYLOAD = {"observation": "The ledger listens on 127.0.0.1:8011.", "method": "command", "source": "ss -ltn"}
DECISION_PAYLOAD = {"statement": "Use PostgreSQL row-level security.", "authority": "operator", "source": "chat:triage"}
POINTER = {"uri": "file:///var/log/big.log", "sha256": "ab" * 32, "size_bytes": 5_000_000, "media_type": "text/plain"}

# Frozen vectors: if these change, every stored id changes. That must never happen silently.
VECTOR_FACT_ID = "eo:sha256:eaf404ef7e0a5575a9e0da81d61b2f4485385d2f8e1ec94d6ccccd27aa4875f6"
VECTOR_DECISION = {"statement": "Use PostgreSQL.", "authority": "operator", "source": "chat:1", "note": "héllo ✓"}
VECTOR_DECISION_ID = "eo:sha256:6d1962bdb6ff01704d2715db68431f9b440e6c42b677e66059cd484ccce7cbea"
VECTOR_POINTER_ID = "eo:sha256:608b276af27168a6cae8d96857f99c8ff79e3617ace19cf9e17e58889d29f318"


def make(schema=CES_FACT, payload=None, **over):
    body = {"schema_id": schema, "payload": dict(FACT_PAYLOAD if schema == CES_FACT else DECISION_PAYLOAD), "source_agent": "test"}
    if payload is not None:
        body["payload"] = payload
    body.update(over)
    return body


def post(**kw):
    return client.post("/api/jarvis/evidence", json=make(**kw))


def codes(resp):
    return [r["code"] for r in resp.json().get("reasons", [])]


# --- the canonical form and the hash --------------------------------------------------------------------------

def test_frozen_vectors_pin_the_canonical_form():
    assert ev.object_id(CES_FACT, FACT_PAYLOAD) == VECTOR_FACT_ID
    assert ev.canonical_bytes(CES_FACT, FACT_PAYLOAD).decode() == (
        '{"payload":{"method":"command","observation":"The ledger listens on 127.0.0.1:8011.","source":"ss -ltn"},'
        '"schema_id":"CES.Local.FactEvidence.v1"}'
    )
    assert ev.object_id(CES_DECISION, VECTOR_DECISION) == VECTOR_DECISION_ID
    assert ev.object_id(CES_FACT, FACT_PAYLOAD, {"uri": "file:///x", "sha256": "0" * 64, "size_bytes": 10}) == VECTOR_POINTER_ID


def test_the_id_does_not_depend_on_key_order_or_whitespace():
    a = {"observation": "o", "method": "file", "source": "s"}
    b = {"source": "s", "observation": "o", "method": "file"}
    assert ev.object_id(CES_FACT, a) == ev.object_id(CES_FACT, b)


def test_any_change_gives_a_different_id():
    base = ev.object_id(CES_FACT, FACT_PAYLOAD)
    assert ev.object_id(CES_FACT, FACT_PAYLOAD | {"source": "ss -ltnp"}) != base
    assert ev.object_id(CES_DECISION, DECISION_PAYLOAD) != ev.object_id(CES_DECISION, DECISION_PAYLOAD | {"source": "chat:other"})
    assert ev.object_id(CES_FACT, FACT_PAYLOAD, POINTER) != base  # a pointer is part of the content


def test_the_id_is_the_sha256_of_the_canonical_bytes():
    import hashlib

    assert ev.object_id(CES_FACT, FACT_PAYLOAD) == "eo:sha256:" + hashlib.sha256(ev.canonical_bytes(CES_FACT, FACT_PAYLOAD)).hexdigest()


def test_unicode_is_hashed_as_utf8_text_not_escapes():
    assert "héllo ✓".encode() in ev.canonical_bytes(CES_DECISION, VECTOR_DECISION)


@pytest.mark.parametrize("bad", [1.5, 1.0, 1e300, float("inf"), float("-inf")])
def test_floats_are_refused_so_the_hash_stays_stable(bad):
    req = EvidenceObjectCreate(schema_id=CES_FACT, payload=FACT_PAYLOAD | {"n": bad})
    with pytest.raises(EvidenceError) as exc:
        ev.build_object(req)
    assert exc.value.code == "evidence_payload_invalid"
    expected = "NaN/Infinity" if bad in (float("inf"), float("-inf")) else "floating-point"
    assert expected in exc.value.reasons[0]["message"]


def test_nan_is_refused():
    with pytest.raises(EvidenceError) as exc:
        ev.build_object(EvidenceObjectCreate(schema_id=CES_FACT, payload=FACT_PAYLOAD | {"n": float("nan")}))
    assert exc.value.code == "evidence_payload_invalid"


def test_integers_booleans_null_lists_and_nested_objects_are_fine():
    resp = post(payload=FACT_PAYLOAD | {"count": 3, "flag": True, "nothing": None, "items": [1, "a", {"k": 2}], "nested": {"a": {"b": 1}}})
    assert resp.status_code == 200


def test_too_deeply_nested_payloads_are_refused():
    deep = current = {}
    for _ in range(12):
        current["x"] = {}
        current = current["x"]
    resp = post(payload=FACT_PAYLOAD | {"deep": deep})
    assert resp.status_code == 422 and "nested too deeply" in json.dumps(resp.json())


# --- the 64 KB inline limit and pointers ----------------------------------------------------------------------

def _padded(extra: int) -> dict:
    """A FactEvidence payload whose canonical envelope is exactly INLINE_LIMIT_BYTES + extra bytes."""
    base = ev.canonical_bytes(CES_FACT, FACT_PAYLOAD | {"pad": ""})
    return FACT_PAYLOAD | {"pad": "x" * (ev.INLINE_LIMIT_BYTES + extra - len(base))}


def test_exactly_64_kb_is_accepted_and_one_byte_more_is_refused_with_a_pointer_hint():
    ok = post(payload=_padded(0))
    assert ok.status_code == 200 and ok.json()["evidence"]["size_bytes"] == ev.INLINE_LIMIT_BYTES
    big = post(payload=_padded(1))
    assert big.status_code == 413 and big.json()["code"] == "evidence_too_large"
    assert "pointer" in big.json()["detail"]


def test_a_pointer_records_large_content_without_storing_it():
    resp = post(pointer=POINTER)
    assert resp.status_code == 200
    stored = resp.json()["evidence"]
    assert stored["pointer"] == POINTER and stored["size_bytes"] < ev.INLINE_LIMIT_BYTES


@pytest.mark.parametrize(
    "bad",
    [
        {"uri": "", "sha256": "ab" * 32, "size_bytes": 1},
        {"uri": "file:///x", "sha256": "AB" * 32, "size_bytes": 1},
        {"uri": "file:///x", "sha256": "ab" * 31, "size_bytes": 1},
        {"uri": "file:///x", "sha256": "ab" * 32, "size_bytes": -1},
        {"uri": "file:///x", "sha256": "ab" * 32, "size_bytes": "10"},
        {"uri": "file:///x", "sha256": "ab" * 32, "size_bytes": True},
        {"uri": "file:///x", "sha256": "ab" * 32},
        {"uri": "file:///x", "sha256": "ab" * 32, "size_bytes": 1, "secret": "x"},
        {"uri": "x" * 1001, "sha256": "ab" * 32, "size_bytes": 1},
    ],
)
def test_a_malformed_pointer_is_refused(bad):
    resp = post(pointer=bad)
    assert resp.status_code == 422 and resp.json()["code"] == "evidence_payload_invalid"


def test_verify_says_a_pointers_content_was_not_checked():
    oid = post(pointer=POINTER).json()["evidence"]["id"]
    out = client.get(f"/api/jarvis/evidence/{oid}/verify").json()
    assert out["ok"] is True and out["pointer"] == {"present": True, "content_hash_checked": False, "note": out["pointer"]["note"]}


# --- the minimal local CES ------------------------------------------------------------------------------------

def test_an_unknown_schema_is_refused():
    resp = post(schema_id="CES.ARIS.Decision.v1")
    assert resp.status_code == 422 and resp.json()["code"] == "evidence_schema_unknown"


@pytest.mark.parametrize("missing", ["statement", "authority", "source"])
def test_a_decision_evidence_needs_its_required_fields(missing):
    payload = {k: v for k, v in DECISION_PAYLOAD.items() if k != missing}
    resp = post(schema=CES_DECISION, payload=payload)
    assert resp.status_code == 422 and resp.json()["code"] == "evidence_payload_invalid"
    assert f"payload.{missing} is required" in json.dumps(resp.json())


@pytest.mark.parametrize("missing", ["observation", "method", "source"])
def test_a_fact_evidence_needs_its_required_fields(missing):
    payload = {k: v for k, v in FACT_PAYLOAD.items() if k != missing}
    resp = post(payload=payload)
    assert resp.status_code == 422 and f"payload.{missing} is required" in json.dumps(resp.json())


@pytest.mark.parametrize("field,value", [("statement", ""), ("statement", "   "), ("statement", 5), ("authority", None), ("source", ["x"]), ("statement", "x" * 4001)])
def test_a_decision_evidence_rejects_empty_or_wrongly_typed_fields(field, value):
    resp = post(schema=CES_DECISION, payload=DECISION_PAYLOAD | {field: value})
    assert resp.status_code == 422


@pytest.mark.parametrize("method", ["chat", "", "telepathy", 7, None])
def test_a_fact_evidence_method_must_be_a_checkable_kind(method):
    resp = post(payload=FACT_PAYLOAD | {"method": method})
    assert resp.status_code == 422


@pytest.mark.parametrize("method", sorted(ev.FACT_METHODS))
def test_every_checkable_method_is_accepted(method):
    assert post(payload=FACT_PAYLOAD | {"method": method}).status_code == 200


def test_timestamps_must_be_iso_8601_when_present():
    assert post(payload=FACT_PAYLOAD | {"observed_at": "2026-10-06T07:00:00Z"}).status_code == 200
    assert post(schema=CES_DECISION, payload=DECISION_PAYLOAD | {"decided_at": "2026-10-06T07:00:00+00:00"}).status_code == 200
    assert post(payload=FACT_PAYLOAD | {"observed_at": "yesterday"}).status_code == 422
    assert post(schema=CES_DECISION, payload=DECISION_PAYLOAD | {"decided_at": "soon"}).status_code == 422


def test_extra_payload_fields_are_allowed_and_part_of_the_hash():
    a = post(payload=FACT_PAYLOAD | {"extra": "one"}).json()["evidence"]["id"]
    b = post(payload=FACT_PAYLOAD | {"extra": "two"}).json()["evidence"]["id"]
    assert a != b


# --- the API ---------------------------------------------------------------------------------------------------

def test_create_get_and_idempotence():
    first = post()
    assert first.status_code == 200 and first.json()["created"] is True
    obj = first.json()["evidence"]
    assert obj["id"] == VECTOR_FACT_ID and obj["schema_id"] == CES_FACT and obj["created_by"] == "test"
    again = post(source_agent="someone-else")
    assert again.status_code == 200 and again.json()["created"] is False
    assert again.json()["evidence"]["created_by"] == "test" and again.json()["evidence"]["created_at"] == obj["created_at"]  # the first record stands
    got = client.get(f"/api/jarvis/evidence/{obj['id']}")
    assert got.status_code == 200 and got.json()["evidence"] == obj


def test_a_supplied_id_must_match_the_content():
    assert post(id=VECTOR_FACT_ID).status_code == 200
    resp = post(id="eo:sha256:" + "0" * 64)
    assert resp.status_code == 422 and resp.json()["code"] == "evidence_hash_mismatch"
    assert VECTOR_FACT_ID in json.dumps(resp.json())


def test_unknown_and_malformed_ids():
    assert client.get("/api/jarvis/evidence/eo:sha256:" + "0" * 64).status_code == 404
    bad = client.get("/api/jarvis/evidence/not-an-id")
    assert bad.status_code == 422 and bad.json()["code"] == "evidence_id_invalid"
    assert client.get("/api/jarvis/evidence/eo:sha256:" + "A" * 64 + "/verify").status_code == 422


def test_there_is_no_way_to_change_or_remove_an_object_through_the_api():
    oid = post().json()["evidence"]["id"]
    for method in ("patch", "put", "delete"):
        assert getattr(client, method)(f"/api/jarvis/evidence/{oid}").status_code == 405
    assert client.get(f"/api/jarvis/evidence/{oid}").status_code == 200


def test_creating_needs_the_operator_key_and_never_an_oauth_token(monkeypatch):
    from app import auth

    monkeypatch.setattr(auth, "oauth_enabled", lambda: True)
    with pytest.raises(HTTPException) as exc:
        ev.require_operator_write()
    assert exc.value.status_code == 403 and "operator key only" in exc.value.detail


def test_creating_is_refused_when_writes_are_off(monkeypatch):
    monkeypatch.setenv("JARVIS_MEMORY_WRITE_ENABLED", "false")
    resp = post()
    assert resp.status_code == 403 and resp.json()["code"] == "denied"
    assert client.get("/api/jarvis/evidence/" + VECTOR_FACT_ID).status_code == 404  # nothing was stored


def _sidecar() -> Path:
    return Path(os.environ["JARVIS_STORE_PATH"]).with_name("jarvis-store.evidence.jsonl")


@pytest.mark.json_store_only
def test_verify_passes_and_then_catches_tampering_with_the_stored_file():
    oid = post().json()["evidence"]["id"]
    assert client.get(f"/api/jarvis/evidence/{oid}/verify").json()["ok"] is True
    path = _sidecar()
    path.write_text(path.read_text("utf-8").replace("127.0.0.1:8011", "127.0.0.1:8001"), "utf-8")  # change the content, keep the id
    out = client.get(f"/api/jarvis/evidence/{oid}/verify").json()
    assert out["ok"] is False and any("hash mismatch" in p for p in out["problems"])


def test_the_legacy_blob_store_has_no_evidence_objects_and_says_so():
    store = PostgresJarvisStore("postgresql://nobody@nowhere/none", "t")
    with pytest.raises(NotImplementedError):
        store.put_evidence_object(EvidenceObjectCreate(schema_id=CES_FACT, payload=FACT_PAYLOAD))
    with pytest.raises(NotImplementedError):
        store.get_evidence_object(VECTOR_FACT_ID)
    assert store._resolve_evidence(VECTOR_FACT_ID) is None  # so a link to one is unresolved


# --- linking from records ---------------------------------------------------------------------------------------

def record(mem_type="fact", links=None, **over):
    body = {
        "content": "The ledger listens on loopback port 8011.",
        "source_agent": "test",
        "session_id": "s1",
        "type": mem_type,
        "evidence": links if links is not None else [],
    }
    body.update(over)
    return client.post("/api/jarvis/memory", json=body)


def link(oid):
    return {"kind": "evidence-object", "ref": oid}


@pytest.fixture
def enforce(monkeypatch):
    monkeypatch.setenv("JARVIS_CLAUSE_V", "enforce")
    monkeypatch.setenv("JARVIS_CLAUSE_V_SOFT", "warn")


@pytest.fixture
def objects():
    return {
        "fact": post().json()["evidence"]["id"],
        "decision": post(schema=CES_DECISION).json()["evidence"]["id"],
    }


@pytest.mark.parametrize("mem_type", ["fact", "architecture", "research"])
def test_an_intact_fact_evidence_object_counts_as_evidence(enforce, objects, mem_type):
    resp = record(mem_type, [link(objects["fact"])])
    assert resp.status_code == 200 and resp.json()["memory"]["evidence"][0]["ref"] == objects["fact"]


@pytest.mark.parametrize("mem_type", ["fact", "architecture", "research"])
def test_a_decision_evidence_object_is_not_evidence_for_a_fact(enforce, objects, mem_type):
    resp = record(mem_type, [link(objects["decision"])])
    assert resp.status_code == 422 and resp.json()["code"] == "clause_v_violation"
    assert codes(resp) == ["clause_v_evidence_required"] and "evidence-object link to an intact" in json.dumps(resp.json())


def test_a_decision_may_cite_either_kind_of_object(enforce, objects):
    assert record("decision", [link(objects["decision"])], content="Decision one.").status_code == 200
    assert record("decision", [link(objects["fact"])], content="Decision two.").status_code == 200


@pytest.mark.parametrize("gate", ["enforce", "off"])
def test_a_link_that_resolves_to_nothing_is_refused_whether_or_not_clause_v_is_on(monkeypatch, gate):
    monkeypatch.setenv("JARVIS_CLAUSE_V", gate)
    ghost = "eo:sha256:" + "f" * 64
    resp = record("decision", [{"kind": "user-request", "ref": "chat:1"}, link(ghost)])
    assert resp.status_code == 422 and resp.json()["code"] == "evidence_object_invalid"
    assert resp.json()["reasons"] == [{"code": "evidence_object_unresolved", "ref": ghost, "message": "no such evidence object in this ledger"}]


@pytest.mark.parametrize("ref", ["eo:sha256:abc", "sha256:" + "a" * 64, "", "EO:SHA256:" + "a" * 64, "eo:sha256:" + "A" * 64])
def test_a_malformed_object_reference_is_refused(enforce, ref):
    resp = record("decision", [{"kind": "evidence-object", "ref": ref or " "}])
    assert resp.status_code == 422 and resp.json()["code"] == "evidence_object_invalid"
    assert resp.json()["reasons"][0]["code"] == "evidence_object_unresolved"


def test_only_the_bad_link_is_reported(enforce, objects):
    ghost = "eo:sha256:" + "e" * 64
    resp = record("fact", [link(objects["fact"]), link(ghost)])
    assert resp.status_code == 422 and [r["ref"] for r in resp.json()["reasons"]] == [ghost]


@pytest.mark.json_store_only
def test_a_damaged_object_cannot_be_linked(enforce, objects):
    path = _sidecar()
    path.write_text(path.read_text("utf-8").replace("127.0.0.1:8011", "127.0.0.1:9999"), "utf-8")
    resp = record("fact", [link(objects["fact"])])
    assert resp.status_code == 422 and resp.json()["reasons"][0]["code"] == "evidence_object_hash_mismatch"


def test_other_evidence_kinds_still_work_and_are_not_resolved(enforce):
    assert record("fact", [{"kind": "file", "ref": "docs/POSTGRES.md"}]).status_code == 200


def test_patch_cannot_add_a_dangling_link_but_can_add_a_real_one(enforce, objects):
    rec = record("fact", [{"kind": "file", "ref": "docs/POSTGRES.md"}]).json()["memory"]
    ghost = "eo:sha256:" + "d" * 64
    bad = client.patch(f"/api/jarvis/memory/{rec['id']}", json={"evidence": [link(ghost)]})
    assert bad.status_code == 422 and bad.json()["code"] == "evidence_object_invalid"
    good = client.patch(f"/api/jarvis/memory/{rec['id']}", json={"evidence": [link(objects["fact"])], "status": "verified"})
    assert good.status_code == 200 and good.json()["memory"]["status"] == "verified"


def test_a_chat_backed_fact_can_be_rescued_by_attaching_an_evidence_object(monkeypatch, objects):
    monkeypatch.setenv("JARVIS_CLAUSE_V", "off")
    legacy = record("fact", [{"kind": "user-request", "ref": "chat:1"}], content="An old chat-backed fact.").json()["memory"]
    monkeypatch.setenv("JARVIS_CLAUSE_V", "enforce")
    assert client.patch(f"/api/jarvis/memory/{legacy['id']}", json={"status": "verified"}).status_code == 422
    fixed = client.patch(f"/api/jarvis/memory/{legacy['id']}", json={"status": "verified", "evidence": [link(objects["fact"])]})
    assert fixed.status_code == 200 and fixed.json()["memory"]["status"] == "verified"


def test_the_ids_in_links_are_the_same_ids_the_api_returns(objects):
    assert objects["fact"] == VECTOR_FACT_ID  # the frozen vector is also what the API stores
