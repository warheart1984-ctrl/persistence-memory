"""The stdio proxy must present a key the server will accept."""

from __future__ import annotations

import io
import json
from unittest.mock import patch

import pytest

from mcp_server.emr_stdio import _http_post


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _sent_headers(monkeypatch, *, recall=None, jarvis=None) -> dict[str, str]:
    monkeypatch.setenv("JARVIS_MEMORYBOARD_URL", "http://127.0.0.1:8011")
    for name, value in (("EMR_RECALL_API_KEY", recall), ("JARVIS_API_KEY", jarvis)):
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured.update({k.lower(): v for k, v in req.header_items()})
        return _Resp(json.dumps({"ok": True}).encode())

    with patch("urllib.request.urlopen", fake_urlopen):
        _http_post("/api/jarvis/tools/emr_recall", {"query": "x"})
    return captured


def test_falls_back_to_jarvis_api_key(monkeypatch):
    assert _sent_headers(monkeypatch, jarvis="ledger-key")["authorization"] == "Bearer ledger-key"


def test_recall_key_takes_precedence(monkeypatch):
    headers = _sent_headers(monkeypatch, recall="recall-key", jarvis="ledger-key")
    assert headers["authorization"] == "Bearer recall-key"


def test_no_key_sends_no_authorization(monkeypatch):
    assert "authorization" not in _sent_headers(monkeypatch)


def test_without_a_url_nothing_is_sent_and_the_key_is_not_used(monkeypatch):
    monkeypatch.delenv("JARVIS_MEMORYBOARD_URL", raising=False)
    monkeypatch.setenv("JARVIS_API_KEY", "must-not-leak")
    attempted = []
    with patch("urllib.request.urlopen", lambda *a, **k: attempted.append(True)):
        with pytest.raises(RuntimeError, match="JARVIS_MEMORYBOARD_URL is not set") as exc:
            _http_post("/api/jarvis/tools/emr_recall", {"query": "x"})
    assert attempted == [] and "must-not-leak" not in str(exc.value)


def test_the_key_is_never_sent_over_plain_http_to_a_non_loopback_host(monkeypatch):
    monkeypatch.setenv("JARVIS_MEMORYBOARD_URL", "http://192.0.2.7:8011")
    monkeypatch.setenv("JARVIS_API_KEY", "must-not-leak")
    attempted = []
    with patch("urllib.request.urlopen", lambda *a, **k: attempted.append(True)):
        with pytest.raises(RuntimeError, match="plain http"):
            _http_post("/api/jarvis/tools/emr_recall", {"query": "x"})
    assert attempted == []


# --- the key file (JARVIS_API_KEY_FILE) ----------------------------------------------------------------------

def _post_with(monkeypatch, *, key_file=None, jarvis=None, recall=None, response=None):
    """Run _http_post once; returns (request headers or None if nothing was sent, exception or None)."""
    monkeypatch.setenv("JARVIS_MEMORYBOARD_URL", "http://127.0.0.1:8011")
    for name, value in (("JARVIS_API_KEY_FILE", key_file), ("JARVIS_API_KEY", jarvis), ("EMR_RECALL_API_KEY", recall)):
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, str(value))
    sent = {}

    def fake_urlopen(req, timeout=None):
        sent.update({k.lower(): v for k, v in req.header_items()})
        if response is not None:
            raise response(req)
        return _Resp(json.dumps({"ok": True}).encode())

    error = None
    with patch("urllib.request.urlopen", fake_urlopen):
        try:
            _http_post("/api/jarvis/tools/emr_recall", {"query": "x"})
        except RuntimeError as exc:
            error = exc
    return (sent or None), error


def test_the_key_is_read_from_the_key_file(monkeypatch, tmp_path):
    key_file = tmp_path / "api-key"
    key_file.write_text("file-key-456\nsecond line ignored\n", "utf-8")
    headers, error = _post_with(monkeypatch, key_file=key_file)
    assert error is None and headers["authorization"] == "Bearer file-key-456"


def test_an_environment_key_wins_over_the_file_when_the_file_is_fine(monkeypatch, tmp_path):
    key_file = tmp_path / "api-key"
    key_file.write_text("file-key", "utf-8")
    headers, _ = _post_with(monkeypatch, key_file=key_file, jarvis="env-key")
    assert headers["authorization"] == "Bearer env-key"
    headers, _ = _post_with(monkeypatch, key_file=key_file, jarvis="env-key", recall="recall-key")
    assert headers["authorization"] == "Bearer recall-key"


@pytest.mark.parametrize("content", [None, "", "   \n\n", "\n"])
def test_a_named_key_file_that_is_missing_or_empty_sends_nothing(monkeypatch, tmp_path, content):
    key_file = tmp_path / "api-key"
    if content is not None:
        key_file.write_text(content, "utf-8")
    headers, error = _post_with(monkeypatch, key_file=key_file)
    assert headers is None  # no request at all, and so no empty key either
    assert error is not None and "Nothing was sent" in str(error) and str(key_file) in str(error)


def test_a_key_file_that_is_a_directory_sends_nothing(monkeypatch, tmp_path):
    headers, error = _post_with(monkeypatch, key_file=tmp_path)
    assert headers is None and "Nothing was sent" in str(error)


def test_a_bad_key_file_refuses_even_when_an_environment_key_is_set(monkeypatch, tmp_path):
    headers, error = _post_with(monkeypatch, key_file=tmp_path / "does-not-exist", jarvis="env-key")
    assert headers is None and error is not None and "env-key" not in str(error)


def test_a_blank_key_file_setting_counts_as_not_named(monkeypatch):
    headers, error = _post_with(monkeypatch, key_file="")
    assert error is None and "authorization" not in headers  # no header at all, never "Bearer "


@pytest.mark.parametrize("value", ["", "   "])
def test_an_empty_environment_key_is_never_sent(monkeypatch, value):
    headers, error = _post_with(monkeypatch, jarvis=value)
    assert error is None and "authorization" not in headers


def test_the_key_never_appears_in_an_error_even_if_the_server_echoes_it(monkeypatch, tmp_path):
    key_file = tmp_path / "api-key"
    key_file.write_text("Zebra-Quartz-Lantern-91", "utf-8")

    def http_error(req):
        import urllib.error

        return urllib.error.HTTPError(req.full_url, 401, "denied", {}, io.BytesIO(b"bad key Zebra-Quartz-Lantern-91"))

    headers, error = _post_with(monkeypatch, key_file=key_file, response=http_error)
    assert error is not None and "Zebra-Quartz-Lantern-91" not in str(error) and "<key>" in str(error)


def test_the_key_from_the_file_is_still_refused_over_plain_http_off_loopback(monkeypatch, tmp_path):
    key_file = tmp_path / "api-key"
    key_file.write_text("file-key-456", "utf-8")
    monkeypatch.setenv("JARVIS_MEMORYBOARD_URL", "http://192.0.2.7:8011")
    monkeypatch.setenv("JARVIS_API_KEY_FILE", str(key_file))
    attempted = []
    with patch("urllib.request.urlopen", lambda *a, **k: attempted.append(True)):
        with pytest.raises(RuntimeError, match="plain http"):
            _http_post("/api/jarvis/tools/emr_recall", {"query": "x"})
    assert attempted == []
