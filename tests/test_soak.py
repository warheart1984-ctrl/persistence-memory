"""The soak tool: its arithmetic and its verdicts on synthetic data (a leak, a cache, an accumulation), the same refusals as the hammer, and a short real
run against an in-process service."""

from __future__ import annotations

import importlib.util
import json
import math
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("soak", ROOT / "scripts" / "chaos" / "soak.py")
soak = importlib.util.module_from_spec(spec)
sys.modules["soak"] = soak
spec.loader.exec_module(soak)
chaos = soak.chaos


def test_linear_fit_recovers_a_line_and_is_safe_on_degenerate_input():
    f = soak.linear_fit([0, 1, 2, 3, 4], [1, 3, 5, 7, 9])
    assert f["slope"] == pytest.approx(2.0) and f["intercept"] == pytest.approx(1.0) and f["r2"] == pytest.approx(1.0)
    assert soak.linear_fit([0, 1, 2, 3], [5, 5, 5, 5]) == {"slope": 0.0, "intercept": 5.0, "r2": 0.0, "n": 4}
    assert soak.linear_fit([1, 1, 1], [1, 2, 3])["slope"] == 0.0 and soak.linear_fit([1, 2], [1, 2])["r2"] == 0.0 and soak.linear_fit([], [])["n"] == 0
    noisy = soak.linear_fit(list(range(10)), [2 * x + (1 if x % 2 else -1) for x in range(10)])
    assert 0.9 < noisy["r2"] < 1.0 and noisy["slope"] == pytest.approx(2.0, abs=0.2)


def test_the_power_law_exponent_tells_linear_from_quadratic_from_flat():
    xs = [100, 200, 400, 800, 1600, 3200]
    assert soak.power_law_exponent(xs, [3 * x for x in xs])["exponent"] == pytest.approx(1.0)
    assert soak.power_law_exponent(xs, [0.01 * x * x for x in xs])["exponent"] == pytest.approx(2.0)
    assert abs(soak.power_law_exponent(xs, [50.0] * len(xs))["exponent"]) < 0.01
    assert soak.power_law_exponent([0, 0, 0], [1, 2, 3])["n"] == 0                    # zero or negative values are dropped, not fitted


def mem(mib, requests=0, records=0, **extra):
    return {"RssAnon": int(mib * 1024), "VmRSS": int((mib + 25) * 1024), "requests": requests, "records": records, **extra}


def growth_series(first_ms=20, exponent=1.0):
    return [mem(60 + i * 2, 0, 1000 * (i + 1), retrieve_typical_ms=first_ms * (i + 1) ** exponent, retrieve_hostile_ms=first_ms * (i + 1) ** exponent,
                blocks_verify_ms=30 * (i + 1), history_verify_ms=10 * (i + 1)) for i in range(8)]


def test_a_leak_is_called_a_leak_only_when_memory_follows_requests_and_a_restart_gets_it_back():
    reads = [mem(100 + i * 5, 10_000 * i) for i in range(10)]                     # +5 MiB per 10k requests on a constant ledger
    v = soak.verdict(growth_series(), reads, [mem(145, 100_000)], [mem(70, 0), mem(72, 50_000)])
    assert v["memory_vs_requests_on_a_constant_ledger"]["mib_per_100k_requests"] == pytest.approx(50, rel=0.01)
    assert any(n.startswith("LEAK SUSPECTED") for n in v["notes"]) and v["restart_gap_fraction"] > 0.4


def test_a_level_that_is_flat_against_requests_and_returns_after_a_restart_is_not_a_leak():
    reads = [mem(120 + (i % 2) * 0.2, 10_000 * i) for i in range(10)]
    v = soak.verdict(growth_series(), reads, [mem(120, 90_000)], [mem(119, 0), mem(121, 50_000)])
    assert any(n.startswith("NOT A LEAK") for n in v["notes"]) and not any("LEAK SUSPECTED" in n for n in v["notes"])
    assert abs(v["restart_gap_fraction"]) <= 0.05


def test_growth_with_requests_that_converges_on_the_same_level_after_a_restart_is_called_a_cache():
    reads = [mem(100 + i * 0.8, 10_000 * i) for i in range(10)]                    # still creeping, +0.8 MiB per 10k
    v = soak.verdict(growth_series(), reads, [mem(107, 100_000)], [mem(104, 0), mem(108, 50_000)])
    assert not any("LEAK SUSPECTED" in n for n in v["notes"])


def test_a_restart_that_uses_far_less_with_no_growth_is_reported_as_accumulation_not_proven_either_way():
    reads = [mem(200, 10_000 * i) for i in range(10)]
    v = soak.verdict(growth_series(), reads, [mem(200, 100_000)], [mem(90, 0), mem(92, 50_000)])
    assert any("accumulation over the process's life" in n for n in v["notes"]) and not any("LEAK SUSPECTED" in n for n in v["notes"])


def test_latency_shapes_are_named_from_the_exponent():
    lin = soak.verdict(growth_series(exponent=1.0), [], [], [])["latency_vs_records"]["retrieve_typical_ms"]
    quad = soak.verdict(growth_series(exponent=2.0), [], [], [])["latency_vs_records"]["retrieve_typical_ms"]
    flat = soak.verdict(growth_series(exponent=0.0), [], [], [])["latency_vs_records"]["retrieve_typical_ms"]
    assert lin["exponent"] == pytest.approx(1.0, abs=0.05) and quad["exponent"] == pytest.approx(2.0, abs=0.05) and abs(flat["exponent"]) < 0.05
    notes = soak.verdict(growth_series(exponent=1.0), [], [], [])["notes"]
    assert any("retrieve_typical_ms" in n and "about linear" in n for n in notes)
    assert any("super-linear" in n for n in soak.verdict(growth_series(exponent=2.0), [], [], [])["notes"])


def test_the_verdict_survives_missing_phases():
    assert soak.verdict([], [], [], [])["notes"] == []
    v = soak.verdict(growth_series(), [], [], [])
    assert "restart_gap_fraction" not in v and "memory_vs_records" in v


def test_process_memory_reads_the_main_process_from_inside_the_container():
    status = "Name:\tuvicorn\nVmHWM:\t  120000 kB\nVmRSS:\t  100000 kB\nRssAnon:\t   60000 kB\nRssFile:\t   40000 kB\nThreads:\t10\n"
    calls = []
    out = soak.process_memory("chaos100x-app", runner=lambda args, **kw: calls.append(args) or SimpleNamespace(returncode=0, stdout=status, stderr=""))
    assert out == {"VmRSS": 100000, "RssAnon": 60000, "RssFile": 40000, "VmHWM": 120000, "Threads": 10}
    assert calls == [["docker", "exec", "chaos100x-app", "cat", "/proc/1/status"]]
    assert soak.process_memory("x", runner=lambda a, **k: SimpleNamespace(returncode=1, stdout="", stderr="")) == {}


def test_the_soak_refuses_the_live_port_and_an_unproven_target_before_doing_anything(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(chaos, "live_ports", lambda: {8011})

    def boom(*a, **k):
        raise AssertionError("a command was run")
    monkeypatch.setattr(chaos, "run_cmd", boom)
    (tmp_path / "stack.json").write_text(json.dumps({"url": "http://127.0.0.1:8011", "port": 8011, "project": "p", "secrets_dir": str(tmp_path), "containers": {}, "keys_dir": str(tmp_path)}))
    assert soak.main(["--stack-dir", str(tmp_path)]) == chaos.EXIT_REFUSED
    assert "live stack's port" in capsys.readouterr().err
    monkeypatch.setattr(chaos, "fetch_ready", lambda url: (200, {}))
    (tmp_path / "stack.json").write_text(json.dumps({"url": "http://127.0.0.1:18017", "port": 18017, "project": "p", "secrets_dir": str(tmp_path), "containers": {}, "keys_dir": str(tmp_path)}))
    assert soak.main(["--stack-dir", str(tmp_path)]) == chaos.EXIT_REFUSED
    assert "no stack identity" in capsys.readouterr().err
    assert soak.main(["--stack-dir", str(tmp_path / "nothing")]) == chaos.EXIT_REFUSED


@pytest.mark.postgres
def test_a_short_real_soak(pg_schema, tmp_path_factory, tmp_path, monkeypatch):
    import socket
    import threading
    import time

    uvicorn = pytest.importorskip("uvicorn")
    from app import pg_store
    from app.main import app
    from app.pg_schema import EXPECTED_SCHEMA_VERSION, migrate

    assert migrate(pg_schema.admin_dsn, schema=pg_schema.schema, app_role="jarvis_app_test") == EXPECTED_SCHEMA_VERSION
    key = "soak-test-key"
    for k, v in (("JARVIS_DATABASE_URL", pg_schema.app_dsn), ("JARVIS_DATABASE_SCHEMA", pg_schema.schema), ("JARVIS_PG_STORE", "rows"), ("JARVIS_API_KEY", key),
                 ("JARVIS_MEMORY_WRITE_ENABLED", "true"), ("JARVIS_STACK_ID", "chaos-throwaway:jarvis-chaos100x"), ("JARVIS_CLAUSE_V", "enforce")):
        monkeypatch.setenv(k, v)
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.1)
    try:
        secrets = tmp_path / "secrets"
        secrets.mkdir()
        (secrets / "api-key").write_text(key)
        stack = {"url": f"http://127.0.0.1:{port}", "port": port, "project": "jarvis-chaos100x", "secrets_dir": str(secrets), "keys_dir": str(tmp_path),
                 "containers": {"app": "chaos100x-app", "db": "chaos100x-db"}}
        (tmp_path / "stack.json").write_text(json.dumps(stack))
        monkeypatch.setattr(chaos, "live_ports", lambda: {8011})
        monkeypatch.setattr(chaos, "throwaway_proof", lambda *a, **k: None)
        calls = []
        monkeypatch.setattr(chaos, "run_cmd", lambda args, **kw: calls.append(args) or SimpleNamespace(returncode=0, stdout="", stderr=""))
        monkeypatch.setattr(soak, "restart_and_wait", lambda stack, target, **kw: calls.append(["docker", "restart", stack["containers"]["app"]]) and None)
        ticks = iter(range(10_000))
        monkeypatch.setattr(soak, "process_memory", lambda c, runner=None: {"RssAnon": 60000 + next(ticks) * 10, "VmRSS": 90000})
        monkeypatch.setattr(soak, "database_mb", lambda *a, **k: 1.5)
        out = tmp_path / "out"
        rc = soak.main(["--stack-dir", str(tmp_path), "--out", str(out), "--growth-min", "0.1", "--reads-min", "0.05", "--idle-min", "0.02", "--control-min", "0.05",
                        "--tick-s", "1", "--records-per-tick", "5", "--sample-s", "2"])
        assert rc == 0
        result = json.loads((out / "soak.json").read_text())
        assert result["records_written"] >= 5 and result["samples"]["growth"] and result["samples"]["reads"] and result["samples"]["control"]
        assert ["docker", "restart", "chaos100x-app"] in calls                      # the control restarts only the throwaway's application container
        assert all(a[0] == "docker" and "jarvis-app" not in a and "jarvis-db" not in a for a in calls)
        assert "notes" in result["verdict"] and result["five_xx"] == 0
        assert (out / "soak.log").read_text().count("anon") >= 3
    finally:
        server.should_exit = True
        thread.join(15)
        pg_store.close_pools()


def test_a_noisy_rise_is_not_called_a_leak_even_when_a_restart_gets_a_lot_back():
    """Memory that jumps about (r2 low) is not a trend, whatever the endpoints say."""
    values = [100, 140, 100, 140, 100, 140, 100, 140, 100, 141]
    reads = [mem(v, 10_000 * i) for i, v in enumerate(values)]
    fit = soak.linear_fit([r["requests"] for r in reads], [r["RssAnon"] / 1024 for r in reads])
    assert fit["r2"] < 0.6 and fit["slope"] * 100_000 > 2.0 and values[-1] - values[0] > 2.0          # the premise of this test
    v = soak.verdict(growth_series(), reads, [mem(120, 100_000)], [mem(60, 0), mem(62, 50_000)])
    assert v["restart_gap_fraction"] > 0.15 and not any("LEAK SUSPECTED" in n for n in v["notes"])


def test_a_perfect_but_tiny_slope_is_not_called_a_leak():
    """+3 MiB over ten million requests is a rounding error, however straight the line."""
    reads = [mem(100 + 3 * i / 9, 1_000_000 * i) for i in range(10)]
    f = soak.verdict(growth_series(), reads, [mem(103, 9_000_000)], [mem(60, 0), mem(62, 50_000)])["memory_vs_requests_on_a_constant_ledger"]
    assert f["r2"] > 0.99 and f["mib_per_100k_requests"] < 2.0 and f["last_mib"] - f["first_mib"] > 2.0     # the premise
    v = soak.verdict(growth_series(), reads, [mem(103, 9_000_000)], [mem(60, 0), mem(62, 50_000)])
    assert not any("LEAK SUSPECTED" in n for n in v["notes"])


def test_a_steep_slope_over_a_tiny_total_rise_is_not_called_a_leak():
    """A line can be steep over a short run and still be 1.5 MiB: below the size at which a trend means anything."""
    reads = [mem(100 + 1.5 * i / 9, 5_000 * i) for i in range(10)]
    f = soak.verdict(growth_series(), reads, [mem(101.5, 50_000)], [mem(60, 0), mem(62, 50_000)])["memory_vs_requests_on_a_constant_ledger"]
    assert f["r2"] > 0.99 and f["mib_per_100k_requests"] > 2.0 and f["last_mib"] - f["first_mib"] < 2.0    # the premise
    v = soak.verdict(growth_series(), reads, [mem(101.5, 50_000)], [mem(60, 0), mem(62, 50_000)])
    assert not any("LEAK SUSPECTED" in n for n in v["notes"])


def test_a_high_water_mark_that_follows_the_callers_is_named_as_such():
    levels = [{"callers": c, "hwm_mib": 90 + 4 * c, "anon_mib": 80 + 4 * c} for c in (1, 4, 16, 40)]
    out = soak.concurrency_note(levels)
    assert out["mib_per_caller"] == pytest.approx(4.0, abs=0.01) and out["r2"] > 0.99
    assert "follows the number of simultaneous callers" in out["notes"][0] and "without any request leaking" in out["notes"][0]


def test_a_flat_high_water_mark_is_not_called_a_function_of_the_callers():
    out = soak.concurrency_note([{"callers": c, "hwm_mib": 100 + (c % 2), "anon_mib": 90} for c in (1, 4, 16, 40)])
    assert "does not follow" in out["notes"][0]
    assert "does not follow" in soak.concurrency_note([{"callers": c, "hwm_mib": 100 + 0.01 * c, "anon_mib": 90} for c in (1, 4, 16, 40)])["notes"][0]     # a steep line over nothing
    assert soak.concurrency_note([{"callers": 1, "hwm_mib": 100, "anon_mib": 90}])["notes"] == []



# --- the control must be a real restart --------------------------------------------------------------------------------------------------------------

def restart_runner(*, restart_rc=0, started=("T1", "T2")):
    seq = iter(started)
    calls = []

    def run(args, **kw):
        calls.append(args)
        if args[:3] == ["docker", "inspect", "-f"]:
            return SimpleNamespace(returncode=0, stdout=next(seq) + "\n", stderr="")
        return SimpleNamespace(returncode=restart_rc, stdout="", stderr="no such container" if restart_rc else "")
    run.calls = calls
    return run


STACK_APP = {"containers": {"app": "chaos100x-app"}}
TARGET_URL = {"url": "http://127.0.0.1:18017"}


def test_a_restart_that_worked_is_a_new_process_that_answers_ready():
    r = restart_runner()
    assert soak.restart_and_wait(STACK_APP, TARGET_URL, runner=r, ready=lambda url: (200, {}), sleep=lambda s: None) is None
    assert ["docker", "restart", "chaos100x-app"] in r.calls


def test_a_failed_restart_is_reported_and_readiness_is_not_even_waited_for():
    r = restart_runner(restart_rc=1)
    why = soak.restart_and_wait(STACK_APP, TARGET_URL, runner=r, ready=lambda url: (_ for _ in ()).throw(AssertionError("readiness was polled")), sleep=lambda s: None)
    assert why and "docker restart chaos100x-app failed" in why


def test_a_restart_that_never_becomes_ready_is_reported():
    polls = []
    why = soak.restart_and_wait(STACK_APP, TARGET_URL, runner=restart_runner(), ready_timeout=5, ready=lambda url: polls.append(1) or (503, {}), sleep=lambda s: None)
    assert why and "did not answer /ready within 5 s" in why and len(polls) == 5


def test_an_unchanged_start_time_means_the_old_process_answered_and_is_not_a_control():
    why = soak.restart_and_wait(STACK_APP, TARGET_URL, runner=restart_runner(started=("T1", "T1")), ready=lambda url: (200, {}), sleep=lambda s: None)
    assert why and "start time did not change" in why
    assert "start time did not change" in soak.restart_and_wait(STACK_APP, TARGET_URL, runner=restart_runner(started=("T1", "")), ready=lambda url: (200, {}), sleep=lambda s: None)


def test_the_soak_aborts_with_a_failure_and_no_verdict_when_the_control_cannot_be_made(monkeypatch, tmp_path):
    import threading
    import time

    monkeypatch.setattr(chaos, "live_ports", lambda: {8011})
    monkeypatch.setattr(chaos, "throwaway_proof", lambda *a, **k: None)
    monkeypatch.setattr(chaos, "fetch_ready", lambda url: (200, {"stack": "chaos-throwaway:jarvis-chaos100x"}))
    (tmp_path / "secrets").mkdir()
    (tmp_path / "secrets" / "api-key").write_text("k")
    (tmp_path / "stack.json").write_text(json.dumps({"url": "http://127.0.0.1:18017", "port": 18017, "project": "p", "secrets_dir": str(tmp_path / "secrets"), "keys_dir": str(tmp_path),
                                                     "containers": {"app": "chaos100x-app", "db": "chaos100x-db"}}))
    monkeypatch.setattr(chaos.Client, "request", lambda self, *a, **k: chaos.Response(200, {"history_seq": 0, "tip": None, "memory": {"id": "mem-1"}}, {}, 1.0))
    monkeypatch.setattr(soak, "process_memory", lambda c, runner=None: {"RssAnon": 60000, "VmRSS": 90000})
    monkeypatch.setattr(soak, "database_mb", lambda *a, **k: 1.0)
    monkeypatch.setattr(soak, "restart_and_wait", lambda stack, target, **k: "docker restart chaos100x-app failed: boom")
    monkeypatch.setattr(soak.time, "sleep", lambda s: None)
    out = tmp_path / "out"
    rc = soak.main(["--stack-dir", str(tmp_path), "--out", str(out), "--growth-min", "0.001", "--reads-min", "0.001", "--idle-min", "0.001", "--control-min", "0.001",
                    "--tick-s", "0.01", "--records-per-tick", "1", "--sample-s", "0.01"])
    assert rc == chaos.EXIT_PROBE_FAILURES
    result = json.loads((out / "soak.json").read_text())
    assert result["aborted"].endswith("boom") and "verdict" not in result
    assert "ABORTED" in (out / "soak.log").read_text()


def test_the_concurrency_phase_aborts_too_when_it_cannot_get_a_restarted_process(monkeypatch, tmp_path):
    monkeypatch.setattr(chaos, "live_ports", lambda: {8011})
    monkeypatch.setattr(chaos, "throwaway_proof", lambda *a, **k: None)
    monkeypatch.setattr(chaos, "fetch_ready", lambda url: (200, {"stack": "chaos-throwaway:jarvis-chaos100x"}))
    (tmp_path / "secrets").mkdir()
    (tmp_path / "secrets" / "api-key").write_text("k")
    (tmp_path / "stack.json").write_text(json.dumps({"url": "http://127.0.0.1:18017", "port": 18017, "project": "p", "secrets_dir": str(tmp_path / "secrets"), "keys_dir": str(tmp_path),
                                                     "containers": {"app": "chaos100x-app", "db": "chaos100x-db"}}))
    monkeypatch.setattr(chaos.Client, "request", lambda self, *a, **k: chaos.Response(200, {"history_seq": 5, "tip": None, "memories": []}, {}, 1.0))
    started = []
    monkeypatch.setattr(soak, "Load", lambda *a, **k: started.append(1))
    monkeypatch.setattr(soak, "restart_and_wait", lambda stack, target, **k: "the container's start time did not change")
    out = tmp_path / "out"
    rc = soak.main(["--stack-dir", str(tmp_path), "--concurrency", "--out", str(out)])
    assert rc == chaos.EXIT_PROBE_FAILURES and started == [] and not (out / "concurrency.json").exists()


# --- a soak with failed requests or unreadable memory is not a valid soak -------------------------------------------------------------------------------

def test_an_unreadable_memory_measurement_is_retried_and_then_stops_the_run_it_is_never_zero():
    answers = iter([SimpleNamespace(returncode=1, stdout="", stderr="x"), SimpleNamespace(returncode=0, stdout="VmRSS:\t 1 kB\n", stderr=""),   # no RssAnon: unusable
                    SimpleNamespace(returncode=0, stdout="VmRSS:\t 100000 kB\nRssAnon:\t 60000 kB\n", stderr="")])
    slept = []
    out = soak.process_memory_checked("c", runner=lambda a, **k: next(answers), sleep=slept.append)
    assert out["RssAnon"] == 60000 and len(slept) == 2
    with pytest.raises(soak.SoakError, match="cannot read the application's memory from c"):
        soak.process_memory_checked("c", runner=lambda a, **k: SimpleNamespace(returncode=1, stdout="", stderr=""), sleep=lambda s: None)


def test_a_timed_request_that_is_not_a_200_is_a_failure_not_a_timing():
    f = soak.Failures()
    soak.timed(lambda: chaos.Response(200, {}, {}, 1.0), f, "retrieve")
    assert not f
    soak.timed(lambda: chaos.Response(503, {}, {}, 1.0), f, "retrieve")
    soak.median_ms(lambda: chaos.Response(0, None, {}, 1.0), n=2, failures=f, what="blocks/verify")
    assert f and f.summary() == {"count": 3, "by_kind": {"retrieve:503": 1, "blocks/verify:0": 2}}
    assert soak.timed(lambda: None) >= 0                                           # a call with no response object still times


def soak_env(monkeypatch, tmp_path, request_fn, memory=None):
    monkeypatch.setattr(chaos, "live_ports", lambda: {8011})
    monkeypatch.setattr(chaos, "throwaway_proof", lambda *a, **k: None)
    monkeypatch.setattr(chaos, "fetch_ready", lambda url: (200, {"stack": "chaos-throwaway:jarvis-chaos100x"}))
    (tmp_path / "secrets").mkdir()
    (tmp_path / "secrets" / "api-key").write_text("k")
    (tmp_path / "stack.json").write_text(json.dumps({"url": "http://127.0.0.1:18017", "port": 18017, "project": "p", "secrets_dir": str(tmp_path / "secrets"), "keys_dir": str(tmp_path),
                                                     "containers": {"app": "chaos100x-app", "db": "chaos100x-db"}}))
    monkeypatch.setattr(chaos.Client, "request", request_fn)
    monkeypatch.setattr(soak, "process_memory", memory or (lambda c, runner=None: {"RssAnon": 60000, "VmRSS": 90000}))
    monkeypatch.setattr(soak, "database_mb", lambda *a, **k: 1.0)
    monkeypatch.setattr(soak, "restart_and_wait", lambda stack, target, **k: None)
    monkeypatch.setattr(soak.time, "sleep", lambda s: None)
    return ["--stack-dir", str(tmp_path), "--growth-min", "0.001", "--reads-min", "0.001", "--idle-min", "0.001", "--control-min", "0.001", "--tick-s", "0.01",
            "--records-per-tick", "2", "--sample-s", "0.01"]


def ok_response(method, path, body):
    return chaos.Response(200, {"history_seq": 0, "tip": None, "memory": {"id": "mem-1"}, "memories": [], "ok": True}, {}, 1.0)


def test_a_clean_soak_exits_zero_and_has_no_invalid_marker(monkeypatch, tmp_path):
    args = soak_env(monkeypatch, tmp_path, lambda self, method, path, body=None, **k: ok_response(method, path, body))
    out = tmp_path / "out"
    assert soak.main([*args, "--out", str(out)]) == 0
    assert "invalid" not in json.loads((out / "soak.json").read_text())


@pytest.mark.parametrize("failing,what", [("POST /api/jarvis/memory", "growth write"), ("POST /api/jarvis/blocks/seal", "seal"), ("GET /api/jarvis/blocks/verify", "blocks/verify"),
                                          ("GET /api/jarvis/memory/history/verify", "history/verify"), ("GET /api/jarvis/memory/retrieve", "retrieve")])
def test_a_request_the_soak_needed_that_failed_makes_the_run_invalid_and_the_exit_nonzero(monkeypatch, tmp_path, failing, what, capsys):
    method, route = failing.split()

    def request(self, m, path, body=None, **k):
        if m == method and path.startswith(route):
            return chaos.Response(503, None, {}, 1.0)
        return ok_response(m, path, body)

    out = tmp_path / "out"
    rc = soak.main([*soak_env(monkeypatch, tmp_path, request), "--out", str(out)])
    result = json.loads((out / "soak.json").read_text())
    assert rc == chaos.EXIT_PROBE_FAILURES and result["invalid"]["count"] > 0
    assert any(key.startswith(what) for key in result["invalid"]["by_kind"]), result["invalid"]
    assert "INVALID RUN" in (out / "soak.log").read_text()


def test_read_load_failures_make_the_run_invalid(monkeypatch, tmp_path):
    def request(self, m, path, body=None, **k):
        if m == "GET" and path.startswith("/api/jarvis/blocks/head") and getattr(request, "calls", 0) > 0:
            return chaos.Response(503, None, {}, 1.0)
        request.calls = getattr(request, "calls", 0) + 1
        return ok_response(m, path, body)

    out = tmp_path / "out"
    rc = soak.main([*soak_env(monkeypatch, tmp_path, request), "--out", str(out)])
    result = json.loads((out / "soak.json").read_text())
    assert rc == chaos.EXIT_PROBE_FAILURES and any(k.startswith("read load") or k.startswith("control read load") for k in result["invalid"]["by_kind"])


def test_unreadable_memory_aborts_the_soak_with_no_verdict(monkeypatch, tmp_path):
    out = tmp_path / "out"
    rc = soak.main([*soak_env(monkeypatch, tmp_path, lambda self, m, p, body=None, **k: ok_response(m, p, body), memory=lambda c, runner=None: {}), "--out", str(out)])
    result = json.loads((out / "soak.json").read_text())
    assert rc == chaos.EXIT_PROBE_FAILURES and "cannot read the application's memory" in result["aborted"] and "verdict" not in result


def test_a_ledger_that_does_not_answer_before_the_soak_begins_is_refused(monkeypatch, tmp_path, capsys):
    rc = soak.main([*soak_env(monkeypatch, tmp_path, lambda self, m, p, body=None, **k: chaos.Response(503, None, {}, 1.0))])
    assert rc == chaos.EXIT_PROBE_FAILURES and "before the soak began" in capsys.readouterr().err


def test_the_concurrency_phase_stops_when_memory_is_unreadable_or_a_level_answered_nothing(monkeypatch, tmp_path, capsys):
    def request(self, m, p, body=None, **k):
        return chaos.Response(200, {"history_seq": 3, "tip": None, "memories": [{"id": "mem-1"}]}, {}, 1.0)

    args = soak_env(monkeypatch, tmp_path, request, memory=lambda c, runner=None: {})
    assert soak.main([*args, "--concurrency"]) == chaos.EXIT_PROBE_FAILURES
    assert "ABORTED" in capsys.readouterr().err


class FakeLoad:
    """A Load that has already 'run': counts and errors are set by the test, one instance per phase in the order the soak creates them."""

    plan: list = []

    def __init__(self, client, record_id, threads=4):
        self.count, self.errors = FakeLoad.plan.pop(0)

    def start(self):
        return self

    def stop(self):
        pass


@pytest.mark.parametrize("plan,expected", [([(100, 3), (100, 0)], "read load"), ([(100, 0), (100, 4)], "control read load")])
def test_load_errors_in_either_phase_make_the_run_invalid_on_their_own(monkeypatch, tmp_path, plan, expected):
    FakeLoad.plan = list(plan)
    monkeypatch.setattr(soak, "Load", FakeLoad)
    out = tmp_path / "out"
    rc = soak.main([*soak_env(monkeypatch, tmp_path, lambda self, m, p, body=None, **k: ok_response(m, p, body)), "--out", str(out)])
    result = json.loads((out / "soak.json").read_text())
    assert rc == chaos.EXIT_PROBE_FAILURES
    assert list(result["invalid"]["by_kind"]) == [f"{expected}:{plan[0][1] or plan[1][1]} of 100 not 200"], result["invalid"]


def test_a_concurrency_level_where_nothing_was_answered_stops_the_run(monkeypatch, tmp_path, capsys):
    FakeLoad.plan = [(5, 5)]
    monkeypatch.setattr(soak, "Load", FakeLoad)
    args = soak_env(monkeypatch, tmp_path, lambda self, m, p, body=None, **k: chaos.Response(200, {"history_seq": 3, "tip": None, "memories": [{"id": "mem-1"}]}, {}, 1.0))
    out = tmp_path / "out"
    assert soak.main([*args, "--concurrency", "--out", str(out)]) == chaos.EXIT_PROBE_FAILURES
    assert "no request was answered with 200 at 1 callers" in capsys.readouterr().err and not (out / "concurrency.json").exists()


@pytest.mark.parametrize("head,retrieve,why", [
    (chaos.Response(503, None, {}, 1.0), chaos.Response(200, {"memories": [{"id": "mem-1"}]}, {}, 1.0), "blocks/head answered 503"),
    (chaos.Response(200, {}, {}, 1.0), chaos.Response(200, {"memories": [{"id": "mem-1"}]}, {}, 1.0), "blocks/head answered 200"),
    (chaos.Response(200, {"history_seq": 3}, {}, 1.0), chaos.Response(503, None, {}, 1.0), "no record to read: retrieve answered 503"),
    (chaos.Response(200, {"history_seq": 3}, {}, 1.0), chaos.Response(200, {"memories": []}, {}, 1.0), "no record to read: retrieve answered 200 with no records"),
])
def test_the_concurrency_phase_checks_its_setup_responses_and_never_substitutes_a_fake_record(monkeypatch, tmp_path, capsys, head, retrieve, why):
    started = []
    monkeypatch.setattr(soak, "Load", lambda *a, **k: started.append(1))

    def request(self, m, path, body=None, **k):
        return head if "/blocks/head" in path else retrieve

    args = soak_env(monkeypatch, tmp_path, request)
    assert soak.main([*args, "--concurrency"]) == chaos.EXIT_PROBE_FAILURES
    assert why in capsys.readouterr().err and started == []


def test_every_concurrency_level_gets_its_own_restarted_process(monkeypatch, tmp_path):
    FakeLoad.plan = [(100, 0)] * 4
    monkeypatch.setattr(soak, "Load", FakeLoad)
    args = soak_env(monkeypatch, tmp_path, lambda self, m, p, body=None, **k: chaos.Response(200, {"history_seq": 3, "tip": None, "memories": [{"id": "mem-1"}]}, {}, 1.0))
    restarts, order = [], []
    monkeypatch.setattr(soak, "restart_and_wait", lambda stack, target, **k: restarts.append(1))
    monkeypatch.setattr(soak, "process_memory_checked", lambda c: order.append(len(restarts)) or {"VmHWM": 100000, "RssAnon": 60000})
    assert soak.main([*args, "--concurrency"]) == chaos.EXIT_OK
    assert len(restarts) == 5                                 # the baseline and each of 1, 4, 16, 40 callers
    assert order == [1, 2, 3, 4, 5]                           # memory is read on the process that level was run on


def test_a_mostly_refused_level_is_marked_invalid_and_kept_out_of_the_analysis(monkeypatch, tmp_path, capsys):
    FakeLoad.plan = [(100, 0), (100, 0), (100, 0), (1273, 1030)]
    monkeypatch.setattr(soak, "Load", FakeLoad)
    args = soak_env(monkeypatch, tmp_path, lambda self, m, p, body=None, **k: chaos.Response(200, {"history_seq": 3, "tip": None, "memories": [{"id": "mem-1"}]}, {}, 1.0))
    out = tmp_path / "out"
    rc = soak.main([*args, "--concurrency", "--out", str(out)])
    result = json.loads((out / "concurrency.json").read_text())
    by = {l["callers"]: l for l in result["levels"]}
    assert rc == chaos.EXIT_OK and by[40]["valid"] is False and by[16]["valid"] is True
    assert result["analysis"]["invalid_levels_excluded"] == [40] and 40 not in [l["callers"] for l in result["analysis"]["levels"]]
    assert "INVALID" in capsys.readouterr().out


def test_fewer_than_three_valid_levels_claims_nothing_and_fails(monkeypatch, tmp_path):
    FakeLoad.plan = [(100, 0), (100, 0), (100, 90), (100, 90)]
    monkeypatch.setattr(soak, "Load", FakeLoad)
    args = soak_env(monkeypatch, tmp_path, lambda self, m, p, body=None, **k: chaos.Response(200, {"history_seq": 3, "tip": None, "memories": [{"id": "mem-1"}]}, {}, 1.0))
    out = tmp_path / "out"
    assert soak.main([*args, "--concurrency", "--out", str(out)]) == chaos.EXIT_PROBE_FAILURES
    notes = json.loads((out / "concurrency.json").read_text())["analysis"]["notes"]
    assert "fewer than three valid levels" in notes[0]
