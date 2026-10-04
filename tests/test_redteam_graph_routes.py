"""Graph routes validate input with bounded models and survive the sparse index."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import app.graph as graph
from app.main import app
from app.models import MemoryCreate
from app.store import get_store


@pytest.fixture
def client():
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


def _make(n: int, *, subject: str | None = "shared-subject") -> list[str]:
    store = get_store()
    return [
        store.create_memory(
            MemoryCreate(
                content=f"graph node number {i}",
                source_agent="t",
                session_id="s",
                type="fact",
                subject=subject,
            )
        ).id
        for i in range(n)
    ]


def test_bfs_non_integer_depth_is_422(client):
    ids = _make(2)
    response = client.post("/api/jarvis/memory/graph/bfs", json={"start_id": ids[0], "depth": "abc"})
    assert response.status_code == 422


@pytest.mark.parametrize(
    "path,body",
    [
        ("bfs", {}),
        ("bfs", {"start_id": "x", "depth": 10_000}),
        ("bfs", {"start_id": "x", "max_nodes": 10**9}),
        ("bfs", {"start_id": "x", "max_nodes": -5}),
        ("bfs", {"start_id": "x", "relations": ["r"] * 500}),
        ("bfs", {"start_id": "x" * 5000}),
        ("bfs", {"start_id": "x", "min_confidence": 7}),
        ("bfs", {"start_id": "x", "surprise": 1}),
        ("shortest-path", {"source": "a"}),
        ("shortest-path", {"source": "a", "target": "b", "max_nodes": "lots"}),
        ("related", {"memory_id": "a", "k": 10**9}),
        ("related", {"memory_id": "a", "max_distance": "far"}),
        ("components", {"min_size": "two"}),
        ("components", {"min_size": 0}),
        ("components", {"limit": 10**9}),
        ("components", {"max_age_days": -1}),
    ],
)
def test_invalid_graph_bodies_are_422(client, path, body):
    assert client.post(f"/api/jarvis/memory/graph/{path}", json=body).status_code == 422


def test_non_object_body_is_422(client):
    assert client.post("/api/jarvis/memory/graph/bfs", json=["not", "an", "object"]).status_code == 422


def test_valid_requests_still_work(client):
    ids = _make(3)
    bfs = client.post("/api/jarvis/memory/graph/bfs", json={"start_id": ids[0], "depth": 1})
    assert bfs.status_code == 200 and bfs.json()["count"] == 2
    path = client.post("/api/jarvis/memory/graph/shortest-path", json={"source": ids[0], "target": ids[2]})
    assert path.status_code == 200
    related = client.post("/api/jarvis/memory/graph/related", json={"memory_id": ids[0]})
    assert related.status_code == 200
    comps = client.post("/api/jarvis/memory/graph/components", json={})
    assert comps.status_code == 200 and len(comps.json()["components"]) == 1


def test_components_above_full_graph_cap_does_not_crash(client, monkeypatch):
    ids = _make(4)
    lone = _make(1, subject=None)[0]
    monkeypatch.setattr(graph, "MAX_MEMORIES_FOR_FULL_GRAPH", 3)  # force SparseMemoryIndex
    response = client.post("/api/jarvis/memory/graph/components", json={"min_size": 2})
    assert response.status_code == 200
    components = response.json()["components"]
    assert components == [sorted(ids)]
    assert lone not in components[0]


def test_components_limit_bounds_the_response(client, monkeypatch):
    for i in range(4):
        store = get_store()
        for j in range(2):
            store.create_memory(
                MemoryCreate(content=f"group {i} item {j}", source_agent="t", session_id="s", type="fact", subject=f"g{i}")
            )
    monkeypatch.setattr(graph, "MAX_MEMORIES_FOR_FULL_GRAPH", 3)
    response = client.post("/api/jarvis/memory/graph/components", json={"limit": 2})
    assert response.status_code == 200
    assert len(response.json()["components"]) == 2
