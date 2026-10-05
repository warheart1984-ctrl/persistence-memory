"""Shared test isolation — EMR dynamics sidecar must never touch repo data/."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

import app.emr as emr
import app.amul as amul
import app.amul_gc as amul_gc
import app.amul_rag as rag
import app.amul_llm as llm
import app.emr_embed as emr_embed


@pytest.fixture(autouse=True)
def _embeddings_off_and_isolated(monkeypatch, tmp_path):
    """EMR scores lexically unless a test opts in; vectors never reach data/."""
    monkeypatch.delenv("JARVIS_EMR_EMBEDDINGS", raising=False)
    monkeypatch.setenv("JARVIS_EMR_EMBED_CACHE", str(tmp_path / "emr-embeddings.json"))
    emr_embed.reset_for_tests()
    yield
    emr_embed.reset_for_tests()


@pytest.fixture(autouse=True)
def _allow_unauthenticated_for_tests(monkeypatch):
    """Ledger tests use the local-dev opt-out; auth-required behavior is in test_auth.py."""
    monkeypatch.delenv("JARVIS_API_KEY", raising=False)
    monkeypatch.setenv("JARVIS_ALLOW_UNAUTHENTICATED", "1")


@pytest.fixture(autouse=True)
def _isolated_store_paths(tmp_path, monkeypatch):
    """Ledger stores (operator + per-tenant) must never touch repo data/."""
    from app.store import reset_store_for_tests

    monkeypatch.setenv("JARVIS_STORE_PATH", str(tmp_path / "jarvis-store.json"))
    monkeypatch.setenv("JARVIS_STORE_BOOTSTRAP", "1")  # the tests use the JSON store on purpose
    monkeypatch.setenv("JARVIS_CLAUSE_V", "off")  # the older tests write preference/task records on purpose; test_clause_v.py opts in
    monkeypatch.setenv("JARVIS_TENANT_STORE_DIR", str(tmp_path / "tenants"))
    reset_store_for_tests()
    yield
    reset_store_for_tests()


@pytest.fixture(autouse=True)
def _isolated_dynamics_sidecar(tmp_path):
    """Point EMR/AMUL/RAG/LLM storage at per-test temp files.

    Unit tests must not read or write real data/ files, and must never hit
    the real LLM backend; adapter behavior is covered with explicit patches.
    """
    sidecar = Path(tempfile.mktemp(suffix="-dynamics.json", dir=str(tmp_path)))
    original = emr.DYNAMICS_PATH
    emr.DYNAMICS_PATH = str(sidecar)
    emr._dynamics_loaded = False  # force reload against isolated path

    amul_path = Path(tempfile.mktemp(suffix="-field.jsonl", dir=str(tmp_path)))
    original_field_path = amul.FIELD_PATH
    amul.FIELD_PATH = str(amul_path)
    amul.reset_field_for_tests()

    rag_docs = Path(tempfile.mktemp(suffix="-ragdocs.jsonl", dir=str(tmp_path)))
    rag_log = Path(tempfile.mktemp(suffix="-raglog.jsonl", dir=str(tmp_path)))
    original_rag_paths = (rag.RAG_DOCS_PATH, rag.RAG_LOG_PATH)
    rag.RAG_DOCS_PATH, rag.RAG_LOG_PATH = str(rag_docs), str(rag_log)
    rag.reset_index_for_tests()

    llm_log = Path(tempfile.mktemp(suffix="-llmlog.jsonl", dir=str(tmp_path)))
    original_llm_paths = (llm.LLM_LOG_PATH, llm.LLM_URL)
    llm.LLM_LOG_PATH = str(llm_log)
    llm.LLM_URL = ""  # force echo-stub path; backend tested via explicit patch

    gc_cps = Path(tempfile.mktemp(suffix="-checkpoints.jsonl", dir=str(tmp_path)))
    original_gc_path = amul_gc.CHECKPOINTS_PATH
    amul_gc.CHECKPOINTS_PATH = str(gc_cps)

    yield
    emr.DYNAMICS_PATH = original
    emr._dynamics_loaded = False
    amul.FIELD_PATH = original_field_path
    amul.reset_field_for_tests()
    rag.RAG_DOCS_PATH, rag.RAG_LOG_PATH = original_rag_paths
    rag.reset_index_for_tests()
    llm.LLM_LOG_PATH, llm.LLM_URL = original_llm_paths
    emr.reset_stm_for_tests()


# --- Postgres (throwaway server only) -------------------------------------------------
# Tests marked ``postgres`` need JARVIS_TEST_PG_DSN pointing at a disposable superuser
# connection (e.g. a local ``postgres:16`` container).  They skip when it is unset.

import contextlib
import secrets
import uuid
from dataclasses import dataclass


@dataclass
class PgSchema:
    schema: str
    admin_dsn: str
    app_dsn: str

    @contextlib.contextmanager
    def app_conn(self, tenant: str | None):
        """Connection as the non-superuser app role, one transaction, tenant set (or not)."""
        import psycopg

        with psycopg.connect(self.app_dsn, options=f"-c search_path={self.schema}") as conn:
            if tenant is not None:
                conn.execute("SELECT set_config('jarvis.tenant_key', %s, true)", (tenant,))
            yield conn

    def admin_conn(self):
        import psycopg

        return psycopg.connect(self.admin_dsn, options=f"-c search_path={self.schema}", autocommit=True)


@pytest.fixture(scope="session")
def pg_server():
    dsn = os.environ.get("JARVIS_TEST_PG_DSN", "").strip()
    if not dsn:
        pytest.skip("JARVIS_TEST_PG_DSN not set (throwaway Postgres required)")
    import psycopg
    from psycopg.conninfo import make_conninfo

    password = secrets.token_hex(12)
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute("DROP ROLE IF EXISTS jarvis_app_test")
        conn.execute(f"CREATE ROLE jarvis_app_test LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE PASSWORD '{password}'")
    app_dsn = make_conninfo(dsn, user="jarvis_app_test", password=password)
    yield dsn, app_dsn


@pytest.fixture
def pg_schema(pg_server):
    """A fresh, empty schema (not yet migrated) plus DSNs for the admin and app roles."""
    import psycopg

    admin_dsn, app_dsn = pg_server
    name = f"t_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(admin_dsn, autocommit=True) as conn:
        conn.execute(f'CREATE SCHEMA "{name}"')
    yield PgSchema(schema=name, admin_dsn=admin_dsn, app_dsn=app_dsn)
    with psycopg.connect(admin_dsn, autocommit=True) as conn:
        conn.execute(f'DROP SCHEMA "{name}" CASCADE')


# --- Run the whole suite against the Postgres row store -------------------------------------
# JARVIS_TEST_BACKEND=postgres (plus JARVIS_TEST_PG_DSN) points every test that goes through
# get_store() / the HTTP API at a fresh, migrated schema on a throwaway server.  Tests that are
# about the JSON file itself carry @pytest.mark.json_store_only and are skipped in that mode;
# the Postgres counterparts (fail-closed, CHECK constraints, RLS, history) are tests/test_pg_*.py.

_PG_MODE = os.environ.get("JARVIS_TEST_BACKEND", "").strip().lower() == "postgres"


def pytest_configure(config):
    if _PG_MODE and not os.environ.get("JARVIS_TEST_PG_DSN", "").strip():
        raise pytest.UsageError("JARVIS_TEST_BACKEND=postgres requires JARVIS_TEST_PG_DSN (a throwaway server)")


def pytest_collection_modifyitems(config, items):
    if not _PG_MODE:
        return
    skip = pytest.mark.skip(reason="specific to the JSON file store; see tests/test_pg_*.py for Postgres")
    for item in items:
        if "json_store_only" in item.keywords:
            item.add_marker(skip)


@pytest.fixture(autouse=True)
def _postgres_backend(request, monkeypatch):
    if not _PG_MODE or request.node.get_closest_marker("postgres") is not None:
        yield  # tests marked postgres build (and sometimes deliberately break) their own schemas
        return
    from app import pg_store
    from app.pg_schema import migrate

    schema = request.getfixturevalue("pg_schema")
    migrate(schema.admin_dsn, schema=schema.schema, app_role="jarvis_app_test")
    monkeypatch.setenv("JARVIS_DATABASE_URL", schema.app_dsn)
    monkeypatch.setenv("JARVIS_DATABASE_SCHEMA", schema.schema)
    monkeypatch.setenv("JARVIS_PG_STORE", "rows")
    monkeypatch.setenv("JARVIS_DATABASE_CONNECT_TIMEOUT", "2")
    yield
    pg_store.close_pools()
