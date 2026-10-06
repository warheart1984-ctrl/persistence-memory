"""jarvisctl seal and the seal timer: what they call, how they fail, and that the timer is installed but never enabled."""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
MINT = ROOT / "deploy" / "mint"
KEY = "seal-test-key-9f3a1c"

HEAD = {"tip": {"height": 4}, "sealed_seq": 40, "history_seq": 43, "unsealed_entries": 3, "oldest_unsealed_at": None}


@pytest.fixture
def deploy(tmp_path):
    """A private copy of deploy/mint with its own JARVIS_HOME and key, so the scripts touch nothing real."""
    d = tmp_path / "mint"
    shutil.copytree(MINT, d, ignore=shutil.ignore_patterns(".env", "secrets"))
    (d / "secrets").mkdir()
    (d / "secrets" / "api-key").write_text(KEY + "\n")
    return d


class Ledger:
    """A stand-in for the app: records what it is asked and answers as configured."""

    def __init__(self, status=200, body=None):
        self.status, self.body, self.requests = status, body, []
        outer = self

        class H(BaseHTTPRequestHandler):
            def _answer(self):
                n = int(self.headers.get("Content-Length") or 0)
                outer.requests.append({"method": self.command, "path": self.path, "key": self.headers.get("X-API-Key"),
                                       "body": self.rfile.read(n).decode() if n else ""})
                payload = json.dumps(outer.body if outer.body is not None else {}).encode()
                self.send_response(outer.status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            do_GET = do_POST = _answer

            def log_message(self, *a):
                pass

        self.server = HTTPServer(("127.0.0.1", 0), H)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()  # really stop listening, so a later connection is refused


@pytest.fixture
def ledger():
    made = []

    def make(**kw):
        l = Ledger(**kw)
        made.append(l)
        return l

    yield make
    for l in made:
        l.close()


def run_seal(deploy, ledger, tmp_path, *args):
    env = {**os.environ, "JARVIS_HOME": str(tmp_path / "home"), "JARVIS_APP_PORT": str(ledger.port)}
    env.pop("DBUS_SESSION_BUS_ADDRESS", None)
    return subprocess.run([str(deploy / "bin" / "seal.sh"), *args], capture_output=True, text=True, env=env, timeout=60)


def state(tmp_path):
    return tmp_path / "home" / "state" / "seal.last_ok"


def test_seal_posts_to_the_seal_endpoint_with_the_operator_key_and_records_success(deploy, ledger, tmp_path):
    l = ledger(body={"sealed": [{"height": 5}, {"height": 6}], "reason": "nothing new to seal", "head": HEAD})
    r = run_seal(deploy, l, tmp_path)
    assert r.returncode == 0, r.stderr
    assert l.requests == [{"method": "POST", "path": "/api/jarvis/blocks/seal", "key": KEY, "body": '{"force": false}'}]
    assert "sealed 2 block(s) (height 5,6)" in r.stdout + r.stderr
    assert state(tmp_path).exists()


def test_the_key_is_never_printed_or_logged(deploy, ledger, tmp_path):
    l = ledger(body={"sealed": [], "reason": "below threshold", "head": HEAD})
    r = run_seal(deploy, l, tmp_path)
    logs = "".join(p.read_text() for p in (tmp_path / "home").rglob("*.log"))
    assert KEY not in r.stdout + r.stderr + logs


def test_nothing_due_is_a_success_and_says_why(deploy, ledger, tmp_path):
    l = ledger(body={"sealed": [], "reason": "below threshold: 3 unsealed entries (need 500)", "head": HEAD})
    r = run_seal(deploy, l, tmp_path)
    assert r.returncode == 0 and "nothing sealed: below threshold: 3 unsealed entries" in r.stdout + r.stderr
    assert state(tmp_path).exists()


def test_force_is_passed_through(deploy, ledger, tmp_path):
    l = ledger(body={"sealed": [{"height": 1}], "reason": "nothing new to seal", "head": HEAD})
    assert run_seal(deploy, l, tmp_path, "--force").returncode == 0
    assert l.requests[0]["body"] == '{"force": true}'


def test_status_only_reads_the_head_and_writes_nothing(deploy, ledger, tmp_path):
    l = ledger(body=HEAD)
    r = run_seal(deploy, l, tmp_path, "--status")
    assert r.returncode == 0 and "newest block: 4, sealed through seq 40, 3 entries unsealed" in r.stdout
    assert [(q["method"], q["path"]) for q in l.requests] == [("GET", "/api/jarvis/blocks/head")]
    assert not state(tmp_path).exists()


@pytest.mark.parametrize("code,needle", [(401, "refused the API key"), (404, "no block endpoints"), (501, "not on the PostgreSQL row store"), (500, "HTTP 500"), (503, "HTTP 503")])
def test_failures_are_loud_and_do_not_count_as_a_success(deploy, ledger, tmp_path, code, needle):
    l = ledger(status=code, body={"detail": "x"})
    r = run_seal(deploy, l, tmp_path)
    assert r.returncode == 1 and needle in r.stdout + r.stderr
    assert not state(tmp_path).exists()


def test_an_unreachable_ledger_fails(deploy, ledger, tmp_path):
    l = ledger()
    l.close()
    r = run_seal(deploy, l, tmp_path)
    assert r.returncode == 1 and not state(tmp_path).exists()


def test_a_missing_key_fails_before_any_request(deploy, ledger, tmp_path):
    (deploy / "secrets" / "api-key").unlink()
    l = ledger()
    r = run_seal(deploy, l, tmp_path)
    assert r.returncode == 1 and l.requests == []


def test_an_unknown_argument_is_refused(deploy, ledger, tmp_path):
    assert run_seal(deploy, ledger(), tmp_path, "--bogus").returncode == 2


def test_jarvisctl_knows_the_seal_command(deploy, ledger, tmp_path):
    l = ledger(body=HEAD)
    env = {**os.environ, "JARVIS_HOME": str(tmp_path / "home"), "JARVIS_APP_PORT": str(l.port)}
    r = subprocess.run([str(deploy / "bin" / "jarvisctl"), "seal", "--status"], capture_output=True, text=True, env=env, timeout=60)
    assert r.returncode == 0 and "newest block: 4" in r.stdout
    assert "seal [--force|--status]" in subprocess.run([str(deploy / "bin" / "jarvisctl"), "help"], capture_output=True, text=True, env=env).stdout


# --- the timer is installed, never enabled -------------------------------------------------------------------------

@pytest.fixture
def fake_systemd(tmp_path):
    bindir = tmp_path / "fakebin"
    bindir.mkdir()
    log = tmp_path / "systemctl.log"
    for tool in ("systemctl", "loginctl"):
        p = bindir / tool
        p.write_text(f'#!/bin/sh\necho "{tool} $*" >> "{log}"\n')
        p.chmod(p.stat().st_mode | stat.S_IEXEC)
    return bindir, log


def install(deploy, tmp_path, fake, *args):
    bindir, _ = fake
    env = {**os.environ, "PATH": f"{bindir}:{os.environ['PATH']}", "JARVIS_HOME": str(tmp_path / "home"), "USER": "tester"}
    return subprocess.run([str(deploy / "bin" / "install-units.sh"), "--dest", str(tmp_path / "units"), *args],
                          capture_output=True, text=True, env=env, timeout=60)


def test_install_writes_the_seal_units_but_enables_everything_except_the_seal_timer(deploy, tmp_path, fake_systemd):
    (deploy / "secrets" / "offsite.conf").write_text("x\n")  # so the offsite timer is enabled too: the full list is exercised
    r = install(deploy, tmp_path, fake_systemd)
    assert r.returncode == 0, r.stderr
    units = tmp_path / "units"
    assert (units / "jarvis-seal.service").exists() and (units / "jarvis-seal.timer").exists()
    calls = fake_systemd[1].read_text()
    enabled = [ln.split()[-1] for ln in calls.splitlines() if " enable " in ln]
    assert sorted(enabled) == sorted(f"jarvis-{t}.timer" for t in ("backup", "offsite", "drill", "watchdog", "heal"))
    assert "seal" not in calls
    assert "jarvis-seal.timer is installed but NOT enabled" in r.stdout


def test_the_seal_units_are_substituted_and_well_formed(deploy, tmp_path, fake_systemd):
    install(deploy, tmp_path, fake_systemd, "--no-enable")
    service = (tmp_path / "units" / "jarvis-seal.service").read_text()
    timer = (tmp_path / "units" / "jarvis-seal.timer").read_text()
    assert f"ExecStart={deploy}/bin/seal.sh" in service and "Type=oneshot" in service and "@DEPLOY_DIR@" not in service + timer
    assert "OnCalendar=*:55:00" in timer and "WantedBy=timers.target" in timer
    assert not fake_systemd[1].exists()  # --no-enable calls nothing


def test_the_repository_never_wires_the_seal_timer_to_be_enabled_automatically():
    text = (MINT / "bin" / "install-units.sh").read_text()
    loop = [ln for ln in text.splitlines() if ln.strip().startswith("for t in")]
    assert loop and all("seal" not in ln for ln in loop)
    assert (MINT / "bin" / "seal.sh").stat().st_mode & stat.S_IXUSR


# --- the watchdog watches the seal only once it has run ----------------------------------------------------------------

@pytest.fixture
def watch(deploy, tmp_path):
    bindir = tmp_path / "shims"
    bindir.mkdir()
    for tool, body in (("docker", "echo healthy"), ("curl", "exit 0")):
        p = bindir / tool
        p.write_text(f"#!/bin/sh\n{body}\n")
        p.chmod(p.stat().st_mode | stat.S_IEXEC)
    home = tmp_path / "home"
    state = home / "state"
    state.mkdir(parents=True)
    now = subprocess.run(["date", "+%s"], capture_output=True, text=True).stdout.strip()
    for f in ("backup", "offsite", "drill"):
        (state / f"{f}.last_ok").write_text(now)

    def run():
        env = {**os.environ, "PATH": f"{bindir}:{os.environ['PATH']}", "JARVIS_HOME": str(home)}
        env.pop("DBUS_SESSION_BUS_ADDRESS", None)
        return subprocess.run([str(deploy / "bin" / "watchdog.sh")], capture_output=True, text=True, env=env, timeout=60)

    return run, state, int(now)


def test_the_watchdog_ignores_the_seal_until_it_has_succeeded_once(watch):
    run, state, _ = watch
    r = run()
    assert r.returncode == 0 and "block seal" not in r.stdout + r.stderr


def test_the_watchdog_accepts_a_recent_seal(watch):
    run, state, now = watch
    (state / "seal.last_ok").write_text(str(now - 600))
    assert run().returncode == 0


def test_the_watchdog_flags_a_seal_that_has_stopped(watch):
    run, state, now = watch
    (state / "seal.last_ok").write_text(str(now - 4 * 3600))
    r = run()
    assert r.returncode == 1 and "the block seal last succeeded 4 h" in r.stdout + r.stderr
