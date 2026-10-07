"""The attestation and trust logs are anchored outside the database like the blocks are: a log re-signed with a taken key passes every
signature check but no longer extends what an earlier backup recorded."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys

import psycopg
import pytest

from app import attest
from tests.attest_support import requires_ssh_keygen, sha, sign
from tests.test_block_anchors import ANCHORS, SHIM, _mutant, bash, check, write  # noqa: F401
from tests.test_signer import (  # noqa: F401  (fixtures and helpers)
    HDR, OP, api, authorize, client, key_info, keys, ok, pg, roots, run, seal,
)

H = "a" * 64
BASE = ["counter|t|5", f"head|t|mem-a|5|{'a' * 64}|f", f"block|t|1|3|{'1' * 64}",
        f"att|t|1|{'2' * 64}", f"att|t|2|{'3' * 64}", f"trust|t|1|{'4' * 64}"]


# --- on plain files ------------------------------------------------------------------------------------------------------------------------

def test_new_attestations_and_statements_make_a_legitimate_successor(tmp_path):
    new = BASE + [f"att|t|3|{'5' * 64}", f"trust|t|2|{'6' * 64}"]
    r = check(write(tmp_path, "a", BASE), write(tmp_path, "b", new))
    assert r.returncode == 0 and r.stdout == ""


def test_a_vanished_attestation_is_refused(tmp_path):
    r = check(write(tmp_path, "a", BASE), write(tmp_path, "b", [l for l in BASE if not l.startswith("att|t|2|")]))
    assert r.returncode == 1 and "attestation t|2 vanished (the signing log lost an entry)" in r.stdout


def test_a_changed_attestation_is_refused(tmp_path):
    new = [l if not l.startswith("att|t|1|") else f"att|t|1|{'9' * 64}" for l in BASE]
    r = check(write(tmp_path, "a", BASE), write(tmp_path, "b", new))
    assert r.returncode == 1 and "attestation t|1 changed its hash (the signing log was rewritten)" in r.stdout


def test_a_vanished_or_changed_trust_statement_is_refused(tmp_path):
    gone = check(write(tmp_path, "a", BASE), write(tmp_path, "b", [l for l in BASE if not l.startswith("trust|")]))
    assert gone.returncode == 1 and "trust statement t|1 vanished (the trust log lost an entry)" in gone.stdout
    changed = check(write(tmp_path, "a", BASE), write(tmp_path, "c", [l if not l.startswith("trust|") else f"trust|t|1|{'8' * 64}" for l in BASE]))
    assert changed.returncode == 1 and "trust statement t|1 changed its hash (the trust log was rewritten)" in changed.stdout


def test_a_first_set_with_signatures_after_one_without_is_fine_and_tenants_stay_apart(tmp_path):
    assert check(write(tmp_path, "a", BASE[:3]), write(tmp_path, "b", BASE)).returncode == 0
    other = BASE + [f"att|u|1|{'7' * 64}"]
    r = check(write(tmp_path, "c", other), write(tmp_path, "d", BASE))
    assert r.returncode == 1 and "attestation u|1 vanished" in r.stdout and "t|" not in r.stdout.replace("attestation u|1", "")


def test_the_older_rules_still_hold_next_to_the_new_ones(tmp_path):
    r = check(write(tmp_path, "a", BASE), write(tmp_path, "b", ["counter|t|3"] + BASE[2:]))
    assert r.returncode == 1 and "counter went BACKWARDS" in r.stdout and "vanished from the chain heads" in r.stdout


# --- against a real database ----------------------------------------------------------------------------------------------------------------

pytestmark_db = pytest.mark.postgres


def collect(pg, tmp_path, name):
    runner = f'anchors_test_psql() {{ "{sys.executable}" "{SHIM}" "$@"; }}; anchors_collect anchors_test_psql'
    r = bash(runner, env={"SHIM_DSN": pg.admin_dsn, "SHIM_SCHEMA": pg.schema})
    assert r.returncode == 0, r.stderr
    p = tmp_path / name
    p.write_text(r.stdout)
    return p


def signed_ledger(client, keys, api):
    seal(client, 6)
    authorize(client, keys)
    run(api, keys)
    assert ok(client)["ok"] is True


@requires_ssh_keygen
@pytest.mark.postgres
def test_every_attestation_and_statement_is_written_to_the_anchors(pg, client, roots, keys, api, tmp_path):
    signed_ledger(client, keys, api)
    lines = collect(pg, tmp_path, "a").read_text().splitlines()
    att = client.get("/api/jarvis/attestations", headers=HDR).json()["attestations"]
    trust = client.get("/api/jarvis/trust/statements", headers=HDR).json()["statements"]
    assert sorted(l for l in lines if l.startswith("att|")) == sorted(f"att|{OP}|{a['signer_seq']}|{a['attestation_hash']}" for a in att) and len(att) == 3
    assert sorted(l for l in lines if l.startswith("trust|")) == sorted(f"trust|{OP}|{s['stmt_seq']}|{s['statement_hash']}" for s in trust) and len(trust) == 1


@requires_ssh_keygen
@pytest.mark.postgres
def test_a_database_older_than_v7_contributes_no_signature_lines(pg_schema, tmp_path):
    from app.pg_schema import migrate

    migrate(pg_schema.admin_dsn, schema=pg_schema.schema, app_role="jarvis_app_test", up_to=6)
    lines = collect(pg_schema, tmp_path, "a").read_text().splitlines()
    assert not any(l.startswith(("att|", "trust|")) for l in lines)


@requires_ssh_keygen
@pytest.mark.postgres
def test_signing_more_is_a_legitimate_successor(pg, client, roots, keys, api, tmp_path):
    signed_ledger(client, keys, api)
    before = collect(pg, tmp_path, "before")
    seal(client, 3)
    run(api, keys)
    assert check(before, collect(pg, tmp_path, "after")).returncode == 0


def resign_everything_with_the_taken_key(pg, keys, new_block_hash):
    """Rewrite block 1's hash and re-sign the whole signing log with the (taken) signing key, every link and hash consistent."""
    with pg.admin_conn() as conn:
        conn.execute("ALTER TABLE blocks DISABLE TRIGGER blocks_no_update")
        conn.execute("UPDATE blocks SET block_hash = %s WHERE height = 1", (new_block_hash,))
        conn.execute("ALTER TABLE blocks ENABLE TRIGGER blocks_no_update")
        for t in ("no_update", "no_delete"):
            conn.execute(f"ALTER TABLE attestations DISABLE TRIGGER attestations_{t}")
        rows = conn.execute("SELECT signer_seq, kind, subject, subject_hash, signed_at, key_id FROM attestations ORDER BY signer_seq").fetchall()
        prev = attest.GENESIS
        for seq, kind, subject, shash, signed_at, key_id in rows:
            if subject == "block:1":
                shash = new_block_hash
            elif kind == "checkpoint":
                c = attest.parse_checkpoint_subject(subject)
                subject = attest.checkpoint_subject(c[0], prev, c[2], c[3])
                shash = attest.checkpoint_hash(OP, c[0], prev, c[2], c[3])
            message = attest.attestation_message(kind, OP, subject, shash, seq, prev, signed_at)
            sig = attest.normalize_signature(sign(keys["mint"], message))
            h = attest.attestation_hash(message, key_id, sig)
            conn.execute("UPDATE attestations SET subject = %s, subject_hash = %s, prev_hash = %s, signature = %s, attestation_hash = %s WHERE signer_seq = %s",
                         (subject, shash, prev, sig, h, seq))
            prev = h
        for t in ("no_update", "no_delete"):
            conn.execute(f"ALTER TABLE attestations ENABLE TRIGGER attestations_{t}")


@requires_ssh_keygen
@pytest.mark.postgres
def test_a_signing_log_rewritten_and_resigned_with_the_taken_key_passes_the_signature_checks_but_not_the_anchors(pg, client, roots, keys, api, tmp_path):
    signed_ledger(client, keys, api)
    before = collect(pg, tmp_path, "before")
    resign_everything_with_the_taken_key(pg, keys, sha("a rewritten block one"))
    v = ok(client)
    assert v["ok"] is True and v["problems"] == []  # every signature, link and subject is valid: the signature layer alone is fooled
    r = check(before, collect(pg, tmp_path, "after"))
    assert r.returncode == 1
    assert "block alice|1" not in r.stdout and f"block {OP}|1 changed its hash" in r.stdout
    assert f"attestation {OP}|1 changed its hash (the signing log was rewritten)" in r.stdout and f"attestation {OP}|3 changed its hash" in r.stdout


@requires_ssh_keygen
@pytest.mark.postgres
def test_removing_the_newest_attestation_is_invisible_to_the_database_and_caught_by_the_anchors(pg, client, roots, keys, api, tmp_path):
    signed_ledger(client, keys, api)
    before = collect(pg, tmp_path, "before")
    with pg.admin_conn() as conn:
        conn.execute("ALTER TABLE attestations DISABLE TRIGGER attestations_no_delete")
        conn.execute("DELETE FROM attestations WHERE signer_seq = 3")
        conn.execute("ALTER TABLE attestations ENABLE TRIGGER attestations_no_delete")
    assert ok(client)["ok"] is True
    r = check(before, collect(pg, tmp_path, "after"))
    assert r.returncode == 1 and f"attestation {OP}|3 vanished (the signing log lost an entry)" in r.stdout


@requires_ssh_keygen
@pytest.mark.postgres
def test_without_the_attestation_rules_both_tampers_would_pass(pg, client, roots, keys, api, tmp_path):
    no_att = _mutant(tmp_path, "no-att-rule.sh", "      for (k in pa) {", "      for (k in pa) { continue }\n      for (k in pa_unused) {")
    no_trust = _mutant(tmp_path, "no-trust-rule.sh", "      for (k in pt) {", "      for (k in pt) { continue }\n      for (k in pt_unused) {")
    signed_ledger(client, keys, api)
    before = collect(pg, tmp_path, "before")
    resign_everything_with_the_taken_key(pg, keys, sha("a rewritten block one"))
    after = collect(pg, tmp_path, "after")
    assert check(before, after).returncode == 1 and check(before, after, anchors=no_att).returncode == 1  # the block rule still sees block 1 changed
    only_att = [l for l in after.read_text().splitlines() if not l.startswith("block|")]
    prev_only = [l for l in before.read_text().splitlines() if not l.startswith("block|")]
    a, b = write(tmp_path, "prev_no_blocks", prev_only), write(tmp_path, "after_no_blocks", only_att)
    assert check(a, b).returncode == 1 and check(a, b, anchors=no_att).returncode == 0  # with the block lines out of the picture, only the att rule catches it
    t_before = write(tmp_path, "tb", [f"trust|t|1|{'4' * 64}"])
    t_after = write(tmp_path, "ta", [f"trust|t|1|{'5' * 64}"])
    assert check(t_before, t_after).returncode == 1 and check(t_before, t_after, anchors=no_trust).returncode == 0


def test_the_real_dump_reader_maps_the_signing_log_columns_correctly(tmp_path):
    """anchors_from_dump is what backup.sh runs on a dump's data; here the dump's data text is fed to the REAL function (pg_exec just cats it)."""
    from tests.test_sigexport import COPY

    f = tmp_path / "data.txt"
    f.write_text(COPY.replace("operator\t", "alice\t"))
    r = bash(f'pg_exec() {{ cat; }}; anchors_from_dump "{f}"')
    assert r.returncode == 0, r.stderr
    lines = r.stdout.splitlines()
    assert f"att|alice|1|{'b' * 64}" in lines and f"att|alice|2|{'c' * 64}" in lines and f"trust|alice|1|{'d' * 64}" in lines
    assert f"block|alice|1|3|{'a' * 64}" in lines
    assert lines == sorted(lines)  # sorted like the live reader's output, so the two can be compared with diff


def test_the_dump_reader_ignores_the_signing_tables_when_the_dump_has_none(tmp_path):
    f = tmp_path / "data.txt"
    f.write_text("COPY jarvis.memories (tenant_key, id) FROM stdin;\nalice\tmem-1\n\\.\n")
    assert bash(f'pg_exec() {{ cat; }}; anchors_from_dump "{f}"').stdout == ""
