"""jarvisctl attest and everything around it on the box: the signer through its wrapper, custody, the roots, the sign timer, the watchdog."""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from app import attest, blocks
from tests.attest_support import make_key, requires_ssh_keygen, sha, statement

ROOT = Path(__file__).resolve().parents[1]
APIKEY = "attest-deploy-test-key"
TENANT = "operator"

pytestmark = requires_ssh_keygen


@pytest.fixture
def repo(tmp_path):
    """A repository-shaped copy (app/, trust/, deploy/mint/) so the scripts find their code, with no secrets and nothing of the real box."""
    r = tmp_path / "repo"
    (r / "app").mkdir(parents=True)
    for name in ("__init__.py", "attest.py", "signer.py", "blocks.py", "witness.py"):
        shutil.copy(ROOT / "app" / name, r / "app" / name)
    shutil.copytree(ROOT / "trust", r / "trust")
    shutil.copytree(ROOT / "deploy" / "mint", r / "deploy" / "mint", ignore=shutil.ignore_patterns(".env", "secrets"))
    (r / "deploy" / "mint" / "secrets").mkdir()
    (r / "deploy" / "mint" / "secrets" / "api-key").write_text(APIKEY + "\n")
    return r


@pytest.fixture
def mint_dir(repo):
    return repo / "deploy" / "mint"


# --- a fake service that verifies signatures for real --------------------------------------------------------------------------------------

class FakeLedger:
    """Just enough of the signature endpoints, with the service's own checks (the real attest module verifies every attestation)."""

    def __init__(self, signing_key: attest.PublicKey, root: attest.PublicKey, block_count: int = 2, authorized: bool = True, verify_ok: bool = True):
        self.key, self.authorized, self.verify_ok = signing_key, authorized, verify_ok
        self.blocks = {}
        prev = attest.GENESIS
        for h in range(1, block_count + 1):
            first = (h - 1) * 3 + 1
            root_hash = sha(f"root {h}")
            bh = blocks.block_hash(tenant=TENANT, height=h, first_seq=first, last_seq=first + 2, entry_count=3, prev_block_hash=prev, entries_root=root_hash)
            self.blocks[h] = {"height": h, "first_seq": first, "last_seq": first + 2, "entry_count": 3, "prev_block_hash": prev, "entries_root": root_hash,
                              "block_hash": bh, "format": 1, "sealed_at": "2026-10-07T10:00:00+00:00", "sealed_by": TENANT}
            prev = bh
        self.rows: list[dict] = []
        self.requests: list[tuple[str, str, str | None]] = []
        outer = self

        class H(BaseHTTPRequestHandler):
            def _go(self):
                n = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(n).decode() if n else None
                outer.requests.append((self.command, self.path, self.headers.get("X-API-Key")))
                status, body = outer.handle(self.command, self.path, json.loads(raw) if raw else None)
                payload = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            do_GET = do_POST = _go

            def log_message(self, *a):
                pass

        self.server = HTTPServer(("127.0.0.1", 0), H)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def head(self):
        last = self.rows[-1] if self.rows else None
        tip = self.blocks[max(self.blocks)]
        return {"tenant": TENANT, "head_seq": len(self.rows), "head_hash": last["attestation_hash"] if last else attest.GENESIS,
                "next_signer_seq": len(self.rows) + 1, "prev_hash": last["attestation_hash"] if last else attest.GENESIS,
                "tip_height": tip["height"], "tip_block_hash": tip["block_hash"]}

    def handle(self, method, path, body):
        if self.key and path == "/api/jarvis/trust":
            keys = [{"key_id": self.key.key_id, "from_signer_seq": 1, "revoked_after_signer_seq": None}] if self.authorized else []
            return 200, {"trust_roots_configured": True, "keys": keys}
        if path == "/api/jarvis/attestations/verify":
            return 200, {"ok": self.verify_ok, "problems": [] if self.verify_ok else [{"check": "attestation", "subject": "attestation 1", "problem": "bad"}],
                         "warnings": [], "notes": ["signatures: fake"], "summary": {}}
        if path == "/api/jarvis/attestations/pending":
            done = {r["subject"] for r in self.rows}
            return 200, {"blocks": [{"height": h, "block_hash": b["block_hash"], "sealed_at": b["sealed_at"]} for h, b in self.blocks.items() if f"block:{h}" not in done],
                         "receipts": [], "head": self.head()}
        if path == "/api/jarvis/attestations/head":
            return 200, self.head()
        m = re.fullmatch(r"/api/jarvis/blocks/(\d+)", path)
        if m:
            return 200, {"block": self.blocks[int(m.group(1))]}
        if method == "POST" and path == "/api/jarvis/attestations":
            h = self.head()
            if body["signer_seq"] != h["next_signer_seq"] or body["prev_hash"] != h["prev_hash"]:
                return 409, {"detail": "out of order", "code": "attestation_out_of_order"}
            message = attest.attestation_message(body["kind"], TENANT, body["subject"], body["subject_hash"], body["signer_seq"], body["prev_hash"], body["signed_at"])
            try:
                signer = attest.verify_sshsig(body["signature"], message.encode())
            except attest.SignatureInvalid as exc:
                return 422, {"detail": f"bad signature: {exc}"}
            if signer.key_id != body["key_id"] or signer.key_id != self.key.key_id:
                return 422, {"detail": "no root authorized that key"}
            row = {**body, "attestation_hash": attest.attestation_hash(message, body["key_id"], body["signature"])}
            self.rows.append(row)
            return 200, {"signer_seq": row["signer_seq"], "attestation_hash": row["attestation_hash"], "key_id": row["key_id"]}
        return 404, {"detail": "Not Found"}

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def home(tmp_path):
    return tmp_path / "home"


@pytest.fixture
def signing_key(home):
    """The signing key, created by the real init-key path's rules (mode 600, in a mode 700 directory)."""
    d = home / "keys"
    d.mkdir(parents=True, mode=0o700)
    os.chmod(d, 0o700)
    k = make_key(d, "jarvis-sign-ed25519")
    os.chmod(k.path, 0o600)
    return k


@pytest.fixture
def fake_bin(tmp_path):
    """A fake docker: `ps` shows no containers; `compose ... run` (the offline verifier) succeeds or fails as the test says."""
    d = tmp_path / "fakebin"
    d.mkdir()
    log = tmp_path / "docker.log"
    exe = d / "docker"
    exe.write_text(f'#!/bin/sh\nprintf "%s\\n" "$*" >> "{log}"\ncase "$1" in ps) exit 0;; inspect) echo healthy; exit 0;; esac\nexit "${{FAKE_DOCKER_RC:-0}}"\n')
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    return d, log


@pytest.fixture
def ledger_factory(signing_key, tmp_path):
    made = []

    def make(**kw):
        root = make_key(tmp_path, f"rootkey{len(made)}")
        led = FakeLedger(signing_key.public, root.public, **kw)
        made.append(led)
        return led

    yield make
    for led in made:
        led.close()


def run_attest(mint_dir, home, port, *args, fake_bin=None, env_extra=None):
    env = {k: v for k, v in os.environ.items() if k not in ("DBUS_SESSION_BUS_ADDRESS", "JARVIS_SIGN_KEY")}
    env.update({"JARVIS_HOME": str(home), "JARVIS_APP_PORT": str(port), "JARVIS_SIGNER_PYTHON": sys.executable, **(env_extra or {})})
    if fake_bin:
        env["PATH"] = f"{fake_bin[0]}:{env['PATH']}"
    return subprocess.run([str(mint_dir / "bin" / "attest.sh"), *args], capture_output=True, text=True, env=env, timeout=120)


def text(r):
    return r.stdout + r.stderr


def state(home, name):
    return home / "state" / name


# --- the key, made the right way -------------------------------------------------------------------------------------------------------------------

def test_init_key_creates_the_key_under_the_home_of_the_ledger_with_the_right_modes(mint_dir, home, fake_bin):
    r = run_attest(mint_dir, home, 1, "init-key", fake_bin=fake_bin)
    assert r.returncode == 0, text(r)
    key = home / "keys" / "jarvis-sign-ed25519"
    assert stat.S_IMODE(key.stat().st_mode) == 0o600 and stat.S_IMODE(key.parent.stat().st_mode) == 0o700 and (home / "keys" / "jarvis-sign-ed25519.pub").exists()
    assert "key id: SHA256:" in r.stdout and "PRIVATE" not in text(r)
    body = [l for l in key.read_text().splitlines() if l and "-----" not in l][0]
    assert body not in text(r)
    assert run_attest(mint_dir, home, 1, "init-key", fake_bin=fake_bin).returncode != 0  # never overwrites


def test_the_key_lives_nowhere_the_backups_the_repository_or_a_volume_can_reach(mint_dir, home, fake_bin):
    run_attest(mint_dir, home, 1, "init-key", fake_bin=fake_bin)
    key = (home / "keys" / "jarvis-sign-ed25519").resolve()
    assert not str(key).startswith(str((home / "backups").resolve())) and not str(key).startswith(str(mint_dir.parents[1].resolve()))


# --- signing through the wrapper --------------------------------------------------------------------------------------------------------------------------

def test_a_dry_run_asks_what_is_pending_and_signs_and_stores_nothing(mint_dir, home, signing_key, ledger_factory, fake_bin):
    led = ledger_factory()
    r = run_attest(mint_dir, home, led.port, "sign", "--dry-run", fake_bin=fake_bin)
    assert r.returncode == 0, text(r)
    assert "would sign blocks [1, 2] and a checkpoint" in r.stdout
    assert not [q for q in led.requests if q[0] == "POST"] and led.rows == [] and not state(home, "sign.last_ok").exists()
    assert APIKEY not in text(r)


def test_signing_stores_every_block_then_a_checkpoint_and_records_success(mint_dir, home, signing_key, ledger_factory, fake_bin):
    led = ledger_factory()
    r = run_attest(mint_dir, home, led.port, "sign", fake_bin=fake_bin)
    assert r.returncode == 0, text(r)
    assert [(x["kind"], x["subject"], x["signer_seq"]) for x in led.rows][:2] == [("block", "block:1", 1), ("block", "block:2", 2)] and led.rows[2]["kind"] == "checkpoint"
    assert "signed blocks 1,2; checkpoint 3 covers 2" in text(r)
    assert state(home, "sign.last_ok").exists() and state(home, "sign.first_ok").exists()
    assert APIKEY not in text(r) + "".join(p.read_text() for p in (home / "logs").rglob("*.log"))
    # the offline verifier ran in a one-off container, once, on the newest block with its hash (compose run ... migrate ... replay verify)
    calls = [l for l in fake_bin[1].read_text().splitlines() if "run" in l and "replay" in l]
    assert len(calls) == 1 and "--at-block 2" in calls[0] and f"--expect-block-hash {led.blocks[2]['block_hash']}" in calls[0]
    again = run_attest(mint_dir, home, led.port, "sign", fake_bin=fake_bin)
    assert again.returncode == 0 and "nothing to sign" in text(again) and len(led.rows) == 3


def test_if_the_offline_verifier_rejects_the_block_nothing_is_signed_and_nothing_counts_as_a_success(mint_dir, home, signing_key, ledger_factory, fake_bin):
    led = ledger_factory()
    r = run_attest(mint_dir, home, led.port, "sign", fake_bin=fake_bin, env_extra={"FAKE_DOCKER_RC": "1"})
    assert r.returncode == 1 and "pre_sign_verify_failed" in text(r)
    assert led.rows == [] and not state(home, "sign.last_ok").exists()


@pytest.mark.parametrize("mode,needle", [(0o644, "must be 600"), (0o666, "must be 600")])
def test_a_loosely_kept_key_stops_the_signer_before_it_asks_the_service_anything(mint_dir, home, signing_key, ledger_factory, fake_bin, mode, needle):
    os.chmod(signing_key.path, mode)
    led = ledger_factory()
    r = run_attest(mint_dir, home, led.port, "sign", fake_bin=fake_bin)
    assert r.returncode == 1 and needle in text(r) and led.requests == [] and led.rows == []


def test_a_key_in_the_backup_directory_is_refused(mint_dir, home, ledger_factory, fake_bin):
    bad = home / "backups" / "k"
    bad.mkdir(parents=True, mode=0o700)
    k = make_key(bad, "oops")
    os.chmod(k.path, 0o600)
    led = ledger_factory()
    r = run_attest(mint_dir, home, led.port, "sign", fake_bin=fake_bin, env_extra={"JARVIS_SIGN_KEY": str(k.path)})
    assert r.returncode == 1 and "must never be in the repository" in text(r) and led.requests == []


def test_a_key_the_service_does_not_know_stops_the_signer(mint_dir, home, signing_key, ledger_factory, fake_bin):
    led = ledger_factory(authorized=False)
    r = run_attest(mint_dir, home, led.port, "sign", fake_bin=fake_bin)
    assert r.returncode == 1 and "key_not_authorized" in text(r) and led.rows == [] and not state(home, "sign.last_ok").exists()


def test_a_log_that_does_not_verify_is_not_extended(mint_dir, home, signing_key, ledger_factory, fake_bin):
    led = ledger_factory(verify_ok=False)
    r = run_attest(mint_dir, home, led.port, "sign", fake_bin=fake_bin)
    assert r.returncode == 1 and "log_unhealthy" in text(r) and led.rows == []


def test_status_reports_the_key_and_the_work_without_a_secret(mint_dir, home, signing_key, ledger_factory, fake_bin):
    led = ledger_factory()
    r = run_attest(mint_dir, home, led.port, "status", fake_bin=fake_bin)
    assert r.returncode == 0
    s = json.loads(r.stdout)
    assert s["key_id"] == signing_key.key_id and s["authorized"] is True and s["pending_blocks"] == [1, 2] and "PRIVATE" not in r.stdout


def test_verify_prints_the_services_findings_and_fails_on_a_problem(mint_dir, home, ledger_factory, fake_bin):
    good = ledger_factory()
    r = run_attest(mint_dir, home, good.port, "verify", fake_bin=fake_bin)
    assert r.returncode == 0 and "signatures: fake" in r.stdout
    bad = ledger_factory(verify_ok=False)
    r = run_attest(mint_dir, home, bad.port, "verify", fake_bin=fake_bin)
    assert r.returncode == 1 and "PROBLEM [attestation] attestation 1: bad" in r.stdout


def test_an_unknown_command_or_argument_is_refused(mint_dir, home, fake_bin):
    assert run_attest(mint_dir, home, 1, fake_bin=fake_bin).returncode == 2
    assert run_attest(mint_dir, home, 1, "frobnicate", fake_bin=fake_bin).returncode == 2
    assert run_attest(mint_dir, home, 1, "sign", "--bogus", fake_bin=fake_bin).returncode == 2


# --- installing the roots (public keys only) --------------------------------------------------------------------------------------------------------

def test_install_roots_refuses_an_empty_file_and_a_file_with_private_material(mint_dir, repo, home, fake_bin):
    roots = repo / "trust" / "roots.pub"
    r = run_attest(mint_dir, home, 1, "install-roots", fake_bin=fake_bin)
    assert r.returncode == 1 and "lists no root key yet" in text(r) and not (mint_dir / "secrets" / "trust-roots.pub").exists()
    roots.write_text("ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFAKEKEY root\n# PRIVATE KEY smuggled\n")
    r = run_attest(mint_dir, home, 1, "install-roots", fake_bin=fake_bin)
    assert r.returncode == 1 and "private key material" in text(r)


def test_install_roots_copies_the_public_keys_and_restarts_only_the_app(mint_dir, repo, home, fake_bin, tmp_path):
    k = make_key(tmp_path, "r")
    (repo / "trust" / "roots.pub").write_text(f"# roots\n{k.pub_text}\n")
    r = run_attest(mint_dir, home, 1, "install-roots", fake_bin=fake_bin)
    assert r.returncode == 0, text(r)
    dest = mint_dir / "secrets" / "trust-roots.pub"
    assert dest.read_text().count("ssh-ed25519 ") == 1 and stat.S_IMODE(dest.stat().st_mode) == 0o644
    calls = fake_bin[1].read_text()
    assert "up -d --force-recreate --no-deps app" in calls
    assert "keys" not in calls  # nothing about the signing key is handed to compose


def test_the_roots_installed_for_the_service_are_the_roots_in_the_repository_not_anything_else(mint_dir, repo, home, fake_bin, tmp_path):
    k = make_key(tmp_path, "r")
    (repo / "trust" / "roots.pub").write_text(k.pub_text + "\n")
    run_attest(mint_dir, home, 1, "install-roots", fake_bin=fake_bin)
    assert attest.load_roots(mint_dir / "secrets" / "trust-roots.pub").keys() == {k.key_id}


# --- the secrets directory always has a roots file --------------------------------------------------------------------------------------------------------

def test_ensure_trust_roots_creates_an_empty_public_file_once(mint_dir, home):
    sh = f'source "{mint_dir}/bin/lib.sh"; ensure_trust_roots; ensure_trust_roots; cat "$SECRETS_DIR/trust-roots.pub"'
    env = {**os.environ, "JARVIS_HOME": str(home)}
    r = subprocess.run(["bash", "-c", sh], capture_output=True, text=True, env=env)
    assert r.returncode == 0 and r.stdout.startswith("# trust roots") and attest.load_roots(mint_dir / "secrets" / "trust-roots.pub") == {}
    assert stat.S_IMODE((mint_dir / "secrets" / "trust-roots.pub").stat().st_mode) == 0o644


def test_ensure_trust_roots_never_overwrites_installed_roots(mint_dir, home, tmp_path):
    k = make_key(tmp_path, "r")
    (mint_dir / "secrets" / "trust-roots.pub").write_text(k.pub_text + "\n")
    subprocess.run(["bash", "-c", f'source "{mint_dir}/bin/lib.sh"; ensure_trust_roots'], env={**os.environ, "JARVIS_HOME": str(home)}, check=True)
    assert k.key_id in attest.load_roots(mint_dir / "secrets" / "trust-roots.pub")


# --- the compose project: public roots in, no private key anywhere near it --------------------------------------------------------------------------------

def compose_text():
    return (ROOT / "deploy" / "mint" / "docker-compose.yml").read_text("utf-8")


def test_the_service_gets_the_public_roots_as_a_read_only_config_file():
    t = compose_text()
    assert re.search(r"^configs:\n  trust_roots:\n    file: \./secrets/trust-roots\.pub", t, re.M)
    assert t.count("source: trust_roots") == 2 and t.count("target: /etc/jarvis/trust-roots.pub") == 2 and t.count("JARVIS_TRUST_ROOTS_FILE: /etc/jarvis/trust-roots.pub") == 2
    # both the app and the migrate service (where verify, the restore gate and the offline replay run) get the roots, nothing else does
    for service in ("migrate", "app"):
        block = t[t.index(f"\n  {service}:"):]
        assert "trust_roots" in block


def test_nothing_in_the_compose_project_can_carry_the_signing_key():
    t = compose_text().lower()
    for word in ("jarvis-sign", "ed25519", "/keys", "ssh-keygen", "private_key", "signing_key"):
        assert word not in t, word
    assert len(re.findall(r"^\s+file: ", t, re.M)) == 1  # the roots file is the only config file


def test_the_roots_file_in_secrets_is_ignored_by_git_and_the_repo_copy_is_the_pin():
    assert "secrets/" in (ROOT / "deploy" / "mint" / ".gitignore").read_text()
    assert (ROOT / "trust" / "roots.pub").exists()


# --- the sign timer --------------------------------------------------------------------------------------------------------------------------------------

@pytest.fixture
def fake_systemd(tmp_path):
    bindir = tmp_path / "fakesys"
    bindir.mkdir()
    log = tmp_path / "systemctl.log"
    for tool in ("systemctl", "loginctl"):
        p = bindir / tool
        p.write_text(f'#!/bin/sh\necho "{tool} $*" >> "{log}"\n')
        p.chmod(p.stat().st_mode | stat.S_IEXEC)
    return bindir, log


def test_the_sign_units_are_installed_but_the_timer_is_never_enabled(mint_dir, home, tmp_path, fake_systemd):
    (mint_dir / "secrets" / "offsite.conf").write_text("x\n")
    env = {**os.environ, "PATH": f"{fake_systemd[0]}:{os.environ['PATH']}", "JARVIS_HOME": str(home), "USER": "tester"}
    r = subprocess.run([str(mint_dir / "bin" / "install-units.sh"), "--dest", str(tmp_path / "units")], capture_output=True, text=True, env=env, timeout=60)
    assert r.returncode == 0, text(r)
    units = tmp_path / "units"
    service, timer = (units / "jarvis-sign.service").read_text(), (units / "jarvis-sign.timer").read_text()
    assert f"ExecStart={mint_dir}/bin/attest.sh sign" in service and "@DEPLOY_DIR@" not in service + timer
    assert "OnCalendar=*:56:00" in timer and "WantedBy=timers.target" in timer
    calls = fake_systemd[1].read_text()
    assert "sign" not in calls and "seal" not in calls  # neither optional timer is touched
    assert "jarvis-sign.timer is installed but NOT enabled" in r.stdout


def test_the_installer_never_lists_the_sign_timer_for_enabling():
    loops = [l for l in (ROOT / "deploy" / "mint" / "bin" / "install-units.sh").read_text().splitlines() if l.strip().startswith("for t in")]
    assert loops and all("sign" not in l and "seal" not in l for l in loops)


def test_the_sign_timer_runs_after_the_seal_and_before_the_backup():
    timer = (ROOT / "deploy" / "mint" / "systemd" / "jarvis-sign.timer").read_text()
    seal = (ROOT / "deploy" / "mint" / "systemd" / "jarvis-seal.timer").read_text()
    assert "OnCalendar=*:55:00" in seal and "OnCalendar=*:56:00" in timer
    assert "OnCalendar=hourly" in (ROOT / "deploy" / "mint" / "systemd" / "jarvis-backup.timer").read_text()


# --- the watchdog ---------------------------------------------------------------------------------------------------------------------------------------

@pytest.fixture
def watch(mint_dir, home, tmp_path):
    bindir = tmp_path / "shims"
    bindir.mkdir()
    for tool, body in (("docker", "echo healthy"),):
        p = bindir / tool
        p.write_text(f"#!/bin/sh\n{body}\n")
        p.chmod(p.stat().st_mode | stat.S_IEXEC)
    st = home / "state"
    st.mkdir(parents=True)
    now = int(subprocess.run(["date", "+%s"], capture_output=True, text=True).stdout.strip())
    for f in ("backup", "offsite", "drill"):
        (st / f"{f}.last_ok").write_text(str(now))

    def go(statements=None):
        """Run the watchdog against a stub that serves /ready and the trust statements."""
        class H(BaseHTTPRequestHandler):
            def do_GET(self):
                body = json.dumps({"statements": statements or []}) if "trust/statements" in self.path else json.dumps({"status": "ready"})
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body.encode())

            def log_message(self, *a):
                pass

        srv = HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            env = {**os.environ, "PATH": f"{bindir}:{os.environ['PATH']}", "JARVIS_HOME": str(home), "JARVIS_APP_PORT": str(srv.server_address[1])}
            env.pop("DBUS_SESSION_BUS_ADDRESS", None)
            return subprocess.run([str(mint_dir / "bin" / "watchdog.sh")], capture_output=True, text=True, env=env, timeout=60)
        finally:
            srv.shutdown()
            srv.server_close()

    return go, st, now


def test_the_watchdog_ignores_signing_until_it_has_succeeded_once(watch):
    go, st, now = watch
    r = go()
    assert r.returncode == 0 and "signing" not in text(r) and "cosign" not in text(r)


def test_the_watchdog_flags_signing_that_has_stopped(watch):
    go, st, now = watch
    (st / "sign.last_ok").write_text(str(now - 5 * 3600))
    (st / "sign.first_ok").write_text(str(now - 5 * 3600))
    (st / "sign.last_ok").write_text(str(now - 5 * 3600))
    r = go([{"kind": "cosign", "stored_at": "2999-01-01T00:00:00+00:00"}])
    assert r.returncode == 1 and "the attestation signing last succeeded 5 h" in text(r)


def test_the_watchdog_flags_a_missing_cosign_after_three_days_but_not_before(watch):
    go, st, now = watch
    (st / "sign.last_ok").write_text(str(now - 60))
    (st / "sign.first_ok").write_text(str(now - 2 * 86400))
    assert go().returncode == 0
    (st / "sign.first_ok").write_text(str(now - 4 * 86400))
    r = go()
    assert r.returncode == 1 and "no root cosign for 96 h" in text(r)


def test_a_recent_cosign_keeps_the_watchdog_quiet_and_an_old_one_does_not(watch):
    go, st, now = watch
    (st / "sign.last_ok").write_text(str(now - 60))
    (st / "sign.first_ok").write_text(str(now - 30 * 86400))
    recent = __import__("datetime").datetime.fromtimestamp(now - 3600, __import__("datetime").timezone.utc).isoformat()
    old = __import__("datetime").datetime.fromtimestamp(now - 5 * 86400, __import__("datetime").timezone.utc).isoformat()
    assert go([{"kind": "key", "stored_at": old}, {"kind": "cosign", "stored_at": recent}]).returncode == 0
    r = go([{"kind": "cosign", "stored_at": old}])
    assert r.returncode == 1 and "no root cosign for 120 h" in text(r)


# --- jarvisctl ---------------------------------------------------------------------------------------------------------------------------------------------

def test_jarvisctl_routes_attest_and_lists_it_in_help(mint_dir, home, signing_key, ledger_factory, fake_bin):
    led = ledger_factory()
    env = {**os.environ, "JARVIS_HOME": str(home), "JARVIS_APP_PORT": str(led.port), "JARVIS_SIGNER_PYTHON": sys.executable, "PATH": f"{fake_bin[0]}:{os.environ['PATH']}"}
    env.pop("JARVIS_SIGN_KEY", None)
    r = subprocess.run([str(mint_dir / "bin" / "jarvisctl"), "attest", "status"], capture_output=True, text=True, env=env, timeout=60)
    assert r.returncode == 0 and json.loads(r.stdout)["key_id"] == signing_key.key_id
    assert "attest status|sign|init-key|install-roots|verify" in subprocess.run([str(mint_dir / "bin" / "jarvisctl"), "help"], capture_output=True, text=True, env=env).stdout
