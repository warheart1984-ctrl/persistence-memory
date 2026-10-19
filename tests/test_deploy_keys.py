"""jarvisctl keys: the box-side wrapper for per-tenant API keys (RFC phase 1). What it validates before Docker is touched, that the pepper and the tokens are never printed or put on a
command line, that a pepper or a token file is never overwritten, and that issuing a key does not switch key authentication on."""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
MINT = ROOT / "deploy" / "mint"
TENANT = "t_" + "ab" * 16
KEY_ID = "0123456789abcdef"


@pytest.fixture
def box(tmp_path):
    d = tmp_path / "mint"
    shutil.copytree(MINT, d, ignore=shutil.ignore_patterns(".env", "secrets"))
    (d / "secrets").mkdir()
    fake = tmp_path / "bin"
    fake.mkdir()
    log = tmp_path / "docker.log"
    # a docker that records every argument (one line per call) and what the container would see in the environment, and answers like the CLI does
    (fake / "docker").write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "$FAKE_DOCKER_LOG"\ncase "$*" in *create-key*) echo "key 0123456789abcdef issued; token written to /keys/x.token" ;; *list-tenants*) echo "t_x  active  a" ;; *"cmp -s"*) echo same ;; esac\nexit ${FAKE_DOCKER_RC:-0}\n')
    (fake / "docker").chmod(0o755)
    env = dict(os.environ, PATH=f"{fake}:{os.environ['PATH']}", JARVIS_HOME=str(tmp_path / "home"), FAKE_DOCKER_LOG=str(log), HOME=str(tmp_path))

    class Box:
        dir, secrets, env_, log_ = d, d / "secrets", env, log

        def run(self, *args):
            return subprocess.run([str(d / "bin" / "keys.sh"), *args], env=env, capture_output=True, text=True, timeout=60)

        def docker_calls(self):
            return log.read_text().splitlines() if log.exists() else []

    return Box()


def with_pepper(box):
    r = box.run("pepper-init")
    assert r.returncode == 0, r.stderr
    return (box.secrets / "key-pepper").read_text().strip()


BAD = [
    ("tenant-create",), ("tenant-create", "--label"), ("tenant-create", "--label", "x", "--tenant", TENANT),
    ("key-create", "--tenant", TENANT), ("key-create", "--tenant", "operator", "--out", "a"), ("key-create", "--tenant", TENANT.upper(), "--out", "a"),
    ("key-create", "--tenant", "t_" + "ab" * 15, "--out", "a"), ("key-create", "--tenant", TENANT, "--out", "../x"), ("key-create", "--tenant", TENANT, "--out", "a/b"),
    ("key-create", "--tenant", TENANT, "--out", ".."), ("key-create", "--tenant", TENANT, "--out", "a b"),
    ("key-create", "--tenant", TENANT, "--out", "a", "--scopes", "admin"), ("key-create", "--tenant", TENANT, "--out", "a", "--scopes", "memory.read,"),
    ("key-create", "--tenant", TENANT, "--out", "a", "--expires-days", "-1"), ("key-create", "--tenant", TENANT, "--out", "a", "--expires-days", "soon"),
    ("key-create", "--tenant", TENANT, "--out", "a", "--status", "active"), ("key-create", "--tenant", TENANT, "--out", "a", "--label", "x" * 81),
    ("key-create", "--tenant", TENANT, "--out", "a", "--label", "two\nlines"),
    ("key-revoke",), ("key-revoke", "jlk_" + KEY_ID + "_" + "A" * 43), ("key-revoke", KEY_ID.upper()), ("key-revoke", KEY_ID, "extra"),
    ("tenant-status", "--tenant", TENANT), ("tenant-status", "--tenant", TENANT, "--status", "gone"), ("tenant-status", "--tenant", "x", "--status", "active"),
    ("tenants", "--all"), ("keys", "--tenant", "nope"), ("status", "x"), ("pepper-init", "--out", "x"), ("nonsense",), ("keys", "--bogus"),
]


@pytest.mark.parametrize("args", BAD, ids=lambda a: " ".join(a)[:70].replace("\n", "\\n"))
def test_a_bad_argument_is_refused_before_docker_is_touched(box, args):
    with_pepper(box)
    r = box.run(*args)
    assert r.returncode == 2, (r.returncode, r.stderr)
    assert box.docker_calls() == []


def test_pepper_init_writes_a_strong_mode_600_secret_and_prints_none_of_it(box):
    r = box.run("pepper-init")
    assert r.returncode == 0
    p = box.secrets / "key-pepper"
    value = p.read_text().strip()
    assert stat.S_IMODE(p.stat().st_mode) == 0o600 and len(value) >= 32
    assert value not in r.stdout + r.stderr
    assert not list(box.secrets.glob("key-pepper.tmp*"))


def test_the_pepper_is_never_overwritten(box):
    first = with_pepper(box)
    r = box.run("pepper-init")
    assert r.returncode == 1 and "already exists" in r.stderr
    assert (box.secrets / "key-pepper").read_text().strip() == first


@pytest.mark.parametrize("args", [("tenant-create", "--label", "alice"), ("tenants",), ("keys",), ("key-revoke", KEY_ID),
                                  ("key-create", "--tenant", TENANT, "--out", "alice")])
def test_nothing_runs_without_a_pepper(box, args):
    r = box.run(*args)
    assert r.returncode == 1 and "pepper-init" in r.stderr
    assert box.docker_calls() == []


def test_the_pepper_must_be_mode_600(box):
    with_pepper(box)
    (box.secrets / "key-pepper").chmod(0o640)
    r = box.run("tenants")
    assert r.returncode == 1 and "600" in r.stderr and box.docker_calls() == []


def test_key_create_hands_the_cli_a_token_file_and_a_read_only_pepper_mount_and_never_the_values(box):
    pepper = with_pepper(box)
    r = box.run("key-create", "--tenant", TENANT, "--out", "alice", "--label", "alice's agent", "--scopes", "memory.read", "--expires-days", "30")
    assert r.returncode == 0, r.stderr
    (call,) = box.docker_calls()
    for needle in ("create-key", f"--tenant {TENANT}", "--token-file /keys/alice.token", "--scopes memory.read", "--expires-days 30", "migrate python -m app.tenant_keys",
                   "-e JARVIS_KEY_PEPPER_FILE=/run/key-pepper", f"{box.secrets}/key-pepper:/run/key-pepper:ro", f"--user {os.getuid()}:{os.getgid()}"):
        assert needle in call, needle
    assert pepper not in call and pepper not in r.stdout + r.stderr
    assert "jlk_" not in call + r.stdout + r.stderr
    assert (box.secrets / "issued-keys").is_dir() and stat.S_IMODE((box.secrets / "issued-keys").stat().st_mode) == 0o700


def test_an_existing_token_file_is_never_overwritten_and_the_cli_is_not_even_called(box):
    with_pepper(box)
    keys = box.secrets / "issued-keys"
    keys.mkdir()
    (keys / "alice.token").write_text("precious\n")
    r = box.run("key-create", "--tenant", TENANT, "--out", "alice")
    assert r.returncode == 1 and "never overwritten" in r.stderr
    assert (keys / "alice.token").read_text() == "precious\n" and box.docker_calls() == []


def test_the_other_commands_pass_exactly_their_arguments(box):
    with_pepper(box)
    assert box.run("tenant-create", "--label", "alice").returncode == 0
    assert box.run("key-revoke", KEY_ID).returncode == 0
    assert box.run("tenant-status", "--tenant", TENANT, "--status", "suspended").returncode == 0
    assert box.run("tenants").returncode == 0
    assert box.run("keys", "--tenant", TENANT).returncode == 0
    calls = box.docker_calls()
    assert [c.split("app.tenant_keys ", 1)[1] for c in calls] == ["create-tenant --label alice", f"revoke-key {KEY_ID}", f"set-status --tenant {TENANT} --status suspended",
                                                                   "list-tenants", f"list-keys --tenant {TENANT}"]


def test_status_reports_whether_key_authentication_is_on_and_issuing_a_key_does_not_turn_it_on(box):
    r = box.run("status")
    assert r.returncode == 0 and "pepper: absent" in r.stdout and "key authentication: off" in r.stdout
    with_pepper(box)
    (box.secrets / "app.env").write_text("JARVIS_API_KEY=secret-value-that-must-not-be-echoed\n")
    box.run("key-create", "--tenant", TENANT, "--out", "alice")
    assert "JARVIS_KEY_TENANTS" not in (box.secrets / "app.env").read_text()
    r = box.run("status")
    assert "key authentication: off" in r.stdout and "secret-value" not in r.stdout + r.stderr
    (box.secrets / "app.env").write_text("JARVIS_KEY_TENANTS=1\n")
    assert "key authentication: ON" in box.run("status").stdout


def test_jarvisctl_dispatches_keys_and_lists_it_in_help(box):
    ctl = str(box.dir / "bin" / "jarvisctl")
    help_text = subprocess.run([ctl, "help"], env=box.env_, capture_output=True, text=True).stdout
    assert "keys pepper-init|pepper-install-app|tenant-create|key-create" in help_text
    r = subprocess.run([ctl, "keys", "nonsense"], env=box.env_, capture_output=True, text=True)
    assert r.returncode == 2 and "keys.sh" in r.stderr


@pytest.mark.parametrize("line,expected", [("JARVIS_KEY_TENANTS=1", "ON"), ("JARVIS_KEY_TENANTS=TRUE", "ON"), ("JARVIS_KEY_TENANTS=Yes", "ON"), ("JARVIS_KEY_TENANTS=on", "ON"),
                                           ("JARVIS_KEY_TENANTS= 1 ", "ON"), ('JARVIS_KEY_TENANTS="true"', "ON"), ("JARVIS_KEY_TENANTS='ON'", "ON"),
                                           ("JARVIS_KEY_TENANTS=0", "off"), ("JARVIS_KEY_TENANTS=false", "off"), ("JARVIS_KEY_TENANTS=", "off"), ("JARVIS_KEY_TENANTS=maybe", "off"),
                                           ("JARVIS_KEY_TENANTS=1 # enable tenant keys", "ON"), ('JARVIS_KEY_TENANTS="true" # enable', "ON"), ("JARVIS_KEY_TENANTS=0 # was 1", "off"), ("JARVIS_KEY_TENANTS=1#not-a-comment", "off"),
                                           ("JARVIS_KEY_TENANTS='on'  # x # y", "ON"), ("# JARVIS_KEY_TENANTS=1", "off"), ("XJARVIS_KEY_TENANTS=1", "off")])
def test_status_reads_the_flag_the_way_the_application_does(box, line, expected):
    """Codex on #115: key_tenants_enabled() strips and lowercases, so status must not report 'off' for a value the app accepts (nor 'ON' for one it rejects)."""
    (box.secrets / "app.env").write_text("JARVIS_API_KEY=x\n" + line + "\n")
    out = box.run("status").stdout
    assert f"key authentication: {expected}" in out, out


def test_status_uses_the_last_assignment_like_compose(box):
    (box.secrets / "app.env").write_text("JARVIS_KEY_TENANTS=1\nJARVIS_KEY_TENANTS=0\n")
    assert "key authentication: off" in box.run("status").stdout
    (box.secrets / "app.env").write_text("JARVIS_KEY_TENANTS=0\nJARVIS_KEY_TENANTS=true\n")
    assert "key authentication: ON" in box.run("status").stdout


def test_status_does_not_turn_a_failed_query_into_an_empty_deployment(box):
    """Codex on #115: an unavailable control plane must not read as 'tenants: 0 / keys: 0'."""
    with_pepper(box)
    box.env_["FAKE_DOCKER_RC"] = "1"
    r = box.run("status")
    assert r.returncode == 1 and "tenants: unavailable" in r.stdout and "keys:    unavailable" in r.stdout
    assert "tenants: 0" not in r.stdout and "keys:    0" not in r.stdout
    box.env_["FAKE_DOCKER_RC"] = "0"
    r = box.run("status")
    assert r.returncode == 0 and "tenants: 1" in r.stdout


def test_pepper_install_app_makes_a_separate_app_owned_copy_and_never_touches_the_operator_copy(box):
    """Codex (P1) on #115: the app needs the pepper owned by uid 10001, and the operator CLI must keep reading ITS copy as the host user (key-revoke must work once keys are on)."""
    pepper = with_pepper(box)
    r = box.run("pepper-install-app")
    assert r.returncode == 0, r.stderr
    (call,) = box.docker_calls()
    for needle in ("run --rm --user 0", "--network none", "--read-only", f"-v {box.secrets}:/s", "chown 10001:10001 /s/key-pepper.app", "chmod 400 /s/key-pepper.app", "cp /s/key-pepper /s/key-pepper.app"):
        assert needle in call, needle
    assert pepper not in call + r.stdout + r.stderr
    assert (box.secrets / "key-pepper").read_text().strip() == pepper and stat.S_IMODE((box.secrets / "key-pepper").stat().st_mode) == 0o600
    # and the management commands still run as the host user against the operator copy, never the app copy
    box.run("key-revoke", KEY_ID)
    revoke = box.docker_calls()[-1]
    assert f"--user {os.getuid()}:{os.getgid()}" in revoke and f"{box.secrets}/key-pepper:/run/key-pepper:ro" in revoke and "key-pepper.app" not in revoke


def test_the_app_copy_is_never_overwritten_and_needs_the_operator_pepper_first(box):
    r = box.run("pepper-install-app")
    assert r.returncode == 1 and "pepper-init" in r.stderr and box.docker_calls() == []
    with_pepper(box)
    (box.secrets / "key-pepper.app").write_text("existing\n")
    r = box.run("pepper-install-app")
    assert r.returncode == 1 and "already exists" in r.stderr and box.docker_calls() == []
    assert (box.secrets / "key-pepper.app").read_text() == "existing\n"


def test_status_reports_the_app_copy(box):
    with_pepper(box)
    assert "app pepper copy: absent" in box.run("status").stdout
    (box.secrets / "key-pepper.app").write_text("x\n")
    assert "app pepper copy: present, identical" in box.run("status").stdout          # the fake docker answers `same`


def test_the_mcp_write_flag_comes_from_the_env_file_and_defaults_to_off():
    """The compose file used to hard-code JARVIS_MCP_WRITE_ENABLED to "false" (compose environment beats secrets/app.env), so it could not be turned on without editing a tracked file on the box.
    It is now ${JARVIS_MCP_WRITE_ENABLED:-false}: set in deploy/mint/.env, and an unset or empty value is OFF."""
    compose = (MINT / "docker-compose.yml").read_text()
    lines = [l.strip() for l in compose.splitlines() if l.strip().startswith("JARVIS_MCP_WRITE_ENABLED")]
    assert lines == ["JARVIS_MCP_WRITE_ENABLED: ${JARVIS_MCP_WRITE_ENABLED:-false}"]
    assert "JARVIS_MCP_WRITE_ENABLED=false" in (MINT / ".env.example").read_text()
