"""The signatures export that rides with every backup set: parsed from the dump's own COPY text."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "deploy" / "mint" / "bin" / "sigexport.py"
spec = importlib.util.spec_from_file_location("sigexport", SCRIPT)
sigexport = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sigexport)  # type: ignore[union-attr]

H = "a" * 64
SIG = "-----BEGIN SSH SIGNATURE-----\\nU1NIU0lH\\nAAAA\\n-----END SSH SIGNATURE-----"  # as COPY writes a newline inside a value
COPY = "\n".join([
    "-- some dump text",
    "COPY jarvis.memories (tenant_key, id, content) FROM stdin;",
    "operator\tmem-1\tnot interesting",
    "\\.",
    "",
    "COPY jarvis.attestations (tenant_key, signer_seq, kind, subject, subject_hash, prev_hash, key_id, signed_at, signature, attestation_hash, stored_at) FROM stdin;",
    f"operator\t2\tblock\tblock:2\t{H}\t{'b' * 64}\tSHA256:{'K' * 43}\t2026-10-07T12:00:00Z\t{SIG}\t{'c' * 64}\t2026-10-07 12:00:01+00",
    f"operator\t1\tblock\tblock:1\t{H}\t{'0' * 64}\tSHA256:{'K' * 43}\t2026-10-07T11:00:00Z\t{SIG}\t{'b' * 64}\t2026-10-07 11:00:01+00",
    "\\.",
    "COPY jarvis.trust_statements (tenant_key, stmt_seq, kind, key_id, pubkey, arg, subject_hash, prev_hash, signed_by, signature, statement_hash, stored_at) FROM stdin;",
    f"operator\t1\tkey\tSHA256:{'K' * 43}\tssh-ed25519 AAAA sign\t1\t\\N\t{'0' * 64}\tSHA256:{'R' * 43}\t{SIG}\t{'d' * 64}\t2026-10-07 10:00:00+00",
    "\\.",
    "COPY jarvis.blocks (tenant_key, height, first_seq, last_seq, entry_count, prev_block_hash, entries_root, block_hash, format, sealed_at, sealed_by) FROM stdin;",
    f"operator\t1\t1\t3\t3\t{'0' * 64}\t{'e' * 64}\t{H}\t1\t2026-10-07 11:00:00+00\toperator",
    "\\.",
    "COPY jarvis.evidence_objects (tenant_key, id, schema_id, payload, pointer, size_bytes, created_at, created_by) FROM stdin;",
    f"operator\teo:sha256:{'1' * 64}\tCES.Local.ReplayReceipt.v1\t{{}}\t\\N\t10\t2026-10-07 11:30:00+00\toperator",
    f"operator\teo:sha256:{'2' * 64}\tCES.Local.FactEvidence.v1\t{{}}\t\\N\t10\t2026-10-07 11:31:00+00\toperator",
    "\\.",
    "",
])


def export():
    return sigexport.build(sigexport.parse(COPY.splitlines(keepends=True)))


def test_the_export_holds_the_logs_the_block_hashes_and_only_the_receipts_ids():
    e = export()
    assert e["format"] == 1 and list(e["tenants"]) == ["operator"]
    t = e["tenants"]["operator"]
    assert [a["signer_seq"] for a in t["attestations"]] == [1, 2]  # sorted by position, whatever order the dump had them in
    assert t["attestations"][0]["prev_hash"] == "0" * 64 and t["attestations"][1]["attestation_hash"] == "c" * 64
    assert [s["stmt_seq"] for s in t["statements"]] == [1] and t["statements"][0]["arg"] == 1 and t["statements"][0]["subject_hash"] is None
    assert t["blocks"] == [{"height": 1, "block_hash": H}]
    assert t["receipts"] == ["eo:sha256:" + "1" * 64]  # the fact evidence object is not a receipt


def test_a_signature_keeps_its_line_breaks():
    sig = export()["tenants"]["operator"]["attestations"][0]["signature"]
    assert sig.startswith("-----BEGIN SSH SIGNATURE-----\nU1NIU0lH\nAAAA\n") and sig.endswith("-----END SSH SIGNATURE-----")


def test_unescaping_follows_the_copy_text_format():
    assert sigexport.unescape("\\N") is None
    assert sigexport.unescape("a\\tb\\nc\\\\d") == "a\tb\nc\\d"
    assert sigexport.unescape("plain") == "plain" and sigexport.unescape("") == ""
    assert sigexport.unescape("trailing\\") == "trailing\\"


def test_other_tables_and_text_are_ignored_and_an_older_database_gives_an_empty_export():
    older = "COPY jarvis.memories (tenant_key, id) FROM stdin;\noperator\tmem-1\n\\.\n"
    assert sigexport.build(sigexport.parse(older.splitlines(keepends=True))) == {"format": 1, "tenants": {}}
    assert sigexport.build(sigexport.parse([])) == {"format": 1, "tenants": {}}


def test_several_tenants_are_kept_apart():
    other = COPY.replace("operator\t", "bob\t")
    both = sigexport.build(sigexport.parse((COPY + "\n" + other).splitlines(keepends=True)))
    assert list(both["tenants"]) == ["bob", "operator"] and both["tenants"]["bob"]["attestations"][0]["signer_seq"] == 1


def test_the_command_line_reads_stdin_and_writes_deterministic_json():
    r = subprocess.run([sys.executable, str(SCRIPT)], input=COPY, capture_output=True, text=True)
    assert r.returncode == 0 and json.loads(r.stdout) == export()
    assert subprocess.run([sys.executable, str(SCRIPT)], input=COPY, capture_output=True, text=True).stdout == r.stdout


def test_the_export_feeds_the_real_verifier_end_to_end(tmp_path):
    """Statements and attestations exactly as the database stores them, exported, then checked with only the root's public key."""
    from app import attest
    from tests.attest_support import make_key, requires_ssh_keygen, roots_of, trust_chain, chain, sha  # noqa: F401

    if not __import__("shutil").which("ssh-keygen"):
        pytest.skip("ssh-keygen not installed")
    root, mint = make_key(tmp_path, "root"), make_key(tmp_path, "mint")
    stmts = trust_chain(root, ("key", {"key": mint, "arg": 1}))
    rows = chain(mint, [("block", "block:1", sha("b1"))])
    lines = ["COPY jarvis.trust_statements (tenant_key, stmt_seq, kind, key_id, pubkey, arg, subject_hash, prev_hash, signed_by, signature, statement_hash, stored_at) FROM stdin;"]
    esc = lambda v: "\\N" if v is None else str(v).replace("\\", "\\\\").replace("\n", "\\n")  # noqa: E731
    for s in stmts:
        lines.append("\t".join(["alice", *(esc(x) for x in (s.stmt_seq, s.kind, s.key_id, s.pubkey, s.arg, s.subject_hash, s.prev_hash, s.signed_by, s.signature, s.statement_hash)), "2026-10-07"]))
    lines += ["\\.", "COPY jarvis.attestations (tenant_key, signer_seq, kind, subject, subject_hash, prev_hash, key_id, signed_at, signature, attestation_hash, stored_at) FROM stdin;"]
    for a in rows:
        lines.append("\t".join(["alice", *(esc(x) for x in (a.signer_seq, a.kind, a.subject, a.subject_hash, a.prev_hash, a.key_id, a.signed_at, a.signature, a.attestation_hash)), "2026-10-07"]))
    lines += ["\\."]
    t = sigexport.build(sigexport.parse([l + "\n" for l in lines]))["tenants"]["alice"]
    statements = [attest.Statement(**s) for s in t["statements"]]
    attestations = [attest.Attestation(**a) for a in t["attestations"]]
    trust = attest.evaluate_trust("alice", roots_of(root), statements)
    problems, summary = attest.evaluate_attestations("alice", trust, attestations, attest.DictTruth({1: sha("b1")}))
    assert trust.problems == [] and problems == [] and summary.blocks == {1}
