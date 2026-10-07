"""CL_CHAOS_100x: the guard that keeps it off the live stack, the probe list and its count, the destructive-probe gate, the
throwaway-stack script's refusals, and the non-destructive probes run for real against an in-process service on a throwaway schema."""

from __future__ import annotations

import importlib.util
import json
import os
import re
import socket
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "chaos" / "cl_chaos_100x.py"
STACK_SH = ROOT / "scripts" / "chaos" / "throwaway_stack.sh"
DOCS = ROOT / "docs" / "chaos"

spec = importlib.util.spec_from_file_location("cl_chaos_100x", SCRIPT)
chaos = importlib.util.module_from_spec(spec)
sys.modules["cl_chaos_100x"] = chaos
spec.loader.exec_module(chaos)


# --- the probe list and its count ----------------------------------------------------------------------------------------------------------

def test_the_per_round_count_is_the_length_of_the_probe_list():
    assert chaos.PROBES_PER_ROUND == len(chaos.PROBES) > 0
    out = subprocess.run([sys.executable, str(SCRIPT), "--count"], capture_output=True, text=True)
    assert out.returncode == 0 and out.stdout.strip() == str(len(chaos.PROBES))
    listed = subprocess.run([sys.executable, str(SCRIPT), "--list"], capture_output=True, text=True).stdout
    assert listed.strip().splitlines()[-1] == f"{len(chaos.PROBES)} probes per round"
    assert len(listed.strip().splitlines()) == len(chaos.PROBES) + 1


def test_probe_ids_are_unique_and_every_phase_is_described():
    ids = [p.id for p in chaos.PROBES]
    assert len(ids) == len(set(ids))
    assert {p.phase for p in chaos.PROBES} <= set(chaos.PHASES) and set(chaos.PHASES) == {p.phase for p in chaos.PROBES}
    for p in chaos.PROBES:
        assert re.fullmatch(r"[A-H]\d", p.id) and p.id[0] == p.phase and p.title and callable(p.fn)


def test_the_probes_the_task_names_are_there_and_say_what_was_asked():
    by_id = {p.id: p for p in chaos.PROBES}
    assert "same id" in by_id["A2"].title and "created once" in by_id["A2"].title      # content-addressed: idempotent, not a rejection
    assert "same id" in by_id["A6"].title and "another key order" in by_id["A6"].title
    assert "match nothing" in by_id["D2"].title and "no SQL" in by_id["D2"].title
    assert "non-superuser" in by_id["E5"].title and by_id["E5"].destructive
    assert "SMALL batch" in by_id["B1"].title and "at most five" in by_id["B1"].title
    assert {p.id for p in chaos.PROBES if p.phase == "F"} >= {"F1", "F2", "F3", "F4", "F5", "F6"}
    assert {p.id for p in chaos.PROBES if p.phase == "G"} >= {"G0", "G1", "G2", "G3", "G4", "G5", "G6", "G7"}
    assert {p.id for p in chaos.PROBES if p.phase == "H"} == {"H1", "H2", "H3", "H4"}   # database down, schema mismatch, pool flood, force-seal


def test_every_probe_that_touches_docker_or_the_database_is_marked_destructive():
    src = SCRIPT.read_text()
    for p in chaos.PROBES:
        body = src[src.index(f"def {p.id.lower()}("):]
        body = body.split("\n\n\n")[0]
        touches = any(w in body for w in ("ctx.docker(", "ctx.psql(", "_app_psql(", "ctx.mint_script(", "need_destructive()", "_offline_verify_scratch("))
        if touches and p.id != "C7" and p.id != "D2":   # C7 and D2 read the database only when the throwaway proof allows it
            assert p.destructive, f"{p.id} runs docker or SQL but is not marked destructive"


def test_the_docs_and_the_sample_log_use_the_computed_count():
    n = chaos.PROBES_PER_ROUND
    doc = (DOCS / "CL_CHAOS_100x.md").read_text()
    assert f"{n} probes per round" in doc and f"{n * 100} probe runs" in doc, "docs/chaos/CL_CHAOS_100x.md does not state the computed count"
    for other in re.findall(r"(\d+) probes per round", doc):
        assert int(other) == n
    sample = (DOCS / "sample-smoke-round.log").read_text().splitlines()
    assert f"{n} probes per round, 1 round(s) = {n} probe runs" in sample[0]
    lines = [l for l in sample if re.match(r"r001 [A-H]\d ", l)]
    assert len(lines) == n and [l.split()[1] for l in lines] == [p.id for p in chaos.PROBES]
    summary = json.loads(sample[-1])
    assert summary["probes_per_round"] == n and summary["probe_runs"] == n


# --- the guard -----------------------------------------------------------------------------------------------------------------------------

def ready(stack=None, status=200, body=True):
    def fetch(url):
        if not body:
            return 0, None
        data = {"status": "ready", "checks": {}}
        if stack is not None:
            data["stack"] = stack
        return status, data
    return fetch


GOOD = "chaos-throwaway:jarvis-chaos100x"


@pytest.mark.parametrize("url", ["http://127.0.0.1:8011", "http://localhost:8011", "http://[::1]:8011", "http://127.0.0.2:8011", "http://127.1:8011/"])
def test_the_live_port_is_refused_on_every_spelling_of_loopback(url):
    with pytest.raises(chaos.Refusal, match="live stack's port"):
        chaos.assess_target(url, ready(GOOD), ports={8011})  # even when /ready claims to be a throwaway


def test_the_port_in_deploy_mint_env_is_a_live_port_too(monkeypatch):
    monkeypatch.setattr(chaos, "live_ports", lambda: {8011, 9123})
    with pytest.raises(chaos.Refusal, match="live stack's port"):
        chaos.assess_target("http://127.0.0.1:9123", ready(GOOD))


@pytest.mark.parametrize("host", ["192.0.2.10", "10.1.2.3", "example.com", "0.0.0.0"])
def test_a_host_that_is_not_loopback_is_refused_and_nothing_lifts_that(host):
    for allow in (False, True):
        with pytest.raises(chaos.Refusal, match="not a loopback"):
            chaos.assess_target(f"http://{host}:18017", ready(GOOD), allow_live=allow, ports={8011})


def test_an_unreadable_ready_is_refused():
    with pytest.raises(chaos.Refusal, match="could not be read"):
        chaos.assess_target("http://127.0.0.1:18017", ready(body=False), ports={8011})


def test_a_target_that_reports_no_identity_is_refused_because_it_cannot_be_proven_a_throwaway():
    with pytest.raises(chaos.Refusal, match="no stack identity"):
        chaos.assess_target("http://127.0.0.1:18017", ready(None), ports={8011})


@pytest.mark.parametrize("identity", ["jarvis-live", "production", "chaos-throwaway", "throwaway:x", " "])
def test_a_target_that_reports_the_live_or_any_other_identity_is_refused(identity):
    with pytest.raises(chaos.Refusal):
        chaos.assess_target("http://127.0.0.1:18017", ready(identity), ports={8011})


def test_a_503_that_still_names_a_throwaway_is_accepted():
    got = chaos.assess_target("http://127.0.0.1:18017", ready(GOOD, status=503), ports={8011})
    assert got["port"] == 18017 and got["stack"] == GOOD


def test_the_flag_lifts_only_the_live_refusals_and_the_chaos_task_never_passes_it():
    got = chaos.assess_target("http://127.0.0.1:8011", ready("jarvis-live"), allow_live=True, ports={8011})
    assert got["port"] == 8011
    # nothing in the repository's chaos tooling or docs runs the hammer with the flag
    for path in [SCRIPT, STACK_SH, *DOCS.glob("*")]:
        for line in path.read_text(errors="replace").splitlines():
            if "--i-know-this-is-live" in line:
                assert not re.search(r"cl_chaos_100x\.py[^`]*--i-know-this-is-live", line) or "never" in line.lower() or "do not" in line.lower(), (path, line)


def test_main_refuses_the_live_port_with_exit_3_and_does_not_touch_the_network(monkeypatch, capsys, tmp_path):
    def boom(url):
        raise AssertionError("the network was touched")
    monkeypatch.setattr(chaos, "fetch_ready", boom)
    monkeypatch.setattr(chaos, "live_ports", lambda: {8011})
    rc = chaos.main(["--target", "http://127.0.0.1:8011", "--stack-dir", str(tmp_path), "--rounds", "1"])
    assert rc == chaos.EXIT_REFUSED and "REFUSED" in capsys.readouterr().err


def test_main_refuses_when_there_is_no_throwaway_stack_description(capsys, tmp_path):
    assert chaos.main(["--stack-dir", str(tmp_path), "--rounds", "1"]) == chaos.EXIT_REFUSED
    assert "throwaway_stack.sh up" in capsys.readouterr().err


def test_main_refuses_a_target_that_is_not_the_stacks_own_secrets_even_if_it_is_a_throwaway(monkeypatch, capsys, tmp_path):
    (tmp_path / "stack.json").write_text(json.dumps({"url": "http://127.0.0.1:18017", "port": 18017, "secrets_dir": str(tmp_path / "nosecrets"), "containers": {}}))
    monkeypatch.setattr(chaos, "fetch_ready", ready(GOOD))
    monkeypatch.setattr(chaos, "live_ports", lambda: {8011})
    assert chaos.main(["--stack-dir", str(tmp_path), "--rounds", "1"]) == chaos.EXIT_REFUSED
    assert "API key" in capsys.readouterr().err


# --- the destructive-probe gate -----------------------------------------------------------------------------------------------------------

STACK = {"port": 18017, "project": "jarvis-chaos100x", "containers": {"db": "chaos100x-db", "app": "chaos100x-app", "migrate": "chaos100x-migrate"},
         "keys_dir": "/tmp/x/keys", "dir": "/tmp/x"}
TARGET = {"port": 18017}


def runner(label="jarvis-chaos100x", rc=0):
    calls = []

    def run(args, **kw):
        calls.append(args)
        return SimpleNamespace(returncode=rc, stdout=label + "\n", stderr="")
    run.calls = calls
    return run


def test_the_proof_passes_for_the_throwaway_and_checks_every_container_label(monkeypatch):
    monkeypatch.setattr(chaos, "live_ports", lambda: {8011})
    r = runner()
    assert chaos.throwaway_proof(STACK, TARGET, r) is None
    assert len(r.calls) == 3 and all("com.docker.compose.project" in c[3] for c in r.calls)


@pytest.mark.parametrize("change,why", [
    ({"containers": {"db": "jarvis-db", "app": "chaos100x-app"}}, "not a throwaway name"),
    ({"containers": {"db": "chaos100x-db", "app": "jarvis-app"}}, "not a throwaway name"),
    ({"containers": {"db": "someone-elses-db"}}, "not a throwaway name"),
    ({"port": 8011}, "own port"),
    ({"keys_dir": str(Path.home() / "jarvis-ledger" / "keys")}, "live ledger home"),
])
def test_the_proof_refuses_live_names_a_live_port_and_live_key_directories(monkeypatch, change, why):
    monkeypatch.setattr(chaos, "live_ports", lambda: {8011})
    assert why in chaos.throwaway_proof(STACK | change, TARGET, runner())


def test_the_proof_refuses_a_container_without_the_throwaway_project_label_or_that_does_not_exist(monkeypatch):
    monkeypatch.setattr(chaos, "live_ports", lambda: {8011})
    assert "project label" in chaos.throwaway_proof(STACK, TARGET, runner(label="jarvis-ledger"))
    assert "project label" in chaos.throwaway_proof(STACK, TARGET, runner(rc=1))
    assert "own port" in chaos.throwaway_proof(STACK, {"port": 18999}, runner())


def test_without_the_proof_every_destructive_probe_is_skipped_and_runs_no_command(monkeypatch):
    def boom(*a, **kw):
        raise AssertionError("a command was run")
    monkeypatch.setattr(chaos, "run_cmd", boom)
    ctx = chaos.Ctx(client=None, stats=chaos.Stats(), stack=STACK, target=TARGET, destructive_ok="not provably a throwaway", state={}, rnd=1,
                    rng=__import__("random").Random(1), max_history=100)
    for p in chaos.PROBES:
        if p.phase == "H" or p.id.startswith("E"):
            with pytest.raises(chaos.Skip, match="not provably"):
                p.fn(ctx)
    with pytest.raises(chaos.Skip):
        chaos.Ctx(client=None, stats=chaos.Stats(), stack=None, target=TARGET, destructive_ok=None, state={}, rnd=1, rng=__import__("random").Random(1), max_history=1).need_destructive()


# --- the throwaway stack script ------------------------------------------------------------------------------------------------------------

@pytest.fixture
def fake_docker(tmp_path):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    log = tmp_path / "docker.log"
    exe = bindir / "docker"
    exe.write_text(f'#!/usr/bin/env bash\necho "$@" >> {log}\nexit 0\n')
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    return bindir, log


def stack_sh(*args, env=None, bindir=None):
    e = dict(os.environ, **(env or {}))
    if bindir:
        e["PATH"] = f"{bindir}:{e['PATH']}"
    return subprocess.run(["bash", str(STACK_SH), *args], capture_output=True, text=True, env=e, timeout=60)


def test_the_stack_script_refuses_the_live_port_and_a_directory_inside_the_repo_or_the_live_home(tmp_path, fake_docker):
    bindir, log = fake_docker
    r = stack_sh("up", env={"JARVIS_CHAOS_PORT": "8011", "JARVIS_CHAOS_DIR": str(tmp_path / "s")}, bindir=bindir)
    assert r.returncode != 0 and "live stack's port" in r.stderr and not (tmp_path / "s").exists()
    r = stack_sh("up", env={"JARVIS_CHAOS_DIR": str(ROOT / "deploy" / "chaos-x")}, bindir=bindir)
    assert r.returncode != 0 and "inside the repository" in r.stderr and not (ROOT / "deploy" / "chaos-x").exists()
    r = stack_sh("up", env={"JARVIS_CHAOS_DIR": str(Path.home() / "jarvis-ledger" / "chaos")}, bindir=bindir)
    assert r.returncode != 0 and "live ledger home" in r.stderr
    assert not log.exists() or log.read_text() == ""   # no docker command ran before the refusals


def test_down_refuses_to_delete_a_directory_that_is_not_a_throwaway_stack(tmp_path, fake_docker):
    bindir, log = fake_docker
    target = tmp_path / "precious"
    target.mkdir()
    (target / "keep.txt").write_text("data")
    r = stack_sh("down", env={"JARVIS_CHAOS_DIR": str(target)}, bindir=bindir)
    assert r.returncode != 0 and "not a throwaway stack directory" in r.stderr and (target / "keep.txt").exists()


def test_down_touches_only_throwaway_names_and_removes_its_own_directory(tmp_path, fake_docker):
    bindir, log = fake_docker
    target = tmp_path / "stack"
    target.mkdir()
    (target / ".jarvis-chaos100x").write_text("")
    r = stack_sh("down", env={"JARVIS_CHAOS_DIR": str(target)}, bindir=bindir)
    assert r.returncode == 0 and not target.exists()
    text = log.read_text()
    assert "chaos100x-db" in text and "jarvis-chaos100x_pgdata" in text
    assert not re.search(r"\bjarvis-(db|app|migrate)\b|jarvis-ledger\b", text), text


def test_the_stack_script_renames_everything_the_live_stack_is_called_in_the_copy():
    text = STACK_SH.read_text()
    for old in ("name: jarvis-ledger", "container_name: jarvis-db", "container_name: jarvis-app", "container_name: jarvis-migrate",
                "image: jarvis-ledger-db:16", "image: jarvis-ledger-app:local", "PROJECT=jarvis-ledger", "DB_CONTAINER=jarvis-db", "APP_CONTAINER=jarvis-app"):
        assert old in text, f"the script does not rename {old!r}"
    assert "JARVIS_STACK_ID" in text and "8011" in text and "ssh-keygen" in text
    compose = (ROOT / "deploy" / "mint" / "docker-compose.yml").read_text()
    for needle in ("name: jarvis-ledger\n", "container_name: jarvis-db", "image: jarvis-ledger-app:local", "${JARVIS_STACK_ID:-jarvis-live}", "context: ../.."):
        assert compose.count(needle) >= 1, f"the script renames {needle!r} but the compose file no longer has it"


# --- the probes, for real, on an in-process service and a throwaway schema -----------------------------------------------------------------------

pytestmark_pg = pytest.mark.postgres


@pytest.fixture
def served(pg_schema, tmp_path_factory, monkeypatch):
    uvicorn = pytest.importorskip("uvicorn")
    from app import attest, pg_store
    from app.main import app
    from app.pg_schema import EXPECTED_SCHEMA_VERSION, migrate
    from tests.attest_support import have_ssh_keygen, make_key

    if not have_ssh_keygen():
        pytest.skip("ssh-keygen not installed")
    assert migrate(pg_schema.admin_dsn, schema=pg_schema.schema, app_role="jarvis_app_test") == EXPECTED_SCHEMA_VERSION
    keys_dir = tmp_path_factory.mktemp("chaoskeys")
    os.chmod(keys_dir, 0o700)
    keys = {n: make_key(keys_dir, n) for n in ("root", "mint", "stranger")}
    roots = keys_dir / "roots.pub"
    roots.write_text(keys["root"].pub_text + "\n")
    key = "chaos-test-api-key"
    for k, v in (("JARVIS_DATABASE_URL", pg_schema.app_dsn), ("JARVIS_DATABASE_SCHEMA", pg_schema.schema), ("JARVIS_PG_STORE", "rows"), ("JARVIS_API_KEY", key),
                 ("JARVIS_MEMORY_WRITE_ENABLED", "true"), ("JARVIS_STACK_ID", GOOD), (attest.ROOTS_ENV, str(roots))):
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("JARVIS_ALLOW_UNAUTHENTICATED", raising=False)
    monkeypatch.delenv("JARVIS_SIGNATURES", raising=False)
    monkeypatch.setenv("JARVIS_CLAUSE_V", "enforce")  # the suite turns it off for older tests; the real stack enforces
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", lifespan="on"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.1)
    assert server.started, "the in-process server did not start"
    yield SimpleNamespace(url=f"http://127.0.0.1:{port}", key=key, keys_dir=keys_dir, keys=keys, pg=pg_schema)
    server.should_exit = True
    thread.join(15)
    pg_store.close_pools()


def make_ctx_args(served, state=None):
    stats = chaos.Stats()
    client = chaos.Client(served.url, served.key, stats)
    stack = {"keys_dir": str(served.keys_dir), "containers": {}, "dir": str(served.keys_dir), "secrets_dir": str(served.keys_dir), "port": 0}
    target = {"url": served.url, "port": 0, "stack": GOOD}
    return stats, dict(client=client, stats=stats, stack=stack, target=target, destructive_ok="in-process test: no docker", state=state if state is not None else {},
                       seed="test", max_history=100000)


@pytest.mark.postgres
def test_every_probe_that_needs_no_docker_passes_against_a_real_service_in_round_after_round(served):
    stats, args = make_ctx_args(served)
    skip_in_process = {"G7"}  # needs the signer pass (G1 needs docker); covered by the next test
    runnable = {p.id for p in chaos.PROBES if not p.destructive} - skip_in_process
    lines = []
    results = []
    for rnd in (1, 2):
        results += chaos.run_round(rnd, args, runnable, lines.append, stats)
    bad = [r for r in results if r["status"] not in ("PASS",)]
    assert not bad, json.dumps([(r['round'], r['probe'], r['status'], r['detail']) for r in bad], indent=1)
    assert {r["probe"] for r in results} == runnable
    assert [s for s in stats.five_xx if not s["expected"]] == []
    assert stats.requests > 100 and 401 in stats.statuses and 422 in stats.statuses


@pytest.mark.postgres
def test_the_signature_level_probe_passes_once_the_real_signer_has_signed(served):
    from app import signer

    stats, args = make_ctx_args(served)
    chaos.run_round(1, args, {"A1", "B1", "F1", "G0"}, lambda l: None, stats)
    ctx = chaos.Ctx(rnd=1, rng=__import__("random").Random(1), **{k: v for k, v in args.items() if k != "seed"})
    os.chmod(served.keys["mint"].path, 0o600)
    api = signer.Api("http://x", "unused", lambda m, p, b: chaos._call(ctx, m, p, b))
    result = signer.run_sign(api, signer.preflight_key(served.keys["mint"].path, mounts=lambda: []), verify_block=lambda h, bh: None, verify_receipt=lambda rid: None)
    assert result["signed_receipts"], result
    [outcome] = chaos.run_round(1, args, {"G7"}, lambda l: None, stats)
    assert outcome["status"] == "PASS", outcome


@pytest.mark.postgres
def test_a_probe_that_fails_is_reported_as_a_failure_and_a_crash_as_an_error(served, monkeypatch):
    stats, args = make_ctx_args(served)
    chaos.run_round(1, args, {"A2"}, lambda l: None, stats)
    monkeypatch.setattr(chaos, "PROBES", [chaos.Probe("Z1", "A", "always fails", lambda c: chaos.check(False, "nope")),
                                          chaos.Probe("Z2", "A", "crashes", lambda c: 1 / 0), chaos.Probe("Z3", "A", "skips", lambda c: (_ for _ in ()).throw(chaos.Skip("why")))])
    got = {r["probe"]: r for r in chaos.run_round(2, args, None, lambda l: None, stats)}
    assert got["Z1"]["status"] == "FAIL" and got["Z1"]["detail"] == "nope"
    assert got["Z2"]["status"] == "ERROR" and "ZeroDivisionError" in got["Z2"]["detail"]
    assert got["Z3"]["status"] == "SKIP" and got["Z3"]["detail"] == "why"


@pytest.mark.postgres
def test_an_unexpected_5xx_is_counted_and_an_expected_one_is_marked_expected(served):
    stats = chaos.Stats()
    stats.current.update(round=1, probe="Q1", expect=(503,))
    stats.record("GET", "/x?secret=1", chaos.Response(503, None, {}, 5.0))
    stats.current.update(probe="Q2", expect=())
    stats.record("GET", "/y", chaos.Response(500, None, {}, 5.0))
    stats.record("GET", "/z", chaos.Response(0, None, {}, 5.0))
    assert [(e["probe"], e["expected"]) for e in stats.five_xx] == [("Q1", True), ("Q2", False), ("Q2", False)]
    assert all("?" not in e["path"] for e in stats.five_xx)  # a query string (which can hold a secret) is never logged


@pytest.mark.postgres
def test_the_ledger_stays_small_the_batch_is_small_and_the_cap_stops_the_run(served, tmp_path, monkeypatch, capsys):
    stats, args = make_ctx_args(served)
    before = chaos.Ctx(rnd=1, rng=__import__("random").Random(1), **{k: v for k, v in args.items() if k != "seed"}).head()["history_seq"]
    chaos.run_round(1, args, {"B1"}, lambda l: None, stats)
    after = chaos.Ctx(rnd=1, rng=__import__("random").Random(1), **{k: v for k, v in args.items() if k != "seed"}).head()
    assert after["history_seq"] - before == 5 and after["unsealed_entries"] == 0  # five records written, five sealed, nothing more
    stack_dir = tmp_path / "stack"
    stack_dir.mkdir()
    (stack_dir / "secrets").mkdir()
    (stack_dir / "secrets" / "api-key").write_text(served.key)
    (stack_dir / "stack.json").write_text(json.dumps({"url": served.url, "port": 0, "project": "jarvis-chaos100x", "secrets_dir": str(stack_dir / "secrets"),
                                                      "containers": {}, "keys_dir": str(stack_dir), "dir": str(stack_dir)}))
    monkeypatch.setattr(chaos, "live_ports", lambda: {8011})
    out = tmp_path / "out"
    rc = chaos.main(["--stack-dir", str(stack_dir), "--rounds", "3", "--max-history", "1", "--only", "A9", "--out", str(out)])
    summary = json.loads((out / "results.json").read_text())
    assert summary["rounds_completed"] == 0 and "reached --max-history" in summary["capped"] and summary["probe_runs"] == 0
    assert rc in (0, 1)


def test_the_summary_breaks_every_5xx_down_by_probe_and_status_and_lists_the_unexpected_ones():
    stats = chaos.Stats()
    for probe_id, expect, status in (("H1", (503,), 503), ("H1", (503,), 503), ("H3", (503,), 503), ("C4", (), 500), ("C4", (), 0)):
        stats.current.update(round=1, probe=probe_id, expect=expect)
        stats.record("GET", "/x", chaos.Response(status, None, {}, 1.0))
    out = chaos.summarize([], stats, 0, time.time(), {}, {}, SimpleNamespace(rounds=1), None)
    assert out["five_xx_by_probe"] == {"C4:0": 1, "C4:500": 1, "H1:503": 2, "H3:503": 1}
    assert out["five_xx_total"] == 5 and out["five_xx_expected"] == 3 and [e["probe"] for e in out["five_xx_unexpected"]] == ["C4", "C4"]
