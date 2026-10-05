"""Record versions and the optional expected_version lock (JSON backend)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.models import MemoryCreate, MemoryUpdate
from app.store import JarvisStore, StoreVersionConflict

_BODY = {"content": "versioned record", "source_agent": "t", "session_id": "s", "type": "fact"}


def _create(store: JarvisStore):
    return store.create_memory(MemoryCreate(content="versioned record", source_agent="t", session_id="s", type="fact"))


def test_new_records_start_at_version_1_and_updates_increment(tmp_path):
    store = JarvisStore(str(tmp_path / "s.json"))
    rec = _create(store)
    assert rec.version == 1
    assert store.update_memory(rec.id, MemoryUpdate(subject="x")).version == 2
    assert store.update_memory(rec.id, MemoryUpdate(subject="y")).version == 3
    assert JarvisStore(str(tmp_path / "s.json")).get_memory(rec.id).version == 3  # persisted


def test_expected_version_match_applies_and_mismatch_is_rejected_unchanged(tmp_path):
    store = JarvisStore(str(tmp_path / "s.json"))
    rec = _create(store)
    ok = store.update_memory(rec.id, MemoryUpdate(subject="first", expected_version=1))
    assert ok.version == 2 and ok.subject == "first"
    with pytest.raises(StoreVersionConflict):
        store.update_memory(rec.id, MemoryUpdate(subject="stale", expected_version=1))
    current = store.get_memory(rec.id)
    assert current.version == 2 and current.subject == "first"


def test_records_without_a_stored_version_load_as_version_1(tmp_path):
    import json

    store = JarvisStore(str(tmp_path / "s.json"))
    rec = _create(store)
    path = tmp_path / "s.json"
    data = json.loads(path.read_text("utf-8"))
    del data["memories"][0]["version"]
    path.write_text(json.dumps(data), "utf-8")
    assert JarvisStore(str(path)).get_memory(rec.id).version == 1


def test_patch_route_returns_409_on_stale_expected_version():
    with TestClient(app) as client:
        created = client.post("/api/jarvis/memory", json=_BODY).json()["memory"]
        assert created["version"] == 1
        ok = client.patch(f"/api/jarvis/memory/{created['id']}", json={"subject": "a", "expected_version": 1})
        assert ok.status_code == 200 and ok.json()["memory"]["version"] == 2
        stale = client.patch(f"/api/jarvis/memory/{created['id']}", json={"subject": "b", "expected_version": 1})
        assert stale.status_code == 409
        assert client.get(f"/api/jarvis/memory/{created['id']}").json()["memory"]["subject"] == "a"
        plain = client.patch(f"/api/jarvis/memory/{created['id']}", json={"subject": "c"})
        assert plain.status_code == 200 and plain.json()["memory"]["version"] == 3


def test_expected_version_must_be_positive():
    with TestClient(app) as client:
        created = client.post("/api/jarvis/memory", json=_BODY).json()["memory"]
        assert client.patch(f"/api/jarvis/memory/{created['id']}", json={"expected_version": 0}).status_code == 422
