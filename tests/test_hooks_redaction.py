"""The hooks' secret filter (used by the ledger MCP server's write tool) finds credentials without leaking them.

Fake secrets are assembled at run time from pieces, so no real-looking token is committed to the repository.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

_HOOKS = Path(__file__).resolve().parents[1] / "agent-hooks"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(f"redaction_{name}", _HOOKS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(_HOOKS))
    try:
        spec.loader.exec_module(module)
    finally:
        sys.path.remove(str(_HOOKS))
    return module


@pytest.fixture
def common(monkeypatch):
    for var in ("JARVIS_API_KEY", "JARVIS_API_KEY_FILE", "EMR_RECALL_API_KEY", "JARVIS_HOOK_SECRET_ENTROPY"):
        monkeypatch.delenv(var, raising=False)
    return _load("jarvis_common")


# --- what must be caught ------------------------------------------------------------------------------------

_POSITIVE = [
    ("private-key-block", "here it is:\n-----BEGIN " + "RSA PRIVATE KEY-----\nMIIEvQIBADANBg"),
    ("private-key-block", "-----BEGIN " + "OPENSSH PRIVATE KEY-----"),
    ("age-secret-key", "AGE-SECRET-" + "KEY-1" + "QPZRY9X8GF2TVDW0S3JN54KHCE6MUA7L"),
    ("openai-style-key", "use sk-" + "a1B2c3D4" * 5 + " for the call"),
    ("openai-style-key", "sk-" + "ant-" + "api03-" + "Zz9Yy8Xx7Ww6Vv5Uu4Tt3"),
    ("github-token", "token ghp_" + "A1b2C3d4E5" * 4),
    ("github-token", "github_pat_" + "11ABCDEFG0" + "x" * 30),
    ("gitlab-token", "glpat-" + "x1y2z3" * 5),
    ("slack-token", "xox" + "b-" + "1234567890-abcdefghijkl"),
    ("aws-access-key-id", "AKIA" + "ABCDEFGHIJKLMNOP"),
    ("google-api-key", "AIza" + "Sy" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6q"),
    ("jwt", "eyJ" + "hbGciOiJIUzI1NiJ9" + ".eyJ" + "zdWIiOiIxMjM0NTY3ODkwIn0" + "." + "dBjftJeZ4CVPmB92K27uhbUJU1p1r"),
    ("bearer-token", "Authorization: Bearer " + "abcDEF123._~+/-" * 2),
    ("bearer-token", "curl -H 'bearer " + "q" * 24 + "'"),
    ("authorization-header", "Authorization: Basic " + "dXNlcjpwYXNzd29yZA=="),
    ("secret-assignment", "password = hunter22!"),
    ("secret-assignment", "password: hunter22"),
    ("secret-assignment", 'api_key = "abcd1234efgh"'),
    ("secret-assignment", "export API-KEY=abcdefgh12345"),
    ("secret-assignment", "client_secret: s3cr3tV4lue"),
    ("secret-assignment", "TOKEN=abc12345"),
    ("url-credentials", "postgresql://jarvis_app:" + "s3cretpw" + "@db:5432/jarvis"),
    ("url-credentials", "https://user:" + "pa55w0rd" + "@example.com/path"),
]


@pytest.mark.parametrize(("name", "text"), _POSITIVE)
def test_credentials_are_found_and_the_match_itself_is_never_returned(common, name, text):
    found = common.find_secrets("Session notes. " + text + " Done.")
    assert name in found
    # only pattern names come back: nothing from the text can be in them
    assert all(token not in "".join(found) for token in text.split() if len(token) >= 8)


def test_the_ledgers_own_api_key_is_caught_even_in_a_shape_no_pattern_knows(common, monkeypatch):
    monkeypatch.setenv("JARVIS_API_KEY", "Zebra-Quartz-Lantern-91")
    assert common.find_secrets("the key is Zebra-Quartz-Lantern-91, careful") == ["ledger-api-key"]


def test_the_key_from_the_key_file_is_caught_too(common, monkeypatch, tmp_path):
    key_file = tmp_path / "api-key"
    key_file.write_text("Mango-Copper-Harbor-4471\n", "utf-8")
    monkeypatch.setenv("JARVIS_API_KEY_FILE", str(key_file))
    assert common.find_secrets("note Mango-Copper-Harbor-4471 end") == ["ledger-api-key"]


def test_a_very_short_configured_key_is_not_used_for_exact_matching(common, monkeypatch):
    monkeypatch.setenv("JARVIS_API_KEY", "abc")  # would match half the alphabet if it were used
    assert common.find_secrets("abc and more words") == []


# --- what must NOT be caught -------------------------------------------------------------------------------

_NEGATIVE = [
    "Session s1 ended (ended) at 2026-10-05T12:00Z. Outcome note: refactored the retry loop.",
    "We decided to use Postgres row-level security for the ledger.",
    "The password policy needs review next week.",
    "Token budget for this task is 200k.",
    "content_sha256 is 124773df194c8d228ba01a5b3b7e08c0a477be995145fddfa13834246a10f5fb",
    "memory mem-da3ddefda22a supersedes mem-79f9aa7e7e44",
    "see http://127.0.0.1:8011/api/jarvis/memory and https://github.com/warheart1984-ctrl/persistence-memory",
    "ssh -N -L 127.0.0.1:8011:127.0.0.1:8011 jon@192.168.1.102",
    "the secret to a good ledger is append-only history",
    "persistence_memory_agent_hooks_jarvis_common_py_session_end_hook_module_name",
    "",
]


@pytest.mark.parametrize("text", _NEGATIVE)
def test_ordinary_text_is_not_flagged(common, text):
    assert common.find_secrets(text) == []


def test_the_entropy_rule_is_off_by_default_and_never_flags_hex(common, monkeypatch):
    blob = "Qm9x" + "7Kp2WzR9tLs4VnB8yHc3JdF6gXa1Ue5Ti0Mo" + "ZqPwEr"  # 40+ chars, mixed, random-looking
    assert common.find_secrets("x " + blob) == []
    monkeypatch.setenv("JARVIS_HOOK_SECRET_ENTROPY", "1")
    assert common.find_secrets("x " + blob) == ["high-entropy-string"]
    sha = "124773df194c8d228ba01a5b3b7e08c0a477be995145fddfa13834246a10f5fb"
    assert common.find_secrets("hash " + sha) == []


# --- the refusal log ----------------------------------------------------------------------------------------

def test_the_refusal_log_has_pattern_names_only(common, monkeypatch, tmp_path):
    monkeypatch.setattr(common, "state_dir", lambda: tmp_path)
    secret = "sk-" + "a1B2c3D4" * 5
    line = common.log_refusal("sessionEnd", "sess/with spaces&" + secret, ["openai-style-key"])
    logged = (tmp_path / "jarvis-hook-refusals.log").read_text("utf-8")
    assert "openai-style-key" in logged and "sessionEnd" in logged
    assert secret not in logged and secret not in line
    assert "session=<redacted>" in line  # a session id that carries a secret is not logged at all
    plain = common.log_refusal("sessionEnd", "sess-42", ["jwt"])
    assert "session=sess-42" in plain


# --- the sessionEnd hook is retired -----------------------------------------------------------------------------

_HOOKS_DIR = Path(__file__).resolve().parents[1] / "agent-hooks"


@pytest.fixture
def end(monkeypatch, tmp_path, common):
    module = _load("jarvis_session_end")
    shared = sys.modules["jarvis_common"]  # the instance the hook imported from
    monkeypatch.setattr(shared, "state_dir", lambda: tmp_path)
    calls: list = []
    monkeypatch.setattr(shared.urllib.request, "urlopen", lambda *a, **k: calls.append(a) or (_ for _ in ()).throw(OSError("no network in this test")))
    module._calls = calls
    module._dir = tmp_path
    return module


@pytest.mark.parametrize(
    "reply",
    [
        "We decided to keep the retry loop simple.",
        "the key is sk-" + "a1B2c3D4" * 5,
        "-----BEGIN " + "RSA PRIVATE KEY-----",
        "",
    ],
)
def test_session_end_posts_nothing_whatever_the_session_said(end, monkeypatch, capsys, tmp_path, reply):
    (tmp_path / "last.txt").write_text(reply, "utf-8")
    monkeypatch.setattr(sys.modules["jarvis_common"], "last_response_path", lambda: tmp_path / "last.txt")
    monkeypatch.setattr(end, "read_stdin_json", lambda: {"session_id": "sess-1", "reason": "ended"})
    assert end.main() == 0
    out, err = capsys.readouterr()
    assert json.loads(out) == {} and err == ""
    assert end._calls == []  # no request was even attempted
    assert sorted(p.name for p in end._dir.iterdir()) == ["last.txt"]  # nothing written either (no log, no state)


def test_session_end_no_longer_knows_how_to_reach_the_ledger():
    text = (_HOOKS_DIR / "jarvis_session_end.py").read_text("utf-8")
    for needle in ("try_http_json", "http_json", "/api/jarvis/memory", "cursor-sessionEnd", "find_secrets", "urllib"):
        assert needle not in text, needle


def test_the_hooks_template_no_longer_lists_session_end():
    template = json.loads((_HOOKS_DIR / "hooks.json").read_text("utf-8"))
    assert "sessionEnd" not in template["hooks"] and set(template["hooks"]) == {"sessionStart", "afterAgentResponse"}
