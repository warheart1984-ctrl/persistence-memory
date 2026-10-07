"""Signatures on PostgreSQL and over HTTP: the store functions, the endpoints, the pg_verify section, and every tamper."""

from __future__ import annotations

import json

import psycopg
import pytest
from fastapi.testclient import TestClient

from app import attest, pg_store, pg_verify
from app.main import app
from app.pg_schema import EXPECTED_SCHEMA_VERSION, migrate
from tests.attest_support import NOW, TENANT, make_key, requires_ssh_keygen, sign, sha, statement

pytestmark = [requires_ssh_keygen, pytest.mark.postgres]

KEY = "attest-api-test-key"
HDR = {"X-API-Key": KEY}
OP = "operator"  # the tenant the API key maps to


@pytest.fixture(scope="module")
def keys(tmp_path_factory):
    d = tmp_path_factory.mktemp("apikeys")
    return {n: make_key(d, n) for n in ("root", "root2", "mint", "mint2", "stranger")}


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


@pytest.fixture
def roots(keys, tmp_path, monkeypatch):
    f = tmp_path / "roots.pub"
    f.write_text(f"# pinned roots\n{keys['root'].pub_text}\n")
    monkeypatch.setenv(attest.ROOTS_ENV, str(f))
    return f


def add_records(client, n, prefix="signed"):
    for i in range(n):
        r = client.post("/api/jarvis/memory", headers=HDR, json={"content": f"{prefix} record {i} for the signature tests", "source_agent": "t", "session_id": "s", "type": "fact"})
        assert r.status_code == 200, r.text


def seal(client, n=6, size=3):
    add_records(client, n)
    r = client.post("/api/jarvis/blocks/seal", headers=HDR, json={"force": True, "max_entries": size})
    assert r.status_code == 200, r.text
    return r.json()["sealed"]


def head(client):
    return client.get("/api/jarvis/attestations/head", headers=HDR).json()


def post_statement(client, root, kind, *, key=None, key_id=None, arg=None, subject_hash=None, expect=200, **override):
    h = head(client)
    s = statement(root, kind, h["next_stmt_seq"], h["trust_head_hash"], key=key, key_id=key_id, arg=arg, subject_hash=subject_hash, tenant=OP)
    body = {"kind": kind, "key_id": s.key_id, "pubkey": s.pubkey, "arg": s.arg, "subject_hash": s.subject_hash, "stmt_seq": s.stmt_seq,
            "prev_hash": s.prev_hash, "signed_by": s.signed_by, "signature": s.signature} | override
    r = client.post("/api/jarvis/trust/statements", headers=HDR, json=body)
    assert r.status_code == expect, r.text
    return r


def post_attestation(client, signer, kind, subject, subject_hash, *, expect=200, signed_at=NOW, **override):
    h = head(client)
    seq, prev = override.pop("signer_seq", h["next_signer_seq"]), override.pop("prev_hash", h["prev_hash"])
    message = attest.attestation_message(kind, OP, subject, subject_hash, seq, prev, signed_at)
    sig = attest.normalize_signature(override.pop("signature", None) or sign(signer, message))
    body = {"kind": kind, "subject": subject, "subject_hash": subject_hash, "signer_seq": seq, "prev_hash": prev,
            "key_id": signer.key_id, "signed_at": signed_at, "signature": sig} | override
    r = client.post("/api/jarvis/attestations", headers=HDR, json=body)
    assert r.status_code == expect, r.text
    return r


def authorize_mint(client, keys, from_seq=1):
    return post_statement(client, keys["root"], "key", key=keys["mint"], arg=from_seq)


def sign_block(client, keys, height, **kw):
    block = client.get(f"/api/jarvis/blocks/{height}", headers=HDR).json()["block"]
    return post_attestation(client, keys["mint"], "block", f"block:{height}", block["block_hash"], **kw)


def verify(client):
    return client.get("/api/jarvis/attestations/verify", headers=HDR).json()


# --- access --------------------------------------------------------------------------------------------------------------------------

ROUTES = [("get", "/api/jarvis/attestations"), ("get", "/api/jarvis/attestations/head"), ("get", "/api/jarvis/attestations/pending"),
          ("get", "/api/jarvis/attestations/verify"), ("post", "/api/jarvis/attestations"), ("get", "/api/jarvis/trust"),
          ("get", "/api/jarvis/trust/statements"), ("post", "/api/jarvis/trust/statements")]


@pytest.mark.parametrize("method,path", ROUTES)
def test_every_signature_route_needs_the_operator_key(client, method, path):
    assert getattr(client, method)(path).status_code == 401
    assert getattr(client, method)(path, headers={"X-API-Key": "wrong"}).status_code == 401


def test_the_routes_are_guarded_by_the_operator_only_dependencies():
    seen = {}
    for route in app.routes:
        path = getattr(route, "path", "")
        if path.startswith(("/api/jarvis/attestations", "/api/jarvis/trust")):
            seen[(tuple(sorted(route.methods)), path)] = {d.call.__name__ for d in route.dependant.dependencies}
    assert len(seen) == 8
    for key, names in seen.items():
        assert names & {"require_operator_read", "require_operator_write"}, key
    for key, names in seen.items():
        if key[0] == ("POST",):
            assert "require_operator_write" in names, key


def test_writes_disabled_stops_storing_but_not_reading(client, roots, keys, monkeypatch):
    authorize_mint(client, keys)
    monkeypatch.setenv("JARVIS_MEMORY_WRITE_ENABLED", "false")
    assert client.post("/api/jarvis/trust/statements", headers=HDR, json={}).status_code == 403
    assert client.get("/api/jarvis/trust", headers=HDR).status_code == 200


@pytest.mark.json_store_only
@pytest.mark.parametrize("method,path", ROUTES)
def test_the_json_store_answers_501(method, path):
    attestation = {"kind": "block", "subject": "block:1", "subject_hash": "a" * 64, "signer_seq": 1, "prev_hash": "0" * 64,
                   "key_id": "SHA256:" + "A" * 43, "signed_at": NOW, "signature": "A" * 120}
    statement_body = {"kind": "key", "key_id": "SHA256:" + "A" * 43, "pubkey": "x" * 80, "arg": 1, "stmt_seq": 1, "prev_hash": "0" * 64,
                      "signed_by": "SHA256:" + "B" * 43, "signature": "A" * 120}
    body = None if method == "get" else (attestation if path.endswith("attestations") else statement_body)
    with TestClient(app, raise_server_exceptions=False) as c:
        r = c.get(path) if body is None else c.post(path, json=body)
        assert r.status_code == 501


# --- without a trust root nothing is stored and nothing is called verified ----------------------------------------------------------------

def test_with_no_trust_root_nothing_can_be_stored(client, keys, monkeypatch):
    monkeypatch.delenv(attest.ROOTS_ENV, raising=False)
    s = statement(keys["root"], "key", 1, attest.GENESIS, key=keys["mint"], arg=1, tenant=OP)
    r = client.post("/api/jarvis/trust/statements", headers=HDR, json={"kind": "key", "key_id": s.key_id, "pubkey": s.pubkey, "arg": 1, "stmt_seq": 1,
                                                                       "prev_hash": s.prev_hash, "signed_by": s.signed_by, "signature": s.signature})
    assert r.status_code == 409 and r.json()["code"] == "no_trust_root"
    assert client.get("/api/jarvis/trust", headers=HDR).json()["trust_roots_configured"] is False
    assert client.get("/api/jarvis/trust/statements", headers=HDR).json()["count"] == 0


def test_rows_that_exist_but_cannot_be_checked_are_never_reported_as_verified(client, roots, keys, monkeypatch):
    seal(client)
    authorize_mint(client, keys)
    sign_block(client, keys, 1)
    monkeypatch.delenv(attest.ROOTS_ENV)
    v = verify(client)
    assert v["ok"] is True and v["summary"] == {} and any("signatures not verified: no trust root is configured" in w for w in v["warnings"])


# --- the trust log through the API ------------------------------------------------------------------------------------------------------

def test_a_root_authorizes_a_signing_key_and_the_state_shows_it(client, roots, keys):
    r = authorize_mint(client, keys).json()
    assert (r["stmt_seq"], r["kind"], r["key_id"]) == (1, "key", keys["mint"].key_id) and len(r["statement_hash"]) == 64
    t = client.get("/api/jarvis/trust", headers=HDR).json()
    assert t["trust_roots_configured"] is True and t["pinned_roots"] == [keys["root"].key_id] and t["problems"] == []
    assert t["keys"] == [{"key_id": keys["mint"].key_id, "from_signer_seq": 1, "revoked_after_signer_seq": None, "authorized_by_statement": 1}]
    listed = client.get("/api/jarvis/trust/statements", headers=HDR).json()
    assert listed["count"] == 1 and listed["statements"][0]["signed_by"] == keys["root"].key_id and listed["statements"][0]["pubkey"] == keys["mint"].pub_text
    assert client.get("/api/jarvis/trust/statements?after_seq=1", headers=HDR).json()["count"] == 0


@pytest.mark.parametrize("name,kw,status,code", [
    ("signed by a stranger", {"root": "stranger"}, 422, "statement_invalid"),
    ("signed by the signing key itself", {"root": "mint"}, 422, "statement_invalid"),
])
def test_a_statement_not_signed_by_a_root_is_refused(client, roots, keys, name, kw, status, code):
    r = post_statement(client, keys[kw["root"]], "key", key=keys["mint2"], arg=1, expect=status)
    assert r.json()["code"] == code and "not a root key" in r.json()["detail"]
    assert client.get("/api/jarvis/trust/statements", headers=HDR).json()["count"] == 0


def test_a_statement_with_a_bad_signature_a_wrong_position_or_a_wrong_key_is_refused(client, roots, keys):
    good = post_statement(client, keys["root"], "key", key=keys["mint"], arg=1, expect=200)
    forged_sig = sign(keys["stranger"], "not the message")
    assert post_statement(client, keys["root"], "revoke", key_id=keys["mint"].key_id, arg=3, signature=forged_sig, expect=422).json()["code"] == "statement_invalid"
    assert post_statement(client, keys["root"], "revoke", key_id=keys["mint"].key_id, arg=3, stmt_seq=5, expect=409).json()["code"] == "statement_out_of_order"
    assert post_statement(client, keys["root"], "revoke", key_id=keys["mint"].key_id, arg=3, prev_hash=sha("else"), expect=409).json()["code"] == "statement_out_of_order"
    r = post_statement(client, keys["root"], "key", key=keys["mint2"], key_id=keys["mint"].key_id, arg=2, pubkey=keys["mint2"].pub_text, expect=422)
    assert r.json()["code"] == "statement_invalid"  # a public key that is not the fingerprint it is filed under
    assert client.get("/api/jarvis/trust/statements", headers=HDR).json()["count"] == 1 and good.json()["stmt_seq"] == 1


def test_a_second_root_can_be_added_by_a_root_and_then_acts_alone(client, roots, keys):
    post_statement(client, keys["root"], "root_add", key=keys["root2"])
    post_statement(client, keys["root2"], "key", key=keys["mint"], arg=1)
    t = client.get("/api/jarvis/trust", headers=HDR).json()
    assert set(t["roots"]) == {keys["root"].key_id, keys["root2"].key_id} and t["pinned_roots"] == [keys["root"].key_id] and len(t["keys"]) == 1


def test_a_revoked_key_keeps_its_old_attestations_and_loses_the_later_ones(client, roots, keys):
    seal(client)  # blocks 1 and 2
    authorize_mint(client, keys)
    sign_block(client, keys, 1)
    post_statement(client, keys["root"], "revoke", key_id=keys["mint"].key_id, arg=1)
    r = sign_block(client, keys, 2, expect=422)
    assert r.json()["code"] == "attestation_invalid" and "revoked with a cutoff of 1" in r.json()["detail"]
    v = verify(client)
    assert v["ok"] is True and v["summary"]["blocks_signed"] == 1  # block 1's signature predates the cutoff and stays valid


def test_a_rotation_keeps_the_log_going_under_the_new_key(client, roots, keys):
    seal(client)
    authorize_mint(client, keys)
    sign_block(client, keys, 1)
    post_statement(client, keys["root"], "key", key=keys["mint2"], arg=2)
    post_statement(client, keys["root"], "revoke", key_id=keys["mint"].key_id, arg=1)
    block = client.get("/api/jarvis/blocks/2", headers=HDR).json()["block"]
    post_attestation(client, keys["mint2"], "block", "block:2", block["block_hash"])
    v = verify(client)
    assert v["ok"] is True and v["summary"]["blocks_signed"] == 2 and v["summary"]["keys_authorized"] == 2


# --- attestations through the API ------------------------------------------------------------------------------------------------------------

def test_a_block_is_attested_listed_and_verified(client, roots, keys):
    seal(client)
    authorize_mint(client, keys)
    pending = client.get("/api/jarvis/attestations/pending", headers=HDR).json()
    assert [b["height"] for b in pending["blocks"]] == [1, 2] and pending["receipts"] == [] and pending["head"]["next_signer_seq"] == 1
    r = sign_block(client, keys, 1).json()
    assert r["signer_seq"] == 1 and r["key_id"] == keys["mint"].key_id
    assert [b["height"] for b in client.get("/api/jarvis/attestations/pending", headers=HDR).json()["blocks"]] == [2]
    sign_block(client, keys, 2)
    rows = client.get("/api/jarvis/attestations", headers=HDR).json()
    assert rows["count"] == 2 and [a["subject"] for a in rows["attestations"]] == ["block:1", "block:2"]
    assert rows["attestations"][1]["prev_hash"] == rows["attestations"][0]["attestation_hash"]
    h = head(client)
    assert (h["head_seq"], h["next_signer_seq"], h["prev_hash"]) == (2, 3, rows["attestations"][1]["attestation_hash"]) and h["tip_height"] == 2
    v = verify(client)
    assert v["ok"] is True and v["problems"] == [] and v["summary"]["blocks_signed"] == 2 and v["summary"]["head_seq"] == 2
    assert client.get("/api/jarvis/attestations?after_seq=1&limit=1", headers=HDR).json()["count"] == 1


def test_the_stored_hash_is_the_one_python_computes(client, roots, keys):
    seal(client)
    authorize_mint(client, keys)
    r = sign_block(client, keys, 1).json()
    row = client.get("/api/jarvis/attestations", headers=HDR).json()["attestations"][0]
    message = attest.attestation_message("block", OP, "block:1", row["subject_hash"], 1, attest.GENESIS, NOW)
    assert r["attestation_hash"] == row["attestation_hash"] == attest.attestation_hash(message, keys["mint"].key_id, row["signature"])


@pytest.mark.parametrize("name,override,status,code,needle", [
    ("a wrong block hash", {"subject_hash": sha("not the block")}, 422, "attestation_invalid", "but the block's hash is"),
    ("a block that does not exist", {"subject": "block:9"}, 422, "attestation_invalid", "does not exist"),
    ("a skipped sequence number", {"signer_seq": 4}, 409, "attestation_out_of_order", "next attestation is 1"),
    ("a wrong previous hash", {"prev_hash": sha("else")}, 409, "attestation_out_of_order", "must name"),
    ("a signature over another message", {"signature": "SIG"}, 422, "attestation_invalid", "bad signature"),
])
def test_a_wrong_attestation_is_refused_and_nothing_is_stored(client, roots, keys, name, override, status, code, needle):
    seal(client)
    authorize_mint(client, keys)
    block = client.get("/api/jarvis/blocks/1", headers=HDR).json()["block"]
    override = dict(override)
    if override.get("signature") == "SIG":
        override["signature"] = sign(keys["mint"], "some other message")
    subject_hash = override.pop("subject_hash", block["block_hash"])
    subject = override.pop("subject", "block:1")
    r = post_attestation(client, keys["mint"], "block", subject, subject_hash, expect=status, **override)
    assert r.json()["code"] == code and needle in r.json()["detail"]
    assert client.get("/api/jarvis/attestations", headers=HDR).json()["count"] == 0


def test_an_unauthorized_key_cannot_attest(client, roots, keys):
    seal(client)
    authorize_mint(client, keys)
    block = client.get("/api/jarvis/blocks/1", headers=HDR).json()["block"]
    r = post_attestation(client, keys["stranger"], "block", "block:1", block["block_hash"], expect=422)
    assert "no root authorized" in r.json()["detail"]


def test_a_key_authorized_from_a_later_position_cannot_attest_earlier(client, roots, keys):
    seal(client)
    post_statement(client, keys["root"], "key", key=keys["mint"], arg=3)
    block = client.get("/api/jarvis/blocks/1", headers=HDR).json()["block"]
    r = post_attestation(client, keys["mint"], "block", "block:1", block["block_hash"], expect=422)
    assert "not authorized until attestation 3" in r.json()["detail"]


def test_the_same_block_cannot_be_attested_twice(client, roots, keys):
    seal(client)
    authorize_mint(client, keys)
    sign_block(client, keys, 1)
    r = sign_block(client, keys, 1, expect=409)
    assert r.json()["code"] == "already_attested"
    assert client.get("/api/jarvis/attestations", headers=HDR).json()["count"] == 1


def test_a_receipt_is_attested_and_a_damaged_or_foreign_one_is_not(client, roots, keys, pg):
    seal(client)
    authorize_mint(client, keys)
    receipt = client.post("/api/jarvis/replay/receipts", headers=HDR, json={"at_block": 2}).json()["receipt"]
    assert [x["id"] for x in client.get("/api/jarvis/attestations/pending", headers=HDR).json()["receipts"]] == [receipt["id"]]
    h = receipt["id"].split(":")[-1]
    post_attestation(client, keys["mint"], "receipt", receipt["id"], h)
    v = verify(client)
    assert v["ok"] is True and v["summary"]["receipts_signed"] == 1
    fact = client.post("/api/jarvis/evidence", headers=HDR, json={"schema_id": "CES.Local.FactEvidence.v1", "source_agent": "t",
                                                                   "payload": {"observation": "o", "method": "command", "source": "s"}}).json()["evidence"]
    assert "does not exist" in post_attestation(client, keys["mint"], "receipt", fact["id"], fact["id"].split(":")[-1], expect=422).json()["detail"]
    with pg.admin_conn() as conn:
        conn.execute("ALTER TABLE evidence_objects DISABLE TRIGGER evidence_objects_no_update")
        conn.execute("UPDATE evidence_objects SET payload = jsonb_set(payload, '{state_root}', %s::jsonb) WHERE id = %s", ('"' + "a" * 64 + '"', receipt["id"]))
        conn.execute("ALTER TABLE evidence_objects ENABLE TRIGGER evidence_objects_no_update")
    v = verify(client)
    assert v["ok"] is False and any("is damaged" in p["problem"] for p in v["problems"])


def test_a_checkpoint_must_describe_the_log_and_the_tip(client, roots, keys):
    seal(client)
    authorize_mint(client, keys)
    sign_block(client, keys, 1)
    sign_block(client, keys, 2)
    h = head(client)
    subject = attest.checkpoint_subject(h["head_seq"], h["head_hash"], h["tip_height"], h["tip_block_hash"])
    good_hash = attest.checkpoint_hash(OP, h["head_seq"], h["head_hash"], h["tip_height"], h["tip_block_hash"])
    bad = post_attestation(client, keys["mint"], "checkpoint", attest.checkpoint_subject(h["head_seq"], sha("not the head"), h["tip_height"], h["tip_block_hash"]),
                           attest.checkpoint_hash(OP, h["head_seq"], sha("not the head"), h["tip_height"], h["tip_block_hash"]), expect=422)
    assert "actual head" in bad.json()["detail"]
    post_attestation(client, keys["mint"], "checkpoint", subject, good_hash)
    v = verify(client)
    assert v["ok"] is True and v["summary"]["newest_checkpoint_seq"] == 3


def test_a_cosign_marks_a_checkpoint_as_witnessed_and_must_name_a_real_one(client, roots, keys):
    seal(client)
    authorize_mint(client, keys)
    sign_block(client, keys, 1)
    h = head(client)
    cp_subject = attest.checkpoint_subject(h["head_seq"], h["head_hash"], h["tip_height"], h["tip_block_hash"])
    cp = post_attestation(client, keys["mint"], "checkpoint", cp_subject, attest.checkpoint_hash(OP, h["head_seq"], h["head_hash"], h["tip_height"], h["tip_block_hash"])).json()
    r = post_statement(client, keys["root"], "cosign", key_id=keys["root"].key_id, arg=cp["signer_seq"], subject_hash=sha("another checkpoint"), expect=422)
    assert r.json()["code"] == "cosign_unknown_checkpoint"
    post_statement(client, keys["root"], "cosign", key_id=keys["root"].key_id, arg=cp["signer_seq"], subject_hash=cp["attestation_hash"])
    v = verify(client)
    assert v["ok"] is True and v["summary"]["cosigned_checkpoint_seq"] == cp["signer_seq"]
    assert client.get("/api/jarvis/trust", headers=HDR).json()["cosigns"][0]["checkpoint_seq"] == cp["signer_seq"]


@pytest.mark.parametrize("body", [{}, {"kind": "block"}, {"signer_seq": 0}, {"subject_hash": "xyz"}, {"key_id": "SHA256:short"}, {"signed_at": "today"}, {"signature": "x"}])
def test_malformed_bodies_are_422(client, roots, keys, body):
    seal(client)
    full = {"kind": "block", "subject": "block:1", "subject_hash": "a" * 64, "signer_seq": 1, "prev_hash": "0" * 64, "key_id": keys["mint"].key_id,
            "signed_at": NOW, "signature": "A" * 120}
    payload = body if not body or body == {} else {**full, **body}
    assert client.post("/api/jarvis/attestations", headers=HDR, json=payload).status_code == 422


# --- the database ------------------------------------------------------------------------------------------------------------------------------

def test_the_application_role_can_read_the_logs_but_not_write_them_except_through_the_functions(pg, client, roots, keys):
    seal(client)
    authorize_mint(client, keys)
    sign_block(client, keys, 1)
    with pg.app_conn(OP) as conn:
        assert conn.execute("SELECT count(*) FROM attestations").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM trust_statements").fetchone()[0] == 1
    attempts = ["UPDATE attestations SET key_id = key_id", "DELETE FROM attestations", "TRUNCATE attestations",
                "DELETE FROM trust_statements", "UPDATE trust_statements SET kind = kind", "TRUNCATE trust_statements",
                "INSERT INTO attestations (tenant_key, signer_seq, kind, subject, subject_hash, prev_hash, key_id, signed_at, signature, attestation_hash) "
                f"VALUES ('{OP}', 2, 'block', 'block:2', '{'a' * 64}', '{'a' * 64}', 'SHA256:{'A' * 43}', '{NOW}', '{'x' * 120}', '{'a' * 64}')"]
    for statement_sql in attempts:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            with pg.app_conn(OP) as conn:
                conn.execute(statement_sql)


def test_even_the_owner_cannot_change_or_remove_a_row(pg, client, roots, keys):
    seal(client)
    authorize_mint(client, keys)
    sign_block(client, keys, 1)
    for sql in ("UPDATE attestations SET key_id = key_id", "DELETE FROM attestations", "TRUNCATE attestations", "UPDATE trust_statements SET kind = kind",
                "DELETE FROM trust_statements", "TRUNCATE trust_statements"):
        with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
            with pg.admin_conn() as conn:
                conn.execute(sql)


def test_the_store_functions_enforce_the_chain_and_the_session_tenant(pg, client, roots, keys):
    seal(client)
    authorize_mint(client, keys)
    sign_block(client, keys, 1)
    call = ("SELECT * FROM jarvis_store_attestation(%s, 'block', 'block:2', %s, %s, %s, %s, %s, %s)")
    block2 = client.get("/api/jarvis/blocks/2", headers=HDR).json()["block"]["block_hash"]
    row = client.get("/api/jarvis/attestations", headers=HDR).json()["attestations"][0]
    args = lambda **kw: (kw.get("tenant", OP), block2, kw.get("seq", 2), kw.get("prev", row["attestation_hash"]), keys["mint"].key_id, NOW, "S" * 120)  # noqa: E731
    for kw in ({"seq": 3}, {"seq": 1}, {"prev": sha("else")}):
        with pytest.raises(psycopg.DatabaseError) as exc:
            with pg.app_conn(OP) as conn:
                conn.execute(call, args(**kw))
        assert exc.value.sqlstate == "JA001"
    with pytest.raises(psycopg.errors.RaiseException, match="not the session tenant"):
        with pg.app_conn("bob") as conn:
            conn.execute(call, args())
    with pytest.raises(psycopg.errors.RaiseException, match="not the session tenant"):
        with pg.app_conn(None) as conn:
            conn.execute(call, args())


def test_the_tables_refuse_malformed_rows_whatever_the_caller_says(pg):
    good = {"tenant_key": "t", "signer_seq": 1, "kind": "block", "subject": "block:1", "subject_hash": "a" * 64, "prev_hash": "0" * 64,
            "key_id": "SHA256:" + "A" * 43, "signed_at": NOW, "signature": "x" * 120, "attestation_hash": "a" * 64}
    bad = [{"kind": "evidence"}, {"subject": "a|b"}, {"subject": "a\nb"}, {"subject_hash": "abc"}, {"prev_hash": "G" * 64}, {"key_id": "md5:abcd"},
           {"signed_at": "yesterday"}, {"signature": "short"}, {"signature": " " + "x" * 119}, {"signature": "x" * 60 + "\r\n" + "x" * 60},
           {"signer_seq": 0}, {"tenant_key": ""}, {"attestation_hash": "nothex"}]
    cols = ", ".join(good)
    marks = ", ".join(["%s"] * len(good))
    with pg.admin_conn() as conn:
        for change in bad:
            row = good | change
            with pytest.raises(psycopg.errors.CheckViolation):
                conn.execute(f"INSERT INTO attestations ({cols}) VALUES ({marks})", list(row.values()))
        conn.execute(f"INSERT INTO attestations ({cols}) VALUES ({marks})", list(good.values()))  # the unmodified row is fine


def test_sql_and_python_build_the_same_messages_and_hashes(pg):
    cases = [("block", "alice", "block:7", sha("a"), 4, sha("p"), NOW), ("receipt", "héllo wörld", "eo:sha256:" + sha("r"), sha("r"), 12, sha("q"), "2026-01-01T00:00:00Z"),
             ("checkpoint", "日本語", "checkpoint:3:" + sha("h") + ":2:" + sha("t"), sha("c"), 99, attest.GENESIS, NOW)]
    with pg.admin_conn() as conn:
        for kind, tenant, subject, shash, seq, prev, at in cases:
            sql_msg = conn.execute("SELECT jarvis_attestation_message(%s, %s, %s, %s, %s, %s, %s)", (kind, tenant, subject, shash, seq, prev, at)).fetchone()[0]
            assert sql_msg == attest.attestation_message(kind, tenant, subject, shash, seq, prev, at)
            sql_hash = conn.execute("SELECT jarvis_signed_hash(%s, %s, %s)", (sql_msg, "SHA256:" + "A" * 43, "SIG\nBODY")).fetchone()[0]
            assert sql_hash == attest.attestation_hash(sql_msg, "SHA256:" + "A" * 43, "  SIG\r\nBODY \n")
        for kind, arg, sh in (("key", 5, None), ("revoke", 0, None), ("root_add", None, None), ("cosign", 9, sha("x"))):
            sql_msg = conn.execute("SELECT jarvis_trust_message(%s, %s, %s, %s, %s, %s, %s)", (kind, "héllo", "SHA256:" + "B" * 43, arg, sh, 3, sha("p"))).fetchone()[0]
            assert sql_msg == attest.trust_message(kind, "héllo", "SHA256:" + "B" * 43, arg, sh, 3, sha("p"))


def test_tenants_have_separate_logs(pg, client, roots, keys):
    seal(client)
    authorize_mint(client, keys)
    with pg.app_conn("bob") as conn:
        assert conn.execute("SELECT count(*) FROM trust_statements").fetchone()[0] == 0
    store = pg_store.PostgresRowStore(pg.app_dsn, "bob", schema=pg.schema)
    assert store.list_trust_statements() == [] and store.attestation_head()["next_stmt_seq"] == 1 and store.trust_state()["keys"] == []


# --- tamper proofs: every way the logs can be damaged, found by verify -----------------------------------------------------------------------------

def tamper(pg, *sql):
    with pg.admin_conn() as conn:
        for table in ("attestations", "trust_statements"):
            for trig in ("no_update", "no_delete"):
                conn.execute(f"ALTER TABLE {table} DISABLE TRIGGER {table}_{trig}")
        try:
            for stmt in sql:
                conn.execute(stmt)
        finally:
            for table in ("attestations", "trust_statements"):
                for trig in ("no_update", "no_delete"):
                    conn.execute(f"ALTER TABLE {table} ENABLE TRIGGER {table}_{trig}")


def signed_ledger(client, keys, blocks=2):
    seal(client, n=blocks * 3)
    authorize_mint(client, keys)
    for h in range(1, blocks + 1):
        sign_block(client, keys, h)
    assert verify(client)["ok"] is True


def test_a_forged_signature_in_the_table_is_found(pg, client, roots, keys):
    signed_ledger(client, keys)
    other = attest.normalize_signature(sign(keys["mint"], "some other message"))
    tamper(pg, f"UPDATE attestations SET signature = '{other}' WHERE signer_seq = 2")
    v = verify(client)
    assert v["ok"] is False and any(p["subject"] == "attestation 2" and "stored attestation hash" in p["problem"] for p in v["problems"])


def test_a_forged_signature_with_the_hash_recomputed_is_still_found(pg, client, roots, keys):
    signed_ledger(client, keys)
    other = attest.normalize_signature(sign(keys["mint"], "some other message"))
    with pg.admin_conn() as conn:
        message = conn.execute("SELECT jarvis_attestation_message(kind, tenant_key, subject, subject_hash, signer_seq, prev_hash, signed_at) FROM attestations WHERE signer_seq = 2").fetchone()[0]
        h = conn.execute("SELECT jarvis_signed_hash(%s, %s, %s)", (message, keys["mint"].key_id, other)).fetchone()[0]
    tamper(pg, f"UPDATE attestations SET signature = '{other}', attestation_hash = '{h}' WHERE signer_seq = 2")
    v = verify(client)
    assert v["ok"] is False and any(p["subject"] == "attestation 2" and "bad signature" in p["problem"] for p in v["problems"])


def test_an_attestation_removed_from_the_middle_or_the_end_is_found_in_the_middle_only(pg, client, roots, keys):
    signed_ledger(client, keys, blocks=3)
    tamper(pg, "DELETE FROM attestations WHERE signer_seq = 3")  # the newest: a shorter log is still a valid log (an off-box copy is what notices)
    assert verify(client)["ok"] is True
    tamper(pg, "DELETE FROM attestations WHERE signer_seq = 1")
    v = verify(client)
    assert v["ok"] is False and any("expected attestation 1" in p["problem"] for p in v["problems"])


def test_an_attestation_whose_block_was_rewritten_afterwards_is_found(pg, client, roots, keys):
    signed_ledger(client, keys)
    with pg.admin_conn() as conn:
        conn.execute("ALTER TABLE blocks DISABLE TRIGGER blocks_no_update")
        conn.execute("UPDATE blocks SET block_hash = %s WHERE height = 1", (sha("a rewritten block"),))
        conn.execute("ALTER TABLE blocks ENABLE TRIGGER blocks_no_update")
    v = verify(client)
    assert v["ok"] is False and any(p["subject"] == "attestation 1" and "but the block's hash is" in p["problem"] for p in v["problems"])


def test_an_attestation_swapped_to_another_subject_is_found(pg, client, roots, keys):
    signed_ledger(client, keys)
    tamper(pg, "UPDATE attestations SET subject = 'block:2' WHERE signer_seq = 1")
    v = verify(client)
    assert v["ok"] is False and any(p["subject"] == "attestation 1" for p in v["problems"])


def test_a_statement_altered_in_the_trust_log_is_found_and_the_key_stops_being_trusted(pg, client, roots, keys):
    signed_ledger(client, keys)
    tamper(pg, "UPDATE trust_statements SET arg = 5 WHERE stmt_seq = 1")
    v = verify(client)
    assert v["ok"] is False and any(p["check"] == "trust" for p in v["problems"]) and any("no root authorized" in p["problem"] for p in v["problems"])


def test_a_bad_row_parked_in_the_log_is_found_and_only_a_root_can_void_it(pg, client, roots, keys):
    """The database cannot check Ed25519, so someone who can call the store function can park an unsigned row at the next position.
    It cannot be deleted (append-only), the log carries on after it, and verify flags it until a ROOT voids exactly that row."""
    signed_ledger(client, keys)
    h = head(client)
    block = client.get("/api/jarvis/blocks/2", headers=HDR).json()["block"]
    junk = attest.normalize_signature(sign(keys["stranger"], "junk"))
    with pg.app_conn(OP) as conn:
        parked = conn.execute("SELECT * FROM jarvis_store_attestation(%s, 'block', 'block:1', %s, %s, %s, %s, %s, %s)",
                              (OP, block["block_hash"], h["next_signer_seq"], h["prev_hash"], keys["stranger"].key_id, NOW, junk)).fetchone()
    v = verify(client)
    assert v["ok"] is False and any(p["subject"] == f"attestation {h['next_signer_seq']}" for p in v["problems"])
    # the honest signer is not blocked: the log simply continues after the bad row
    cp_head = head(client)
    assert cp_head["next_signer_seq"] == h["next_signer_seq"] + 1
    # a non-root cannot void it; a root can, but only by naming exactly this row
    assert post_statement(client, keys["mint"], "void", key_id=keys["mint"].key_id, arg=parked[0], subject_hash=parked[1], expect=422).json()["code"] == "statement_invalid"
    assert post_statement(client, keys["root"], "void", key_id=keys["root"].key_id, arg=parked[0], subject_hash=sha("another row"), expect=422).json()["code"] == "void_unknown_attestation"
    post_statement(client, keys["root"], "void", key_id=keys["root"].key_id, arg=parked[0], subject_hash=parked[1])
    v = verify(client)
    assert v["ok"] is True and v["summary"]["voided"] == [parked[0]]
    assert client.get("/api/jarvis/trust", headers=HDR).json()["voids"] == [{"signer_seq": parked[0], "attestation_hash": parked[1]}]


def test_a_void_cannot_be_used_to_hide_a_replaced_row(pg, client, roots, keys):
    signed_ledger(client, keys)
    row = client.get("/api/jarvis/attestations", headers=HDR).json()["attestations"][1]
    post_statement(client, keys["root"], "void", key_id=keys["root"].key_id, arg=2, subject_hash=row["attestation_hash"])
    assert verify(client)["ok"] is True  # voiding a good row is allowed: it simply stops counting
    tamper(pg, f"UPDATE attestations SET subject_hash = '{'e' * 64}' WHERE signer_seq = 2")
    v = verify(client)
    assert v["ok"] is False and any("stored attestation hash" in p["problem"] for p in v["problems"])  # a changed row is still caught structurally


def test_equivocation_two_different_hashes_for_one_block_is_found(pg, client, roots, keys):
    signed_ledger(client, keys, blocks=1)
    h = head(client)
    other = sha("a second, different block 1")
    message = attest.attestation_message("block", OP, "block:1", other, h["next_signer_seq"], h["prev_hash"], NOW)
    sig = attest.normalize_signature(sign(keys["mint"], message))
    with pg.app_conn(OP) as conn:
        conn.execute("SELECT * FROM jarvis_store_attestation(%s, 'block', 'block:1', %s, %s, %s, %s, %s, %s)",
                     (OP, other, h["next_signer_seq"], h["prev_hash"], keys["mint"].key_id, NOW, sig))
    v = verify(client)
    assert v["ok"] is False and any("DIFFERENT hash (equivocation)" in p["problem"] for p in v["problems"])


# --- pg_verify and the attest CLI -----------------------------------------------------------------------------------------------------------------

@pytest.fixture
def verify_env(pg, monkeypatch):
    monkeypatch.setenv("JARVIS_DATABASE_MIGRATE_URL", pg.admin_dsn)
    monkeypatch.setenv("JARVIS_DATABASE_SCHEMA", pg.schema)


def run_pg_verify(capsys, *args):
    rc = pg_verify.main(list(args) or ["--tenant", OP])
    out = capsys.readouterr()
    assert "postgresql://" not in out.out + out.err
    return rc, out.out


def test_pg_verify_says_signing_is_not_set_up_before_a_key_is_authorized(client, verify_env, capsys, roots):
    seal(client)
    rc, out = run_pg_verify(capsys)
    assert rc == 0 and "signatures: signing is not set up" in out and "WARNING" not in out


def test_pg_verify_reports_the_signatures_and_exits_clean(client, roots, keys, verify_env, capsys):
    signed_ledger(client, keys)
    rc, out = run_pg_verify(capsys)
    assert rc == 0 and "signatures: 2 attestation(s) up to 2, 2 block(s) and 0 receipt(s) signed, 0 unsigned" in out and "WARNING" not in out


def test_pg_verify_fails_on_a_bad_signature_in_every_mode_but_off(client, roots, keys, pg, verify_env, capsys, monkeypatch):
    signed_ledger(client, keys)
    tamper(pg, "UPDATE attestations SET subject_hash = '" + "e" * 64 + "' WHERE signer_seq = 2")
    for mode, expect in (("warn", 1), ("require", 1), ("off", 0)):
        monkeypatch.setenv(attest.MODE_ENV, mode)
        rc, out = run_pg_verify(capsys)
        assert rc == expect, (mode, out)
    monkeypatch.setenv(attest.MODE_ENV, "warn")
    rc, out = run_pg_verify(capsys)
    assert "PROBLEM tenant=operator record=attestation 2: signatures: [attestation]" in out


def test_unsigned_blocks_warn_after_the_grace_period_and_fail_only_in_require_mode(client, roots, keys, pg, verify_env, capsys, monkeypatch):
    seal(client)
    authorize_mint(client, keys)
    sign_block(client, keys, 1)  # block 2 stays unsigned
    rc, out = run_pg_verify(capsys)
    assert rc == 0 and "WARNING" not in out and "1 unsigned" in out  # inside the grace period
    monkeypatch.setenv(attest.GRACE_ENV, "0")
    rc, out = run_pg_verify(capsys)
    assert rc == 0 and "WARNING tenant=operator: block 2 is unsigned" in out
    monkeypatch.setenv(attest.MODE_ENV, "require")
    rc, out = run_pg_verify(capsys)
    assert rc == 1 and "[unsigned] block 2 is unsigned" in out


def test_with_no_trust_root_pg_verify_warns_and_never_claims_success(client, roots, keys, verify_env, capsys, monkeypatch):
    signed_ledger(client, keys)
    monkeypatch.delenv(attest.ROOTS_ENV)
    rc, out = run_pg_verify(capsys)
    assert rc == 0 and "signatures not verified: no trust root is configured" in out and "signatures: 2 attestation" not in out
    monkeypatch.setenv(attest.MODE_ENV, "require")
    assert run_pg_verify(capsys)[0] == 1


def test_a_default_mode_of_warn_and_junk_values_fall_back_to_it(monkeypatch):
    monkeypatch.delenv(attest.MODE_ENV, raising=False)
    assert attest.signatures_mode() == "warn"
    for value, expect in (("REQUIRE", "require"), ("off", "off"), ("sometimes", "warn"), ("", "warn")):
        monkeypatch.setenv(attest.MODE_ENV, value)
        assert attest.signatures_mode() == expect
    monkeypatch.setenv(attest.GRACE_ENV, "x")
    assert attest.grace_hours() == attest.DEFAULT_GRACE_HOURS


def test_the_attest_command_line_verifies_and_prints_a_fingerprint(client, roots, keys, verify_env, capsys):
    signed_ledger(client, keys)
    assert attest.main(["verify", "--tenant", OP]) == 0
    assert "signatures: 2 attestation(s)" in capsys.readouterr().out
    assert attest.main(["fingerprint", str(keys["mint"].path) + ".pub"]) == 0
    assert capsys.readouterr().out.strip() == keys["mint"].key_id
    assert attest.main(["fingerprint", "/nonexistent"]) == 2


def test_the_attest_command_line_exits_non_zero_on_a_problem(client, roots, keys, pg, verify_env, capsys):
    signed_ledger(client, keys)
    tamper(pg, "UPDATE attestations SET subject_hash = '" + "d" * 64 + "' WHERE signer_seq = 1")
    assert attest.main(["verify", "--tenant", OP]) == 1
    assert "PROBLEM tenant=operator attestation 1" in capsys.readouterr().out
