"""A key file saved by Windows (UTF-8 BOM, UTF-16) must work, and a bad one must be refused cleanly, never crash."""

from __future__ import annotations

import importlib.util
import io
import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from mcp_server import ledger_stdio
from mcp_server.emr_stdio import _http_post, api_key as emr_api_key
from mcp_server.jarvis_keyfile import KeyFileError, parse_key_text, read_key_file

KEY = "Zebra-Quartz-Lantern-91"
_HOOKS = Path(__file__).resolve().parents[1] / "agent-hooks"


def _hooks_common():
    spec = importlib.util.spec_from_file_location("keyfile_jarvis_common", _HOOKS / "jarvis_common.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# every way Windows (or a user) might save the same key
_ENCODINGS = {
    "utf-8": KEY.encode("utf-8"),
    "utf-8 + newline": (KEY + "\n").encode("utf-8"),
    "utf-8 + CRLF": (KEY + "\r\n").encode("utf-8"),
    "utf-8 BOM": b"\xef\xbb\xbf" + KEY.encode("utf-8"),
    "utf-8 BOM + CRLF": b"\xef\xbb\xbf" + (KEY + "\r\n").encode("utf-8"),
    "utf-16 (BOM, little-endian: PowerShell Out-File)": (KEY + "\r\n").encode("utf-16"),
    "utf-16 BOM big-endian": b"\xfe\xff" + (KEY + "\n").encode("utf-16-be"),
    "utf-16-le without a BOM": (KEY + "\n").encode("utf-16-le"),
    "utf-16-be without a BOM": (KEY + "\n").encode("utf-16-be"),
    "extra lines are ignored": (KEY + "\nnot the key\nneither this\n").encode("utf-8"),
    "leading blank line and spaces": ("\n  " + KEY + "  \n").encode("utf-8"),
}


@pytest.mark.parametrize("raw", _ENCODINGS.values(), ids=list(_ENCODINGS))
def test_every_encoding_gives_the_same_key(raw):
    assert parse_key_text(raw) == KEY


@pytest.mark.parametrize("raw", _ENCODINGS.values(), ids=list(_ENCODINGS))
def test_the_hooks_copy_agrees(raw, tmp_path):
    f = tmp_path / "k"
    f.write_bytes(raw)
    assert _hooks_common()._read_key_file(str(f)) == KEY


# what must be refused
_BAD = {
    "empty": b"",
    "only newlines": b"\n\r\n",
    "only a BOM": b"\xef\xbb\xbf",
    "only a utf-16 BOM": b"\xff\xfe",
    "invalid utf-8": b"\xff\xfe\xfa\x80abc\xc3(",
    "binary with zeros": b"\x00\x01\x02\x00\x00\x03",
    "odd-length utf-16": b"\xff\xfe" + b"a\x00b",
    "a space inside": b"Bearer abc123def456",
    "non-ascii": "clé-secrète-9".encode("utf-8"),
    "control character": b"abc\x07def123456",
    "tab inside": b"abc\tdef123456",
}


@pytest.mark.parametrize("raw", _BAD.values(), ids=list(_BAD))
def test_a_malformed_key_file_is_refused_with_a_clean_error(raw, tmp_path):
    f = tmp_path / "k"
    f.write_bytes(raw)
    with pytest.raises(KeyFileError) as exc:
        read_key_file(f)
    assert not isinstance(exc.value, UnicodeError)
    assert _hooks_common()._read_key_file(str(f)) is None  # hooks: no key, no crash


def test_missing_file_and_directory_are_refused(tmp_path):
    for target in (tmp_path / "nope", tmp_path):
        with pytest.raises(KeyFileError, match="missing or cannot be read"):
            read_key_file(target)


def test_the_error_never_contains_the_files_content(tmp_path):
    f = tmp_path / "k"
    f.write_bytes(b"Zebra Quartz Lantern 91")
    with pytest.raises(KeyFileError) as exc:
        read_key_file(f)
    assert "Zebra" not in str(exc.value)


# --- each consumer ------------------------------------------------------------------------------------------

class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for var in ("JARVIS_API_KEY", "JARVIS_API_KEY_FILE", "EMR_RECALL_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("JARVIS_MEMORYBOARD_URL", "http://127.0.0.1:8011")


@pytest.mark.parametrize("raw", _ENCODINGS.values(), ids=list(_ENCODINGS))
def test_emr_stdio_sends_the_right_bearer_whatever_the_encoding(raw, tmp_path, monkeypatch):
    f = tmp_path / "k"
    f.write_bytes(raw)
    monkeypatch.setenv("JARVIS_API_KEY_FILE", str(f))
    sent = {}

    def fake_urlopen(req, timeout=None):
        sent.update({k.lower(): v for k, v in req.header_items()})
        return _Resp(json.dumps({"ok": True}).encode())

    with patch("urllib.request.urlopen", fake_urlopen):
        _http_post("/api/jarvis/tools/emr_recall", {"query": "x"})
    assert sent["authorization"] == f"Bearer {KEY}"


@pytest.mark.parametrize("raw", _BAD.values(), ids=list(_BAD))
def test_emr_stdio_refuses_a_malformed_key_file_before_any_request(raw, tmp_path, monkeypatch):
    f = tmp_path / "k"
    f.write_bytes(raw)
    monkeypatch.setenv("JARVIS_API_KEY_FILE", str(f))
    attempted = []
    with patch("urllib.request.urlopen", lambda *a, **k: attempted.append(True)):
        with pytest.raises(RuntimeError, match="Nothing was sent") as exc:
            _http_post("/api/jarvis/tools/emr_recall", {"query": "x"})
    assert attempted == [] and not isinstance(exc.value, UnicodeError)
    with pytest.raises(RuntimeError):
        emr_api_key()


@pytest.mark.parametrize("raw", _ENCODINGS.values(), ids=list(_ENCODINGS))
def test_the_ledger_mcp_server_reads_the_key_whatever_the_encoding(raw, tmp_path, monkeypatch):
    f = tmp_path / "k"
    f.write_bytes(raw)
    monkeypatch.setenv("JARVIS_API_KEY_FILE", str(f))
    assert ledger_stdio._api_key() == KEY


@pytest.mark.parametrize("raw", _BAD.values(), ids=list(_BAD))
def test_the_ledger_mcp_server_refuses_a_malformed_key_file_cleanly(raw, tmp_path, monkeypatch):
    f = tmp_path / "k"
    f.write_bytes(raw)
    monkeypatch.setenv("JARVIS_API_KEY_FILE", str(f))
    attempted = []
    monkeypatch.setattr(ledger_stdio.urllib.request, "urlopen", lambda *a, **k: attempted.append(True))
    result = ledger_stdio.handle_tools_call({"name": "health", "arguments": {}})
    assert result["isError"] is True and result["structuredContent"]["error"]["code"] == "no_key"
    assert "Nothing was sent" in result["content"][0]["text"] and attempted == []


@pytest.mark.parametrize("raw", _ENCODINGS.values(), ids=list(_ENCODINGS))
def test_the_hooks_read_the_key_whatever_the_encoding(raw, tmp_path, monkeypatch):
    f = tmp_path / "k"
    f.write_bytes(raw)
    monkeypatch.setenv("JARVIS_API_KEY_FILE", str(f))
    assert _hooks_common().api_key() == KEY


def test_the_ledger_script_still_runs_standalone_with_a_utf16_key_file(tmp_path):
    """python mcp_server/ledger_stdio.py (no package on the path) must find its keyfile module."""
    import os
    import subprocess

    f = tmp_path / "k"
    f.write_bytes((KEY + "\r\n").encode("utf-16"))
    env = {k: v for k, v in os.environ.items() if not k.startswith(("JARVIS_", "PYTHON"))}
    env.update({"JARVIS_MEMORYBOARD_URL": "http://127.0.0.1:9", "JARVIS_API_KEY_FILE": str(f)})
    proc = subprocess.run(
        [sys.executable, "-B", str(Path(__file__).resolve().parents[1] / "mcp_server" / "ledger_stdio.py")],
        input=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}) + "\n",
        capture_output=True,
        text=True,
        env=env,
        cwd=str(tmp_path),
        timeout=60,
    )
    assert json.loads(proc.stdout.splitlines()[0])["id"] == 1
    assert "No API key" not in proc.stderr and "names" not in proc.stderr and KEY not in proc.stdout + proc.stderr
