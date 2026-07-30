from __future__ import annotations

import json
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.store import JarvisStore, get_store


@pytest.fixture(autouse=True)
def _fresh_store():
    tmp = Path(tempfile.mktemp(suffix=".json"))
    store = JarvisStore(str(tmp))
    with patch("app.main.get_store", return_value=store):
        yield


client = TestClient(app)


class TestBoardEndpoints:
    def test_get_board_default(self):
        resp = client.get("/api/jarvis/memory/board")
        assert resp.status_code == 200
        data = resp.json()
        assert "memory_board" in data
        assert data["memory_board"]["board_id"] == "default_board"

    def test_set_board(self):
        payload = {
            "board_id": "custom_board",
            "summary": "Custom board",
            "linked_subsystems": ["jarvis", "mrs"],
        }
        resp = client.post("/api/jarvis/memory/board", json=payload)
        assert resp.status_code == 200
        data = resp.json()
        assert data["memory_board"]["board_id"] == "custom_board"
        assert data["memory_board"]["summary"] == "Custom board"

    def test_patch_board(self):
        resp = client.patch("/api/jarvis/memory/board", json={"summary": "Patched"})
        assert resp.status_code == 200
        assert resp.json()["memory_board"]["summary"] == "Patched"


class TestMemoryEndpoints:
    def test_list_empty(self):
        resp = client.get("/api/jarvis/memory")
        assert resp.status_code == 200
        assert resp.json()["memories"] == []

    def test_create_memory(self):
        resp = client.post("/api/jarvis/memory", json={"content": "Hello world"})
        assert resp.status_code == 200
        data = resp.json()["memory"]
        assert data["content"] == "Hello world"
        assert data["id"].startswith("mem-")

    def test_create_memory_validation(self):
        resp = client.post("/api/jarvis/memory", json={})
        assert resp.status_code == 422

    def test_get_memory(self):
        created = client.post("/api/jarvis/memory", json={"content": "Find me"}).json()["memory"]
        resp = client.get(f"/api/jarvis/memory/{created['id']}")
        assert resp.status_code == 200
        assert resp.json()["memory"]["content"] == "Find me"

    def test_get_memory_not_found(self):
        resp = client.get("/api/jarvis/memory/nonexistent")
        assert resp.status_code == 404

    def test_update_memory(self):
        created = client.post("/api/jarvis/memory", json={"content": "Old", "tags": ["a"]}).json()["memory"]
        resp = client.patch(f"/api/jarvis/memory/{created['id']}", json={"content": "New", "tags": ["a", "b"]})
        assert resp.status_code == 200
        data = resp.json()["memory"]
        assert data["content"] == "New"
        assert data["tags"] == ["a", "b"]

    def test_update_memory_not_found(self):
        resp = client.patch("/api/jarvis/memory/nonexistent", json={"content": "New"})
        assert resp.status_code == 404

    def test_delete_memory(self):
        created = client.post("/api/jarvis/memory", json={"content": "Delete me"}).json()["memory"]
        resp = client.delete(f"/api/jarvis/memory/{created['id']}")
        assert resp.status_code == 200
        assert resp.json()["status"] == "deleted"
        get_resp = client.get(f"/api/jarvis/memory/{created['id']}")
        assert get_resp.status_code == 404

    def test_delete_memory_not_found(self):
        resp = client.delete("/api/jarvis/memory/nonexistent")
        assert resp.status_code == 404

    def test_list_memories_returns_all(self):
        client.post("/api/jarvis/memory", json={"content": "A"})
        client.post("/api/jarvis/memory", json={"content": "B"})
        resp = client.get("/api/jarvis/memory")
        assert len(resp.json()["memories"]) == 2

    def test_list_filter_by_query(self):
        client.post("/api/jarvis/memory", json={"content": "Tesseract lattice"})
        client.post("/api/jarvis/memory", json={"content": "Glass cathedral"})
        resp = client.get("/api/jarvis/memory", params={"query": "tesseract"})
        assert len(resp.json()["memories"]) == 1

    def test_list_filter_by_truth_scope(self):
        client.post("/api/jarvis/memory", json={"content": "Live", "state_class": "live"})
        client.post("/api/jarvis/memory", json={"content": "Archived", "state_class": "archived"})
        resp = client.get("/api/jarvis/memory", params={"truth_scope": "live"})
        assert len(resp.json()["memories"]) == 1
