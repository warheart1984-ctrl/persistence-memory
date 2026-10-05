"""A damaged or failing store must fail closed, never reset the ledger."""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.models import MemoryCreate
from app.store import JarvisStore, get_store

_MEMORY = {"content": "must not overwrite a damaged ledger", "source_agent": "t", "session_id": "s", "type": "fact"}


def _create(content: str = "ledger record") -> MemoryCreate:
    return MemoryCreate(content=content, source_agent="t", session_id="s", type="fact")


def _seed(path: Path, count: int = 3) -> None:
    store = JarvisStore(str(path))
    for i in range(count):
        store.create_memory(_create(f"record number {i}"))


def _truncate(path: Path) -> bytes:
    data = path.read_bytes()
    path.write_bytes(data[: len(data) // 2])
    return path.read_bytes()


def test_truncated_store_rejects_writes_and_leaves_file_unchanged(tmp_path):
    path = tmp_path / "jarvis-store.json"  # conftest points JARVIS_STORE_PATH here
    _seed(path)
    damaged = _truncate(path)
    with TestClient(app) as client:
        first = client.post("/api/jarvis/memory", json=_MEMORY)
        second = client.post("/api/jarvis/memory", json=_MEMORY)  # retry must not "heal" into empty
        read = client.get("/api/jarvis/memory")
    assert 500 <= first.status_code < 600
    assert 500 <= second.status_code < 600
    assert 500 <= read.status_code < 600
    assert path.read_bytes() == damaged


def test_health_is_not_ok_when_store_is_damaged(tmp_path):
    path = tmp_path / "jarvis-store.json"
    _seed(path)
    _truncate(path)
    with TestClient(app) as client:
        response = client.get("/ready")
    assert response.status_code == 503
    assert response.json()["status"] != "ok"


def test_empty_existing_file_is_not_treated_as_an_empty_ledger(tmp_path):
    path = tmp_path / "jarvis-store.json"
    path.write_text("")
    store = JarvisStore(str(path))
    with pytest.raises(RuntimeError):
        store.create_memory(_create())
    assert path.read_text() == ""


def test_missing_file_still_starts_empty(tmp_path):
    store = JarvisStore(str(tmp_path / "fresh.json"))
    assert store.list_memories() == []
    store.create_memory(_create())
    assert len(JarvisStore(str(tmp_path / "fresh.json")).list_memories()) == 1


def test_torn_write_leaves_original_intact(tmp_path, monkeypatch):
    path = tmp_path / "jarvis-store.json"
    _seed(path)
    original = path.read_bytes()

    def torn_write(self, data, *args, **kwargs):  # crash after half the bytes hit the target
        with open(self, "wb") as handle:
            handle.write(data.encode("utf-8")[: len(data) // 2])
        raise OSError("disk full")

    monkeypatch.setattr(Path, "write_text", torn_write)
    store = JarvisStore(str(path))
    try:
        store.create_memory(_create("this write dies midway"))
    except Exception:  # noqa: BLE001 - the write may succeed or fail; the file must never be torn
        pass
    on_disk = json.loads(path.read_text("utf-8"))  # raises if the ledger was truncated
    assert {m["content"] for m in on_disk["memories"]} >= {f"record number {i}" for i in range(3)}


def test_failed_replace_leaves_original_intact_and_cleans_up(tmp_path, monkeypatch):
    path = tmp_path / "jarvis-store.json"
    _seed(path)
    original = path.read_bytes()
    store = JarvisStore(str(path))

    def boom(src, dst):
        raise OSError("replace failed")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(RuntimeError):
        store.create_memory(_create("never lands"))
    monkeypatch.undo()
    assert path.read_bytes() == original
    assert [p.name for p in tmp_path.iterdir() if p.name != "jarvis-store.json"] == []


def test_failed_fsync_leaves_original_intact_and_no_temp_files(tmp_path, monkeypatch):
    path = tmp_path / "jarvis-store.json"
    _seed(path)
    original = path.read_bytes()
    store = JarvisStore(str(path))
    assert len(store.list_memories()) == 3

    def boom(fd):
        raise OSError("io error")

    monkeypatch.setattr(os, "fsync", boom)
    with pytest.raises(RuntimeError):
        store.create_memory(_create("never durable"))
    monkeypatch.undo()
    assert path.read_bytes() == original
    assert sorted(p.name for p in tmp_path.iterdir() if p.name.startswith("jarvis-store")) == ["jarvis-store.json"]
    assert len(store.list_memories()) == 3  # in-memory state rolled back too


def test_concurrent_writes_are_all_persisted(tmp_path):
    path = tmp_path / "jarvis-store.json"
    store = JarvisStore(str(path))
    errors: list[BaseException] = []

    def worker(n: int) -> None:
        try:
            for i in range(15):
                store.create_memory(_create(f"thread {n} record {i}"))
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    on_disk = json.loads(path.read_text("utf-8"))["memories"]
    assert len(on_disk) == 8 * 15


# The Postgres counterparts of these properties live in tests/test_pg_*.py (CHECK constraints,
# fail-closed on a database outage, generic MCP errors, history verification).
import pytest as _pytest_marker  # noqa: E402

pytestmark = _pytest_marker.mark.json_store_only
