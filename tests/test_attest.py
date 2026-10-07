"""Signatures, verification side, without a database: SSH signatures against the real ssh-keygen, the messages, the trust log
and the attestation log, with every way each can be wrong."""

from __future__ import annotations

import base64

import pytest

from app import attest
from tests.attest_support import (
    NOW, TENANT, attestation, block_attestation, chain, checkpoint_attestation, make_key, requires_ssh_keygen, roots_of, sha, sign, ssh_verify,
    statement, trust_chain,
)

pytestmark = requires_ssh_keygen

BH = {1: sha("block one"), 2: sha("block two")}


@pytest.fixture(scope="module")
def keys(tmp_path_factory):
    d = tmp_path_factory.mktemp("keys")
    return {n: make_key(d, n) for n in ("root", "root2", "mint", "mint2", "stranger")}


# --- keys and fingerprints --------------------------------------------------------------------------------------------

def test_the_fingerprint_matches_what_ssh_keygen_prints(keys, tmp_path):
    import subprocess

    for k in keys.values():
        out = subprocess.run(["ssh-keygen", "-lf", str(k.path) + ".pub"], capture_output=True, text=True).stdout
        assert k.key_id in out and k.key_id.startswith("SHA256:") and len(k.key_id) == 50


def test_a_public_key_line_round_trips_and_keeps_its_comment(keys):
    k = keys["mint"]
    again = attest.parse_public_key(k.pub_text + "\n")
    assert again.key_id == k.key_id and again.comment == "mint" and again.text() == k.pub_text


@pytest.mark.parametrize("line,code", [
    ("", "key_malformed"), ("ssh-ed25519", "key_malformed"), ("ssh-ed25519 !!!notbase64 x", "key_malformed"),
    ("ssh-ed25519 " + base64.b64encode(b"short").decode(), "key_malformed"),
    ("ssh-rsa AAAAB3NzaC1yc2E x", "key_unsupported"), ("sk-ssh-ed25519@openssh.com AAAA x", "key_unsupported"),
    ("ecdsa-sha2-nistp256 AAAA x", "key_unsupported"),
])
def test_unsupported_and_malformed_public_keys_are_refused(line, code):
    with pytest.raises(attest.AttestError) as exc:
        attest.parse_public_key(line)
    assert exc.value.code == code


def test_an_rsa_key_made_by_ssh_keygen_is_refused(tmp_path):
    k = make_key(tmp_path, "rsa", "rsa")
    with pytest.raises(attest.AttestError) as exc:
        attest.parse_public_key((tmp_path / "rsa.pub").read_text())
    assert exc.value.code == "key_unsupported"


def test_the_roots_file_is_read_with_comments_and_refuses_junk(keys, tmp_path):
    f = tmp_path / "roots.pub"
    f.write_text(f"# the roots\n\n{keys['root'].pub_text}\n  {keys['root2'].pub_text}  \n")
    assert set(attest.load_roots(f)) == {keys["root"].key_id, keys["root2"].key_id}
    assert attest.load_roots(None) == {}
    f.write_text("# nothing here\n")
    assert attest.load_roots(f) == {}
    f.write_text("this is not a key\n")
    with pytest.raises(attest.AttestError) as exc:
        attest.load_roots(f)
    assert "line 1" in exc.value.message
    with pytest.raises(attest.AttestError) as exc:
        attest.load_roots(tmp_path / "missing")
    assert exc.value.code == "trust_roots_unreadable"


def test_the_roots_file_is_found_through_the_environment(keys, tmp_path, monkeypatch):
    f = tmp_path / "r.pub"
    f.write_text(keys["root"].pub_text + "\n")
    monkeypatch.setenv(attest.ROOTS_ENV, str(f))
    assert set(attest.load_roots()) == {keys["root"].key_id}


# --- signatures, checked against the real tool --------------------------------------------------------------------------------

@pytest.mark.parametrize("hash_alg", [None, "sha256", "sha512"])
def test_a_real_signature_verifies_in_python_and_in_ssh_keygen(keys, hash_alg):
    msg = "jarvis-attest|v1|block|5:alice|block:1|" + BH[1]
    sig = sign(keys["mint"], msg, hash_alg=hash_alg)
    assert attest.verify_sshsig(sig, msg.encode()).key_id == keys["mint"].key_id
    assert ssh_verify(keys["mint"], msg, sig) is True


def test_the_python_verifier_agrees_with_ssh_keygen_on_every_tamper(keys):
    msg = "a message to sign"
    good = sign(keys["mint"], msg)
    cases = {
        "another message": (msg + " ", good, attest.NAMESPACE),
        "another namespace": (msg, good, "someone-elses-namespace"),
        "another key": (msg, sign(keys["stranger"], msg), attest.NAMESPACE),
    }
    for name, (m, sig, ns) in cases.items():
        python_ok = True
        try:
            attest.verify_sshsig(sig, m.encode(), namespace=ns)
        except attest.SignatureInvalid:
            python_ok = False
        # the same signature, judged by the real tool against the key it claims to come from
        claimed = keys["stranger"] if name == "another key" else keys["mint"]
        assert python_ok == ssh_verify(claimed, m, sig, ns) == (name == "another key" and ns == attest.NAMESPACE and m == msg), name


def test_a_signature_for_another_namespace_is_refused(keys):
    sig = sign(keys["mint"], "m", namespace="git")
    with pytest.raises(attest.SignatureInvalid, match="namespace"):
        attest.verify_sshsig(sig, b"m")
    assert attest.verify_sshsig(sig, b"m", namespace="git").key_id == keys["mint"].key_id


def test_a_changed_message_or_signature_is_refused(keys):
    sig = sign(keys["mint"], "m")
    with pytest.raises(attest.SignatureInvalid, match="does not match"):
        attest.verify_sshsig(sig, b"n")
    lines = sig.strip().split("\n")
    raw = bytearray(base64.b64decode("".join(lines[1:-1])))
    raw[-1] ^= 1  # flip a bit inside the Ed25519 signature
    forged = "\n".join([lines[0], base64.b64encode(bytes(raw)).decode(), lines[-1]])
    with pytest.raises(attest.SignatureInvalid):
        attest.verify_sshsig(forged, b"m")


@pytest.mark.parametrize("armored", [
    "", "garbage", "-----BEGIN SSH SIGNATURE-----\n-----END SSH SIGNATURE-----",
    "-----BEGIN SSH SIGNATURE-----\n!!!!\n-----END SSH SIGNATURE-----",
    "-----BEGIN SSH SIGNATURE-----\n" + base64.b64encode(b"NOTSSH" + b"\x00" * 40).decode() + "\n-----END SSH SIGNATURE-----",
    "-----BEGIN SSH SIGNATURE-----\n" + base64.b64encode(b"SSHSIG" + b"\x00\x00\x00\x02").decode() + "\n-----END SSH SIGNATURE-----",
    "-----BEGIN SSH SIGNATURE-----\n" + base64.b64encode(b"SSHSIG\x00\x00\x00\x01\x00\x00").decode() + "\n-----END SSH SIGNATURE-----",
])
def test_garbage_is_never_a_signature(armored):
    with pytest.raises(attest.SignatureInvalid):
        attest.verify_sshsig(armored, b"m")


def test_a_signature_with_a_rsa_key_inside_is_refused(tmp_path):
    k = make_key(tmp_path, "rsa", "rsa")
    sig = sign(k.path, "m")
    with pytest.raises(attest.SignatureInvalid):
        attest.verify_sshsig(sig, b"m")


def test_crlf_and_blanks_around_a_stored_signature_do_not_matter(keys):
    sig = sign(keys["mint"], "m")
    assert attest.verify_sshsig("\n\n" + sig.replace("\n", "\r\n") + "\n\n", b"m").key_id == keys["mint"].key_id
    assert attest.normalize_signature("  a\r\nb \r\n") == "a\nb"


# --- the messages ----------------------------------------------------------------------------------------------------------------

def test_the_attestation_message_known_answer():
    m = attest.attestation_message("block", "alice", "block:7", BH[1], 4, attest.GENESIS, NOW)
    assert m == f"jarvis-attest|v1|block|5:alice|block:7|{BH[1]}|4|{'0' * 64}|{NOW}"
    assert attest.attestation_message("block", "héllo", "block:1", BH[1], 1, attest.GENESIS, NOW).split("|")[3] == "6:héllo"


@pytest.mark.parametrize("kw", [
    {"kind": "evidence"}, {"subject_hash": "abc"}, {"prev_hash": "G" * 64}, {"signed_at": "yesterday"}, {"subject": "a|b"}, {"subject": "a\nb"},
])
def test_a_bad_field_never_makes_a_message(kw):
    args = dict(kind="block", tenant="alice", subject="block:1", subject_hash=BH[1], signer_seq=1, prev_hash=attest.GENESIS, signed_at=NOW) | kw
    with pytest.raises(attest.AttestError):
        attest.attestation_message(**args)


def test_every_field_of_a_message_is_covered_by_the_attestation_hash(keys):
    base = attestation(keys["mint"], "block", "block:1", BH[1], 1, attest.GENESIS)
    for change in ({"subject": "block:2"}, {"subject_hash": BH[2]}, {"signer_seq": 2}, {"prev_hash": sha("x")}, {"signed_at": "2026-10-07T12:00:01Z"}):
        args = dict(kind="block", subject="block:1", subject_hash=BH[1], signer_seq=1, prev_hash=attest.GENESIS, signed_at=NOW) | change
        other = attest.attestation_message(args["kind"], TENANT, args["subject"], args["subject_hash"], args["signer_seq"], args["prev_hash"], args["signed_at"])
        assert attest.attestation_hash(other, base.key_id, base.signature) != base.attestation_hash
    assert attest.attestation_hash("m", "k", "s") != attest.attestation_hash("m", "k2", "s") != attest.attestation_hash("m", "k", "s2")


def test_the_trust_message_known_answer():
    assert attest.trust_message("key", "alice", "SHA256:" + "A" * 43, 5, None, 2, sha("p")) == f"jarvis-trust|v1|key|5:alice|SHA256:{'A' * 43}|5||2|{sha('p')}"
    assert attest.trust_message("cosign", "alice", "k", 9, BH[1], 3, attest.GENESIS) == f"jarvis-trust|v1|cosign|5:alice|k|9|{BH[1]}|3|{'0' * 64}"
    with pytest.raises(attest.AttestError):
        attest.trust_message("promote", "alice", "k", None, None, 1, attest.GENESIS)


def test_the_checkpoint_digest_covers_every_part_of_it():
    base = attest.checkpoint_hash("alice", 5, sha("head"), 2, BH[2])
    for other in (attest.checkpoint_hash("bob", 5, sha("head"), 2, BH[2]), attest.checkpoint_hash("alice", 6, sha("head"), 2, BH[2]),
                  attest.checkpoint_hash("alice", 5, sha("h2"), 2, BH[2]), attest.checkpoint_hash("alice", 5, sha("head"), 3, BH[2]),
                  attest.checkpoint_hash("alice", 5, sha("head"), 2, BH[1])):
        assert other != base
    assert attest.parse_checkpoint_subject(attest.checkpoint_subject(5, sha("head"), 2, BH[2])) == (5, sha("head"), 2, BH[2])
    assert attest.parse_checkpoint_subject("checkpoint:x:y") is None and attest.parse_block_subject("block:0") is None and attest.parse_block_subject("block:12") == 12


# --- the trust log ----------------------------------------------------------------------------------------------------------------

def test_a_root_can_authorize_a_key_and_the_log_replays(keys):
    log = trust_chain(keys["root"], ("key", {"key": keys["mint"], "arg": 1}))
    t = attest.evaluate_trust(TENANT, roots_of(keys["root"]), log)
    assert t.problems == [] and set(t.keys) == {keys["mint"].key_id} and t.keys[keys["mint"].key_id].from_seq == 1
    assert (t.head_seq, t.head_hash) == (1, log[0].statement_hash)


def test_with_no_pinned_root_nothing_is_trusted(keys):
    log = trust_chain(keys["root"], ("key", {"key": keys["mint"], "arg": 1}))
    t = attest.evaluate_trust(TENANT, {}, log)
    assert t.keys == {} and "not a root key" in t.problems[0]["problem"]


def test_a_statement_signed_by_the_signing_key_itself_is_refused(keys):
    log = trust_chain(keys["root"], ("key", {"key": keys["mint"], "arg": 1}), ("key", {"key": keys["mint2"], "arg": 5, "by": keys["mint"]}))
    t = attest.evaluate_trust(TENANT, roots_of(keys["root"]), log)
    assert set(t.keys) == {keys["mint"].key_id} and "not a root key" in t.problems[0]["problem"]  # a taken Mint key cannot authorize its successor


def test_a_stranger_cannot_authorize_anything(keys):
    t = attest.evaluate_trust(TENANT, roots_of(keys["root"]), trust_chain(keys["stranger"], ("key", {"key": keys["mint"], "arg": 1})))
    assert t.keys == {} and "not a root key" in t.problems[0]["problem"]


def test_adding_a_second_root_makes_either_enough(keys):
    log = trust_chain(keys["root"], ("root_add", {"key": keys["root2"]}), ("key", {"key": keys["mint"], "arg": 1, "by": keys["root2"]}))
    t = attest.evaluate_trust(TENANT, roots_of(keys["root"]), log)
    assert t.problems == [] and set(t.roots) == {keys["root"].key_id, keys["root2"].key_id} and keys["mint"].key_id in t.keys
    again = trust_chain(keys["root"], ("root_add", {"key": keys["root2"]}), ("root_add", {"key": keys["root2"]}))
    assert "already trusted" in attest.evaluate_trust(TENANT, roots_of(keys["root"]), again).problems[0]["problem"]


def test_a_root_added_by_a_non_root_does_not_count(keys):
    log = trust_chain(keys["root"], ("root_add", {"key": keys["mint2"], "by": keys["stranger"]}))
    assert keys["mint2"].key_id not in attest.evaluate_trust(TENANT, roots_of(keys["root"]), log).roots


def test_revoking_sets_a_cutoff_and_the_lowest_cutoff_wins(keys):
    log = trust_chain(keys["root"], ("key", {"key": keys["mint"], "arg": 1}), ("revoke", {"key": keys["mint"], "arg": 9}), ("revoke", {"key": keys["mint"], "arg": 4}),
                      ("revoke", {"key": keys["mint"], "arg": 7}))
    t = attest.evaluate_trust(TENANT, roots_of(keys["root"]), log)
    assert t.problems == [] and t.keys[keys["mint"].key_id].cutoff == 4


def test_revoking_a_key_that_was_never_authorized_is_a_problem(keys):
    t = attest.evaluate_trust(TENANT, roots_of(keys["root"]), trust_chain(keys["root"], ("revoke", {"key": keys["mint"], "arg": 3})))
    assert "never authorized" in t.problems[0]["problem"]


@pytest.mark.parametrize("arg", [0, None])
def test_a_key_must_start_at_a_signer_seq_of_at_least_one(keys, arg):
    t = attest.evaluate_trust(TENANT, roots_of(keys["root"]), trust_chain(keys["root"], ("key", {"key": keys["mint"], "arg": arg})))
    assert t.keys == {} and t.problems


def test_authorizing_the_same_key_twice_is_refused(keys):
    t = attest.evaluate_trust(TENANT, roots_of(keys["root"]), trust_chain(keys["root"], ("key", {"key": keys["mint"], "arg": 1}), ("key", {"key": keys["mint"], "arg": 5})))
    assert "already authorized" in t.problems[0]["problem"] and t.keys[keys["mint"].key_id].from_seq == 1


def test_a_cosign_must_come_from_the_root_that_names_itself(keys):
    ok = trust_chain(keys["root"], ("cosign", {"key_id": keys["root"].key_id, "arg": 3, "subject_hash": BH[1]}))
    t = attest.evaluate_trust(TENANT, roots_of(keys["root"]), ok)
    assert t.problems == [] and [(c.checkpoint_seq, c.checkpoint_hash, c.root_id) for c in t.cosigns] == [(3, BH[1], keys["root"].key_id)]
    other = trust_chain(keys["root"], ("cosign", {"key_id": keys["root2"].key_id, "arg": 3, "subject_hash": BH[1]}))
    assert attest.evaluate_trust(TENANT, roots_of(keys["root"], keys["root2"]), other).cosigns == []


def test_a_rotation_continues_the_signing_log(keys):
    log = trust_chain(keys["root"], ("key", {"key": keys["mint"], "arg": 1}), ("key", {"key": keys["mint2"], "arg": 4}))
    t = attest.evaluate_trust(TENANT, roots_of(keys["root"]), log)
    assert t.problems == [] and t.keys[keys["mint2"].key_id].from_seq == 4


def rehash(s: attest.Statement, **change) -> attest.Statement:
    """What someone with write access to the table would do: change a field, then recompute the stored hash so it is consistent."""
    s2 = attest.Statement(**{**s.__dict__, **change})
    message = attest.trust_message(s2.kind, TENANT, s2.key_id, s2.arg, s2.subject_hash, s2.stmt_seq, s2.prev_hash)
    return attest.Statement(**{**s2.__dict__, "statement_hash": attest.statement_hash(message, s2.signed_by, s2.signature)})


@pytest.mark.parametrize("tamper,needle", [
    (lambda s: rehash(s, signature=s.signature.replace("A", "B", 3)), "bad signature"),
    (lambda s: attest.Statement(**{**s.__dict__, "signature": s.signature.replace("A", "B", 3)}), "statement hash"),
    (lambda s: attest.Statement(**{**s.__dict__, "arg": 99}), "statement hash"),
    (lambda s: rehash(s, arg=99), "bad signature"),  # the argument is covered by the signature
    (lambda s: attest.Statement(**{**s.__dict__, "prev_hash": sha("else")}), "prev_hash"),
    (lambda s: attest.Statement(**{**s.__dict__, "stmt_seq": 3}), "expected statement"),
    (lambda s: rehash(s, signed_by="SHA256:" + "B" * 43), "not a root key"),
])
def test_every_way_a_statement_can_be_tampered_is_reported_and_it_is_ignored(keys, tamper, needle):
    log = trust_chain(keys["root"], ("key", {"key": keys["mint"], "arg": 1}))
    bad = tamper(log[0])
    t = attest.evaluate_trust(TENANT, roots_of(keys["root"]), [bad])
    assert t.keys == {} and any(needle in p["problem"] for p in t.problems), t.problems


def test_a_missing_statement_is_one_problem_not_a_cascade(keys):
    log = trust_chain(keys["root"], ("key", {"key": keys["mint"], "arg": 1}), ("revoke", {"key": keys["mint"], "arg": 3}), ("root_add", {"key": keys["root2"]}))
    t = attest.evaluate_trust(TENANT, roots_of(keys["root"]), [log[0], log[2]])
    assert len(t.problems) == 2 and keys["root2"].key_id not in t.roots  # the gap itself, and the statement after it no longer chains


def test_a_pubkey_that_does_not_match_its_key_id_is_refused(keys):
    s = statement(keys["root"], "key", 1, attest.GENESIS, key=keys["mint"], key_id=keys["mint2"].key_id, arg=1)
    t = attest.evaluate_trust(TENANT, roots_of(keys["root"]), [s])
    assert t.keys == {} and "fingerprint" in t.problems[0]["problem"]


def test_the_statements_are_for_one_tenant(keys):
    log = trust_chain(keys["root"], ("key", {"key": keys["mint"], "arg": 1}), tenant="bob")
    assert attest.evaluate_trust("alice", roots_of(keys["root"]), log).keys == {}


# --- the attestation log ------------------------------------------------------------------------------------------------------------

def trusted(keys, from_seq=1, cutoff=None):
    steps = [("key", {"key": keys["mint"], "arg": from_seq})]
    if cutoff is not None:
        steps.append(("revoke", {"key": keys["mint"], "arg": cutoff}))
    return attest.evaluate_trust(TENANT, roots_of(keys["root"]), trust_chain(keys["root"], *steps))


def truth(blocks=None, receipts=None):
    return attest.DictTruth(blocks if blocks is not None else BH, receipts or {})


def test_a_clean_log_of_blocks_and_a_checkpoint_verifies(keys):
    rows = chain(keys["mint"], [("block", "block:1", BH[1]), ("block", "block:2", BH[2])])
    rows.append(checkpoint_attestation(keys["mint"], 3, rows, 2, BH[2]))
    problems, s = attest.evaluate_attestations(TENANT, trusted(keys), rows, truth())
    assert problems == [] and s.count == 3 and s.head_seq == 3 and s.head_hash == rows[-1].attestation_hash
    assert s.blocks == {1, 2} and s.newest_checkpoint_seq == 3 and s.cosigned_checkpoint_seq == 0 and s.keys_used == {keys["mint"].key_id}


def test_a_receipt_attestation_names_the_receipts_evidence_id(keys):
    eo = "eo:sha256:" + sha("receipt payload")
    rows = chain(keys["mint"], [("receipt", eo, sha("receipt payload"))])
    problems, s = attest.evaluate_attestations(TENANT, trusted(keys), rows, truth(receipts={eo: None}))
    assert problems == [] and s.receipts == {eo}
    problems, _ = attest.evaluate_attestations(TENANT, trusted(keys), rows, truth(receipts={eo: f"receipt {eo} is damaged: x"}))
    assert "damaged" in problems[0]["problem"]
    problems, _ = attest.evaluate_attestations(TENANT, trusted(keys), rows, truth())
    assert "does not exist" in problems[0]["problem"]
    wrong = chain(keys["mint"], [("receipt", eo, sha("another"))])
    problems, _ = attest.evaluate_attestations(TENANT, trusted(keys), wrong, truth(receipts={eo: None}))
    assert "same 64 hex" in problems[0]["problem"]


def test_a_block_attestation_must_match_the_blocks_real_hash(keys):
    rows = chain(keys["mint"], [("block", "block:1", sha("a different block"))])
    problems, s = attest.evaluate_attestations(TENANT, trusted(keys), rows, truth())
    assert "but the block's hash is" in problems[0]["problem"] and s.blocks == set()
    problems, _ = attest.evaluate_attestations(TENANT, trusted(keys), chain(keys["mint"], [("block", "block:9", BH[1])]), truth())
    assert "does not exist" in problems[0]["problem"]
    problems, _ = attest.evaluate_attestations(TENANT, trusted(keys), chain(keys["mint"], [("block", "block:x", BH[1])]), truth())
    assert "looks like block:" in problems[0]["problem"]


@pytest.mark.parametrize("name,mutate,needle", [
    ("signature", lambda a: attest.Attestation(**{**a.__dict__, "signature": a.signature.replace("A", "B", 2)}), "bad signature"),
    ("content", lambda a: attest.Attestation(**{**a.__dict__, "subject_hash": sha("else")}), "stored attestation hash"),
    ("prev", lambda a: attest.Attestation(**{**a.__dict__, "prev_hash": sha("else")}), "prev_hash"),
    ("seq", lambda a: attest.Attestation(**{**a.__dict__, "signer_seq": 5}), "expected attestation"),
    ("key id", lambda a: attest.Attestation(**{**a.__dict__, "key_id": "SHA256:" + "C" * 43}), "no root authorized"),
])
def test_every_way_an_attestation_can_be_tampered_is_reported(keys, name, mutate, needle):
    rows = chain(keys["mint"], [("block", "block:1", BH[1])])
    problems, s = attest.evaluate_attestations(TENANT, trusted(keys), [mutate(rows[0])], truth())
    assert any(needle in p["problem"] for p in problems), problems
    assert s.blocks == set()


def test_a_signature_made_by_another_key_than_the_one_named_is_refused(keys):
    msg = attest.attestation_message("block", TENANT, "block:1", BH[1], 1, attest.GENESIS, NOW)
    sig = attest.normalize_signature(sign(keys["stranger"], msg))
    a = attest.Attestation(1, "block", "block:1", BH[1], attest.GENESIS, keys["mint"].key_id, NOW, sig, attest.attestation_hash(msg, keys["mint"].key_id, sig))
    problems, _ = attest.evaluate_attestations(TENANT, trusted(keys), [a], truth())
    assert "different key than the one named" in problems[0]["problem"]


def test_a_key_no_root_authorized_cannot_attest(keys):
    rows = chain(keys["stranger"], [("block", "block:1", BH[1])])
    problems, _ = attest.evaluate_attestations(TENANT, trusted(keys), rows, truth())
    assert "no root authorized" in problems[0]["problem"]


def test_a_key_cannot_attest_before_it_was_authorized_to_start(keys):
    rows = chain(keys["mint"], [("block", "block:1", BH[1]), ("block", "block:2", BH[2])])
    problems, s = attest.evaluate_attestations(TENANT, trusted(keys, from_seq=2), rows, truth())
    assert len(problems) == 1 and "not authorized until attestation 2" in problems[0]["problem"] and s.blocks == {2}


def test_attestations_after_a_revocation_cutoff_are_untrusted_and_earlier_ones_stay_valid(keys):
    rows = chain(keys["mint"], [("block", "block:1", BH[1]), ("block", "block:2", BH[2])])
    problems, s = attest.evaluate_attestations(TENANT, trusted(keys, cutoff=1), rows, truth())
    assert len(problems) == 1 and "revoked with a cutoff of 1" in problems[0]["problem"] and s.blocks == {1}


def test_a_rotation_keeps_the_log_going_under_the_new_key(keys):
    old = chain(keys["mint"], [("block", "block:1", BH[1])])
    new = block_attestation(keys["mint2"], 2, BH[2], 2, old[-1].attestation_hash)
    log = trust_chain(keys["root"], ("key", {"key": keys["mint"], "arg": 1}), ("key", {"key": keys["mint2"], "arg": 2}), ("revoke", {"key": keys["mint"], "arg": 1}))
    trust = attest.evaluate_trust(TENANT, roots_of(keys["root"]), log)
    problems, s = attest.evaluate_attestations(TENANT, trust, old + [new], truth())
    assert problems == [] and s.blocks == {1, 2} and s.keys_used == {keys["mint"].key_id, keys["mint2"].key_id}


def test_removing_an_attestation_from_the_middle_is_found(keys):
    rows = chain(keys["mint"], [("block", "block:1", BH[1]), ("block", "block:2", BH[2]), ("checkpoint", "x", sha("x"))])
    problems, _ = attest.evaluate_attestations(TENANT, trusted(keys), [rows[0], rows[2]], truth())
    assert any("expected attestation 2" in p["problem"] for p in problems) and any("prev_hash" in p["problem"] for p in problems)


def test_attesting_the_same_block_twice_is_flagged_and_a_different_hash_is_equivocation(keys):
    first = block_attestation(keys["mint"], 1, BH[1], 1, attest.GENESIS)
    again = block_attestation(keys["mint"], 1, BH[1], 2, first.attestation_hash)
    problems, _ = attest.evaluate_attestations(TENANT, trusted(keys), [first, again], truth())
    assert len(problems) == 1 and "already attested at 1" in problems[0]["problem"] and "DIFFERENT" not in problems[0]["problem"]
    forked = block_attestation(keys["mint"], 1, sha("rewritten block"), 2, first.attestation_hash)
    problems, _ = attest.evaluate_attestations(TENANT, trusted(keys), [first, forked], truth({1: sha("rewritten block")}))
    assert any("DIFFERENT hash (equivocation)" in p["problem"] for p in problems)


def test_the_log_is_for_one_tenant(keys):
    rows = chain(keys["mint"], [("block", "block:1", BH[1])], tenant="bob")
    problems, _ = attest.evaluate_attestations("alice", trusted(keys), rows, truth())
    assert any("bad signature" in p["problem"] for p in problems)


def test_an_unknown_kind_is_never_valid(keys):
    rows = chain(keys["mint"], [("block", "block:1", BH[1])])
    bad = attest.Attestation(**{**rows[0].__dict__, "kind": "evidence"})
    problems, _ = attest.evaluate_attestations(TENANT, trusted(keys), [bad], truth())
    assert problems and "an attestation is one of" in problems[0]["problem"]


@pytest.mark.parametrize("name,build,needle", [
    ("wrong covered seq", lambda keys, rows: attestation(keys["mint"], "checkpoint", attest.checkpoint_subject(0, attest.GENESIS, 2, BH[2]),
                                                          attest.checkpoint_hash(TENANT, 0, attest.GENESIS, 2, BH[2]), 2, rows[-1].attestation_hash), "just before it"),
    ("wrong covered head", lambda keys, rows: attestation(keys["mint"], "checkpoint", attest.checkpoint_subject(1, sha("not the head"), 2, BH[2]),
                                                           attest.checkpoint_hash(TENANT, 1, sha("not the head"), 2, BH[2]), 2, rows[-1].attestation_hash), "actual head"),
    ("wrong tip", lambda keys, rows: attestation(keys["mint"], "checkpoint", attest.checkpoint_subject(1, rows[-1].attestation_hash, 2, sha("fake")),
                                                  attest.checkpoint_hash(TENANT, 1, rows[-1].attestation_hash, 2, sha("fake")), 2, rows[-1].attestation_hash), "tip block 2"),
    ("hash not matching its subject", lambda keys, rows: attestation(keys["mint"], "checkpoint", attest.checkpoint_subject(1, rows[-1].attestation_hash, 2, BH[2]),
                                                                      sha("whatever"), 2, rows[-1].attestation_hash), "does not match its subject"),
    ("malformed", lambda keys, rows: attestation(keys["mint"], "checkpoint", "checkpoint:oops", sha("x"), 2, rows[-1].attestation_hash), "looks like checkpoint"),
])
def test_a_checkpoint_must_describe_the_log_and_the_tip_exactly(keys, name, build, needle):
    rows = chain(keys["mint"], [("block", "block:1", BH[1])])
    problems, s = attest.evaluate_attestations(TENANT, trusted(keys), rows + [build(keys, rows)], truth())
    assert any(needle in p["problem"] for p in problems), problems
    assert s.newest_checkpoint_seq == 0


def test_a_checkpoint_before_any_block_uses_height_zero(keys):
    cp = attestation(keys["mint"], "checkpoint", attest.checkpoint_subject(0, attest.GENESIS, 0, attest.GENESIS),
                     attest.checkpoint_hash(TENANT, 0, attest.GENESIS, 0, attest.GENESIS), 1, attest.GENESIS)
    problems, s = attest.evaluate_attestations(TENANT, trusted(keys), [cp], truth({}))
    assert problems == [] and s.newest_checkpoint_seq == 1


def test_a_root_cosign_marks_a_checkpoint_as_witnessed(keys):
    rows = chain(keys["mint"], [("block", "block:1", BH[1])])
    cp = checkpoint_attestation(keys["mint"], 2, rows, 1, BH[1])
    log = trust_chain(keys["root"], ("key", {"key": keys["mint"], "arg": 1}),
                      ("cosign", {"key_id": keys["root"].key_id, "arg": 2, "subject_hash": cp.attestation_hash}))
    trust = attest.evaluate_trust(TENANT, roots_of(keys["root"]), log)
    problems, s = attest.evaluate_attestations(TENANT, trust, rows + [cp], truth())
    assert problems == [] and s.cosigned_checkpoint_seq == 2 and s.newest_checkpoint_seq == 2


def test_a_cosign_of_a_checkpoint_the_log_does_not_hold_is_a_fork(keys):
    rows = chain(keys["mint"], [("block", "block:1", BH[1])])
    cp = checkpoint_attestation(keys["mint"], 2, rows, 1, BH[1])
    log = trust_chain(keys["root"], ("key", {"key": keys["mint"], "arg": 1}),
                      ("cosign", {"key_id": keys["root"].key_id, "arg": 2, "subject_hash": sha("a different checkpoint")}))
    trust = attest.evaluate_trust(TENANT, roots_of(keys["root"]), log)
    problems, s = attest.evaluate_attestations(TENANT, trust, rows + [cp], truth())
    assert any(p["check"] == "cosign" and "a fork" in p["problem"] for p in problems) and s.cosigned_checkpoint_seq == 0


# --- voiding a bad row -----------------------------------------------------------------------------------------------------------------

def test_a_root_can_void_a_bad_attestation_and_the_rest_of_the_log_still_verifies(keys):
    good = chain(keys["mint"], [("block", "block:1", BH[1])])
    parked = attestation(keys["stranger"], "block", "block:2", BH[2], 2, good[-1].attestation_hash)  # signed by a key no root authorized
    after = block_attestation(keys["mint"], 2, BH[2], 3, parked.attestation_hash)
    base = trust_chain(keys["root"], ("key", {"key": keys["mint"], "arg": 1}))
    problems, _ = attest.evaluate_attestations(TENANT, attest.evaluate_trust(TENANT, roots_of(keys["root"]), base), good + [parked, after], truth())
    assert any("no root authorized" in p["problem"] for p in problems)
    voided = trust_chain(keys["root"], ("key", {"key": keys["mint"], "arg": 1}), ("void", {"key_id": keys["root"].key_id, "arg": 2, "subject_hash": parked.attestation_hash}))
    t = attest.evaluate_trust(TENANT, roots_of(keys["root"]), voided)
    problems, s = attest.evaluate_attestations(TENANT, t, good + [parked, after], truth())
    assert problems == [] and s.voided == [2] and s.blocks == {1, 2}  # block 2 counts through the honest attestation after the voided one


def test_a_void_names_exactly_one_row_and_a_replaced_row_is_still_caught(keys):
    good = chain(keys["mint"], [("block", "block:1", BH[1])])
    parked = attestation(keys["stranger"], "block", "block:2", BH[2], 2, good[-1].attestation_hash)
    void = trust_chain(keys["root"], ("key", {"key": keys["mint"], "arg": 1}), ("void", {"key_id": keys["root"].key_id, "arg": 2, "subject_hash": sha("some other row")}))
    t = attest.evaluate_trust(TENANT, roots_of(keys["root"]), void)
    problems, s = attest.evaluate_attestations(TENANT, t, good + [parked], truth())
    assert any("different hash (the row was replaced)" in p["problem"] for p in problems) and s.voided == []


def test_only_a_root_can_void_and_it_must_name_itself(keys):
    base = [("key", {"key": keys["mint"], "arg": 1})]
    by_signer = trust_chain(keys["root"], *base, ("void", {"key_id": keys["mint"].key_id, "arg": 2, "subject_hash": sha("x"), "by": keys["mint"]}))
    t = attest.evaluate_trust(TENANT, roots_of(keys["root"]), by_signer)
    assert t.voids == {} and "not a root key" in t.problems[0]["problem"]
    other_root = trust_chain(keys["root"], *base, ("void", {"key_id": keys["root2"].key_id, "arg": 2, "subject_hash": sha("x")}))
    assert attest.evaluate_trust(TENANT, roots_of(keys["root"]), other_root).voids == {}
    for bad in ({"arg": None, "subject_hash": sha("x")}, {"arg": 0, "subject_hash": sha("x")}, {"arg": 2, "subject_hash": None}):
        log = trust_chain(keys["root"], *base, ("void", {"key_id": keys["root"].key_id, **bad}))
        assert attest.evaluate_trust(TENANT, roots_of(keys["root"]), log).voids == {}


def test_a_statement_naming_a_root_but_signed_by_a_stranger_is_refused(keys):
    """The signature is valid, just not by the root it claims: without checking WHO signed, anyone could pose as a root."""
    forged = statement(keys["stranger"], "key", 1, attest.GENESIS, key=keys["mint"], arg=1)
    message = attest.trust_message("key", TENANT, forged.key_id, 1, None, 1, attest.GENESIS)
    posing = attest.Statement(**{**forged.__dict__, "signed_by": keys["root"].key_id,
                                 "statement_hash": attest.statement_hash(message, keys["root"].key_id, forged.signature)})
    t = attest.evaluate_trust(TENANT, roots_of(keys["root"]), [posing])
    assert t.keys == {} and "different key than the one named" in t.problems[0]["problem"]


def test_an_attestation_naming_the_authorized_key_but_signed_by_another_is_refused_through_the_whole_log(keys):
    msg = attest.attestation_message("block", TENANT, "block:1", BH[1], 1, attest.GENESIS, NOW)
    sig = attest.normalize_signature(sign(keys["stranger"], msg))
    posing = attest.Attestation(1, "block", "block:1", BH[1], attest.GENESIS, keys["mint"].key_id, NOW, sig, attest.attestation_hash(msg, keys["mint"].key_id, sig))
    problems, s = attest.evaluate_attestations(TENANT, trusted(keys), [posing], truth())
    assert s.blocks == set() and any("different key than the one named" in p["problem"] for p in problems)
