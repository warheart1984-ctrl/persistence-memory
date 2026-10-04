"""A ledger file that does not validate cleanly must never be rewritten."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.models import MemoryCreate
from app.store import JarvisStore, StoreUnavailableError

_MEMORY = {"content": "must not shrink a damaged ledger", "source_agent": "t", "session_id": "s", "type": "fact"}


def _seed(path: Path) -> dict:
    store = JarvisStore(str(path))
    for i in range(3):
        store.create_memory(MemoryCreate(content=f"record number {i}", source_agent="t", session_id="s", type="fact"))
    return json.loads(path.read_text("utf-8"))


def _damage(path: Path, mutate) -> bytes:
    data = json.loads(path.read_text("utf-8"))
    mutate(data)
    path.write_text(json.dumps(data, indent=2), "utf-8")
    return path.read_bytes()


def _bad_type(data):
    data["memories"][1]["type"] = "BOGUS"


def _memories_not_list(data):
    data["memories"] = "notalist"


def _bad_board(data):
    data["board"] = {"slots": "not-a-list-of-slots"}


def _record_without_id(data):
    del data["memories"][2]["id"]


def _record_not_object(data):
    data["memories"][0] = "just a string"


_CASES = [_bad_type, _memories_not_list, _bad_board, _record_without_id, _record_not_object]


@pytest.mark.parametrize("mutate", _CASES, ids=lambda f: f.__name__)
def test_damaged_ledger_gives_503_and_file_is_byte_identical(tmp_path, mutate):
    path = tmp_path / "jarvis-store.json"  # conftest points JARVIS_STORE_PATH here
    _seed(path)
    damaged = _damage(path, mutate)
    with TestClient(app, raise_server_exceptions=False) as client:
        assert client.get("/api/jarvis/memory").status_code == 503
        assert client.post("/api/jarvis/memory", json=_MEMORY).status_code == 503
        assert client.post("/api/jarvis/memory", json=_MEMORY).status_code == 503  # retry must not heal
        health = client.get("/health")
    assert health.status_code == 503 and health.json()["status"] == "unavailable"
    assert path.read_bytes() == damaged


def test_error_names_bad_record_in_exception_not_response(tmp_path):
    path = tmp_path / "jarvis-store.json"
    seeded = _seed(path)
    bad_id = seeded["memories"][1]["id"]
    _damage(path, _bad_type)
    with pytest.raises(StoreUnavailableError) as excinfo:
        JarvisStore(str(path)).list_memories()
    assert bad_id in str(excinfo.value)
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/api/jarvis/memory")
    assert bad_id not in response.text


def test_missing_memories_or_board_keys_still_load_as_empty(tmp_path):
    path = tmp_path / "jarvis-store.json"
    path.write_text("{}", "utf-8")
    store = JarvisStore(str(path))
    assert store.list_memories() == []
    assert store.get_board().board_id == "default_board"


def test_legacy_rows_still_migrate_and_persist(tmp_path):
    path = tmp_path / "jarvis-store.json"
    legacy = {"id": "mem-legacy1", "content": "Old signal", "category": "decision", "tags": ["sess-9"],
              "created_at": "2026-07-01T00:00:00+00:00", "updated_at": "2026-07-01T00:00:00+00:00"}
    path.write_text(json.dumps({"memories": [legacy]}), "utf-8")
    store = JarvisStore(str(path))
    rec = store.get_memory("mem-legacy1")
    assert rec is not None and rec.type == "decision" and rec.session_id == "sess-9"
    on_disk = json.loads(path.read_text("utf-8"))["memories"][0]
    assert on_disk["content_sha256"] and "category" not in on_disk


# The Postgres counterparts of these properties live in tests/test_pg_*.py (CHECK constraints,
# fail-closed on a database outage, generic MCP errors, history verification).
import pytest as _pytest_marker  # noqa: E402

pytestmark = _pytest_marker.mark.json_store_only
