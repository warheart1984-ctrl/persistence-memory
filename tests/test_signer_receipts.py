"""The signer and replay receipts: sealed points only, re-derived from the raw rows before signing, after the block's own
attestation, refused (never signed) when anything about them is off."""

from __future__ import annotations

import subprocess
from types import SimpleNamespace

import pytest

from app import attest, signer
from tests.attest_support import NOW, requires_ssh_keygen, sign
from tests.test_signer import HDR, OP, api, authorize, client, key_info, keys, ok, pg, roots, seal  # noqa: F401  (fixtures by name)

pytestmark = [requires_ssh_keygen, pytest.mark.postgres]

NOOP_BLOCK = lambda h, bh: None  # noqa: E731


def receipt(client, block):
    r = client.post("/api/jarvis/replay/receipts", headers=HDR, json={"at_block": block})
    assert r.status_code == 200, r.text
    return r.json()["receipt"]["id"]


def run(api, keys, key="mint", *, rederive=None, **kw):
    seen = []
    kw.setdefault("verify_block", NOOP_BLOCK)
    kw["verify_receipt"] = rederive or (lambda rid: seen.append(rid))
    result = signer.run_sign(api, key_info(keys[key]), **kw)
    result["_rederived"] = seen
    return result


def rows(client):
    return client.get("/api/jarvis/attestations", headers=HDR).json()["attestations"]


def rewriting(api, route, edit):
    """An Api whose answer to GET <route> is changed by ``edit(json)``: a service that lies to the signer."""
    real = api._transport

    def transport(method, path, body):
        status, data = real(method, path, body)
        if method == "GET" and route(path):
            data = edit(data) or data
        return status, data
    return signer.Api("http://test", "unused", transport)


# --- the pass ------------------------------------------------------------------------------------------------------------------------------

def test_receipts_are_signed_after_blocks_and_before_the_checkpoint_and_the_service_verifies_them(client, roots, keys, api):
    seal(client, 6)
    r1, r2 = receipt(client, 1), receipt(client, 2)
    authorize(client, keys)
    result = run(api, keys)
    assert [r["id"] for r in result["signed_receipts"]] == sorted([r1, r2], key=lambda i: (client.get(f"/api/jarvis/replay/receipts/{i}", headers=HDR).json()["receipt"]["created_at"], i))
    assert [x["kind"] for x in rows(client)] == ["block", "block", "receipt", "receipt", "checkpoint"]
    assert result["checkpoint"]["signer_seq"] == 5 and sorted(result["_rederived"]) == sorted([r1, r2])
    v = ok(client)
    assert v["ok"] is True and v["summary"]["receipts_signed"] == 2 and v["summary"]["blocks_signed"] == 2
    check = client.get(f"/api/jarvis/replay/receipts/{r1}/verify", headers=HDR).json()
    assert check["ok"] is True and check["signatures"]["level"] == 1


def test_a_receipt_is_never_attested_before_its_block(client, roots, keys, api):
    seal(client, 3)
    authorize(client, keys)
    run(api, keys)  # block 1 signed on its own
    r1 = receipt(client, 1)
    seal(client, 3)
    r2 = receipt(client, 2)
    run(api, keys)
    by_subject = {x["subject"]: x["signer_seq"] for x in rows(client)}
    assert by_subject["block:1"] < by_subject[r1] and by_subject["block:2"] < by_subject[r2]


def test_each_receipt_is_re_derived_from_the_raw_rows_before_it_is_signed_and_a_failure_means_not_signed(client, roots, keys, api):
    seal(client, 6)
    good, bad = receipt(client, 1), receipt(client, 2)
    authorize(client, keys)

    def rederive(rid):
        if rid == bad:
            raise signer.SignerError("pre_sign_verify_failed", "the offline replay does not re-derive this receipt", signer.EXIT_UNHEALTHY)

    result = run(api, keys, rederive=rederive)
    assert [r["id"] for r in result["signed_receipts"]] == [good]
    assert result["refused_receipts"] == [{"id": bad, "why": "the offline replay does not re-derive this receipt"}]
    assert bad not in {x["subject"] for x in rows(client)}
    assert result["checkpoint"] is not None and ok(client)["ok"] is True  # the others still got signed, and the log is fine
    assert bad in [r["id"] for r in client.get("/api/jarvis/attestations/pending", headers=HDR).json()["receipts"]]  # still pending, never signed


def test_the_command_exits_unhealthy_when_a_receipt_was_refused(client, roots, keys, api, monkeypatch, capsys):
    seal(client, 3)
    receipt(client, 1)
    authorize(client, keys)
    monkeypatch.setattr(signer, "preflight_key", lambda path, **kw: key_info(keys["mint"]))
    monkeypatch.setattr(signer, "default_key_path", lambda: keys["mint"].path)
    monkeypatch.setattr(signer, "Api", lambda *a, **k: api)

    def failing(rid):
        raise signer.SignerError("pre_sign_verify_failed", "does not re-derive", signer.EXIT_UNHEALTHY)

    real = signer.run_sign
    monkeypatch.setattr(signer, "run_sign", lambda a, k, **kw: real(a, k, verify_block=NOOP_BLOCK, verify_receipt=failing, **kw))
    assert signer.main(["sign"]) == signer.EXIT_UNHEALTHY
    assert "REFUSED receipt eo:sha256:" in capsys.readouterr().err
    monkeypatch.setattr(signer, "run_sign", lambda a, k, **kw: real(a, k, verify_block=NOOP_BLOCK, verify_receipt=lambda rid: None, **kw))
    assert signer.main(["sign"]) == signer.EXIT_OK


def test_a_dry_run_plans_receipts_but_signs_and_re_derives_nothing(client, roots, keys, api):
    seal(client, 3)
    r1 = receipt(client, 1)
    authorize(client, keys)
    result = run(api, keys, dry_run=True)
    assert result["planned_receipts"] == [r1] and result["signed_receipts"] == [] and result["_rederived"] == []
    assert rows(client) == []


def test_only_receipts_pending_is_enough_to_run_and_the_block_check_is_skipped(client, roots, keys, api):
    seal(client, 3)
    authorize(client, keys)
    run(api, keys)
    r1 = receipt(client, 1)
    blocks_checked = []
    result = run(api, keys, verify_block=lambda h, bh: blocks_checked.append(h))
    assert [r["id"] for r in result["signed_receipts"]] == [r1] and blocks_checked == [] and result["signed_blocks"] == []
    assert [x["kind"] for x in rows(client)] == ["block", "checkpoint", "receipt", "checkpoint"]


def test_the_number_of_receipts_per_pass_is_capped_and_the_rest_wait(client, roots, keys, api):
    seal(client, 6)
    receipt(client, 1), receipt(client, 2)
    authorize(client, keys)
    first = run(api, keys, limit_receipts=1)
    assert len(first["signed_receipts"]) == 1 and first["deferred_receipts"] == 1
    second = run(api, keys, limit_receipts=1)
    assert len(second["signed_receipts"]) == 1 and second["deferred_receipts"] == 0
    assert run(api, keys)["note"] == "nothing pending"


def test_the_receipt_cap_has_a_default_and_a_safe_fallback(monkeypatch):
    monkeypatch.delenv("JARVIS_SIGN_MAX_RECEIPTS", raising=False)
    assert signer.max_receipts() == signer.DEFAULT_MAX_RECEIPTS
    monkeypatch.setenv("JARVIS_SIGN_MAX_RECEIPTS", "3")
    assert signer.max_receipts() == 3
    monkeypatch.setenv("JARVIS_SIGN_MAX_RECEIPTS", "lots")
    assert signer.max_receipts() == signer.DEFAULT_MAX_RECEIPTS


def test_it_builds_the_receipt_message_itself_and_the_signature_is_over_that(client, roots, keys, api):
    seal(client, 3)
    r1 = receipt(client, 1)
    authorize(client, keys)
    run(api, keys)
    row = next(x for x in rows(client) if x["kind"] == "receipt")
    expected = attest.attestation_message("receipt", OP, r1, r1[len("eo:sha256:"):], row["signer_seq"], rows(client)[row["signer_seq"] - 2]["attestation_hash"], row["signed_at"])
    assert attest.verify_sshsig(row["signature"], expected.encode()).key_id == keys["mint"].key_id
    assert row["subject"] == r1 and row["subject_hash"] == r1[len("eo:sha256:"):]


# --- what it refuses without spending a replay -----------------------------------------------------------------------------------------------------

def lying_about_the_receipt(api, rid, **changes):
    def edit(data):
        if data.get("receipt", {}).get("id") == rid:
            for k, v in changes.items():
                if k in ("id", "schema_id"):
                    data["receipt"][k] = v
                else:
                    data["receipt"]["payload"][k] = v
    return rewriting(api, lambda p: p == f"/api/jarvis/replay/receipts/{rid}", edit)


@pytest.mark.parametrize("changes,why", [
    ({"tenant": "someone-else"}, "for tenant"),
    ({"block_hash": "e" * 64}, "is not the block the receipt names"),
    ({"block_height": 99}, "does not exist"),
    ({"at_seq": 5}, "not inside block"),
    ({"block_height": "one"}, "malformed"),
    ({"schema_id": "CES.Local.Other.v1"}, "not a replay receipt"),
    ({"id": "eo:sha256:" + "0" * 64}, "different object"),
])
def test_a_receipt_the_signer_cannot_vouch_for_is_refused_before_any_replay(client, roots, keys, api, changes, why):
    seal(client, 3)
    r1 = receipt(client, 1)
    authorize(client, keys)
    result = run(lying_about_the_receipt(api, r1, **changes), keys)
    assert result["signed_receipts"] == [] and [r["id"] for r in result["refused_receipts"]] == [r1] and why in result["refused_receipts"][0]["why"]
    assert result["_rederived"] == []
    assert r1 not in {x["subject"] for x in rows(client)}


def test_ids_the_service_lists_but_that_are_not_receipts_are_refused(client, roots, keys, api):
    seal(client, 3)
    authorize(client, keys)
    fakes = ["not-an-id", "eo:sha256:" + "0" * 64, "eo:sha256:" + "A" * 64, "eo:sha256:" + "a" * 63]

    def edit(data):
        data["receipts"] = [{"id": f, "created_at": NOW} for f in fakes]
    result = run(rewriting(api, lambda p: p == "/api/jarvis/attestations/pending", edit), keys)
    assert result["signed_receipts"] == [] and {r["id"] for r in result["refused_receipts"]} == set(fakes) and result["_rederived"] == []
    assert rows(client)[0]["kind"] == "block"  # the block was still signed; nothing about the fakes was


def test_it_will_not_extend_a_log_that_already_has_a_bad_row_even_for_receipts(pg, client, roots, keys, api):
    seal(client, 3)
    authorize(client, keys)
    run(api, keys)
    r1 = receipt(client, 1)  # pending when the bad row appears
    h = client.get("/api/jarvis/attestations/head", headers=HDR).json()
    message = attest.attestation_message("block", OP, "block:1", "a" * 64, h["next_signer_seq"], h["prev_hash"], NOW)
    with pg.app_conn(OP) as conn:
        conn.execute("SELECT * FROM jarvis_store_attestation(%s, 'block', 'block:1', %s, %s, %s, %s, %s, %s)",
                     (OP, "a" * 64, h["next_signer_seq"], h["prev_hash"], keys["stranger"].key_id, NOW, attest.normalize_signature(sign(keys["stranger"], message))))
    with pytest.raises(signer.SignerError) as exc:
        run(api, keys)
    assert exc.value.code == "log_unhealthy"
    assert r1 not in {x["subject"] for x in rows(client)}


def test_an_unauthorized_or_revoked_key_signs_no_receipts(client, roots, keys, api):
    seal(client, 3)
    receipt(client, 1)
    with pytest.raises(signer.SignerError) as exc:
        run(api, keys)
    assert exc.value.code == "key_not_authorized"
    assert rows(client) == []


# --- the real re-derivation command -------------------------------------------------------------------------------------------------------------

def test_the_default_receipt_check_runs_the_raw_row_verifier_with_signature_checking_off(monkeypatch):
    calls = []
    monkeypatch.setattr(signer.subprocess, "run", lambda cmd, **kw: calls.append(cmd) or SimpleNamespace(returncode=0, stdout="ok", stderr=""))
    rid = "eo:sha256:" + "ab" * 32
    signer.default_verify_receipt(rid)
    cmd = calls[0]
    assert cmd[0].endswith("deploy/mint/bin/replay.sh") and cmd[1:] == ["verify", "--receipt", rid, "--signatures", "off"]
    signer.default_verify_block(3, "cd" * 32)
    assert calls[1][1:] == ["verify", "--at-block", "3", "--expect-block-hash", "cd" * 32, "--signatures", "off"]


def test_a_failing_re_derivation_is_an_unhealthy_exit_with_the_reason(monkeypatch):
    monkeypatch.setattr(signer.subprocess, "run", lambda cmd, **kw: SimpleNamespace(returncode=1, stdout="PROBLEM x [state_root]: root differs\n", stderr=""))
    with pytest.raises(signer.SignerError) as exc:
        signer.default_verify_receipt("eo:sha256:" + "ab" * 32)
    assert exc.value.exit_code == signer.EXIT_UNHEALTHY and "state_root" in exc.value.message
