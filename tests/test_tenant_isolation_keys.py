"""The whole isolation matrix, run again with the tenants authenticated by per-tenant API keys (RFC phase 1) instead of OAuth subjects.

Every case in tests/test_tenant_isolation.py is imported and runs unchanged, against a Postgres row store with forced RLS, with JARVIS_KEY_TENANTS on and a shared operator key configured
(the deployment the box runs): `alice`, `bob` and the other names the cases use become key tenants minted on first use. The two modules therefore prove the same isolation properties over both
kinds of tenant identity, and a route added to the app without a matrix classification fails here exactly as it does there.
"""

from __future__ import annotations

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg.rows import dict_row

import tests.test_tenant_isolation as base
from app import pg_store, tenant_keys
from app.main import app
from app.pg_schema import control_schema_name, migrate
from app.store import reset_store_for_tests
from tests.test_tenant_isolation import *  # noqa: F401,F403  (the cases and their fixtures; `client` is overridden below)

pytestmark = pytest.mark.postgres
PEPPER = "matrix-key-tenants-pepper-0123456789-0123456789"
OPERATOR_KEY = "matrix-operator-key-0123456789-0123456789"


class KeyTenants:
    """h(who) -> that name's own API key, minted on first use (a fresh tenant each, so `carol-who-never-wrote` really is a tenant with nothing)."""

    def __init__(self, admin_dsn: str, control: str):
        self.admin_dsn, self.control, self.tokens, self.tenants = admin_dsn, control, {}, {}

    def header(self, who: str) -> dict[str, str]:
        if who not in self.tokens:
            with psycopg.connect(self.admin_dsn, autocommit=True, row_factory=dict_row) as c:
                tenant = tenant_keys.create_tenant(c, self.control, who)
                self.tenants[who] = tenant
                self.tokens[who] = tenant_keys.create_key(c, self.control, tenant, PEPPER.encode(), label=who)
        return {"Authorization": f"Bearer {self.tokens[who]}"}

    def aliases(self, who: str) -> list[str]:
        self.header(who)
        return [self.tenants[who], who, "operator"]


@pytest.fixture
def client(pg_schema, monkeypatch, tmp_path):
    migrate(pg_schema.admin_dsn, schema=pg_schema.schema, app_role="jarvis_app_test")
    for k, v in {"JARVIS_DATABASE_URL": pg_schema.app_dsn, "JARVIS_DATABASE_SCHEMA": pg_schema.schema, "JARVIS_PG_STORE": "rows", "JARVIS_DATABASE_POOL_TIMEOUT_MS": "2000",
                 "JARVIS_KEY_TENANTS": "1", "JARVIS_KEY_PEPPER": PEPPER, "JARVIS_API_KEY": OPERATOR_KEY, "JARVIS_MEMORY_WRITE_ENABLED": "true", "JARVIS_MCP_WRITE_ENABLED": "true",
                 "JARVIS_CURSOR_HMAC_KEY": "isolation-matrix-cursor-key-0123456789", "JARVIS_PUBLIC_BASE_URL": "https://memory.example"}.items():
        monkeypatch.setenv(k, v)
    for k in ("JARVIS_AUTH_MODE", "JARVIS_ALLOW_UNAUTHENTICATED", "JARVIS_MULTI_TENANT", "EMR_RECALL_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    for k in ("JARVIS_AMUL_PATH", "JARVIS_EMR_DYNAMICS_PATH", "JARVIS_AMUL_GC_PATH"):
        monkeypatch.setenv(k, str(tmp_path / (k.lower() + ".dat")))
    reset_store_for_tests()
    monkeypatch.setattr(base, "AUTH", KeyTenants(pg_schema.admin_dsn, control_schema_name(pg_schema.schema)))
    # The shared-key deployment answers a few requests with no credential that carry no ledger data; named here, not hidden (they are the same on the live box today).
    monkeypatch.setattr(base, "NO_CREDENTIAL_OK", {("DELETE", "/mcp"): 204, ("DELETE", "/mcp/"): 204, ("GET", "/mcp"): 405, ("GET", "/mcp/"): 405, ("GET", "/api/jarvis/tools"): 200,
                                                  ("GET", "/api/jarvis/rag/log"): 503, ("GET", "/api/jarvis/rag/status"): 503, ("POST", "/api/jarvis/rag/documents"): 503,
                                                  ("POST", "/api/jarvis/rag/maintenance"): 503, ("POST", "/api/jarvis/rag/query"): 503})
    monkeypatch.setattr(base, "PUBLIC_NEEDS_THE_SHARED_KEY", {("GET", "/docs/oauth2-redirect")})
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c
    pg_store.close_pools()
