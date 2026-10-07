"""Key custody guards that can be checked today (the signer and its enforcement come later): no private key is committed, the compose
project mounts no host paths, and nothing in it mentions a signing key."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
COMPOSE = ROOT / "deploy" / "mint" / "docker-compose.yml"
PRIVATE_KEY_MARKERS = ("BEGIN OPENSSH PRIVATE KEY", "BEGIN RSA PRIVATE KEY", "BEGIN EC PRIVATE KEY", "BEGIN PRIVATE KEY", "BEGIN DSA PRIVATE KEY",
                       "BEGIN ENCRYPTED PRIVATE KEY")


def tracked_files() -> list[Path]:
    try:
        out = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, check=True).stdout
    except (OSError, subprocess.CalledProcessError):
        pytest.skip("not a git checkout")
    return [ROOT / p for p in out.decode().split("\0") if p]


def test_no_private_key_is_committed_anywhere():
    offenders = []
    for path in tracked_files():
        if not path.is_file() or path.stat().st_size > 2_000_000:
            continue
        data = path.read_bytes()
        for marker in PRIVATE_KEY_MARKERS:
            # the guards and docs may NAME the marker; a key is the marker followed by base64 and an END line
            if re.search(rb"-----" + marker.encode() + rb"-----\s*[A-Za-z0-9+/=\r\n]{40,}", data):
                offenders.append(str(path.relative_to(ROOT)))
    assert offenders == []


def test_the_trust_roots_file_holds_public_keys_only():
    text = (ROOT / "trust" / "roots.pub").read_text("utf-8")
    for line in text.splitlines():
        line = line.strip()
        assert not line or line.startswith("#") or line.startswith("ssh-ed25519 "), line
    assert "PRIVATE" not in "".join(l for l in text.splitlines() if not l.lstrip().startswith("#"))


def test_the_compose_project_mounts_no_host_path():
    text = COMPOSE.read_text("utf-8")
    mounts = re.findall(r"^\s+- ([^\s#]+:[^\s#]+)", text, re.M)
    volume_lines = [m for m in mounts if not m.startswith("127.0.0.1:") and not re.match(r"^\d+:\d+", m)]
    assert volume_lines, "the compose file should mount its named volumes"
    for m in volume_lines:
        source = m.split(":")[0]
        assert not source.startswith(("/", ".", "~", "$")), f"a host path is mounted: {m}"  # only named volumes (pgdata, appdata)


def test_the_compose_project_never_names_a_signing_key_or_the_keys_directory():
    text = COMPOSE.read_text("utf-8").lower()
    for word in ("ed25519", "id_ed25519", "signing", "ssh-keygen", "/keys", "jarvis-sign", "private_key"):
        assert word not in text, word


def test_the_secrets_gitignore_keeps_secrets_out_of_the_repository():
    ignored = (ROOT / "deploy" / "mint" / ".gitignore").read_text("utf-8")
    assert "secrets" in ignored
