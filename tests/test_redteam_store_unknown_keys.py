"""A ledger document with data under unrecognised keys must not load as empty."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.models import MemoryCreate
from app.store import JarvisStore, StoreUnavailableError

_MEMORY = {"content": "must not wipe a misspelled ledger", "source_agent": "t", "session_id": "s", "type": "fact"}


def _valid_record(path: Path) -> dict:
    store = JarvisStore(str(path))
    rec = store.create_memory(MemoryCreate(content="a valid record", source_agent="t", session_id="s", type="fact"))
    record = rec.model_dump()
    path.unlink()
    return record


def test_misspelled_memories_key_is_503_and_file_unchanged(tmp_path):
    path = tmp_path / "jarvis-store.json"  # conftest points JARVIS_STORE_PATH here
    record = _valid_record(path)
    path.write_text(json.dumps({"board": {}, "memorie": [record]}), "utf-8")
    before = path.read_bytes()
    with TestClient(app, raise_server_exceptions=False) as client:
        assert client.get("/api/jarvis/memory").status_code == 503
        assert client.post("/api/jarvis/memory", json=_MEMORY).status_code == 503
        assert client.get("/health").status_code == 503
    assert path.read_bytes() == before


def test_exception_names_the_unknown_key(tmp_path):
    path = tmp_path / "jarvis-store.json"
    path.write_text(json.dumps({"memorie": [{"id": "x"}]}), "utf-8")
    with pytest.raises(StoreUnavailableError, match="memorie"):
        JarvisStore(str(path)).list_memories()


@pytest.mark.parametrize(
    "document",
    [{}, {"board": {}}, {"schema": "continuity-ledger-v1"}, {"board": {}, "schema": "x", "memories": []}, {"memorie": []}],
)
def test_empty_or_recognised_documents_still_load_empty(tmp_path, document):
    path = tmp_path / "jarvis-store.json"
    path.write_text(json.dumps(document), "utf-8")
    store = JarvisStore(str(path))
    assert store.list_memories() == []


def test_brand_new_file_still_starts_empty(tmp_path):
    store = JarvisStore(str(tmp_path / "fresh.json"))
    assert store.list_memories() == []
    store.create_memory(MemoryCreate(content="first record", source_agent="t", session_id="s", type="fact"))
    assert len(JarvisStore(str(tmp_path / "fresh.json")).list_memories()) == 1
