"""Signature levels for replay receipts and their sealed blocks (L0 unsigned, L1 Mint-signed, L2 root-cosigned), the
JARVIS_SIGNATURES=require switch, and block.signed in the replay state.  Real Ed25519 keys and signatures from ssh-keygen; the
service, the offline verifier and the CLI are checked against the same database."""

from __future__ import annotations

import psycopg
import pytest

from app import attest, replay
from tests.attest_support import NOW, make_key, requires_ssh_keygen, sha, sign
from tests.test_attest_api import (  # noqa: F401  (fixtures are used by name)
    HDR, OP, add_records, authorize_mint, client, head, keys, pg, post_attestation, post_statement, roots, seal, sign_block, tamper, verify,
)

pytestmark = [requires_ssh_keygen, pytest.mark.postgres]


def make_receipt(client, block):
    r = client.post("/api/jarvis/replay/receipts", headers=HDR, json={"at_block": block})
    assert r.status_code == 200, r.text
    return r.json()["receipt"]["id"]


def sign_receipt(client, signer, rid, **kw):
    return post_attestation(client, signer, "receipt", rid, rid[len("eo:sha256:"):], **kw)


def check(client, rid):
    r = client.get(f"/api/jarvis/replay/receipts/{rid}/verify", headers=HDR)
    assert r.status_code == 200, r.text
    return r.json()


def state_block(client, height):
    r = client.get("/api/jarvis/replay/state", headers=HDR, params={"at_block": height, "limit": 1})
    assert r.status_code == 200, r.text
    return r.json()["block"]


def park(pg, kind, subject, subject_hash, seq, prev, key_id, signature, signed_at=NOW):
    """Put a row in the log without the service's signature check (the database cannot verify Ed25519): what a bad row looks like."""
    with pg.app_conn(OP) as conn:
        conn.execute("SELECT * FROM jarvis_store_attestation(%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                     (OP, kind, subject, subject_hash, seq, prev, key_id, signed_at, signature))


@pytest.fixture
def two_blocks(client, roots, keys):
    seal(client, n=6, size=3)
    authorize_mint(client, keys)
    return make_receipt(client, 1), make_receipt(client, 2)


@pytest.fixture
def mode(monkeypatch):
    def set_mode(value):
        monkeypatch.setenv("JARVIS_SIGNATURES", value)
    set_mode("warn")
    return set_mode


# --- levels -------------------------------------------------------------------------------------------------------------------------------

def test_nothing_signed_is_l0_and_only_a_warning_in_warn(client, roots, keys, mode):
    seal(client, n=3, size=3)
    rid = make_receipt(client, 1)
    out = check(client, rid)
    assert out["ok"] is True  # the receipt still re-derives; unsigned is a warning in warn
    s = out["signatures"]
    assert s["level"] == 0 and s["block"]["level"] == 0 and s["receipt"]["level"] == 0 and s["mode"] == "warn" and s["verified"] is True
    assert any("unsigned" in w for w in s["warnings"]) and s["problems"] == []


def test_a_signed_block_with_an_unsigned_receipt_is_l0_overall(client, roots, keys, mode, two_blocks):
    rid = two_blocks[0]
    sign_block(client, keys, 1)
    s = check(client, rid)["signatures"]
    assert s["block"]["level"] == 1 and s["receipt"]["level"] == 0 and s["level"] == 0
    assert state_block(client, 1)["signed"] is True


def test_a_signed_receipt_on_an_unsigned_block_is_l0_overall(client, roots, keys, mode, two_blocks):
    rid = two_blocks[0]
    sign_receipt(client, keys["mint"], rid)  # the service accepts it; the report must not call it signed
    out = check(client, rid)
    s = out["signatures"]
    assert s["receipt"]["level"] == 1 and s["block"]["level"] == 0 and s["level"] == 0 and out["ok"] is True
    assert any("signed but its block 1 is not" in w for w in s["warnings"])
    assert state_block(client, 1)["signed"] is False


def test_block_and_receipt_signed_is_l1_and_ok_in_require(client, roots, keys, mode, two_blocks):
    rid = two_blocks[0]
    sign_block(client, keys, 1)
    sign_receipt(client, keys["mint"], rid)
    for m in ("warn", "require"):
        mode(m)
        out = check(client, rid)
        s = out["signatures"]
        assert out["ok"] is True and s["level"] == 1 and s["label"] == "L1 Mint-signed" and s["problems"] == [] and s["warnings"] == []
        assert s["block"]["attestation_seq"] == 1 and s["receipt"]["attestation_seq"] == 2
    assert state_block(client, 1)["signed"] is True and state_block(client, 1)["signature_level"] == 1


def test_a_cosigned_checkpoint_makes_what_it_covers_l2_and_only_that(client, roots, keys, mode, two_blocks):
    r1, r2 = two_blocks
    sign_block(client, keys, 1)
    sign_receipt(client, keys["mint"], r1)
    h = head(client)
    subject = attest.checkpoint_subject(h["head_seq"], h["head_hash"], h["tip_height"], h["tip_block_hash"])
    cp = post_attestation(client, keys["mint"], "checkpoint", subject, attest.checkpoint_hash(OP, h["head_seq"], h["head_hash"], h["tip_height"], h["tip_block_hash"])).json()
    # a checkpoint nobody cosigned adds nothing
    assert check(client, r1)["signatures"]["level"] == 1
    post_statement(client, keys["root"], "cosign", key_id=keys["root"].key_id, arg=cp["signer_seq"], subject_hash=cp["attestation_hash"])
    s = check(client, r1)["signatures"]
    assert s["level"] == 2 and s["label"] == "L2 root-cosigned" and s["block"]["level"] == 2 and s["receipt"]["level"] == 2
    assert state_block(client, 1)["signature_level"] == 2
    # signed after the cosigned checkpoint: only Mint-signed
    sign_block(client, keys, 2)
    sign_receipt(client, keys["mint"], r2)
    s2 = check(client, r2)["signatures"]
    assert s2["level"] == 1 and s2["block"]["level"] == 1 and s2["receipt"]["level"] == 1


def test_a_receipt_is_only_as_signed_as_its_block_for_l2(client, roots, keys, mode, two_blocks):
    r1 = two_blocks[0]
    sign_receipt(client, keys["mint"], r1)
    h = head(client)
    cp = post_attestation(client, keys["mint"], "checkpoint", attest.checkpoint_subject(h["head_seq"], h["head_hash"], h["tip_height"], h["tip_block_hash"]),
                          attest.checkpoint_hash(OP, h["head_seq"], h["head_hash"], h["tip_height"], h["tip_block_hash"])).json()
    post_statement(client, keys["root"], "cosign", key_id=keys["root"].key_id, arg=cp["signer_seq"], subject_hash=cp["attestation_hash"])
    s = check(client, r1)["signatures"]
    assert s["receipt"]["level"] == 2 and s["block"]["level"] == 0 and s["level"] == 0


# --- no trust root, off, and require ----------------------------------------------------------------------------------------------------------

def test_with_no_trust_root_nothing_is_reported_signed(client, keys, mode, monkeypatch, tmp_path):
    seal(client, n=3, size=3)
    rid = make_receipt(client, 1)
    monkeypatch.delenv(attest.ROOTS_ENV, raising=False)
    out = check(client, rid)
    s = out["signatures"]
    assert out["ok"] is True and s["verified"] is False and s["level"] == 0 and "not verified" in s["label"]
    assert any("no trust root" in w for w in s["warnings"])
    assert state_block(client, 1)["signed"] is False
    mode("require")
    out = check(client, rid)
    assert out["ok"] is False and any("no trust root" in p["problem"] for p in out["problems"])
    assert out["signatures"]["verified"] is False and out["signatures"]["level"] == 0


def test_an_empty_roots_file_is_no_trust_root_too(client, keys, mode, monkeypatch, tmp_path):
    seal(client, n=3, size=3)
    rid = make_receipt(client, 1)
    f = tmp_path / "empty.pub"
    f.write_text("# no roots yet\n")
    monkeypatch.setenv(attest.ROOTS_ENV, str(f))
    mode("require")
    out = check(client, rid)
    assert out["ok"] is False and out["signatures"]["verified"] is False


def test_require_fails_what_is_unsigned_and_warn_does_not(client, roots, keys, mode, two_blocks):
    rid = two_blocks[0]
    assert check(client, rid)["ok"] is True
    mode("require")
    out = check(client, rid)
    assert out["ok"] is False and out["signatures"]["level"] == 0
    assert any(p["check"] == "signatures" and "unsigned" in p["problem"] for p in out["problems"])
    sign_block(client, keys, 1)
    assert check(client, rid)["ok"] is False  # the receipt itself is still unsigned
    sign_receipt(client, keys["mint"], rid)
    assert check(client, rid)["ok"] is True


def test_require_before_any_key_is_authorized_is_a_failure_not_a_pass(client, roots, keys, mode):
    seal(client, n=3, size=3)
    rid = make_receipt(client, 1)
    mode("require")
    out = check(client, rid)
    assert out["ok"] is False and any("signing is not set up" in p["problem"] for p in out["problems"])


def test_off_does_not_check_and_says_so(client, roots, keys, mode, two_blocks):
    mode("off")
    out = check(client, two_blocks[0])
    assert out["ok"] is True and out["signatures"] is None
    b = state_block(client, 1)
    assert b["signed"] is False and b["signature_level"] is None


def test_the_default_mode_is_warn(client, roots, keys, monkeypatch, two_blocks):
    monkeypatch.delenv("JARVIS_SIGNATURES", raising=False)
    out = check(client, two_blocks[0])
    assert out["ok"] is True and out["signatures"]["mode"] == "warn"
    monkeypatch.setenv("JARVIS_SIGNATURES", "nonsense")
    assert check(client, two_blocks[0])["signatures"]["mode"] == "warn"  # an unknown value never turns into require or off


# --- forged, wrong key, revoked ---------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("m", ["warn", "require"])
def test_a_forged_receipt_attestation_fails_in_every_mode(pg, client, roots, keys, mode, two_blocks, m):
    """Names the authorized Mint key but is signed by someone else: a bad row parked in the log (the database cannot check it)."""
    rid = two_blocks[0]
    sign_block(client, keys, 1)
    h = head(client)
    message = attest.attestation_message("receipt", OP, rid, rid[len("eo:sha256:"):], h["next_signer_seq"], h["prev_hash"], NOW)
    park(pg, "receipt", rid, rid[len("eo:sha256:"):], h["next_signer_seq"], h["prev_hash"], keys["mint"].key_id,
         attest.normalize_signature(sign(keys["stranger"], message)))
    mode(m)
    out = check(client, rid)
    assert out["ok"] is False and out["signatures"]["receipt"]["level"] == 0 and out["signatures"]["level"] == 0
    assert any("different key than the one named" in p["problem"] or "bad signature" in p["problem"] for p in out["problems"])


@pytest.mark.parametrize("m", ["warn", "require"])
def test_a_receipt_attestation_by_an_unauthorized_key_fails(pg, client, roots, keys, mode, two_blocks, m):
    rid = two_blocks[0]
    sign_block(client, keys, 1)
    h = head(client)
    message = attest.attestation_message("receipt", OP, rid, rid[len("eo:sha256:"):], h["next_signer_seq"], h["prev_hash"], NOW)
    park(pg, "receipt", rid, rid[len("eo:sha256:"):], h["next_signer_seq"], h["prev_hash"], keys["stranger"].key_id,
         attest.normalize_signature(sign(keys["stranger"], message)))
    mode(m)
    out = check(client, rid)
    assert out["ok"] is False and out["signatures"]["receipt"]["level"] == 0
    assert any("no root authorized" in p["problem"] for p in out["problems"])


@pytest.mark.parametrize("m", ["warn", "require"])
def test_a_garbled_receipt_signature_fails(pg, client, roots, keys, mode, two_blocks, m):
    rid = two_blocks[0]
    sign_block(client, keys, 1)
    sign_receipt(client, keys["mint"], rid)
    other = attest.normalize_signature(sign(keys["mint"], "a different message"))
    tamper(pg, f"UPDATE attestations SET signature = '{other}' WHERE signer_seq = 2")
    mode(m)
    out = check(client, rid)
    assert out["ok"] is False and out["signatures"]["receipt"]["level"] == 0 and out["signatures"]["block"]["level"] == 1


@pytest.mark.parametrize("m", ["warn", "require"])
def test_a_receipt_signed_after_the_key_was_revoked_does_not_count(client, roots, keys, mode, two_blocks, m):
    rid = two_blocks[0]
    sign_block(client, keys, 1)
    sign_receipt(client, keys["mint"], rid)  # attestation 2
    post_statement(client, keys["root"], "revoke", key_id=keys["mint"].key_id, arg=1)  # cutoff: only attestation 1 stays trusted
    mode(m)
    out = check(client, rid)
    s = out["signatures"]
    assert out["ok"] is False and s["receipt"]["level"] == 0 and s["block"]["level"] == 1 and s["level"] == 0
    assert any("revoked with a cutoff of 1" in p["problem"] for p in out["problems"])


def test_a_revoked_key_keeps_what_it_signed_before_the_cutoff(client, roots, keys, mode, two_blocks):
    rid = two_blocks[0]
    sign_block(client, keys, 1)
    sign_receipt(client, keys["mint"], rid)
    post_statement(client, keys["root"], "revoke", key_id=keys["mint"].key_id, arg=2)
    mode("require")
    out = check(client, rid)
    assert out["ok"] is True and out["signatures"]["level"] == 1


def test_a_receipt_attestation_naming_another_receipt_is_found(pg, client, roots, keys, mode, two_blocks):
    r1, r2 = two_blocks
    sign_block(client, keys, 1)
    h = head(client)
    # signed correctly for r2's id but with r1's hash: the subject and its hash disagree
    message = attest.attestation_message("receipt", OP, r2, r1[len("eo:sha256:"):], h["next_signer_seq"], h["prev_hash"], NOW)
    park(pg, "receipt", r2, r1[len("eo:sha256:"):], h["next_signer_seq"], h["prev_hash"], keys["mint"].key_id, attest.normalize_signature(sign(keys["mint"], message)))
    assert check(client, r2)["ok"] is False


def test_an_attestation_of_a_different_block_hash_does_not_make_the_block_signed(pg, client, roots, keys, mode, two_blocks):
    rid = two_blocks[0]
    h = head(client)
    wrong = sha("not block 1's hash")
    message = attest.attestation_message("block", OP, "block:1", wrong, h["next_signer_seq"], h["prev_hash"], NOW)
    park(pg, "block", "block:1", wrong, h["next_signer_seq"], h["prev_hash"], keys["mint"].key_id, attest.normalize_signature(sign(keys["mint"], message)))
    out = check(client, rid)
    assert out["ok"] is False and out["signatures"]["block"]["level"] == 0
    assert state_block(client, 1)["signed"] is False


def test_a_root_void_removes_a_bad_row_from_the_verdict(pg, client, roots, keys, mode, two_blocks):
    rid = two_blocks[0]
    sign_block(client, keys, 1)
    h = head(client)
    message = attest.attestation_message("receipt", OP, rid, rid[len("eo:sha256:"):], h["next_signer_seq"], h["prev_hash"], NOW)
    park(pg, "receipt", rid, rid[len("eo:sha256:"):], h["next_signer_seq"], h["prev_hash"], keys["stranger"].key_id, attest.normalize_signature(sign(keys["stranger"], message)))
    assert check(client, rid)["ok"] is False
    bad = client.get("/api/jarvis/attestations", headers=HDR).json()["attestations"][-1]
    post_statement(client, keys["root"], "void", key_id=keys["root"].key_id, arg=bad["signer_seq"], subject_hash=bad["attestation_hash"])
    out = check(client, rid)
    assert out["ok"] is True and out["signatures"]["receipt"]["level"] == 0  # voided: no longer a problem, and not a signature either
    assert any("voided by a root" in w for w in out["signatures"]["warnings"])
    sign_receipt(client, keys["mint"], rid)
    mode("require")
    sign_block_state = check(client, rid)
    assert sign_block_state["ok"] is True and sign_block_state["signatures"]["level"] == 1


def test_unrelated_log_problems_warn_in_warn_and_fail_in_require(pg, client, roots, keys, mode, two_blocks):
    r1, r2 = two_blocks
    sign_block(client, keys, 1)
    sign_receipt(client, keys["mint"], r1)
    h = head(client)
    message = attest.attestation_message("receipt", OP, r2, r2[len("eo:sha256:"):], h["next_signer_seq"], h["prev_hash"], NOW)
    park(pg, "receipt", r2, r2[len("eo:sha256:"):], h["next_signer_seq"], h["prev_hash"], keys["stranger"].key_id, attest.normalize_signature(sign(keys["stranger"], message)))
    out = check(client, r1)  # r1's own attestation is fine; r2's is bad
    assert out["ok"] is True and out["signatures"]["level"] == 1 and any("other problem" in w for w in out["signatures"]["warnings"])
    mode("require")
    out = check(client, r1)
    assert out["ok"] is False and any("signing log" == p["subject"] for p in out["problems"])


# --- the offline verifier and the CLI -----------------------------------------------------------------------------------------------------------

def offline(pg, rid, **kw):
    with psycopg.connect(pg.admin_dsn, options=f"-c search_path={pg.schema}") as conn:
        return replay.verify_receipt(conn, OP, rid, **kw)


def test_the_offline_verifier_reports_the_same_levels(pg, client, roots, keys, mode, two_blocks):
    rid = two_blocks[0]
    unsigned = offline(pg, rid)
    assert unsigned["signatures"]["level"] == 0 and unsigned["ok"] is True
    assert unsigned["block"]["signed"] is False and unsigned["block"]["signature_level"] == 0  # unsigned is never reported as signed
    sign_block(client, keys, 1)
    sign_receipt(client, keys["mint"], rid)
    out = offline(pg, rid)
    assert out["ok"] is True and out["signatures"]["level"] == 1 and out["block"]["signed"] is True and out["block"]["signature_level"] == 1
    assert offline(pg, rid, signatures="off")["signatures"] is None  # what the signer asks for before it signs


def test_the_offline_verifier_fails_unsigned_in_require_and_never_passes_without_roots(pg, client, roots, keys, mode, two_blocks, monkeypatch):
    rid = two_blocks[0]
    out = offline(pg, rid, signatures="require")
    assert out["ok"] is False and out["signatures"]["level"] == 0
    sign_block(client, keys, 1)
    sign_receipt(client, keys["mint"], rid)
    assert offline(pg, rid, signatures="require")["ok"] is True  # signed and verifiable ...
    monkeypatch.delenv(attest.ROOTS_ENV)  # ... until the roots are gone: then nothing is verified
    out = offline(pg, rid, signatures="require")
    assert out["ok"] is False and any("no trust root" in p["problem"] for p in out["problems"])


def test_a_forged_row_fails_the_offline_verifier_too(pg, client, roots, keys, mode, two_blocks):
    rid = two_blocks[0]
    sign_block(client, keys, 1)
    sign_receipt(client, keys["mint"], rid)
    tamper(pg, f"UPDATE attestations SET signature = '{attest.normalize_signature(sign(keys['mint'], 'x'))}' WHERE signer_seq = 2")
    out = offline(pg, rid)
    assert out["ok"] is False and out["signatures"]["receipt"]["level"] == 0


@pytest.fixture
def cli_env(pg, monkeypatch):
    monkeypatch.setenv("JARVIS_DATABASE_MIGRATE_URL", pg.admin_dsn)
    monkeypatch.setenv("JARVIS_DATABASE_SCHEMA", pg.schema)


def test_the_cli_prints_the_level_and_exits_by_mode(client, roots, keys, mode, two_blocks, cli_env, capsys):
    rid = two_blocks[0]
    assert replay.main(["verify", "--tenant", OP, "--receipt", rid]) == 0
    out = capsys.readouterr().out
    assert out.startswith(f"ok: receipt {rid} re-derived") and "signatures (warn): L0 unsigned" in out and "WARNING" in out
    assert replay.main(["verify", "--tenant", OP, "--receipt", rid, "--signatures", "require"]) == 1
    out = capsys.readouterr().out
    assert "PROBLEM" in out and "unsigned" in out and "signatures (require): L0 unsigned" in out
    sign_block(client, keys, 1)
    sign_receipt(client, keys["mint"], rid)
    assert replay.main(["verify", "--tenant", OP, "--receipt", rid, "--signatures", "require"]) == 0
    assert "signatures (require): L1 Mint-signed" in capsys.readouterr().out
    assert replay.main(["verify", "--tenant", OP, "--receipt", rid, "--signatures", "off"]) == 0
    assert "signatures" not in capsys.readouterr().out


def test_the_cli_honours_the_environment_switch(client, roots, keys, mode, two_blocks, cli_env, capsys):
    rid = two_blocks[0]
    mode("require")
    assert replay.main(["verify", "--tenant", OP, "--receipt", rid]) == 1
    capsys.readouterr()
    mode("warn")
    assert replay.main(["verify", "--tenant", OP, "--receipt", rid]) == 0


def test_a_block_replay_reports_whether_the_block_is_signed(client, roots, keys, mode, two_blocks, cli_env, capsys):
    assert replay.main(["verify", "--tenant", OP, "--at-block", "1"]) == 0
    assert "signatures (warn): L0 unsigned" in capsys.readouterr().out
    sign_block(client, keys, 1)
    assert replay.main(["verify", "--tenant", OP, "--at-block", "1", "--signatures", "require"]) == 0
    assert "L1 Mint-signed" in capsys.readouterr().out
    assert replay.main(["verify", "--tenant", OP, "--at-block", "2", "--signatures", "require"]) == 1  # block 2 is not signed


def test_a_replay_that_is_not_at_a_sealed_point_has_no_signature_section(client, roots, keys, mode, two_blocks, cli_env, pg):
    add_records(client, 2, prefix="tail")
    with psycopg.connect(pg.admin_dsn, options=f"-c search_path={pg.schema}") as conn:
        out = replay.verify_replay(conn, OP, signatures="require")
    assert out["ok"] is True and out["block"] is None and out["signatures"] is None


# --- the receipt itself is untouched --------------------------------------------------------------------------------------------------------

def test_signing_state_never_changes_what_a_receipt_is(client, roots, keys, mode, two_blocks):
    """The receipt payload must stay deterministic: same replay, same id, whatever is signed."""
    r1 = two_blocks[0]
    sign_block(client, keys, 1)
    again = client.post("/api/jarvis/replay/receipts", headers=HDR, json={"at_block": 1}).json()
    assert again["receipt"]["id"] == r1 and again["created"] is False
    assert set(again["receipt"]["payload"]) == {"contract", "contract_version", "tenant", "at_seq", "block_height", "block_hash", "state_root", "record_count", "deleted_count"}


def test_a_block_hash_that_is_not_the_attested_one_is_never_reported_signed(pg, client, roots, keys, mode, two_blocks):
    sign_block(client, keys, 1)
    block = client.get("/api/jarvis/blocks/1", headers=HDR).json()["block"]
    with psycopg.connect(pg.admin_dsn, options=f"-c search_path={pg.schema}") as conn:
        good = attest.signature_report(conn, OP, block_height=1, block_hash=block["block_hash"])
        assert good["block"]["level"] == 1 and good["problems"] == []
        other = attest.signature_report(conn, OP, block_height=1, block_hash=sha("a different block 1"))
    assert other["block"]["level"] == 0 and any("different block hash" in p["problem"] for p in other["problems"])
