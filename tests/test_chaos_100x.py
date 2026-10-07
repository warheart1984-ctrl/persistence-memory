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
        assert re.fullmatch(r"[A-K]\d", p.id) and p.id[0] == p.phase and p.title and callable(p.fn)


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
    sample = (DOCS / "sample-smoke-round.txt").read_text().splitlines()
    assert f"{n} probes per round, 1 round(s) = {n} probe runs" in sample[0]
    lines = [l for l in sample if re.match(r"r001 [A-K]\d ", l)]
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
    fake_home = tmp_path / "home"          # no ~/jarvis-ledger here, as on a CI runner: the guard must not need the live directory to exist
    fake_home.mkdir()
    for target in (fake_home / "jarvis-ledger" / "chaos", fake_home / "jarvis-ledger", fake_home / "jarvis-ledger" / "a" / "b" / ".." / "c"):
        r = stack_sh("up", env={"JARVIS_CHAOS_DIR": str(target), "HOME": str(fake_home)}, bindir=bindir)
        assert r.returncode != 0 and "live ledger home" in r.stderr and not (fake_home / "jarvis-ledger").exists(), (target, r.stderr)
    link = tmp_path / "linkhome"
    link.symlink_to(ROOT)
    r = stack_sh("up", env={"JARVIS_CHAOS_DIR": str(link / "chaos-y")}, bindir=bindir)
    assert r.returncode != 0 and "inside the repository" in r.stderr   # a symlink into the repository does not get round the guard
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


# --- phase 2: the ugly-conditions faults (I, J, K) -----------------------------------------------------------------------------------------------------

def test_the_fault_phases_exist_and_are_destructive_and_expect_only_failures_a_fault_causes():
    by_id = {p.id: p for p in chaos.PROBES}
    for pid, phase in (("I1", "I"), ("J1", "J"), ("K1", "K")):
        assert by_id[pid].phase == phase and by_id[pid].destructive and set(by_id[pid].expect_5xx) <= {0, -1, 503}
    assert "kill -9" in by_id["I1"].title and "partition" in by_id["J1"].title and "fill" in by_id["K1"].title
    assert set("IJK") <= set(chaos.PHASES)


def test_the_full_disk_fault_is_skipped_without_the_size_capped_volume_and_never_fills_a_real_disk(monkeypatch):
    def boom(*a, **kw):
        raise AssertionError("a command was run")
    monkeypatch.setattr(chaos, "run_cmd", boom)
    stack = dict(STACK, containers={"db": "chaos100x-db", "app": "chaos100x-app", "migrate": "chaos100x-migrate"})      # no keeper, no pgdata_mb
    ctx = chaos.Ctx(client=None, stats=chaos.Stats(), stack=stack, target=TARGET, destructive_ok=None, state={}, rnd=1, rng=__import__("random").Random(1), max_history=100)
    with pytest.raises(chaos.Skip, match="size-capped database volume"):
        chaos.k1(ctx)
    src = SCRIPT.read_text()
    k1_body = src[src.index("def k1("):src.index("# --- the runner")]
    assert "/keep/chaos-filler" in k1_body and "/var/lib/docker" not in k1_body and "fallocate" not in k1_body   # it fills the keeper's mount of the capped volume only


def att(token, status, t0, t1, rid=None):
    return chaos.Attempt(token, f"chaos100x {token} T", status, rid, t0, t1)


def test_fail_closed_passes_when_everything_after_the_fault_was_refused():
    attempts = [att("a", 200, 0.0, 0.2, "mem-1"), att("b", 0, 1.6, 1.7), att("c", 503, 2.0, 2.5), att("d", -1, 2.1, 3.0), att("long", -1, 2.2, 21.0),
                att("e", 200, 9.0, 9.1, "mem-2")]
    out = chaos.fail_closed(attempts, t_fault=1.0, t_up=8.0, allowed=(0, -1, 503))
    # "long" began during the fault but was answered only after the application was back: it is neither counted nor held against it
    assert out["writes_during_fault"] == 3 and out["statuses_during_fault"] == {0: 1, 503: 1, -1: 1} and out["longest_request_s"] == 18.8


def test_fail_closed_trips_on_an_acknowledged_write_during_the_fault_and_on_an_unexpected_status():
    ok_then_wrong = [att("a", 200, 1.8, 2.0, "mem-9")]
    with pytest.raises(chaos.ProbeFail, match="acknowledged with 200 while it was in force"):
        chaos.fail_closed(ok_then_wrong, t_fault=1.0, t_up=8.0, allowed=(0, -1, 503))
    with pytest.raises(chaos.ProbeFail, match=r"answered \[500\]"):
        chaos.fail_closed([att("a", 500, 2.0, 2.1)], t_fault=1.0, t_up=8.0, allowed=(0, -1, 503))
    with pytest.raises(chaos.ProbeFail, match=r"answered \[0\]"):
        chaos.fail_closed([att("a", 0, 2.0, 2.1)], t_fault=1.0, t_up=8.0, allowed=(503,))       # a refused connection is not a 503 when only 503 is acceptable
    # a request begun before the fault, or answered just before the application was back, is not held against it
    chaos.fail_closed([att("a", 200, 0.5, 1.2, "m"), att("b", 200, 7.8, 8.1, "n")], t_fault=1.0, t_up=8.0, allowed=(503,))


class FakeReady:
    def __init__(self, script):
        self.script, self.calls = list(script), 0

    def get(self, path, **kw):
        self.calls += 1
        status = self.script.pop(0) if self.script else 200
        return chaos.Response(status, {}, {}, 1.0)


def fake_ctx(client):
    return SimpleNamespace(client=client, stats=chaos.Stats(), state={}, metrics={})


def test_watch_recovery_reports_detection_and_the_third_consecutive_good_answer(monkeypatch):
    monkeypatch.setattr(chaos.time, "sleep", lambda s: None)
    clock = iter(x * 0.2 for x in range(1, 1000))
    monkeypatch.setattr(chaos.time, "time", lambda: next(clock))
    out = chaos.watch_recovery(fake_ctx(FakeReady([200, 0, 0, 503, 200, 503, 200, 200, 200])), t_fault=0.0, max_s=60)
    assert out["detect_s"] is not None and out["recover_s"] is not None and out["recover_s"] > out["detect_s"]
    assert out["ready_statuses"] == {200: 5, 0: 2, 503: 2} and out["gave_up_and_intervened"] is False       # a single 200 between failures is not a recovery


def test_watch_recovery_never_recovers_when_the_answers_never_settle_and_it_steps_in_once(monkeypatch):
    monkeypatch.setattr(chaos.time, "sleep", lambda s: None)
    clock = iter(x * 1.0 for x in range(1, 5000))
    monkeypatch.setattr(chaos.time, "time", lambda: next(clock))
    helped = []
    out = chaos.watch_recovery(fake_ctx(FakeReady([503] * 500)), t_fault=0.0, max_s=60, give_up=lambda: helped.append(1))
    assert out["recover_s"] is None and out["detect_s"] is not None and helped == [1] and out["gave_up_and_intervened"] is True


class GateCtx:
    """Just enough of a Ctx for fault_gates: rows the database returns, what the API says, and what the offline verifier does."""

    def __init__(self, attempts, **over):
        self.round, self.tag, self.state, self.metrics, self.stats = 1, "TAG", {"f1": {"id": "eo:sha256:" + "a" * 64}}, {}, chaos.Stats()
        self.attempts = attempts
        self.over = over
        self.client = self
        self.created = 0

    # the database
    def psql(self, sql, **kw):
        if "FROM memories m WHERE m.content LIKE" in sql:
            if "rows" in self.over:
                return self.over["rows"]
            return "\n".join(f"mem-{i}|{a.content}|create" for i, a in enumerate(self.attempts) if a.status == 200)
        if "NOT EXISTS" in sql:
            return self.over.get("orphans", "0")
        if "last_seq FROM history_counters" in sql:
            return self.over.get("counter_ok", "true")
        if "jarvis_verify_history" in sql:
            return self.over.get("hist_problems", "0")
        if "jarvis_verify_blocks" in sql:
            return self.over.get("block_problems", "0")
        raise AssertionError(sql)

    # the API
    def get(self, path, **kw):
        if path.endswith("/history/verify"):
            return chaos.Response(200, {"ok": self.over.get("history_ok", True), "problems": ["x"]}, {}, 1)
        if path.endswith("/blocks/verify"):
            return chaos.Response(200, {"ok": self.over.get("blocks_ok", True), "problems": ["x"]}, {}, 1)
        if "/replay/receipts/" in path:
            return chaos.Response(200, {"ok": self.over.get("receipt_ok", True), "problems": [{"problem": "x"}]}, {}, 1)
        return chaos.Response(200, {"memory": {}}, {}, 1)

    def ensure_receipt(self):
        return self.state["f1"]

    def content(self, what="probe"):
        return f"chaos100x {what} TAG"

    def mint_script(self, *a, **kw):
        return SimpleNamespace(returncode=self.over.get("offline_rc", 0), stdout="", stderr="no")

    def create(self, **kw):
        self.created += 1
        return {"id": "mem-after"}


def good_attempts():
    return [att("w0n1", 200, 0, 1, "mem-0"), att("w0n2", 200, 1, 2, "mem-1"), att("w1n1", 503, 2, 3)]


def test_the_gates_pass_when_everything_acknowledged_is_whole_and_everything_verifies():
    ctx = GateCtx(good_attempts())
    out = chaos.fault_gates(ctx, ctx.attempts, "w")
    assert out == {"attempts": 3, "acknowledged": 2, "failed": 1, "landed_but_unacknowledged": 0} and ctx.created == 1


def test_a_write_that_landed_without_being_acknowledged_is_counted_not_failed():
    a = good_attempts()
    ctx = GateCtx(a, rows="\n".join(f"mem-{i}|{x.content}|create" for i, x in enumerate(a)))
    out = chaos.fault_gates(ctx, a, "w")
    assert out["landed_but_unacknowledged"] == 1


@pytest.mark.parametrize("over,message", [
    ({"rows": "mem-0|someone elses content|create"}, "no request sent"),
    ({"rows": "mem-0|{c0}|create\nmem-0b|{c0}|create"}, "two records"),
    ({"rows": "mem-0|{c0}|create,update"}, "not exactly one create"),
    ({"rows": "mem-0|{c0}|"}, "not exactly one create"),
    ({"rows": ""}, "acknowledged are not in the ledger"),
    ({"rows": "mem-WRONG|{c0}|create\nmem-1|{c1}|create"}, "acknowledged are not in the ledger"),
    ({"orphans": "2"}, r"no history \(a half-write\)"),
    ({"counter_ok": "false"}, "counter and the newest history entry disagree"),
    ({"hist_problems": "1"}, "history verifier"),
    ({"block_problems": "3"}, "block verifier"),
    ({"history_ok": False}, "history does not verify"),
    ({"blocks_ok": False}, "blocks do not verify"),
    ({"receipt_ok": False}, "no longer re-derives"),
    ({"offline_rc": 1}, "offline from the raw rows"),
])
def test_every_gate_trips_on_its_own_failure(over, message):
    a = good_attempts()
    over = dict(over)
    if "rows" in over:
        over["rows"] = over["rows"].replace("{c0}", a[0].content).replace("{c1}", a[1].content)
    with pytest.raises(chaos.ProbeFail, match=message):
        chaos.fault_gates(GateCtx(a, **over), a, "w")


@pytest.mark.postgres
def test_writers_run_concurrently_and_remember_what_each_request_was_told(served):
    stats, args = make_ctx_args(served)
    ctx = chaos.Ctx(rnd=1, rng=__import__("random").Random(1), **{k: v for k, v in args.items() if k != "seed"})
    writers = chaos.Writers(ctx, "t1", n=4).start()
    time.sleep(1.0)
    attempts = writers.stop()
    assert len(attempts) >= 4 and all(a.status == 200 and a.id and a.t1 >= a.t0 for a in attempts)
    assert len({a.content for a in attempts}) == len(attempts)                       # every request carries a distinct, recognisable payload
    assert ctx.state["writes"] == len(attempts)
    rows = {r["id"] for r in ctx.client.get("/api/jarvis/memory?limit=200").json["memories"]}
    assert {a.id for a in attempts if a.id} <= rows or len(attempts) > 200


def test_the_fault_summary_gives_min_median_max_and_totals():
    faults = {"I1": [{"recover_s": 3.0, "detect_s": 0.4, "attempts": 10, "acknowledged": 6, "failed": 4, "statuses_during_fault": {0: 3, 503: 1}},
                     {"recover_s": 9.0, "detect_s": 0.2, "attempts": 12, "acknowledged": 7, "failed": 5, "statuses_during_fault": {0: 4}},
                     {"recover_s": 5.0, "detect_s": 0.3, "attempts": 8, "acknowledged": 5, "failed": 3, "statuses_during_fault": {}}]}
    out = chaos.fault_summary(faults)["I1"]
    assert out["runs"] == 3 and out["recover_s"] == {"min": 3.0, "median": 5.0, "max": 9.0} and out["detect_s"] == {"min": 0.2, "median": 0.3, "max": 0.4}
    assert out["attempts"] == 30 and out["acknowledged"] == 18 and out["failed"] == 12 and out["statuses_during_fault"] == {"0": 7, "503": 1}


def test_the_phases_option_selects_whole_phases(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(chaos, "fetch_ready", lambda url: (200, {"stack": GOOD}))
    monkeypatch.setattr(chaos, "live_ports", lambda: {8011})
    (tmp_path / "secrets").mkdir()
    (tmp_path / "secrets" / "api-key").write_text("k")
    (tmp_path / "stack.json").write_text(json.dumps({"url": "http://127.0.0.1:18017", "port": 18017, "project": "p", "secrets_dir": str(tmp_path / "secrets"),
                                                     "containers": {}, "keys_dir": str(tmp_path), "dir": str(tmp_path)}))
    seen = []
    monkeypatch.setattr(chaos, "run_round", lambda rnd, args, only, log, stats: seen.append(only) or [])
    monkeypatch.setattr(chaos.Client, "request", lambda self, *a, **k: chaos.Response(200, {"history_seq": 0, "tip": None}, {}, 1.0))
    monkeypatch.setattr(chaos, "final_checks", lambda *a, **k: {"history_verify": {"ok": True}, "blocks_verify": {"ok": True}, "attestations_verify": {"ok": True}, "receipts": {"failing_rederivation": []}})
    chaos.main(["--stack-dir", str(tmp_path), "--rounds", "1", "--phases", "i,J,K"])
    assert seen == [{"I1", "J1", "K1"}]
    seen.clear()
    chaos.main(["--stack-dir", str(tmp_path), "--rounds", "1", "--phases", "I", "--only", "A1"])
    assert seen == [{"I1", "A1"}]


def test_the_stack_script_can_put_the_database_on_a_size_capped_tmpfs_volume_held_by_a_keeper():
    yaml = pytest.importorskip("yaml")
    text = STACK_SH.read_text()
    start = text.index("python3 - \"$MINT\"")
    code = text[text.index("\n", start) + 1:text.index("\nPY\n", start)]
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        mint = Path(tmp) / "mint"
        (mint / "bin").mkdir(parents=True)
        for name in ("docker-compose.yml",):
            (mint / name).write_text((ROOT / "deploy" / "mint" / name).read_text())
        (mint / "bin" / "lib.sh").write_text((ROOT / "deploy" / "mint" / "bin" / "lib.sh").read_text())
        for size in ("", "256"):
            shutil_target = Path(tmp) / f"run{size or 'plain'}"
            shutil_target.mkdir()
            (shutil_target / "bin").mkdir()
            (shutil_target / "docker-compose.yml").write_text((mint / "docker-compose.yml").read_text())
            (shutil_target / "bin" / "lib.sh").write_text((mint / "bin" / "lib.sh").read_text())
            r = subprocess.run([sys.executable, "-c", code, str(shutil_target), str(ROOT), "18017", "jarvis-chaos100x", "chaos100x-db", "chaos100x-app", "chaos100x-migrate",
                                "chaos100x-db:test", "chaos100x-app:test", size, "chaos100x-keeper"], capture_output=True, text=True)
            assert r.returncode == 0, r.stderr
            compose = yaml.safe_load((shutil_target / "docker-compose.yml").read_text())
            if not size:
                assert "keeper" not in compose["services"] and compose["volumes"]["pgdata"] in ({}, None)
            else:
                keeper = compose["services"]["keeper"]
                assert keeper["container_name"] == "chaos100x-keeper" and keeper["volumes"] == ["pgdata:/keep"] and keeper["image"] == "chaos100x-db:test"
                opts = compose["volumes"]["pgdata"]["driver_opts"]
                assert opts["type"] == "tmpfs" and opts["o"].startswith("size=256m,")
                assert compose["services"]["db"]["volumes"] == ["pgdata:/var/lib/postgresql/data"]


def test_a_response_cut_off_by_a_dying_server_is_a_failed_request_not_a_crash(monkeypatch):
    import http.client

    class Dying:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        status, headers = 200, {}

        def read(self):
            raise http.client.IncompleteRead(b"", 522)

    monkeypatch.setattr(chaos.urllib.request, "urlopen", lambda *a, **k: Dying())
    stats = chaos.Stats()
    r = chaos.Client("http://127.0.0.1:1", "k", stats).get("/x")
    assert r.status == 0 and stats.five_xx and stats.five_xx[0]["status"] == 0


def test_a_writer_never_dies_silently(monkeypatch):
    class Boom:
        def post(self, *a, **k):
            raise RuntimeError("unexpected")

    ctx = SimpleNamespace(client=Boom(), round=1, tag="T", stats=chaos.Stats(), state={})
    w = chaos.Writers(ctx, "z", n=1, timeout=1).start()
    time.sleep(0.4)
    attempts = w.stop()
    assert attempts and all(a.status == 0 for a in attempts)


# --- `rebuild` is the only way to run `jarvisctl up`, and only on a stack that says it is a throwaway -----------------------------------------------------

@pytest.fixture
def existing_stack(tmp_path, fake_docker):
    bindir, log = fake_docker
    d = tmp_path / "stack"
    (d / "mint" / "bin").mkdir(parents=True)
    (d / ".jarvis-chaos100x").write_text("")
    (d / "mint" / "docker-compose.yml").write_text("name: jarvis-chaos100x\n")
    (d / "stack.json").write_text(json.dumps({"port": 18017}))
    ctl_log = tmp_path / "jarvisctl.log"
    ctl = d / "mint" / "bin" / "jarvisctl"
    ctl.write_text(f"#!/usr/bin/env bash\necho \"jarvisctl $* HOME=$JARVIS_HOME\" >> {ctl_log}\n")
    ctl.chmod(ctl.stat().st_mode | stat.S_IEXEC)
    curl = bindir / "curl"
    curl.write_text(f"#!/usr/bin/env bash\necho \"curl $*\" >> {tmp_path / 'curl.log'}\ncat {tmp_path / 'ready.txt'} 2>/dev/null\n")
    curl.chmod(curl.stat().st_mode | stat.S_IEXEC)
    return SimpleNamespace(dir=d, bindir=bindir, ctl_log=ctl_log, ready=tmp_path / "ready.txt", tmp=tmp_path)


def rebuild(box):
    return stack_sh("rebuild", env={"JARVIS_CHAOS_DIR": str(box.dir)}, bindir=box.bindir)


def test_rebuild_runs_jarvisctl_up_only_when_the_stacks_own_ready_says_throwaway(existing_stack):
    existing_stack.ready.write_text('{"status":"ready","checks":{},"stack":"chaos-throwaway:jarvis-chaos100x"}')
    r = rebuild(existing_stack)
    assert r.returncode == 0, r.stderr
    assert existing_stack.ctl_log.read_text().strip() == f"jarvisctl up HOME={existing_stack.dir}/home"      # in the throwaway's own copy, with its own home


@pytest.mark.parametrize("ready", ["", "not json", "{}", '{"status":"ready","checks":{}}', '{"status":"ready","stack":"jarvis-live"}', '{"stack":""}',
                                   '{"stack":"production"}', '{"stack":"chaos-throwaway"}', "null", '{"stack":["chaos-throwaway:x"]}'])
def test_rebuild_refuses_a_missing_ready_a_ready_with_no_identity_and_jarvis_live(existing_stack, ready):
    existing_stack.ready.write_text(ready)
    r = rebuild(existing_stack)
    assert r.returncode != 0 and "refusing to run jarvisctl up" in r.stderr
    assert not existing_stack.ctl_log.exists()


def test_rebuild_refuses_when_the_stack_does_not_answer_at_all(existing_stack):
    r = rebuild(existing_stack)                                      # no ready.txt: curl prints nothing, as for a connection that is refused
    assert r.returncode != 0 and "refusing to run jarvisctl up" in r.stderr and not existing_stack.ctl_log.exists()


def test_rebuild_refuses_the_live_port_a_foreign_compose_project_and_a_directory_without_the_marker(existing_stack):
    existing_stack.ready.write_text('{"stack":"chaos-throwaway:jarvis-chaos100x"}')
    (existing_stack.dir / "stack.json").write_text(json.dumps({"port": 8011}))
    assert "the live stack's" in rebuild(existing_stack).stderr
    (existing_stack.dir / "stack.json").write_text(json.dumps({"port": 18017}))
    (existing_stack.dir / "mint" / "docker-compose.yml").write_text("name: jarvis-ledger\n")
    assert "not the throwaway project" in rebuild(existing_stack).stderr
    (existing_stack.dir / "mint" / "docker-compose.yml").write_text("name: jarvis-chaos100x\n")
    (existing_stack.dir / ".jarvis-chaos100x").unlink()
    assert "not a throwaway stack directory" in rebuild(existing_stack).stderr
    assert not existing_stack.ctl_log.exists()


def test_the_only_jarvisctl_up_in_the_chaos_tooling_is_guarded():
    text = STACK_SH.read_text()
    ups = [l.strip() for l in text.splitlines() if re.search(r'jarvisctl"\s+up\b', l)]
    assert len(ups) == 2                                                # the creation of a new stack, and rebuild
    new_stack = text[:text.index("rebuild() {")]
    assert new_stack.index("JARVIS_STACK_ID=chaos-throwaway:") < new_stack.index('"$MINT/bin/jarvisctl" up')   # a new stack's identity is verified first
    for path in [SCRIPT, ROOT / "scripts" / "chaos" / "soak.py"]:
        assert "jarvisctl" not in path.read_text() or "jarvisctl\", \"verify\"" in path.read_text() or '/jarvisctl"' in path.read_text()
        assert not re.search(r"jarvisctl.{0,12}\bup\b", path.read_text())      # the hammer and the soak never bring a stack up


def test_the_chaos_tooling_leaves_signatures_in_warn_mode_and_never_touches_the_sign_timer():
    for path in [SCRIPT, STACK_SH, ROOT / "scripts" / "chaos" / "soak.py"]:
        text = path.read_text()
        assert "JARVIS_SIGNATURES=require" not in text and "JARVIS_SIGNATURES: require" not in text, path
        assert "jarvis-sign.timer" not in text and "enable --now" not in text and "install-units" not in text, path
    assert "mode is warn" in next(p.title for p in chaos.PROBES if p.id == "G0")
    assert "expected warn" in SCRIPT.read_text()                       # G0 fails if the throwaway ever runs in another mode
