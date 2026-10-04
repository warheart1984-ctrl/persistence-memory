"""A stored record with out-of-range confidence makes the store fail closed."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.main import app
from app.models import MemoryCreate, MemoryRecord
from app.store import JarvisStore, StoreUnavailableError

_MEMORY = {"content": "must not shrink a ledger", "source_agent": "t", "session_id": "s", "type": "fact"}


def _seed(path: Path) -> str:
    store = JarvisStore(str(path))
    ids = [
        store.create_memory(MemoryCreate(content=f"record {i}", source_agent="t", session_id="s", type="fact")).id
        for i in range(3)
    ]
    return ids[1]


@pytest.mark.parametrize("bad", [5, -0.1, 1.0001])
def test_out_of_range_confidence_is_503_and_file_unchanged(tmp_path, bad):
    path = tmp_path / "jarvis-store.json"  # conftest points JARVIS_STORE_PATH here
    _seed(path)
    data = json.loads(path.read_text("utf-8"))
    data["memories"][1]["confidence"] = bad
    path.write_text(json.dumps(data, indent=2), "utf-8")
    before = path.read_bytes()
    with TestClient(app, raise_server_exceptions=False) as client:
        assert client.get("/api/jarvis/memory").status_code == 503
        assert client.post("/api/jarvis/memory", json=_MEMORY).status_code == 503
        assert client.get("/health").status_code == 503
    assert path.read_bytes() == before


def test_exception_names_record_and_value(tmp_path):
    path = tmp_path / "jarvis-store.json"
    bad_id = _seed(path)
    data = json.loads(path.read_text("utf-8"))
    data["memories"][1]["confidence"] = 5
    path.write_text(json.dumps(data), "utf-8")
    with pytest.raises(StoreUnavailableError, match=bad_id):
        JarvisStore(str(path)).list_memories()


@pytest.mark.parametrize("ok", [0, 0.0, 0.5, 1, 1.0])
def test_boundary_values_still_load(tmp_path, ok):
    path = tmp_path / "jarvis-store.json"
    _seed(path)
    data = json.loads(path.read_text("utf-8"))
    data["memories"][0]["confidence"] = ok
    path.write_text(json.dumps(data), "utf-8")
    assert len(JarvisStore(str(path)).list_memories()) == 3


def test_model_rejects_out_of_range():
    base = {"id": "m", "content": "c", "created_at": "t", "updated_at": "t", "source_agent": "a", "session_id": "s", "type": "fact", "status": "draft"}
    with pytest.raises(ValidationError):
        MemoryRecord(**base, confidence=5)
    assert MemoryRecord(**base, confidence=1).confidence == 1


# The Postgres counterparts of these properties live in tests/test_pg_*.py (CHECK constraints,
# fail-closed on a database outage, generic MCP errors, history verification).
import pytest as _pytest_marker  # noqa: E402

pytestmark = _pytest_marker.mark.json_store_only
