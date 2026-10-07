"""The witness: verifies a backup's signatures export from outside the box, remembers what it saw, cosigns, and builds root statements."""

from __future__ import annotations

import copy
import json
import subprocess
import sys

import pytest

from app import attest, signer, witness
from tests.attest_support import requires_ssh_keygen, sha, sign
from tests.test_signer import (  # noqa: F401  (fixtures and helpers shared with the signer tests)
    HDR, OP, api, authorize, client, key_info, keys, ok, pg, revoke, roots, run, seal,
)

pytestmark = [requires_ssh_keygen, pytest.mark.postgres]


def export_from(client):
    """The signatures export as the backup would write it, assembled from the service's own answers."""
    stmts = client.get("/api/jarvis/trust/statements?limit=1000", headers=HDR).json()["statements"]
    atts = client.get("/api/jarvis/attestations?limit=1000", headers=HDR).json()["attestations"]
    blocks = [client.get(f"/api/jarvis/blocks/{h}", headers=HDR).json()["block"] for h in range(1, 1 + client.get("/api/jarvis/blocks", headers=HDR).json()["count"])]
    receipts = [r["id"] for r in client.get("/api/jarvis/replay/receipts", headers=HDR).json()["receipts"]]
    keep_s = ("stmt_seq", "kind", "key_id", "pubkey", "arg", "subject_hash", "prev_hash", "signed_by", "signature", "statement_hash")
    keep_a = ("signer_seq", "kind", "subject", "subject_hash", "prev_hash", "key_id", "signed_at", "signature", "attestation_hash")
    return {"format": 1, "tenants": {OP: {"statements": [{k: s[k] for k in keep_s} for s in stmts], "attestations": [{k: a[k] for k in keep_a} for a in atts],
                                          "blocks": [{"height": b["height"], "block_hash": b["block_hash"]} for b in blocks], "receipts": receipts}}}


def write(tmp_path, export, name="signatures.json"):
    p = tmp_path / name
    p.write_text(json.dumps(export))
    return p


def signed_ledger(client, keys, api, blocks=2):
    seal(client, 3 * blocks)
    authorize(client, keys)
    run(api, keys)
    assert ok(client)["ok"] is True


def cli(capsys, *args):
    rc = witness.main(list(args))
    out = capsys.readouterr()
    return rc, out.out, out.err


# --- verifying an export ----------------------------------------------------------------------------------------------------------------

def test_a_clean_export_verifies_with_only_the_roots_public_key(client, roots, keys, api, tmp_path, capsys):
    signed_ledger(client, keys, api)
    f = write(tmp_path, export_from(client))
    rc, out, err = cli(capsys, "verify-export", str(f), "--roots", str(roots))
    assert rc == 0 and f"ok: tenant {OP}: 3 attestation(s) to 3, 2 block(s) signed, 1 trust statement(s), newest checkpoint 3, cosigned checkpoint none" in out


def test_no_roots_means_no_verification_at_all(client, roots, keys, api, tmp_path, capsys):
    signed_ledger(client, keys, api)
    f = write(tmp_path, export_from(client))
    empty = tmp_path / "empty.pub"
    empty.write_text("# nobody\n")
    rc, out, err = cli(capsys, "verify-export", str(f), "--roots", str(empty))
    assert rc == 2 and "no trust roots" in err and "ok:" not in out


def test_the_wrong_roots_authorize_nothing(client, roots, keys, api, tmp_path, capsys):
    signed_ledger(client, keys, api)
    f = write(tmp_path, export_from(client))
    other = tmp_path / "other.pub"
    other.write_text(keys["stranger"].pub_text + "\n")
    rc, out, err = cli(capsys, "verify-export", str(f), "--roots", str(other))
    assert rc == 1 and "not a root key" in out and "ok:" not in out


@pytest.mark.parametrize("name,mutate,needle", [
    ("a signature", lambda e: e["tenants"][OP]["attestations"][0].update(signature=e["tenants"][OP]["attestations"][0]["signature"].replace("A", "B", 4)), "attestation"),
    ("a subject hash", lambda e: e["tenants"][OP]["attestations"][0].update(subject_hash=sha("x")), "attestation 1"),
    ("a block hash", lambda e: e["tenants"][OP]["blocks"][0].update(block_hash=sha("another block")), "but the block's hash is"),
    ("a removed middle attestation", lambda e: e["tenants"][OP]["attestations"].pop(1), "expected attestation 2"),
    ("an altered statement", lambda e: e["tenants"][OP]["statements"][0].update(arg=7), "trust statement 1"),
])
def test_every_tampered_field_of_the_export_is_found(client, roots, keys, api, tmp_path, capsys, name, mutate, needle):
    signed_ledger(client, keys, api)
    e = export_from(client)
    mutate(e)
    rc, out, err = cli(capsys, "verify-export", str(write(tmp_path, e)), "--roots", str(roots))
    assert rc == 1 and needle in out


@pytest.mark.parametrize("content", ["not json", "[]", '{"format": 2, "tenants": {}}', '{"format": 1}'])
def test_a_file_that_is_not_an_export_is_refused(tmp_path, roots, capsys, content):
    f = tmp_path / "x.json"
    f.write_text(content)
    rc, out, err = cli(capsys, "verify-export", str(f), "--roots", str(roots))
    assert rc == 2 and ("cannot read" in err or "not a signatures export" in err)


# --- remembering what it saw: the part the box cannot do for itself --------------------------------------------------------------------

def test_a_later_export_that_extends_the_state_is_accepted_and_recorded(client, roots, keys, api, tmp_path, capsys):
    signed_ledger(client, keys, api)
    state = tmp_path / "state.json"
    assert cli(capsys, "verify-export", str(write(tmp_path, export_from(client))), "--roots", str(roots), "--state", str(state), "--update-state")[0] == 0
    saved = json.loads(state.read_text())
    assert set(saved["tenants"][OP]["attestations"]) == {"1", "2", "3"} and set(saved["tenants"][OP]["blocks"]) == {"1", "2"}
    seal(client, 3)
    run(api, keys)
    rc, out, _ = cli(capsys, "verify-export", str(write(tmp_path, export_from(client), "later.json")), "--roots", str(roots), "--state", str(state), "--update-state")
    assert rc == 0 and "5 attestation(s) to 5" in out and set(json.loads(state.read_text())["tenants"][OP]["attestations"]) == {"1", "2", "3", "4", "5"}


def test_a_state_is_not_updated_by_a_run_that_found_a_problem(client, roots, keys, api, tmp_path, capsys):
    signed_ledger(client, keys, api)
    state = tmp_path / "state.json"
    e = export_from(client)
    e["tenants"][OP]["blocks"][0]["block_hash"] = sha("x")
    assert cli(capsys, "verify-export", str(write(tmp_path, e)), "--roots", str(roots), "--state", str(state), "--update-state")[0] == 1
    assert not state.exists()


def rewrite_history_and_resign_with_the_taken_key(client, keys, export):
    """What someone holding the Mint signing key can do: change block 1 (here: its recorded hash), re-sign every attestation that
    covers it with the stolen key, and keep every link, hash and position valid.  Everything in the export is then self-consistent."""
    e = copy.deepcopy(export)
    t = e["tenants"][OP]
    new_hash = sha("a rewritten block 1")
    t["blocks"][0]["block_hash"] = new_hash
    prev = attest.GENESIS
    for a in t["attestations"]:
        subject_hash = new_hash if a["subject"] == "block:1" else a["subject_hash"]
        subject = a["subject"]
        if a["kind"] == "checkpoint":
            c = attest.parse_checkpoint_subject(subject)
            subject = attest.checkpoint_subject(c[0], prev, c[2], c[3])
            subject_hash = attest.checkpoint_hash(OP, c[0], prev, c[2], c[3])
        message = attest.attestation_message(a["kind"], OP, subject, subject_hash, a["signer_seq"], prev, a["signed_at"])
        sig = attest.normalize_signature(sign(keys["mint"], message))
        a.update(subject=subject, subject_hash=subject_hash, prev_hash=prev, signature=sig, attestation_hash=attest.attestation_hash(message, a["key_id"], sig))
        prev = a["attestation_hash"]
    return e


def test_a_history_rewritten_and_resigned_with_the_taken_key_passes_on_its_own_but_not_against_what_the_witness_saw(client, roots, keys, api, tmp_path, capsys):
    signed_ledger(client, keys, api)
    state = tmp_path / "state.json"
    honest = export_from(client)
    assert cli(capsys, "verify-export", str(write(tmp_path, honest)), "--roots", str(roots), "--state", str(state), "--update-state")[0] == 0
    forged = rewrite_history_and_resign_with_the_taken_key(client, keys, honest)
    f = write(tmp_path, forged, "forged.json")
    # on its own, the forged export is perfectly valid: that is exactly what a taken signing key buys an attacker
    rc, out, _ = cli(capsys, "verify-export", str(f), "--roots", str(roots))
    assert rc == 0 and "ok: tenant" in out
    # against what the witness recorded earlier it is not
    rc, out, _ = cli(capsys, "verify-export", str(f), "--roots", str(roots), "--state", str(state))
    assert rc == 1 and "changed since an earlier run" in out
    assert "block 1 changed" in out and "attestation 1 changed" in out and "attestation 3 changed" in out
    # every later attestation changes too, because each one names the hash of the one before it
    assert "attestation 2 changed" in out


def test_a_log_that_has_lost_its_newest_entries_is_noticed(client, roots, keys, api, tmp_path, capsys):
    signed_ledger(client, keys, api)
    state = tmp_path / "state.json"
    full = export_from(client)
    assert cli(capsys, "verify-export", str(write(tmp_path, full)), "--roots", str(roots), "--state", str(state), "--update-state")[0] == 0
    cut = copy.deepcopy(full)
    cut["tenants"][OP]["attestations"].pop()
    rc, out, _ = cli(capsys, "verify-export", str(write(tmp_path, cut, "cut.json")), "--roots", str(roots), "--state", str(state))
    assert rc == 1 and "attestation 3 was seen on an earlier run and is gone now" in out
    # the same cut, with no memory of the earlier run, is a perfectly valid shorter log: the state is what makes the difference
    assert cli(capsys, "verify-export", str(write(tmp_path, cut, "cut2.json")), "--roots", str(roots))[0] == 0


def test_a_missing_block_or_statement_since_the_last_run_is_noticed(client, roots, keys, api, tmp_path, capsys):
    signed_ledger(client, keys, api)
    state = tmp_path / "state.json"
    full = export_from(client)
    assert cli(capsys, "verify-export", str(write(tmp_path, full)), "--roots", str(roots), "--state", str(state), "--update-state")[0] == 0
    no_block = copy.deepcopy(full)
    no_block["tenants"][OP]["blocks"].pop()
    rc, out, _ = cli(capsys, "verify-export", str(write(tmp_path, no_block, "a.json")), "--roots", str(roots), "--state", str(state))
    assert rc == 1 and "block 2 was seen on an earlier run and is gone now" in out
    no_stmt = copy.deepcopy(full)
    no_stmt["tenants"][OP]["statements"] = []
    rc, out, _ = cli(capsys, "verify-export", str(write(tmp_path, no_stmt, "b.json")), "--roots", str(roots), "--state", str(state))
    assert rc == 1 and "trust statement 1 was seen on an earlier run and is gone now" in out


def test_a_corrupt_state_file_is_refused_not_ignored(client, roots, keys, api, tmp_path, capsys):
    signed_ledger(client, keys, api)
    state = tmp_path / "state.json"
    state.write_text("{not json")
    rc, out, err = cli(capsys, "verify-export", str(write(tmp_path, export_from(client))), "--roots", str(roots), "--state", str(state))
    assert rc == 2 and "cannot read the witness state" in err


# --- cosigning ------------------------------------------------------------------------------------------------------------------------------

def post_body(client, body):
    return client.post("/api/jarvis/trust/statements", headers=HDR, json=body)


@pytest.fixture
def witness_api(client, monkeypatch):
    def transport(method, path, body):
        r = client.request(method, path, headers=HDR, json=body)
        return r.status_code, (r.json() if r.content else {})
    monkeypatch.setattr(witness, "_api", lambda: signer.Api("http://test", "unused", transport))


def test_a_cosign_built_by_the_witness_is_accepted_by_the_service_and_shows_as_witnessed(client, roots, keys, api, tmp_path, capsys, witness_api):
    signed_ledger(client, keys, api)
    f = write(tmp_path, export_from(client))
    rc, out, err = cli(capsys, "cosign", str(f), "--roots", str(roots), "--root-key", str(keys["root"].path), "--post")
    assert rc == 0 and "stored statement 2 (cosign)" in out
    v = ok(client)
    assert v["ok"] is True and v["summary"]["cosigned_checkpoint_seq"] == 3
    assert client.get("/api/jarvis/trust", headers=HDR).json()["cosigns"][0]["root"] == keys["root"].key_id


def test_cosigning_twice_changes_nothing_and_a_new_checkpoint_gets_its_own(client, roots, keys, api, tmp_path, capsys, witness_api):
    signed_ledger(client, keys, api)
    state = tmp_path / "state.json"
    args = ["--roots", str(roots), "--root-key", str(keys["root"].path), "--post", "--state", str(state), "--update-state"]
    assert cli(capsys, "cosign", str(write(tmp_path, export_from(client))), *args)[0] == 0
    rc, out, _ = cli(capsys, "cosign", str(write(tmp_path, export_from(client), "b.json")), *args)
    assert rc == 0 and "checkpoint 3 is already cosigned" in out
    seal(client, 3)
    run(api, keys)
    rc, out, _ = cli(capsys, "cosign", str(write(tmp_path, export_from(client), "c.json")), *args)
    assert rc == 0 and "stored statement 3 (cosign)" in out
    assert ok(client)["summary"]["cosigned_checkpoint_seq"] == 5


def test_nothing_is_cosigned_when_the_export_does_not_verify(client, roots, keys, api, tmp_path, capsys, witness_api):
    signed_ledger(client, keys, api)
    e = export_from(client)
    e["tenants"][OP]["blocks"][0]["block_hash"] = sha("x")
    rc, out, err = cli(capsys, "cosign", str(write(tmp_path, e)), "--roots", str(roots), "--root-key", str(keys["root"].path), "--post")
    assert rc == 1 and "not cosigning" in err
    assert client.get("/api/jarvis/trust/statements", headers=HDR).json()["count"] == 1


def test_nothing_is_cosigned_against_a_rewritten_history(client, roots, keys, api, tmp_path, capsys, witness_api):
    """The point of the witness: a forged-but-valid export is refused a cosignature once it contradicts what was witnessed."""
    signed_ledger(client, keys, api)
    state = tmp_path / "state.json"
    honest = export_from(client)
    assert cli(capsys, "verify-export", str(write(tmp_path, honest)), "--roots", str(roots), "--state", str(state), "--update-state")[0] == 0
    forged = write(tmp_path, rewrite_history_and_resign_with_the_taken_key(client, keys, honest), "forged.json")
    rc, out, err = cli(capsys, "cosign", str(forged), "--roots", str(roots), "--root-key", str(keys["root"].path), "--state", str(state), "--out", str(tmp_path / "o.json"))
    assert rc == 1 and "not cosigning" in err and not (tmp_path / "o.json").exists()


def test_a_cosign_can_be_written_to_a_file_for_posting_later(client, roots, keys, api, tmp_path, capsys):
    signed_ledger(client, keys, api)
    f = write(tmp_path, export_from(client))
    out_file = tmp_path / "cosign.json"
    head = client.get("/api/jarvis/attestations/head", headers=HDR).json()
    rc, out, _ = cli(capsys, "cosign", str(f), "--roots", str(roots), "--root-key", str(keys["root"].path), "--out", str(out_file),
                     "--stmt-seq", str(head["next_stmt_seq"]), "--prev-hash", head["trust_head_hash"])
    assert rc == 0 and f"wrote {out_file}" in out
    body = json.loads(out_file.read_text())
    assert body["kind"] == "cosign" and body["arg"] == 3 and body["signed_by"] == keys["root"].key_id and post_body(client, body).status_code == 200


def test_without_a_known_position_it_will_not_guess_one(client, roots, keys, api, tmp_path, capsys):
    signed_ledger(client, keys, api)
    rc, out, err = cli(capsys, "cosign", str(write(tmp_path, export_from(client))), "--roots", str(roots), "--root-key", str(keys["root"].path))
    assert rc == 2 and "--stmt-seq and --prev-hash" in err


def test_nothing_to_cosign_before_any_checkpoint(client, roots, keys, tmp_path, capsys, witness_api):
    authorize(client, keys)
    rc, out, _ = cli(capsys, "cosign", str(write(tmp_path, export_from(client))), "--roots", str(roots), "--root-key", str(keys["root"].path), "--post")
    assert rc == 0 and "nothing to cosign" in out


def test_a_root_key_that_is_not_in_the_roots_is_refused_before_anything_is_signed(client, roots, keys, api, tmp_path, capsys, witness_api):
    signed_ledger(client, keys, api)
    rc, out, err = cli(capsys, "cosign", str(write(tmp_path, export_from(client))), "--roots", str(roots), "--root-key", str(keys["stranger"].path), "--post")
    assert rc == 2 and "is not in the trust roots" in err
    assert client.get("/api/jarvis/trust/statements", headers=HDR).json()["count"] == 1


# --- the ceremony statements --------------------------------------------------------------------------------------------------------------------

def test_the_ceremony_end_to_end_authorize_a_key_add_a_second_root_rotate_and_revoke(client, roots, keys, api, tmp_path, capsys, witness_api):
    r = lambda *a: cli(capsys, "statement", *a, "--roots", str(roots), "--root-key", str(keys["root"].path), "--tenant", OP, "--post")  # noqa: E731
    rc, out, _ = r("key", "--mint-pub", str(keys["mint"].path) + ".pub", "--from-seq", "1")
    assert rc == 0 and "stored statement 1 (key)" in out
    rc, out, _ = r("root-add", "--new-root-pub", str(keys["stranger"].path) + ".pub")
    assert rc == 0 and "stored statement 2 (root_add)" in out
    t = client.get("/api/jarvis/trust", headers=HDR).json()
    assert set(t["roots"]) == {keys["root"].key_id, keys["stranger"].key_id} and [k["key_id"] for k in t["keys"]] == [keys["mint"].key_id]
    seal(client)
    run(api, keys)
    rc, out, _ = r("key", "--mint-pub", str(keys["mint2"].path) + ".pub", "--from-seq", "4")
    assert rc == 0
    rc, out, _ = r("revoke", "--key-id", keys["mint"].key_id, "--cutoff", "3")
    assert rc == 0 and "stored statement 4 (revoke)" in out
    assert ok(client)["ok"] is True
    seal(client, 3)  # block 3, signed by the NEW key after the old one was revoked
    assert run(api, keys, key="mint2")["signed_blocks"][0]["signer_seq"] == 4
    v = ok(client)
    assert v["ok"] is True and v["summary"]["blocks_signed"] == 3 and v["summary"]["keys_authorized"] == 2


def test_a_void_statement_names_the_row_it_voids(client, roots, keys, api, tmp_path, capsys, witness_api, pg):
    signed_ledger(client, keys, api)
    row = client.get("/api/jarvis/attestations", headers=HDR).json()["attestations"][0]
    rc, out, _ = cli(capsys, "statement", "void", "--roots", str(roots), "--root-key", str(keys["root"].path), "--tenant", OP, "--seq", "1", "--hash", row["attestation_hash"], "--post")
    assert rc == 0 and "stored statement 2 (void)" in out and ok(client)["summary"]["voided"] == [1]


def test_a_statement_can_be_written_to_a_file_with_a_stated_position(client, roots, keys, tmp_path, capsys):
    out_file = tmp_path / "key.json"
    rc, out, _ = cli(capsys, "statement", "key", "--roots", str(roots), "--root-key", str(keys["root"].path), "--tenant", OP, "--mint-pub",
                     str(keys["mint"].path) + ".pub", "--from-seq", "1", "--stmt-seq", "1", "--prev-hash", attest.GENESIS, "--out", str(out_file))
    assert rc == 0 and post_body(client, json.loads(out_file.read_text())).status_code == 200


@pytest.mark.parametrize("args,needle", [
    (["key"], "key needs --mint-pub and --from-seq"), (["revoke"], "revoke needs --key-id and --cutoff"),
    (["root-add"], "root-add needs --new-root-pub"), (["void"], "void needs --seq and --hash"),
])
def test_incomplete_statements_are_refused(client, roots, keys, capsys, args, needle):
    rc, out, err = cli(capsys, "statement", *args, "--roots", str(roots), "--root-key", str(keys["root"].path), "--stmt-seq", "1", "--prev-hash", attest.GENESIS)
    assert rc == 2 and needle in err


def test_posting_needs_the_environment(roots, keys, capsys, monkeypatch):
    monkeypatch.delenv("JARVIS_MEMORYBOARD_URL", raising=False)
    monkeypatch.delenv("JARVIS_API_KEY_FILE", raising=False)
    rc, out, err = cli(capsys, "statement", "key", "--roots", str(roots), "--root-key", str(keys["root"].path), "--mint-pub", str(keys["mint"].path) + ".pub", "--from-seq", "1", "--post")
    assert rc == 2 and "JARVIS_MEMORYBOARD_URL" in err


def test_the_service_refuses_what_the_witness_signs_if_the_position_is_wrong(client, roots, keys, tmp_path, capsys, witness_api):
    rc, out, err = cli(capsys, "statement", "key", "--roots", str(roots), "--root-key", str(keys["root"].path), "--tenant", OP, "--mint-pub", str(keys["mint"].path) + ".pub",
                       "--from-seq", "1", "--stmt-seq", "9", "--prev-hash", attest.GENESIS, "--out", str(tmp_path / "x.json"))
    assert rc == 0
    assert post_body(client, json.loads((tmp_path / "x.json").read_text())).status_code == 409


# --- running where the application's packages are not -------------------------------------------------------------------------------------------------------

def test_the_witness_verifies_without_the_cryptography_package(client, roots, keys, api, tmp_path):
    signed_ledger(client, keys, api)
    f = write(tmp_path, export_from(client))
    code = (f"import sys\nsys.modules['cryptography'] = None\nfrom app import witness\n"
            f"raise SystemExit(witness.main(['verify-export', {str(f)!r}, '--roots', {str(roots)!r}]))")
    r = subprocess.run([sys.executable, "-c", code], cwd=signer.repo_root(), capture_output=True, text=True)
    assert r.returncode == 0 and "ok: tenant" in r.stdout, r.stderr
    tampered = export_from(client)
    tampered["tenants"][OP]["attestations"][0]["subject_hash"] = sha("x")
    f2 = write(tmp_path, tampered, "t.json")
    code2 = code.replace(str(f), str(f2))
    assert subprocess.run([sys.executable, "-c", code2], cwd=signer.repo_root(), capture_output=True, text=True).returncode == 1
