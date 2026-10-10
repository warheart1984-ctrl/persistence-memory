"""scripts/emr_latest_crosscheck.py: it can only ever run on a scratch stack, and it proves the discovery claim."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "emr_latest_crosscheck.py"


@pytest.fixture(scope="module")
def xcheck():
    spec = importlib.util.spec_from_file_location("emr_latest_crosscheck", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    "target",
    ["8011", "127.0.0.1:8011", "localhost:8011", "http://127.0.0.1:8011/mcp", "http://localhost:8011", "http://[::1]:8011/health",
     "http://127.1:8011/", "https://ledger.example:8011", "8001", "http://127.0.0.1:8002/health"],
)
def test_the_live_ledger_ports_are_refused_on_every_spelling(xcheck, target):
    with pytest.raises(xcheck.CrosscheckError, match="live ledger"):
        xcheck.refuse_live(target)
    if not target.isdigit():
        with pytest.raises(xcheck.CrosscheckError):
            xcheck.http("GET", target if "://" in target else f"http://{target}")  # the request layer refuses before connecting


@pytest.mark.parametrize("target", ["8012", "127.0.0.1:60123", "http://127.0.0.1:18011/mcp"])
def test_other_ports_are_not_refused(xcheck, target):
    xcheck.refuse_live(target)


def test_the_script_has_no_way_to_name_an_existing_server(xcheck):
    text = SCRIPT.read_text(encoding="utf-8")
    import re

    assert re.findall(r'add_argument\("(--[a-z-]+)"', text) == ["--out", "--backend"]  # nothing that names a server
    assert xcheck.free_port() not in xcheck.LIVE_PORTS


def test_a_remote_or_live_port_database_is_not_a_scratch_database(xcheck):
    ok, why = xcheck.scratch_dsn_ok("postgresql://u:p@db.example.com:5432/x")
    assert not ok and "loopback" in why
    ok, why = xcheck.scratch_dsn_ok("postgresql://u:p@127.0.0.1:8011/x")
    assert not ok and "live ledger" in why
    assert xcheck.scratch_dsn_ok("postgresql://u:p@127.0.0.1:55432/x")[0]


def _run(backend: str, tmp_path):
    out = tmp_path / "report.json"
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "--backend", backend, "--out", str(out)],
        cwd=str(tmp_path), capture_output=True, text=True, encoding="utf-8", timeout=240, env={**os.environ, "PYTHONUTF8": "1"},
    )
    assert out.exists(), proc.stdout + proc.stderr
    return proc, json.loads(out.read_text(encoding="utf-8"))


def _assert_proves_discovery(backend_report):
    assert backend_report["ran"] and backend_report["ok"], backend_report.get("failures")
    r1, r2 = (r["clients"] for r in backend_report["rounds"])
    assert [c["client"] for c in r1] == ["devin", "opencode"]
    assert len({c["top_id"] for c in r1}) == 1 and len({c["result_digest"] for c in r1}) == 1
    assert {c["top_id"] for c in r2} == {backend_report["new_record"]}
    assert len({c["result_digest"] for c in r2}) == 1 and r2[0]["result_digest"] != r1[0]["result_digest"]
    for c in r1 + r2:
        assert set(c) >= {"client", "top_id", "result_digest", "ledger_head", "timestamp"}


def test_end_to_end_on_the_json_backend(tmp_path):
    proc, report = _run("json", tmp_path)
    assert proc.returncode == 0 and report["ok"] and report["scratch_stack_only"]
    _assert_proves_discovery(report["backends"][0])
    assert report["backends"][0]["rounds"][0]["clients"][0]["ledger_head"] is None  # no chain on the JSON store


@pytest.mark.skipif(not os.environ.get("JARVIS_TEST_PG_DSN"), reason="needs JARVIS_TEST_PG_DSN (CI's test-postgres job sets it)")
def test_end_to_end_on_postgres(tmp_path):
    proc, report = _run("postgres", tmp_path)
    assert proc.returncode == 0 and report["postgres"]["ran"] is True
    pg = next(b for b in report["backends"] if b["backend"] == "postgres")
    _assert_proves_discovery(pg)
    assert pg["rounds"][1]["clients"][0]["ledger_head"].startswith(("seq:", "block:"))


def test_a_postgres_run_that_could_not_happen_is_reported_as_not_run_and_fails(tmp_path, monkeypatch):
    env = {k: v for k, v in os.environ.items() if k != "JARVIS_TEST_PG_DSN"}
    out = tmp_path / "r.json"
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "--backend", "postgres", "--out", str(out)],
        cwd=str(tmp_path), capture_output=True, text=True, encoding="utf-8", timeout=120, env=env,
    )
    report = json.loads(out.read_text(encoding="utf-8"))
    assert proc.returncode == 1 and report["ok"] is False
    assert report["postgres"] == {"ran": False, "ok": False, "reason": "JARVIS_TEST_PG_DSN is not set (no scratch Postgres)"}
