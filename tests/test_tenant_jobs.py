"""The per-tenant jobs (app/tenant_jobs.py): sealing each tenant's own chain from a command run on the box, never from the public API.

Covers the four rules the work was held to: nothing about the existing timers changes; one tenant's failure cannot stop the others and always shows up; with only
the operator tenant the output is what seal.sh logs today; and each of several tenants gets its own sealed block (and its own attestation head).
"""

from __future__ import annotations

import io
import json
import re
from pathlib import Path

import pytest

from app import pg_store, tenant_jobs
from app.identity import acting_for, current_tenant_key
from app.models import MemoryCreate
from app.pg_schema import EXPECTED_SCHEMA_VERSION, migrate
from app.pg_store import PostgresRowStore
from app.store import reset_store_for_tests

ROOT = Path(__file__).resolve().parents[1]
SEAL_SH = (ROOT / "deploy" / "mint" / "bin" / "seal.sh").read_text(encoding="utf-8")


@pytest.fixture
def pg(pg_schema, monkeypatch):
    assert migrate(pg_schema.admin_dsn, schema=pg_schema.schema, app_role="jarvis_app_test") == EXPECTED_SCHEMA_VERSION
    monkeypatch.setenv("JARVIS_DATABASE_URL", pg_schema.app_dsn)
    monkeypatch.setenv("JARVIS_DATABASE_SCHEMA", pg_schema.schema)
    monkeypatch.setenv("JARVIS_PG_STORE", "rows")
    reset_store_for_tests()
    yield pg_schema
    pg_store.close_pools()


def write(pg, tenant: str, n: int) -> None:
    store = PostgresRowStore(pg.app_dsn, tenant, schema=pg.schema)
    for i in range(n):
        store.create_memory(MemoryCreate(content=f"{tenant} record {i} for the per-tenant jobs", source_agent="t", session_id="s", type="fact"))


def blocks_of(pg, tenant: str) -> list[tuple]:
    store = PostgresRowStore(pg.app_dsn, tenant, schema=pg.schema)
    return [(b["height"], b["first_seq"], b["last_seq"], b["entry_count"], b["block_hash"]) for b in store.list_blocks()]


pytestmark = pytest.mark.postgres


# ------------------------------------------------------------------------------------------------ each tenant gets its own block
def test_each_tenant_gets_its_own_sealed_block_and_nothing_crosses(pg):
    for tenant, n in (("operator", 3), ("t_alice", 4), ("t_bob", 2)):
        write(pg, tenant, n)
    report = tenant_jobs.seal(["operator", "t_alice", "t_bob"], force=True)
    assert report["failed"] == [] and [r["tenant"] for r in report["results"]] == ["operator", "t_alice", "t_bob"]
    sealed = {r["tenant"]: r["sealed"] for r in report["results"]}
    assert [len(s) for s in sealed.values()] == [1, 1, 1]
    rows = {t: blocks_of(pg, t) for t in ("operator", "t_alice", "t_bob")}
    assert [(r[0][0], r[0][3]) for r in rows.values()] == [(1, 3), (1, 4), (1, 2)]      # height 1 in every chain, each covering that tenant's own entries
    assert len({r[0][4] for r in rows.values()}) == 3                                    # three different block hashes: the tenant is part of the preimage
    # one tenant's seal never touches another's chain
    write(pg, "t_alice", 2)
    again = tenant_jobs.seal(["t_alice"], force=True)
    assert again["failed"] == [] and blocks_of(pg, "operator") == rows["operator"] and blocks_of(pg, "t_bob") == rows["t_bob"]
    assert [b[0] for b in blocks_of(pg, "t_alice")] == [1, 2]


def test_an_unrelated_tenants_seal_leaves_this_ones_tail_unsealed(pg):
    write(pg, "operator", 5)
    write(pg, "t_bob", 5)
    tenant_jobs.seal(["t_bob"], force=True)
    assert blocks_of(pg, "operator") == []                                               # sealing t_bob did not seal the operator's history
    assert tenant_jobs.seal(["operator"], force=False)["results"][0]["sealed"] == []     # and the operator's own thresholds still apply (5 entries, not yet due)


def test_the_blocks_each_tenant_got_verify(pg, monkeypatch, capsys):
    from app import pg_verify

    for tenant in ("operator", "t_alice"):
        write(pg, tenant, 3)
    tenant_jobs.seal(["operator", "t_alice"], force=True)
    monkeypatch.setenv("JARVIS_DATABASE_MIGRATE_URL", pg.admin_dsn)
    for tenant in ("operator", "t_alice"):
        assert pg_verify.main(["--tenant", tenant]) == 0
        assert "PROBLEM" not in capsys.readouterr().out


# ------------------------------------------------------------------------------------- one tenant cannot break the others
def test_a_failing_tenant_is_reported_and_the_others_still_run(pg, monkeypatch, capsys):
    for tenant in ("operator", "t_alice", "t_carol"):
        write(pg, tenant, 3)
    real = tenant_jobs.seal_one

    def flaky(tenant, *, force=False):
        if tenant == "t_boom":
            raise RuntimeError("the database refused this tenant")
        return real(tenant, force=force)

    monkeypatch.setattr(tenant_jobs, "seal_one", flaky)
    report = tenant_jobs.seal(["operator", "t_alice", "t_boom", "t_carol"], force=True)
    assert report["failed"] == ["t_boom"]
    by = {r["tenant"]: r for r in report["results"]}
    assert not by["t_boom"]["ok"] and "RuntimeError" in by["t_boom"]["error"] and all(by[t]["ok"] and len(by[t]["sealed"]) == 1 for t in ("operator", "t_alice", "t_carol"))
    assert [b[0] for b in blocks_of(pg, "t_carol")] == [1]                               # the tenant AFTER the failure was still sealed
    lines = tenant_jobs.seal_lines(report)
    assert any("t_boom" in ln and ln.startswith("ERROR") and "RuntimeError" in ln for ln in lines)
    assert lines[-1] == "ERROR tenants that FAILED: t_boom (3 of 4 ok)"
    monkeypatch.setattr(tenant_jobs, "seal", lambda tenants, force=False: report)
    assert tenant_jobs.main(["seal", "--tenants", "operator,t_alice,t_boom,t_carol"]) == 1       # and the job's exit code says so
    assert "t_boom" in capsys.readouterr().out


def test_every_failure_is_reported_never_skipped():
    def op(tenant):
        raise ValueError(f"no good: {tenant}")

    report = tenant_jobs.run_for_tenants(op, ["a", "b", "c"])
    assert report["failed"] == ["a", "b", "c"] and all(not r["ok"] and r["error"].startswith("ValueError") for r in report["results"])


def test_a_tenant_that_raises_something_unusual_still_does_not_stop_the_loop():
    def op(tenant):
        if tenant == "b":
            raise KeyboardInterrupt if False else SystemError("worse than usual")
        return {"x": tenant}

    report = tenant_jobs.run_for_tenants(op, ["a", "b", "c"])
    assert report["failed"] == ["b"] and [r["ok"] for r in report["results"]] == [True, False, True]


# --------------------------------------------------------------------------- with only the operator the output is seal.sh's
def test_the_operator_alone_gets_exactly_the_lines_seal_sh_logs(pg):
    write(pg, "operator", 3)
    sealed = tenant_jobs.seal(["operator"], force=True)
    (line,) = tenant_jobs.seal_lines(sealed)
    assert re.fullmatch(r"sealed 1 block\(s\) \(height 1\); newest block: 1, sealed through seq 3, 0 entries unsealed \(history at seq 3\)", line), line
    nothing = tenant_jobs.seal(["operator"], force=False)
    (line2,) = tenant_jobs.seal_lines(nothing)
    assert line2 == "nothing sealed: nothing new to seal; newest block: 1, sealed through seq 3, 0 entries unsealed (history at seq 3)"


def test_the_summary_format_is_the_one_in_seal_sh():
    """Drift guard: if seal.sh's wording changes, the per-tenant output must change with it (the operator's lines must stay identical)."""
    assert 'print("newest block: %s, sealed through seq %s, %s entries unsealed (history at seq %s)"' in SEAL_SH
    assert 'log INFO "sealed $n block(s) (height $heights); $(summary < "$out")"' in SEAL_SH
    assert 'log INFO "nothing sealed: $(python3 -c \'import json,sys; print(json.load(sys.stdin)["reason"])\' < "$out"); $(summary < "$out")"' in SEAL_SH
    head = {"tip": None, "sealed_seq": 0, "unsealed_entries": 7, "history_seq": 7}
    assert tenant_jobs._summary(head) == "newest block: none, sealed through seq 0, 7 entries unsealed (history at seq 7)"


def test_another_tenants_lines_carry_its_key_and_a_multi_tenant_run_ends_with_a_summary(pg):
    write(pg, "operator", 3)
    write(pg, "t_alice", 3)
    lines = tenant_jobs.seal_lines(tenant_jobs.seal(["operator", "t_alice"], force=True))
    assert lines[0].startswith("sealed 1 block(s)") and lines[1].startswith("tenant t_alice: sealed 1 block(s)") and lines[2] == "all 2 tenants ok"


def test_the_tenant_list_puts_the_operator_first_and_drops_blanks_and_duplicates():
    assert tenant_jobs.parse_tenants("t_b, ,t_a,operator,t_b") == ["operator", "t_b", "t_a"]
    assert tenant_jobs.parse_tenants("") == []


# ---------------------------------------------------------- the public API still cannot pick a tenant; only this process can
def test_acting_for_is_the_only_way_to_bind_a_tenant_and_it_does_not_leak_out(pg):
    assert current_tenant_key() is None
    with acting_for("t_alice"):
        assert current_tenant_key() == "t_alice"
        with acting_for("operator"):
            assert current_tenant_key() is None                                         # the operator's own namespace has no key
        assert current_tenant_key() == "t_alice"
    assert current_tenant_key() is None


def test_a_request_run_as_a_tenant_sees_that_tenants_chain_only(pg):
    write(pg, "operator", 3)
    write(pg, "t_alice", 4)
    tenant_jobs.seal(["operator", "t_alice"], force=True)
    seen = {}
    for tenant in ("operator", "t_alice"):
        status, body = tenant_jobs.request_as(tenant, "GET", "/api/jarvis/attestations/head")
        assert status == 200, body
        seen[tenant] = body
        status, pending = tenant_jobs.request_as(tenant, "GET", "/api/jarvis/attestations/pending")
        assert status == 200 and pending["head"]["tenant"] == tenant, pending           # the signer reads the tenant from here: it is the one it was bound to
    assert seen["operator"]["tip_height"] == 1 and seen["t_alice"]["tip_height"] == 1
    assert seen["operator"]["tip_block_hash"] != seen["t_alice"]["tip_block_hash"]
    status, body = tenant_jobs.request_as("t_alice", "GET", "/api/jarvis/blocks/1")
    assert status == 200 and body["block"]["entry_count"] == 4 and body["block"]["first_seq"] == 1
    status, body = tenant_jobs.request_as("t_bob", "GET", "/api/jarvis/blocks/1")
    assert status == 404                                                                  # a tenant with no blocks has none: nothing leaks from the others


def test_the_request_path_carries_a_query_and_a_body(pg):
    write(pg, "t_alice", 3)
    tenant_jobs.seal(["t_alice"], force=True)
    status, body = tenant_jobs.request_as("t_alice", "GET", "/api/jarvis/blocks?after_height=0&limit=5")
    assert status == 200 and body["count"] == 1
    status, body = tenant_jobs.request_as("t_alice", "POST", "/api/jarvis/blocks/seal", {"force": True})
    assert status == 200 and body["sealed"] == [] and "nothing" in body["reason"]


def test_serve_answers_every_line_and_survives_a_bad_one(pg):
    write(pg, "t_alice", 2)
    out = io.StringIO()
    lines = "\n".join([json.dumps({"method": "GET", "path": "/api/jarvis/attestations/head"}), "not json at all", json.dumps({"method": "GET", "path": "/api/jarvis/nope"}),
                       json.dumps({"method": "GET", "path": "/api/jarvis/blocks/head"})]) + "\n"
    assert tenant_jobs.serve("t_alice", stdin=io.StringIO(lines), stdout=out) == 0
    answers = [json.loads(x) for x in out.getvalue().splitlines()]
    assert [a["status"] for a in answers] == [200, 500, 404, 200] and answers[0]["body"]["tenant"] == "t_alice"


# ================================================================================================================ signing, per tenant
import os
import subprocess
import sys
import time

from app import attest, signer
from tests.attest_support import make_key, requires_ssh_keygen, statement


@pytest.fixture(scope="module")
def sign_keys(tmp_path_factory):
    d = tmp_path_factory.mktemp("tenantsignkeys")
    return {"root": make_key(d, "root"), "mint": make_key(d, "mint")}


@pytest.fixture
def roots(sign_keys, tmp_path, monkeypatch):
    f = tmp_path / "roots.pub"
    f.write_text(sign_keys["root"].pub_text + "\n")
    monkeypatch.setenv(attest.ROOTS_ENV, str(f))
    return f


def authorize_for(tenant: str, keys) -> None:
    """The key ceremony for ONE tenant: a root-signed statement in that tenant's own trust log (the statement names the tenant)."""
    status, head = tenant_jobs.request_as(tenant, "GET", "/api/jarvis/attestations/head")
    assert status == 200, head
    s = statement(keys["root"], "key", head["next_stmt_seq"], head["trust_head_hash"], key=keys["mint"], arg=1, tenant=tenant)
    status, body = tenant_jobs.request_as(tenant, "POST", "/api/jarvis/trust/statements", {
        "kind": "key", "key_id": s.key_id, "pubkey": s.pubkey, "arg": s.arg, "stmt_seq": s.stmt_seq, "prev_hash": s.prev_hash, "signed_by": s.signed_by, "signature": s.signature})
    assert status == 200, body


def open_api_for(tenant: str):
    """The signer's API for one tenant, reached the way the box reaches it (the in-process runner), without a container."""
    return signer.Api("tenant:" + tenant, "", transport=lambda m, p, b=None: tenant_jobs.request_as(tenant, m, p, b)), (lambda: None)


def key_info(keys):
    return signer.KeyInfo(path=keys["mint"].path, public=keys["mint"].public)


def no_replay(*_a):
    return None   # (the offline replay check is a separate container; it is exercised on the throwaway stack in the drill)


@requires_ssh_keygen
def test_each_tenant_gets_its_own_attestations_and_every_chain_verifies(pg, sign_keys, roots):
    for tenant, n in (("operator", 6), ("t_alice", 4)):
        write(pg, tenant, n)
    tenant_jobs.seal(["operator", "t_alice"], force=True)
    for tenant in ("operator", "t_alice"):
        authorize_for(tenant, sign_keys)
    outcomes = signer.sign_tenants(["operator", "t_alice"], key_info(sign_keys), dry_run=False, open_api=open_api_for, verify_block=no_replay, verify_receipt=no_replay)
    assert [o["ok"] for o in outcomes] == [True, True]
    assert {o["tenant"]: [b["height"] for b in o["result"]["signed_blocks"]] for o in outcomes} == {"operator": [1], "t_alice": [1]}
    heads = {}
    for tenant in ("operator", "t_alice"):
        status, check = tenant_jobs.request_as(tenant, "GET", "/api/jarvis/attestations/verify")
        assert status == 200 and check["ok"] is True and check["problems"] == [], check
        status, heads[tenant] = tenant_jobs.request_as(tenant, "GET", "/api/jarvis/attestations/head")
        assert heads[tenant]["tenant"] == tenant and heads[tenant]["head_seq"] == 2           # one block attestation and one checkpoint, in THIS tenant's log
    assert heads["operator"]["head_hash"] != heads["t_alice"]["head_hash"]                     # two separate signing logs


@requires_ssh_keygen
def test_an_unauthorized_tenant_fails_loudly_and_the_others_are_still_signed(pg, sign_keys, roots, capsys):
    for tenant in ("operator", "t_noroot", "t_alice"):
        write(pg, tenant, 3)
    tenant_jobs.seal(["operator", "t_noroot", "t_alice"], force=True)
    for tenant in ("operator", "t_alice"):                                                      # t_noroot never had the key ceremony
        authorize_for(tenant, sign_keys)
    outcomes = signer.sign_tenants(["operator", "t_noroot", "t_alice"], key_info(sign_keys), dry_run=False, open_api=open_api_for, verify_block=no_replay, verify_receipt=no_replay)
    assert [(o["tenant"], o["ok"]) for o in outcomes] == [("operator", True), ("t_noroot", False), ("t_alice", True)]     # the tenant AFTER the failure was still signed
    bad = outcomes[1]
    assert bad["code"] == "key_not_authorized" and bad["exit_code"] == signer.EXIT_UNHEALTHY
    code = signer.report_tenants(outcomes)
    err = capsys.readouterr().err
    assert code == signer.EXIT_UNHEALTHY and "tenant t_noroot: key_not_authorized" in err and "FAILED tenants: t_noroot (2 of 3 ok)" in err


@requires_ssh_keygen
def test_a_tenant_whose_api_blows_up_is_recorded_and_the_loop_goes_on(pg, sign_keys, roots):
    write(pg, "operator", 3)
    tenant_jobs.seal(["operator"], force=True)
    authorize_for("operator", sign_keys)

    def opener(tenant):
        if tenant == "t_broken":
            raise RuntimeError("cannot start its API")
        return open_api_for(tenant)

    outcomes = signer.sign_tenants(["t_broken", "operator"], key_info(sign_keys), dry_run=False, open_api=opener, verify_block=no_replay, verify_receipt=no_replay)
    assert [(o["tenant"], o["ok"]) for o in outcomes] == [("t_broken", False), ("operator", True)] and "RuntimeError" in outcomes[0]["message"]


@requires_ssh_keygen
def test_the_operator_alone_prints_exactly_what_sign_prints_today(pg, sign_keys, roots, capsys):
    write(pg, "operator", 6)
    tenant_jobs.seal(["operator"], force=True)
    authorize_for("operator", sign_keys)
    (outcome,) = signer.sign_tenants(["operator"], key_info(sign_keys), dry_run=True, open_api=open_api_for, verify_block=no_replay, verify_receipt=no_replay)
    assert signer.report_tenants([outcome]) == signer.EXIT_OK
    per_tenant = capsys.readouterr().out
    assert signer.report_sign(outcome["result"]) == signer.EXIT_OK                              # the single-tenant `sign` path
    assert capsys.readouterr().out == per_tenant == "dry run: would sign blocks [1], 0 receipt(s) and a checkpoint; 0 receipt(s) would be refused, 0 deferred\n"


def test_another_tenants_report_carries_its_key_and_a_multi_tenant_run_ends_with_a_summary(capsys):
    result = {"dry_run": False, "signed_blocks": [{"height": 1, "signer_seq": 1}], "signed_receipts": [], "refused_receipts": [], "deferred_receipts": 0,
              "checkpoint": {"signer_seq": 2, "covers": 1}, "planned_blocks": [1], "planned_receipts": []}
    assert signer.report_tenants([{"tenant": "operator", "ok": True, "result": result}, {"tenant": "t_alice", "ok": True, "result": result}]) == signer.EXIT_OK
    out = capsys.readouterr().out.splitlines()
    assert out == ["signed blocks 1, 0 receipt(s); checkpoint 2 covers 1", "tenant t_alice: signed blocks 1, 0 receipt(s); checkpoint 2 covers 1", "all 2 tenants ok"]


def _refused_result():
    return {"dry_run": False, "signed_blocks": [], "signed_receipts": [], "refused_receipts": [{"id": "r1", "why": "not rederivable"}], "deferred_receipts": 0,
            "checkpoint": None, "planned_blocks": [], "planned_receipts": [], "note": "x"}


def test_a_tenant_that_signed_with_refused_receipts_is_a_failed_tenant_not_an_ok_one(capsys):
    ok = {"dry_run": False, "signed_blocks": [{"height": 1, "signer_seq": 1}], "signed_receipts": [], "refused_receipts": [], "deferred_receipts": 0,
          "checkpoint": {"signer_seq": 2, "covers": 1}, "planned_blocks": [1], "planned_receipts": []}
    code = signer.report_tenants([{"tenant": "operator", "ok": True, "result": ok}, {"tenant": "t_alice", "ok": True, "result": _refused_result()}])
    cap = capsys.readouterr()
    assert code == signer.EXIT_UNHEALTHY
    assert "FAILED tenants: t_alice (1 of 2 ok)" in cap.err and "all 2 tenants ok" not in cap.out         # tenants.sh names the tenant in its critical notification from this line
    assert "REFUSED tenant t_alice: receipt r1" in cap.err


def test_the_operator_alone_with_refused_receipts_prints_exactly_what_it_prints_today(capsys):
    code = signer.report_tenants([{"tenant": "operator", "ok": True, "result": _refused_result()}])
    cap = capsys.readouterr()
    assert code == signer.EXIT_UNHEALTHY and cap.err == "REFUSED receipt r1: not rederivable\n"


@pytest.mark.parametrize("value", ["", " ", ",", " , "])
def test_an_empty_tenant_selection_is_refused_never_the_operator_by_default(monkeypatch, capsys, value):
    monkeypatch.setattr(signer, "preflight_key", lambda path: pytest.fail("must be refused before any key or API is touched"))
    assert signer.main(["sign", "--tenants", value]) == signer.EXIT_USAGE
    assert "no_tenants" in capsys.readouterr().err


def test_the_replay_check_is_told_the_tenant_but_the_operators_command_line_is_unchanged():
    assert signer._replay_args("operator") == [] and signer._replay_args("t_alice") == ["--tenant", "t_alice"]
    seen = []
    real = subprocess.run
    subprocess.run = lambda cmd, **kw: seen.append(cmd) or type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()
    try:
        signer.default_verify_block(3, "ab" * 32)
        signer.default_verify_block(3, "ab" * 32, "t_alice")
        signer.default_verify_receipt("eo:sha256:" + "c" * 64, "t_alice")
    finally:
        subprocess.run = real
    assert "--tenant" not in seen[0] and seen[1][seen[1].index("--tenant") + 1] == "t_alice" and seen[2][seen[2].index("--tenant") + 1] == "t_alice"


def test_the_container_transport_speaks_json_lines_to_the_per_tenant_server(pg, monkeypatch):
    """The real line protocol through a real subprocess running `python -m app.tenant_jobs serve` (what tenant-api.sh runs in the migrate container)."""
    monkeypatch.chdir(ROOT)
    write(pg, "t_alice", 2)
    tenant_jobs.seal(["t_alice"], force=True)
    transport = signer.ContainerTransport("t_alice", command=[sys.executable, "-m", "app.tenant_jobs", "serve", "--tenant", "t_alice"])
    try:
        status, head = transport("GET", "/api/jarvis/attestations/head")
        assert status == 200 and head["tenant"] == "t_alice" and head["tip_height"] == 1
        status, body = transport("GET", "/api/jarvis/blocks/9")
        assert status == 404                                                                   # an answer for every line, errors included
    finally:
        transport.close()


def test_a_container_transport_that_dies_is_reported_not_hung(monkeypatch):
    transport = signer.ContainerTransport("t_x", command=[sys.executable, "-c", "import sys; sys.exit(3)"])
    with pytest.raises(signer.SignerError) as exc:
        transport("GET", "/api/jarvis/trust")
    assert exc.value.code == "api_unreachable"
    transport.close()


def test_a_container_transport_that_stops_answering_times_out_and_is_killed():
    """A container that is alive but stuck must fail THIS tenant (api_timeout) instead of holding it and every later tenant forever."""
    stuck = [sys.executable, "-c", "import sys, time; sys.stdin.readline(); time.sleep(120)"]
    transport = signer.ContainerTransport("t_stuck", command=stuck, timeout=0.5)
    started = time.monotonic()
    with pytest.raises(signer.SignerError) as exc:
        transport("GET", "/api/jarvis/trust")
    assert exc.value.code == "api_timeout" and "t_stuck" in str(exc.value) and time.monotonic() - started < 10
    assert transport._proc.wait(timeout=10) is not None                 # the stuck process was killed, not left running
    transport.close()


def test_the_api_timeout_comes_from_the_environment_and_is_always_finite_and_positive(monkeypatch):
    for raw, expected in (("", 60.0), ("5", 5.0), ("0", 60.0), ("-3", 60.0), ("inf", 60.0), ("nan", 60.0), ("soon", 60.0)):
        monkeypatch.setenv("JARVIS_SIGNER_TENANT_API_TIMEOUT", raw)
        assert signer._tenant_api_timeout() == expected, raw


def test_a_stuck_tenant_does_not_stop_the_tenants_after_it():
    """The timeout is what makes 'one tenant cannot break the others' true for a hang, not only for an exception."""
    stuck = [sys.executable, "-c", "import sys, time; sys.stdin.readline(); time.sleep(120)"]
    fine = [sys.executable, "-c", "import sys, json\nfor l in sys.stdin: print(json.dumps({'status': 200, 'body': {}}), flush=True)"]
    answered = []

    def visit(tenant):
        t = signer.ContainerTransport(tenant, command=stuck if tenant == "t_stuck" else fine, timeout=0.5)
        try:
            answered.append((tenant, t("GET", "/x")[0]))
        finally:
            t.close()
        return {}

    report = tenant_jobs.run_for_tenants(visit, ["operator", "t_stuck", "t_after"])
    assert report["failed"] == ["t_stuck"] and answered == [("operator", 200), ("t_after", 200)]


def _tenants_sh(tmp_path, *args):
    """Run tenants.sh with a fake `docker` first on PATH: arguments are validated before anything touches Docker, so a rejected combination never reaches it."""
    fake = tmp_path / "bin"
    fake.mkdir(exist_ok=True)
    (fake / "docker").write_text("#!/bin/sh\nexit 1\n")
    (fake / "docker").chmod(0o755)
    env = {**os.environ, "PATH": f"{fake}{os.pathsep}{os.environ['PATH']}", "JARVIS_HOME": str(tmp_path / "home")}
    return subprocess.run(["bash", str(BIN / "tenants.sh"), *args], capture_output=True, text=True, env=env, timeout=60)


@pytest.mark.skipif(sys.platform == "win32", reason="bash scripts for the box")
@pytest.mark.parametrize("args", [("seal", "--dry-run"), ("sign", "--force"), ("status", "--dry-run"), ("status", "--force"), ("verify", "--force"),
                                  ("verify", "--dry-run"), ("list", "--force"), ("list", "--tenants", "operator"), ("seal", "--bogus")])
def test_a_flag_that_does_not_apply_to_the_command_is_refused_not_ignored(tmp_path, args):
    r = _tenants_sh(tmp_path, *args)
    assert r.returncode == 2, (r.returncode, r.stderr)
    assert not (tmp_path / "home" / "state" / "tenants-seal.last_ok").exists()


@pytest.mark.skipif(sys.platform == "win32", reason="bash scripts for the box")
@pytest.mark.parametrize("args", [("seal", "--force", "--tenants", "operator"), ("sign", "--dry-run", "--tenants", "operator"), ("status", "--tenants", "operator"),
                                  ("verify", "--tenants", "operator")])
def test_the_flags_that_do_apply_get_past_the_check(tmp_path, args):
    """Positive control: these pass validation (and then fail at the fake docker, which is not exit 2)."""
    r = _tenants_sh(tmp_path, *args)
    assert r.returncode != 2 and "does not apply" not in r.stderr and "takes only" not in r.stderr and "has no" not in r.stderr, (r.returncode, r.stderr)


# ======================================================================================================== the box scripts: new, and nothing else moved
BIN = ROOT / "deploy" / "mint" / "bin"


def test_the_new_scripts_parse_and_are_executable():
    for name in ("tenants.sh", "tenant-api.sh", "jarvisctl"):
        assert subprocess.run(["bash", "-n", str(BIN / name)], capture_output=True).returncode == 0, name
    assert os.access(BIN / "tenants.sh", os.X_OK) and os.access(BIN / "tenant-api.sh", os.X_OK)


def test_the_per_tenant_command_never_touches_the_state_files_the_watchdog_reads():
    """The watchdog only watches seal.last_ok / sign.last_ok / sign.first_ok after they have succeeded once: the new command has its own."""
    text = (BIN / "tenants.sh").read_text(encoding="utf-8")
    assert not re.search(r"(?<!tenants-)\bseal\.last_ok", text) and not re.search(r"(?<!tenants-)\bsign\.last_ok", text) and "sign.first_ok" not in text
    assert "tenants-seal.last_ok" in text and "tenants-sign.last_ok" in text
    assert "seal.last_ok" not in (BIN / "tenant-api.sh").read_text(encoding="utf-8")


def test_the_timers_and_the_operator_scripts_do_not_know_about_the_new_command():
    """Nothing is switched on: the systemd units still run seal.sh / attest.sh, and those scripts do not call the per-tenant tool."""
    for unit in (ROOT / "deploy" / "mint" / "systemd").iterdir():
        assert "tenants" not in unit.read_text(encoding="utf-8"), unit.name
    for name in ("seal.sh", "attest.sh", "watchdog.sh", "heal.sh"):
        assert "tenant_jobs" not in (BIN / name).read_text(encoding="utf-8") and "tenants.sh" not in (BIN / name).read_text(encoding="utf-8"), name


def test_jarvisctl_dispatches_the_new_command_and_keeps_the_old_ones():
    text = (BIN / "jarvisctl").read_text(encoding="utf-8")
    assert 'tenants)  exec "$here/tenants.sh" "$@" ;;' in text
    for old in ('seal)     exec "$here/seal.sh" "$@" ;;', 'attest)   exec "$here/attest.sh" "$@" ;;', 'verify)'):
        assert old in text


def test_the_tenant_list_is_taken_with_the_superuser_path_not_the_migrator_role():
    """The migrator cannot bypass row-level security and so cannot enumerate tenants: the listing is a docker-exec psql as postgres, like the backups."""
    text = (BIN / "tenants.sh").read_text(encoding="utf-8")
    assert "pg_exec psql" in text and "chain_heads" in text and "history_counters" in text


def _fake_docker(tmp_path, body):
    """A `docker` first on PATH that logs its arguments to docker.log and then behaves as `body` says."""
    fake = tmp_path / "fakebin"
    fake.mkdir(exist_ok=True)
    (fake / "docker").write_text('#!/bin/sh\necho "$@" >> "$FAKE_DOCKER_LOG"\n' + body)
    (fake / "docker").chmod(0o755)
    return fake


@pytest.mark.skipif(sys.platform == "win32", reason="bash scripts for the box")
def test_a_stuck_container_is_removed_by_name_on_timeout_not_just_the_compose_client_killed(tmp_path, monkeypatch):
    """Killing the compose client does not stop the container it started: the signer names the one-off container and removes it by that name."""
    fake = _fake_docker(tmp_path, 'case "$1" in compose) read line; exec sleep 120 ;; esac\n')
    log = tmp_path / "docker.log"
    monkeypatch.setenv("PATH", f"{fake}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_DOCKER_LOG", str(log))
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path / "home"))
    transport = signer.ContainerTransport("t_stuck", timeout=1.0)
    with pytest.raises(signer.SignerError) as exc:
        transport("GET", "/api/jarvis/trust")
    assert exc.value.code == "api_timeout"
    lines = log.read_text().splitlines()
    run = next(l for l in lines if l.startswith("compose"))
    name = re.search(r"--name (\S+)", run).group(1)
    assert name.startswith("jarvis-tenant-api-t_stuck-") and re.fullmatch(r"[A-Za-z0-9_.-]+", name)
    assert f"rm -f {name}" in lines                                                  # the container itself is removed, by the name it was started with
    transport.close()


def test_the_container_names_are_unique_and_safe_for_any_tenant_key(tmp_path, monkeypatch):
    fake = _fake_docker(tmp_path, "exit 0\n")          # never the real docker: the default command is the box's own wrapper script
    monkeypatch.setenv("PATH", f"{fake}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_DOCKER_LOG", str(tmp_path / "docker.log"))
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path / "home"))
    names = set()
    for tenant in ("operator", "t a/b;rm -rf", "x" * 200, "ü"):
        t = signer.ContainerTransport(tenant)       # (no command: the real wrapper path, against the fake docker; only the name matters)
        names.add(t._container)
        assert re.fullmatch(r"[A-Za-z0-9_.-]+", t._container) and len(t._container) < 100
        t._proc.kill()
    assert len(names) == 4


@pytest.mark.skipif(sys.platform == "win32", reason="bash scripts for the box")
def test_tenant_api_sh_starts_the_one_off_container_with_the_name_it_was_given(tmp_path):
    fake = _fake_docker(tmp_path, "exit 0\n")
    log = tmp_path / "docker.log"
    env = {**os.environ, "PATH": f"{fake}{os.pathsep}{os.environ['PATH']}", "FAKE_DOCKER_LOG": str(log), "JARVIS_HOME": str(tmp_path / "home")}
    subprocess.run(["bash", str(BIN / "tenant-api.sh"), "t_alice"], env={**env, "JARVIS_TENANT_API_NAME": "jarvis-tenant-api-test"}, timeout=60, check=True)
    subprocess.run(["bash", str(BIN / "tenant-api.sh"), "t_bob"], env=env, timeout=60, check=True)               # no name given: no --name (as before)
    first, second = log.read_text().splitlines()
    assert "--name jarvis-tenant-api-test" in first and "serve --tenant t_alice" in first and "--name" not in second and "serve --tenant t_bob" in second


@pytest.mark.skipif(sys.platform == "win32", reason="bash scripts for the box")
@pytest.mark.parametrize("args", [("sign", "--tenants", ""), ("seal", "--tenants", " , "), ("status", "--tenants", ","), ("verify", "--tenants", " ")])
def test_an_explicitly_empty_tenant_selection_is_refused_not_widened_to_every_tenant(tmp_path, args):
    r = _tenants_sh(tmp_path, *args)
    assert r.returncode == 2 and "names no tenant" in r.stderr, (r.returncode, r.stderr)


def test_the_acting_tenant_resolves_in_strict_mode_too(monkeypatch):
    """The box's per-tenant jobs must work when JARVIS_MULTI_TENANT is on: no request identity, but the tenant the operator started the job for."""
    from app.identity import request_tenant
    monkeypatch.setenv("JARVIS_MULTI_TENANT", "1")
    assert request_tenant() is None
    with acting_for("operator"):
        assert request_tenant() == "operator" and current_tenant_key() is None
    with acting_for("t_alice"):
        assert request_tenant() == "t_alice" and current_tenant_key() == "t_alice"
    assert request_tenant() is None
