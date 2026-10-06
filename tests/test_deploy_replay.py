"""jarvisctl replay: what it calls, how it fails, and that `verify` runs the offline verifier in a one-off container."""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

import pytest

from tests.test_deploy_seal import KEY, Ledger, deploy, ledger  # noqa: F401  (fixtures and the stub ledger)

STATE = {"contract": "RC.Ledger.v1", "tenant": "operator", "at_seq": 132, "history_seq": 140, "sealed_seq": 132, "sealed": True, "at_block_boundary": True,
         "block": {"height": 1, "first_seq": 1, "last_seq": 132, "block_hash": "5df7daf4793ffa09c673dc5d67d0681d37f6b18eed2f577a714bc2e0f72addeb"},
         "record_count": 57, "deleted_count": 11, "state_root": "ab" * 32, "records": [], "next_after_id": None}
RECEIPT_ID = "eo:sha256:" + "c" * 64
ISSUED = {"receipt": {"id": RECEIPT_ID, "payload": {}}, "created": True, "state": STATE}


def run(deploy, ledger, tmp_path, *args, extra_env=None, path_prefix=None):
    env = {**os.environ, "JARVIS_HOME": str(tmp_path / "home"), "JARVIS_APP_PORT": str(ledger.port), **(extra_env or {})}
    if path_prefix:
        env["PATH"] = f"{path_prefix}:{env['PATH']}"
    env.pop("DBUS_SESSION_BUS_ADDRESS", None)
    return subprocess.run([str(deploy / "bin" / "replay.sh"), *args], capture_output=True, text=True, env=env, timeout=60)


def text(r):
    return r.stdout + r.stderr


def test_state_asks_for_one_record_and_prints_the_root_and_the_block(deploy, ledger, tmp_path):
    l = ledger(body=STATE)
    r = run(deploy, l, tmp_path, "state")
    assert r.returncode == 0, text(r)
    assert [(q["method"], q["path"], q["key"]) for q in l.requests] == [("GET", "/api/jarvis/replay/state?limit=1", KEY)]
    assert "RC.Ledger.v1 as of seq 132 of 140: 57 record(s), 11 deleted, state root " + "ab" * 32 in r.stdout
    assert "sealed: block 1 (5df7daf4793ffa09...), at its last entry" in r.stdout


@pytest.mark.parametrize("args,query", [(["--at-seq", "5"], "&at_seq=5"), (["--at-block", "2"], "&at_block=2")])
def test_state_passes_the_point_through(deploy, ledger, tmp_path, args, query):
    l = ledger(body=STATE)
    assert run(deploy, l, tmp_path, "state", *args).returncode == 0
    assert l.requests[0]["path"] == "/api/jarvis/replay/state?limit=1" + query


@pytest.mark.parametrize("args", [["--at-seq", "x"], ["--at-block", "-1"], ["--at-seq", "1", "--at-block", "1"], ["--bogus"]])
def test_bad_points_are_refused_before_any_request(deploy, ledger, tmp_path, args):
    l = ledger(body=STATE)
    for cmd in ("state", "receipt"):
        assert run(deploy, l, tmp_path, cmd, *args).returncode == 2
    assert l.requests == []


def test_an_unsealed_state_says_so(deploy, ledger, tmp_path):
    l = ledger(body=STATE | {"sealed": False, "block": None, "at_block_boundary": False})
    assert "not covered by a sealed block" in run(deploy, l, tmp_path, "state").stdout


def test_receipt_posts_the_point_and_prints_the_id(deploy, ledger, tmp_path):
    l = ledger(body=ISSUED)
    r = run(deploy, l, tmp_path, "receipt")
    assert r.returncode == 0, text(r)
    assert l.requests == [{"method": "POST", "path": "/api/jarvis/replay/receipts", "key": KEY, "body": "{}"}]
    assert f"receipt {RECEIPT_ID} (created) at seq 132, block 1, 57 record(s), state root " + "ab" * 32 in r.stdout
    l.body = ISSUED | {"created": False}
    assert "(already existed)" in run(deploy, l, tmp_path, "receipt", "--at-block", "1").stdout
    assert l.requests[-1]["body"] == '{"at_block": 1}'
    run(deploy, l, tmp_path, "receipt", "--at-seq", "7")
    assert l.requests[-1]["body"] == '{"at_seq": 7}'


def test_a_refused_receipt_shows_the_ledgers_own_reason_and_fails(deploy, ledger, tmp_path):
    l = ledger(status=422, body={"detail": "replay_not_sealed: receipts are issued only at sealed points; the ledger is sealed through seq 132"})
    r = run(deploy, l, tmp_path, "receipt", "--at-seq", "140")
    assert r.returncode == 1 and "receipts are issued only at sealed points" in text(r)


def test_the_key_never_appears_in_the_output_or_logs(deploy, ledger, tmp_path):
    l = ledger(body=ISSUED)
    r = run(deploy, l, tmp_path, "receipt")
    logs = "".join(p.read_text() for p in (tmp_path / "home").rglob("*.log"))
    assert KEY not in text(r) + logs


def test_receipts_lists_them_or_says_there_are_none(deploy, ledger, tmp_path):
    l = ledger(body={"receipts": [], "count": 0})
    assert "no receipts yet" in run(deploy, l, tmp_path, "receipts").stdout
    l.body = {"receipts": [{"id": RECEIPT_ID, "payload": {"at_seq": 6, "block_height": 2, "record_count": 5, "state_root": "ab" * 32}}], "count": 1}
    r = run(deploy, l, tmp_path, "receipts")
    assert f"{RECEIPT_ID}  seq 6  block 2  5 record(s)  root {'ab' * 8}..." in r.stdout
    assert l.requests[-1]["path"] == "/api/jarvis/replay/receipts"


def test_check_passes_for_a_good_receipt_and_fails_loudly_for_a_bad_one(deploy, ledger, tmp_path):
    good = {"ok": True, "receipt_id": RECEIPT_ID, "problems": [], "receipt": {"at_seq": 6, "record_count": 5, "state_root": "ab" * 32, "block_height": 2}}
    l = ledger(body=good)
    r = run(deploy, l, tmp_path, "check", RECEIPT_ID)
    assert r.returncode == 0 and f"ok: receipt {RECEIPT_ID} re-derived: seq 6, 5 record(s)" in r.stdout
    assert l.requests[0]["path"] == f"/api/jarvis/replay/receipts/{RECEIPT_ID}/verify"
    l.body = good | {"ok": False, "problems": [{"check": "receipt", "subject": RECEIPT_ID, "problem": "the state root on replay is x, the receipt says y"}]}
    r = run(deploy, l, tmp_path, "check", RECEIPT_ID)
    assert r.returncode == 1 and "PROBLEM [receipt]: the state root on replay is x" in r.stdout and "ok:" not in r.stdout


def test_check_needs_exactly_one_id(deploy, ledger, tmp_path):
    l = ledger()
    assert run(deploy, l, tmp_path, "check").returncode == 2 and run(deploy, l, tmp_path, "check", "a", "b").returncode == 2
    assert l.requests == []


@pytest.mark.parametrize("code,needle", [
    (401, "refused the API key"), (501, "not on the PostgreSQL row store"), (404, "no replay endpoints"),
    (503, "HTTP 503"),
])
def test_failures_are_loud(deploy, ledger, tmp_path, code, needle):
    l = ledger(status=code, body={"detail": "Not Found" if code == 404 else "x"})
    r = run(deploy, l, tmp_path, "state")
    assert r.returncode == 1 and needle in text(r)


def test_a_specific_404_keeps_the_ledgers_reason(deploy, ledger, tmp_path):
    l = ledger(status=404, body={"detail": "replay_block_not_found: there is no sealed block 9"})
    r = run(deploy, l, tmp_path, "state", "--at-block", "9")
    assert r.returncode == 1 and "there is no sealed block 9" in text(r)


def test_an_unreachable_ledger_fails(deploy, ledger, tmp_path):
    l = ledger()
    l.close()
    assert run(deploy, l, tmp_path, "state").returncode == 1


def test_a_missing_key_fails_before_any_request(deploy, ledger, tmp_path):
    (deploy / "secrets" / "api-key").unlink()
    l = ledger()
    assert run(deploy, l, tmp_path, "state").returncode == 1 and l.requests == []


def test_no_command_prints_usage_and_exits_2(deploy, ledger, tmp_path):
    assert run(deploy, ledger(), tmp_path).returncode == 2
    assert run(deploy, ledger(), tmp_path, "frobnicate").returncode == 2


# --- verify: the offline verifier, in a one-off container -------------------------------------------------------------

@pytest.fixture
def fake_docker(tmp_path):
    bindir = tmp_path / "fakebin"
    bindir.mkdir()
    log = tmp_path / "docker.log"
    exe = bindir / "docker"
    exe.write_text(f'#!/bin/sh\nprintf "%s\\n" "$*" >> "{log}"\nexit "${{FAKE_DOCKER_RC:-0}}"\n')
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    return bindir, log


def test_verify_runs_the_replay_verifier_in_the_migrate_container(deploy, ledger, tmp_path, fake_docker):
    bindir, log = fake_docker
    l = ledger()
    r = run(deploy, l, tmp_path, "verify", "--tenant", "alice", "--at-block", "3", "--expect-block-hash", "ab" * 32, path_prefix=bindir)
    assert r.returncode == 0, text(r)
    call = log.read_text().strip()
    assert call.startswith("compose -f ") and "run --rm --no-deps -T migrate python -m app.replay verify --tenant alice --at-block 3 --expect-block-hash " + "ab" * 32 in call
    assert l.requests == []  # the offline verifier never talks to the service


def test_verify_defaults_to_the_operator_tenant_and_passes_a_receipt(deploy, ledger, tmp_path, fake_docker):
    bindir, log = fake_docker
    assert run(deploy, ledger(), tmp_path, "verify", "--receipt", RECEIPT_ID, path_prefix=bindir).returncode == 0
    assert log.read_text().strip().endswith(f"python -m app.replay verify --tenant operator --receipt {RECEIPT_ID}")


def test_verify_passes_the_verifiers_exit_code_through(deploy, ledger, tmp_path, fake_docker):
    bindir, _ = fake_docker
    assert run(deploy, ledger(), tmp_path, "verify", extra_env={"FAKE_DOCKER_RC": "1"}, path_prefix=bindir).returncode == 1


def test_verify_refuses_unknown_arguments_without_running_anything(deploy, ledger, tmp_path, fake_docker):
    bindir, log = fake_docker
    assert run(deploy, ledger(), tmp_path, "verify", "--bogus", path_prefix=bindir).returncode == 2
    assert not log.exists()


def test_verify_needs_no_api_key(deploy, ledger, tmp_path, fake_docker):
    bindir, _ = fake_docker
    (deploy / "secrets" / "api-key").unlink()
    assert run(deploy, ledger(), tmp_path, "verify", path_prefix=bindir).returncode == 0


def test_jarvisctl_routes_replay_and_lists_it_in_help(deploy, ledger, tmp_path):
    l = ledger(body=STATE)
    env = {**os.environ, "JARVIS_HOME": str(tmp_path / "home"), "JARVIS_APP_PORT": str(l.port)}
    r = subprocess.run([str(deploy / "bin" / "jarvisctl"), "replay", "state"], capture_output=True, text=True, env=env, timeout=60)
    assert r.returncode == 0 and "state root" in r.stdout
    assert "replay state|receipt|receipts|check|verify" in subprocess.run([str(deploy / "bin" / "jarvisctl"), "help"], capture_output=True, text=True, env=env).stdout
