"""The ledger must never silently fall back to a local JSON file: no database means 503 unless explicitly opted in."""

from __future__ import annotations

import sys
import types

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.store import StoreUnavailableError, get_store, reset_store_for_tests, store_bootstrap_enabled


@pytest.fixture
def fresh(monkeypatch, tmp_path):
    """No database, no opt-in, and a working directory where a stray data/ folder would be noticed."""
    monkeypatch.delenv("JARVIS_DATABASE_URL", raising=False)
    monkeypatch.delenv("JARVIS_STORE_BOOTSTRAP", raising=False)
    monkeypatch.delenv("JARVIS_STORE_PATH", raising=False)  # so the default "data/jarvis-store.json" is in play
    monkeypatch.delenv("JARVIS_TENANT_STORE_DIR", raising=False)
    monkeypatch.chdir(tmp_path)
    reset_store_for_tests()
    return tmp_path


def _noise_free(tmp_path) -> bool:
    return not (tmp_path / "data").exists() and not list(tmp_path.glob("**/*.json"))


def test_without_a_database_or_the_opt_in_the_store_refuses(fresh):
    with pytest.raises(StoreUnavailableError) as exc:
        get_store()
    assert "JARVIS_DATABASE_URL" in str(exc.value) and "JARVIS_STORE_BOOTSTRAP" in str(exc.value)
    assert _noise_free(fresh)


def test_the_api_answers_503_and_creates_no_data_folder(fresh):
    client = TestClient(app, raise_server_exceptions=False)
    read = client.get("/api/jarvis/memory")
    assert read.status_code == 503 and read.json()["code"] == "ledger_unavailable"
    write = client.post(
        "/api/jarvis/memory",
        json={"content": "must not be stored", "source_agent": "t", "session_id": "s", "type": "fact"},
    )
    assert write.status_code == 503
    assert client.get("/api/jarvis/memory/board").status_code == 503
    assert _noise_free(fresh)


def test_ready_is_503_but_liveness_is_unaffected(fresh):
    client = TestClient(app, raise_server_exceptions=False)
    ready = client.get("/ready")
    assert ready.status_code == 503 and ready.json()["code"] == "ledger_unavailable"
    assert client.get("/health").status_code == 200  # liveness never touches the ledger
    assert _noise_free(fresh)


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "Yes", "on", " 1 "])
def test_an_explicit_opt_in_enables_the_json_store(fresh, monkeypatch, value):
    monkeypatch.setenv("JARVIS_STORE_BOOTSTRAP", value)
    monkeypatch.setenv("JARVIS_STORE_PATH", str(fresh / "opted-in" / "jarvis-store.json"))
    assert store_bootstrap_enabled() is True
    client = TestClient(app, raise_server_exceptions=False)
    assert client.get("/api/jarvis/memory").status_code == 200
    assert client.get("/ready").status_code == 200


@pytest.mark.parametrize("value", ["", "0", "false", "no", "off", "2", "maybe", "  "])
def test_anything_else_is_not_an_opt_in(fresh, monkeypatch, value):
    monkeypatch.setenv("JARVIS_STORE_BOOTSTRAP", value)
    assert store_bootstrap_enabled() is False
    assert TestClient(app, raise_server_exceptions=False).get("/api/jarvis/memory").status_code == 503
    assert _noise_free(fresh)


def test_the_tenant_store_path_is_closed_off_too(fresh, monkeypatch):
    from app import store as store_module

    monkeypatch.setattr(store_module, "current_tenant_key", lambda: "someone")
    with pytest.raises(StoreUnavailableError):
        get_store()
    assert _noise_free(fresh)


def test_with_a_database_url_no_opt_in_is_needed(fresh, monkeypatch):
    built = []

    class FakeRowStore:
        def __init__(self, url, tenant, schema=None):
            built.append((url, tenant, schema))

    fake = types.ModuleType("app.pg_store")
    fake.PostgresRowStore = FakeRowStore
    fake.close_pools = lambda: None
    monkeypatch.setitem(sys.modules, "app.pg_store", fake)
    monkeypatch.setenv("JARVIS_DATABASE_URL", "postgresql://u:p@db/jarvis")
    monkeypatch.setenv("JARVIS_PG_STORE", "rows")
    monkeypatch.delenv("JARVIS_DATABASE_SCHEMA", raising=False)  # CI's Postgres job sets one for the real-database tests
    store = get_store()
    assert isinstance(store, FakeRowStore) and built == [("postgresql://u:p@db/jarvis", "operator", None)]
    assert _noise_free(fresh)


def test_the_suite_opts_in_through_one_conftest_line():
    """That one line is what keeps every JSON-store test file working; this test pins it so it is not dropped."""
    import pathlib

    conftest = (pathlib.Path(__file__).parent / "conftest.py").read_text("utf-8")
    assert 'monkeypatch.setenv("JARVIS_STORE_BOOTSTRAP", "1")' in conftest
