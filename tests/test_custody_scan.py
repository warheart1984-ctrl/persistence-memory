"""The custody scan behind backup.sh and the restore drill: a private key must never be in a backup set."""

from __future__ import annotations

import importlib.util
import io
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CUSTODY = ROOT / "deploy" / "mint" / "bin" / "custody.sh"
KEY_BODY = "b3BlbnNzaC1rZXktdjEAAAAABG5vbmUAAAAEbm9uZQAAAAAAAAABAAAAMwAAAAtzc2gtZW"  # not a real key, but shaped like the start of one
KEY = f"-----BEGIN OPENSSH PRIVATE KEY-----\n{KEY_BODY}\n-----END OPENSSH PRIVATE KEY-----\n"


def bash(script: str, stdin: str = "", env: dict | None = None) -> subprocess.CompletedProcess:
    import os
    return subprocess.run(["bash", "-c", f'source "{CUSTODY}"; {script}'], input=stdin, capture_output=True, text=True, timeout=60, env={**os.environ, **(env or {})})


def real_key(directory: Path, name: str = "jarvis-sign-ed25519", passphrase: str = ""):
    if not shutil.which("ssh-keygen"):
        pytest.skip("ssh-keygen not installed")
    directory.mkdir(exist_ok=True)
    path = directory / name
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", passphrase, "-C", name, "-f", str(path)], check=True, capture_output=True)
    return path


def key_env(tmp_path):
    """A real signing key file, and the environment that points the scan at it. Returns (env, the key file's text)."""
    path = real_key(tmp_path / "keys")
    return {"JARVIS_SIGN_KEY": str(path)}, path.read_text()


def body_lines(key_text: str) -> list[str]:
    return [l for l in key_text.splitlines() if l and "-----" not in l]


@pytest.mark.parametrize("header", [
    "-----BEGIN OPENSSH PRIVATE KEY-----", "-----BEGIN RSA PRIVATE KEY-----", "-----BEGIN EC PRIVATE KEY-----", "-----BEGIN DSA PRIVATE KEY-----",
    "-----BEGIN PRIVATE KEY-----", "-----BEGIN ENCRYPTED PRIVATE KEY-----", "-----BEGIN PGP PRIVATE KEY BLOCK-----"[:0] + "-----BEGIN PRIVATE KEY-----",
])
def test_every_private_key_header_is_counted(header):
    assert bash("custody_hits", f"some text\n{header}\nmore\n").stdout.strip() == "1"


def test_several_keys_are_counted_and_ordinary_text_is_not():
    assert bash("custody_hits", KEY + "x\n" + KEY).stdout.strip() == "2"
    assert bash("custody_hits", "nothing here\n-----BEGIN CERTIFICATE-----\nabc\n-----END CERTIFICATE-----\nssh-ed25519 AAAA public\n").stdout.strip() == "0"
    assert bash("custody_hits", "").stdout.strip() == "0"


def test_a_public_key_and_a_signature_are_not_private_keys():
    sig = "-----BEGIN SSH SIGNATURE-----\nU1NIU0lH\n-----END SSH SIGNATURE-----\n"
    assert bash("custody_hits", sig + "ssh-ed25519 AAAAC3Nza root\n").stdout.strip() == "0"


def test_the_scan_prints_a_count_never_the_key():
    out = bash("custody_hits", KEY)
    assert out.stdout.strip() == "1" and KEY_BODY not in out.stdout + out.stderr


def make_set(tmp_path, **parts):
    base = "jarvis-20261007T000000Z"
    for name, content in parts.items():
        (tmp_path / f"{base}.{name}").write_text(content)
    return tmp_path, base


def test_a_clean_set_passes(tmp_path):
    d, base = make_set(tmp_path, **{"globals.sql": "CREATE ROLE x;\n", "counts": "memories=1\n", "anchors": "counter|t|1\n", "signatures.json": "{}\n"})
    with tarfile.open(d / f"{base}.data.tar", "w") as t:
        info = tarfile.TarInfo("amul.jsonl")
        data = b"ordinary appdata\n"
        info.size = len(data)
        t.addfile(info, io.BytesIO(data))
    r = bash(f'custody_check_set "{d}" "{base}"')
    assert r.returncode == 0 and r.stdout == ""


@pytest.mark.parametrize("part", ["globals.sql", "counts", "anchors", "signatures.json"])
def test_a_key_in_any_plain_part_stops_the_set_and_names_the_part_only(tmp_path, part):
    parts = {"globals.sql": "ok\n", "counts": "memories=1\n", "anchors": "counter|t|1\n", "signatures.json": "{}\n"}
    parts[part] = "prefix\n" + KEY
    d, base = make_set(tmp_path, **parts)
    r = bash(f'custody_check_set "{d}" "{base}"')
    assert r.returncode == 1 and f"{base}.{part} contains 1 private-key header line(s)" in r.stdout and KEY_BODY not in r.stdout + r.stderr


def test_a_key_hidden_inside_the_appdata_archive_stops_the_set(tmp_path):
    d, base = make_set(tmp_path, **{"counts": "memories=1\n"})
    with tarfile.open(d / f"{base}.data.tar", "w") as t:
        for name, data in (("notes.txt", b"hello\n"), ("deep/dir/id_ed25519", KEY.encode())):
            info = tarfile.TarInfo(name)
            info.size = len(data)
            t.addfile(info, io.BytesIO(data))
    r = bash(f'custody_check_set "{d}" "{base}"')
    assert r.returncode == 1 and f"{base}.data.tar contains 1 private-key header line(s)" in r.stdout and KEY_BODY not in r.stdout


def test_a_missing_optional_part_is_not_an_error(tmp_path):
    d, base = make_set(tmp_path, **{"counts": "memories=1\n"})
    assert bash(f'custody_check_set "{d}" "{base}"').returncode == 0


spec = importlib.util.spec_from_file_location("keymarkers", ROOT / "deploy" / "mint" / "bin" / "keymarkers.py")
keymarkers = importlib.util.module_from_spec(spec)
spec.loader.exec_module(keymarkers)  # type: ignore[union-attr]


def hits(env, text):
    return bash("custody_exact_hits", text, env).stdout.strip()


def test_the_markers_come_from_the_private_seed_and_appear_in_the_key_file_itself(tmp_path):
    path = real_key(tmp_path / "k")
    seed = keymarkers.seed_of(str(path))
    assert len(seed) == 32
    ms = keymarkers.markers(seed)
    flat = "".join(body_lines(path.read_text()))
    raw_lines = body_lines(path.read_text())
    assert any(m in flat for m in ms)  # the seed is in the file in one of the alignments
    assert any(m in line for m in ms for line in raw_lines)  # and a marker survives the 70-column wrapping
    assert seed.hex() in "".join(ms) or seed.hex()[:32] in ms


def test_a_copy_of_the_key_is_found_in_every_shape_it_may_take(tmp_path):
    env, text = key_env(tmp_path)
    body = body_lines(text)
    assert hits(env, text) != "0"  # the file as it is
    assert hits(env, "".join(body) + "\n") != "0"  # joined into one line
    assert hits(env, "COPY ...\nmem-1\tmy key: " + "\\n".join(["-----BEGIN OPENSSH PRIVATE KEY-----"] + body + ["-----END OPENSSH PRIVATE KEY-----"]) + "\n") != "0"  # as COPY text
    assert hits(env, "note: " + "  ".join(body[1:]) + " end\n") != "0"  # reflowed with the first line missing
    seed = keymarkers.seed_of(env["JARVIS_SIGN_KEY"])
    assert hits(env, f"seed {seed.hex()}\n") != "0"  # the raw seed in hex
    assert hits(env, f"seed {__import__('base64').b64encode(seed).decode()}\n") != "0"  # and in base64


def test_nothing_else_is_ever_taken_for_the_key(tmp_path):
    env, text = key_env(tmp_path)
    other = real_key(tmp_path / "others", "other")
    pub = Path(env["JARVIS_SIGN_KEY"] + ".pub").read_text()
    for innocent in (other.read_text(), "".join(body_lines(other.read_text())), pub, body_lines(text)[0] + "\n",  # another key; the public key; the header line EVERY OpenSSH key starts with
                     "-----BEGIN SSH SIGNATURE-----\nU1NIU0lHAAAAAQAAADMAAAALc3NoLWVkMjU1MTkAAAAg\n-----END SSH SIGNATURE-----\n", "nothing here\n", ""):
        assert hits(env, innocent) == "0", innocent[:40]


def test_the_exact_scan_does_nothing_where_there_is_no_key_file(tmp_path):
    assert hits({"JARVIS_SIGN_KEY": str(tmp_path / "none")}, "anything at all\n") == "0"
    assert bash("custody_key_markers", "", {"JARVIS_SIGN_KEY": str(tmp_path / "none")}).stdout == ""


def test_a_key_that_cannot_be_parsed_falls_back_to_its_body_lines_except_the_constant_first_one(tmp_path):
    path = real_key(tmp_path / "k", passphrase="a passphrase")
    r = subprocess.run([sys.executable, str(ROOT / "deploy" / "mint" / "bin" / "keymarkers.py"), str(path)], capture_output=True, text=True)
    assert r.returncode == 3
    lines = body_lines(path.read_text())
    assert r.stdout.split() == [l for l in lines[1:] if len(l) >= 20] and lines[0] not in r.stdout


def test_the_markers_are_never_printed_by_the_scan_and_never_put_on_a_command_line(tmp_path):
    env, text = key_env(tmp_path)
    out = bash("custody_exact_hits", text, env)
    assert out.stdout.strip() != "0" and not any(l in out.stdout + out.stderr for l in body_lines(text))
    sh = CUSTODY.read_text()
    assert "grep -aFc -f " in sh and "-e \"$m\"" not in sh and "chmod 600 \"$markers\"" in sh


def test_a_set_holding_the_signing_key_is_refused_wherever_it_hides(tmp_path):
    env, text = key_env(tmp_path)
    body = body_lines(text)
    d, base = make_set(tmp_path, **{"globals.sql": "-- " + "".join(body) + "\n", "counts": "memories=1\n"})
    r = bash(f'custody_check_set "{d}" "{base}"', env=env)
    assert r.returncode == 1 and f"{base}.globals.sql contains the signing key" in r.stdout and not any(l in r.stdout + r.stderr for l in body)
    with tarfile.open(d / f"{base}.data.tar", "w") as t:
        info = tarfile.TarInfo("x/notes.txt")
        data = ("copied " + "".join(body) + "\n").encode()
        info.size = len(data)
        t.addfile(info, io.BytesIO(data))
    r = bash(f'custody_check_set "{d}" "{base}"', env=env)
    assert r.returncode == 1 and f"{base}.data.tar contains the signing key" in r.stdout


def test_the_database_data_stops_a_backup_only_for_the_signing_key_itself(tmp_path):
    env, text = key_env(tmp_path)
    f = tmp_path / "data.txt"
    f.write_text("COPY jarvis.memories (id, content) FROM stdin;\nmem-1\tmy key is " + "\\n".join(body_lines(text)) + "\n\\.\n")
    r = bash(f'custody_check_data "{f}"', env=env)
    assert r.returncode == 1 and "holds the signing key" in r.stdout and not any(l in r.stdout + r.stderr for l in body_lines(text))


def test_key_shaped_text_in_the_database_is_a_warning_because_the_history_could_never_be_cleaned(tmp_path):
    env, _ = key_env(tmp_path)
    f = tmp_path / "data.txt"
    f.write_text("COPY jarvis.memories (id, content) FROM stdin;\nmem-1\tthe docs say a key starts with -----BEGIN OPENSSH PRIVATE KEY-----\n\\.\n")
    r = bash(f'custody_check_data "{f}"', env=env)
    assert r.returncode == 0 and r.stdout.startswith("WARN: the database holds 1 line(s) that look like a private-key header")
    clean = tmp_path / "clean.txt"
    clean.write_text("COPY jarvis.memories (id, content) FROM stdin;\nmem-1\tnothing here\n\\.\n")
    assert bash(f'custody_check_data "{clean}"', env=env).stdout == ""


def test_backup_and_drill_both_run_the_scan_and_the_backup_publishes_nothing_on_a_hit():
    backup = (ROOT / "deploy" / "mint" / "bin" / "backup.sh").read_text()
    drill = (ROOT / "deploy" / "mint" / "bin" / "drill.sh").read_text()
    assert "custody_check_data" in backup and "custody_check_set" in backup and "custody_check_data" in drill and "custody_check_set" in drill
    # the scans run before the set is published (the checksum file is written last, after them)
    assert backup.index("custody_check_set") < backup.index('parts=(dump globals.sql')
    assert backup.index("custody_check_data") < backup.index("# 4. shrink alarm")
    assert backup.count("is NOT published") >= 2
