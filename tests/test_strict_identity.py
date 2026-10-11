"""Strict identity (RFC phase 0): "operator" is something a request IS, never what it falls back to.

* In every mode the identity middleware binds an explicit operator principal when the operator credential was presented (so the call log, the stores and the
  gates read an identity instead of inferring one from its absence).
* JARVIS_MULTI_TENANT makes the absence of an identity a refusal everywhere: no store, no operator-only surface, no "operator" label in the logs, and a
  configuration that could fail open does not start.
* Without the flag nothing changes for the shared-key deployment (these tests pin both sides).
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app import auth, call_log, drift
from app.identity import (OPERATOR_TENANT, UNAUTHENTICATED_LABEL, Principal, current_principal, is_operator, operator_principal, request_tenant,
                          reset_principal, set_principal)
from app.main import app
from app.store import get_store, reset_store_for_tests
from app.store_errors import TenantUnresolvedError

KEY = "strict-identity-ledger-key-0123456789"
RECALL = "strict-identity-recall-key-0123456789"


@pytest.fixture
def keyed(monkeypatch):
    monkeypatch.setenv("JARVIS_API_KEY", KEY)
    monkeypatch.delenv("JARVIS_ALLOW_UNAUTHENTICATED", raising=False)
    monkeypatch.delenv("EMR_RECALL_API_KEY", raising=False)
    monkeypatch.delenv("JARVIS_MULTI_TENANT", raising=False)
    monkeypatch.setenv("JARVIS_MEMORY_WRITE_ENABLED", "true")
    monkeypatch.setenv("JARVIS_CURSOR_HMAC_KEY", "strict-identity-cursor-key-0123456789")
    reset_store_for_tests()
    return monkeypatch


# ------------------------------------------------------------------------------------- who carries the operator credential
@pytest.mark.parametrize(
    "path,headers,expected",
    [
        ("/api/jarvis/memory", {"x-api-key": KEY}, True),
        ("/api/jarvis/memory", {"authorization": f"Bearer {KEY}"}, True),
        ("/api/jarvis/memory", {"x-api-key": "wrong"}, False),
        ("/api/jarvis/memory", {}, False),
        ("/api/jarvis/tools/emr_latest", {"x-api-key": KEY}, True),          # no recall key configured: the ledger key opens the tool routes
        ("/mcp", {"authorization": f"Bearer {KEY}"}, True),
        ("/api/jarvis/rag/status", {"x-api-key": KEY}, False),                # the RAG routes have a key of their own: the ledger key is not the operator there
    ],
)
def test_who_carries_the_operator_credential(keyed, path, headers, expected):
    assert auth.credential_is_operator(headers, path) is expected


def test_a_configured_recall_key_decides_the_tool_routes_and_the_ledger_key_no_longer_opens_them(keyed):
    keyed.setenv("EMR_RECALL_API_KEY", RECALL)
    assert auth.credential_is_operator({"authorization": f"Bearer {RECALL}"}, "/api/jarvis/tools/calls") is True
    assert auth.credential_is_operator({"x-emr-recall-key": RECALL}, "/mcp") is True
    assert auth.credential_is_operator({"x-api-key": KEY}, "/api/jarvis/tools/calls") is False
    assert auth.credential_is_operator({"x-api-key": KEY}, "/api/jarvis/memory") is True     # the ledger routes still take the ledger key


def test_with_no_key_configured_only_the_explicit_local_dev_opt_out_is_the_operator(keyed):
    keyed.delenv("JARVIS_API_KEY")
    assert auth.credential_is_operator({}, "/api/jarvis/memory") is False
    keyed.setenv("JARVIS_ALLOW_UNAUTHENTICATED", "1")
    assert auth.credential_is_operator({}, "/api/jarvis/memory") is True


def test_a_public_deployment_without_a_recall_key_has_no_operator_on_the_tool_routes(keyed):
    keyed.setenv("JARVIS_PUBLIC_MODE", "1")
    assert auth.credential_is_operator({"x-api-key": KEY}, "/api/jarvis/tools/calls") is False


def test_oauth_mode_has_no_operator(keyed):
    keyed.setenv("JARVIS_AUTH_MODE", "oauth")
    assert auth.credential_is_operator({"x-api-key": KEY}, "/api/jarvis/memory") is False
    assert auth.caller_is_operator() is False


# ------------------------------------------------------------------------------------- the identity the request carries
def test_a_request_with_the_operator_key_is_bound_to_an_operator_principal(keyed):
    seen = {}
    real = call_log.make_fields

    def spy(**kw):
        seen["tenant"] = kw.get("tenant")
        return real(**kw)

    keyed.setattr(call_log, "make_fields", spy)
    with TestClient(app) as c:
        r = c.post("/api/jarvis/tools/emr_latest", headers={"X-API-Key": KEY}, json={})
    assert r.status_code == 200
    assert seen["tenant"] == OPERATOR_TENANT          # the call log is told who it was, not left to default


def test_the_operator_principal_is_the_operator_and_has_no_hashed_tenant():
    p = operator_principal()
    token = set_principal(p)
    try:
        assert is_operator() and request_tenant() == OPERATOR_TENANT
        from app.identity import current_tenant_key

        assert current_tenant_key() is None            # the operator's namespace is the default one, not a hashed OAuth subject
    finally:
        reset_principal(token)


def test_a_tenant_principal_is_never_the_operator():
    token = set_principal(Principal(subject="alice", scopes=frozenset({"memory.read"}), issuer="https://issuer.example"))
    try:
        assert not is_operator() and request_tenant() not in (None, OPERATOR_TENANT)
    finally:
        reset_principal(token)


# ------------------------------------------------------------------------- strict mode: no identity is not the operator
def test_without_the_flag_nothing_changes_no_identity_still_resolves_to_the_operator(keyed):
    assert current_principal() is None
    assert request_tenant() == OPERATOR_TENANT                       # the shared-key deployment's historical behaviour, pinned
    assert auth.caller_is_operator() is True
    assert call_log.default_tenant_label() == "operator"


def test_strict_mode_resolves_no_identity_to_nothing(keyed):
    keyed.setenv("JARVIS_MULTI_TENANT", "1")
    assert current_principal() is None
    assert request_tenant() is None
    assert call_log.default_tenant_label() == UNAUTHENTICATED_LABEL and UNAUTHENTICATED_LABEL != "operator"


def test_strict_mode_refuses_to_build_a_store_without_an_identity(keyed, tmp_path):
    keyed.setenv("JARVIS_MULTI_TENANT", "1")
    keyed.setenv("JARVIS_STORE_BOOTSTRAP", "1")
    keyed.setenv("JARVIS_STORE_PATH", str(tmp_path / "s.json"))
    with pytest.raises(TenantUnresolvedError):
        get_store()
    token = set_principal(operator_principal())                       # the operator, authenticated as one, is served
    try:
        assert get_store() is not None
    finally:
        reset_principal(token)


def test_strict_mode_operator_gates_need_an_operator_principal(keyed):
    assert auth.caller_is_operator() is True                          # without the flag: legacy rule
    keyed.setenv("JARVIS_MULTI_TENANT", "1")
    assert auth.caller_is_operator() is False
    with pytest.raises(HTTPException) as exc:
        auth.require_operator_read()
    assert exc.value.status_code == 403
    with pytest.raises(HTTPException):
        auth.require_nx_operator()
    token = set_principal(operator_principal())
    try:
        assert auth.caller_is_operator() is True
        auth.require_operator_read()
        auth.require_nx_operator()
    finally:
        reset_principal(token)
    token = set_principal(Principal(subject="alice", scopes=frozenset({"memory.read"}), issuer="https://issuer.example"))
    try:
        assert auth.caller_is_operator() is False                     # a tenant principal is never the operator
    finally:
        reset_principal(token)


def test_strict_mode_files_an_unidentified_entry_under_unauth_not_operator(keyed):
    keyed.setenv("JARVIS_MULTI_TENANT", "1")
    f = call_log.make_fields(transport="http-api", tool="x", args={}, outcome="denied", started=0.0)
    assert f["tenant"] == UNAUTHENTICATED_LABEL
    e = drift.log_entry({"verdict": "pass", "counts": {}, "claims": [], "check_digest": "0" * 64, "duration_ms": 1, "records": [], "call_log": {}},
                        {"summary": "s", "records": [], "client": None, "served_digest": None}, tenant=None, client_header=None, matches={})
    assert e["tenant"] == UNAUTHENTICATED_LABEL


def test_strict_mode_serves_the_operator_and_refuses_everyone_else_over_http(keyed, monkeypatch, tmp_path):
    keyed.setenv("JARVIS_MULTI_TENANT", "1")
    keyed.setenv("JARVIS_STORE_BOOTSTRAP", "1")
    keyed.setenv("JARVIS_STORE_PATH", str(tmp_path / "s.json"))
    c = TestClient(app)                                               # (no lifespan: the startup guard is tested below, the JSON store is only for this test)
    good = c.post("/api/jarvis/tools/emr_latest", headers={"X-API-Key": KEY}, json={})
    assert good.status_code == 200
    assert c.get("/api/jarvis/tools/calls", headers={"X-API-Key": KEY}).status_code in (200, 404)    # operator reaches the operator surface (404 if the log is off)
    for headers in ({}, {"X-API-Key": "wrong"}):
        assert c.post("/api/jarvis/tools/emr_latest", headers=headers, json={}).status_code == 401
        assert c.get("/api/jarvis/memory", headers=headers).status_code == 401


def test_the_public_routes_still_answer_in_strict_mode_with_no_credential(keyed, tmp_path):
    """/ready builds a store to check the database; strict mode refuses a store to an unidentified request, but readiness is infrastructure and must keep
    working, or the container would look unhealthy forever (found by the throwaway drill)."""
    keyed.setenv("JARVIS_MULTI_TENANT", "1")
    keyed.setenv("JARVIS_STORE_BOOTSTRAP", "1")
    keyed.setenv("JARVIS_STORE_PATH", str(tmp_path / "s.json"))
    c = TestClient(app)
    for path in ("/ready", "/health", "/", "/privacy", "/terms", "/api/jarvis/tools", "/.well-known/oauth-protected-resource"):
        r = c.get(path)
        assert r.status_code == 200, f"{path}: {r.status_code} {r.text[:120]}"     # (the tool catalog is public in key mode; it needs a token only under OAuth)


def test_system_scope_is_the_operator_only_inside_the_block():
    assert not is_operator()
    from app.identity import system_scope

    with system_scope():
        assert is_operator() and request_tenant() == OPERATOR_TENANT
    assert not is_operator()


# ----------------------------------------------------------------------------------------------------- the startup guard
@pytest.mark.parametrize(
    "env,fragment",
    [
        ({"JARVIS_ALLOW_UNAUTHENTICATED": "1", "JARVIS_DATABASE_URL": "postgresql://x/y"}, "JARVIS_ALLOW_UNAUTHENTICATED"),
        ({"JARVIS_DATABASE_URL": ""}, "no PostgreSQL store"),
        ({"JARVIS_DATABASE_URL": "postgresql://x/y", "JARVIS_PG_STORE": "blob"}, "not 'rows'"),
        ({"JARVIS_DATABASE_URL": "postgresql://x/y", "JARVIS_API_KEY": ""}, "no credential"),
    ],
)
def test_strict_mode_refuses_to_start_with_a_configuration_that_could_fail_open(keyed, env, fragment):
    keyed.setenv("JARVIS_MULTI_TENANT", "1")
    keyed.setenv("JARVIS_DATABASE_URL", "postgresql://x/y")
    for k, v in env.items():
        if v == "":
            keyed.delenv(k, raising=False)
        else:
            keyed.setenv(k, v)
    with pytest.raises(RuntimeError, match="JARVIS_MULTI_TENANT refuses to start") as exc:
        auth.assert_multi_tenant_config()
    assert fragment in str(exc.value)


def test_strict_mode_starts_with_a_safe_configuration_and_the_flag_off_never_checks(keyed):
    keyed.setenv("JARVIS_DATABASE_URL", "postgresql://x/y")
    keyed.setenv("JARVIS_MULTI_TENANT", "1")
    auth.assert_multi_tenant_config()                                 # key + row store + no opt-out: fine
    keyed.setenv("JARVIS_AUTH_MODE", "oauth")
    keyed.delenv("JARVIS_API_KEY")
    auth.assert_multi_tenant_config()                                 # OAuth needs no operator key
    keyed.delenv("JARVIS_MULTI_TENANT")
    keyed.setenv("JARVIS_ALLOW_UNAUTHENTICATED", "1")
    keyed.delenv("JARVIS_DATABASE_URL")
    auth.assert_multi_tenant_config()                                 # without the flag, no checks at all


def test_the_app_itself_refuses_to_start_in_strict_mode_with_the_dev_opt_out(keyed):
    keyed.setenv("JARVIS_MULTI_TENANT", "1")
    keyed.setenv("JARVIS_ALLOW_UNAUTHENTICATED", "1")
    keyed.setenv("JARVIS_DATABASE_URL", "postgresql://x/y")
    with pytest.raises(RuntimeError, match="refuses to start"):
        with TestClient(app):
            pass
