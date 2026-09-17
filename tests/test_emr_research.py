"""OpenAI search/fetch compatibility layer tests (read-only on EMR recall + resolve)."""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app.emr import reset_stm_for_tests
from app.emr_research import (
    EmrFetchRequest,
    EmrSearchRequest,
    citation_url,
    emr_fetch,
    emr_search,
)
from app.main import app
from app.models import MemoryCreate
from app.store import JarvisStore, reset_store_for_tests
from mcp_server.protocol import MCP_TOOLS, handle_tools_call


@pytest.fixture(autouse=True)
def _clean():
    reset_stm_for_tests()
    yield
    reset_stm_for_tests()


@pytest.fixture
def store(tmp_path, monkeypatch):
    db = tmp_path / "ledger.json"
    monkeypatch.setenv("JARVIS_STORE_PATH", str(db))
    return JarvisStore(path=str(db))


def test_search_returns_results_shape_with_url(store: JarvisStore, monkeypatch):
    monkeypatch.delenv("JARVIS_LEDGER_CITATION_BASE", raising=False)
    rec = store.create_memory(
        MemoryCreate(
            content="Governed recall prefers verified ledger records for constitutional work.",
            source_agent="test",
            session_id="s1",
            type="decision",
            confidence=0.9,
            evidence=[],
            status="verified",
            subject="constitutional-recall",
            tags=["governance"],
        )
    )
    result = emr_search(
        store,
        EmrSearchRequest(query="constitutional verified recall governance"),
    )
    assert "results" in result
    assert len(result["results"]) >= 1
    hit = next(r for r in result["results"] if r["id"] == rec.id)
    assert hit["title"] == "constitutional-recall"
    assert hit["url"] == citation_url(rec.id)
    assert hit["url"].startswith("ledger://")
    assert hit["url"]


def test_search_respects_citation_base(store: JarvisStore, monkeypatch):
    monkeypatch.setenv("JARVIS_LEDGER_CITATION_BASE", "https://ledger.example/mem")
    rec = store.create_memory(
        MemoryCreate(
            content="Citation base test memory.",
            source_agent="test",
            session_id="s1",
            type="fact",
            confidence=0.5,
            evidence=[],
            status="draft",
            subject="citation-test",
        )
    )
    result = emr_search(store, EmrSearchRequest(query="citation base test"))
    hit = next((r for r in result["results"] if r["id"] == rec.id), None)
    assert hit is not None
    assert hit["url"] == f"https://ledger.example/mem/{rec.id}"


def test_fetch_returns_full_text_for_known_id(store: JarvisStore):
    rec = store.create_memory(
        MemoryCreate(
            content="Full fetch body text for deep research.",
            source_agent="agent-a",
            session_id="sess-42",
            type="research",
            confidence=0.7,
            evidence=[{"kind": "ref", "ref": "test/evidence-1"}],
            status="verified",
            subject="fetch-subject",
            tags=["research", "test"],
        )
    )
    payload = emr_fetch(store, EmrFetchRequest(id=rec.id))
    assert payload["id"] == rec.id
    assert payload["title"] == "fetch-subject"
    assert payload["text"] == "Full fetch body text for deep research."
    assert payload["url"] == citation_url(rec.id)
    assert payload["metadata"]["type"] == "research"
    assert payload["metadata"]["status"] == "verified"
    assert payload["metadata"]["subject"] == "fetch-subject"
    assert payload["metadata"]["tags"] == ["research", "test"]
    assert payload["metadata"]["content_sha256"] == rec.content_sha256
    assert payload["metadata"]["evidence"][0]["ref"] == "test/evidence-1"


def test_fetch_unknown_id_errors_gracefully(store: JarvisStore):
    with pytest.raises(ValueError, match="not found"):
        emr_fetch(store, EmrFetchRequest(id="mem-does-not-exist"))


def test_mcp_tools_list_includes_search_fetch():
    names = [t["name"] for t in MCP_TOOLS]
    assert "search" in names
    assert "fetch" in names
    assert "emr_search" in names
    assert "emr_fetch" in names
    search_tool = next(t for t in MCP_TOOLS if t["name"] == "search")
    fetch_tool = next(t for t in MCP_TOOLS if t["name"] == "fetch")
    assert search_tool["annotations"]["readOnlyHint"] is True
    assert fetch_tool["annotations"]["readOnlyHint"] is True


def test_mcp_call_search_happy_path(store: JarvisStore):
    rec = store.create_memory(
        MemoryCreate(
            content="MCP search path memory about render pipelines.",
            source_agent="test",
            session_id="s1",
            type="architecture",
            confidence=0.8,
            evidence=[],
            status="verified",
            subject="render-pipeline",
        )
    )

    def _invoke(name: str, arguments: dict) -> dict:
        if name == "search":
            return emr_search(store, EmrSearchRequest.model_validate(arguments))
        raise RuntimeError(name)

    result = handle_tools_call(
        {"name": "search", "arguments": {"query": "render pipeline architecture"}},
        _invoke,
    )
    assert result["isError"] is False
    structured = result["structuredContent"]
    assert "results" in structured
    assert any(r["id"] == rec.id for r in structured["results"])
    text_payload = json.loads(result["content"][0]["text"])
    assert text_payload["results"][0]["url"]


def test_mcp_call_fetch_happy_path(store: JarvisStore):
    rec = store.create_memory(
        MemoryCreate(
            content="MCP fetch full text content.",
            source_agent="test",
            session_id="s1",
            type="fact",
            confidence=0.6,
            evidence=[],
            status="draft",
        )
    )

    def _invoke(name: str, arguments: dict) -> dict:
        if name == "fetch":
            return emr_fetch(store, EmrFetchRequest.model_validate(arguments))
        raise RuntimeError(name)

    result = handle_tools_call(
        {"name": "fetch", "arguments": {"id": rec.id}},
        _invoke,
    )
    assert result["isError"] is False
    assert result["structuredContent"]["text"] == "MCP fetch full text content."
    assert result["structuredContent"]["url"].startswith("ledger://")


def test_rest_search_and_fetch(store: JarvisStore, monkeypatch):
    reset_store_for_tests()
    monkeypatch.delenv("EMR_RECALL_API_KEY", raising=False)
    with patch("app.main.get_store", return_value=store):
        client = TestClient(app)
        rec = store.create_memory(
            MemoryCreate(
                content="REST search fetch integration memory.",
                source_agent="test",
                session_id="s1",
                type="fact",
                confidence=0.5,
                evidence=[],
                status="draft",
                subject="rest-integration",
            )
        )
        search_resp = client.post(
            "/api/jarvis/tools/search",
            json={"query": "REST integration memory"},
        )
        assert search_resp.status_code == 200
        search_body = search_resp.json()
        assert any(r["id"] == rec.id for r in search_body["results"])

        fetch_resp = client.post(
            "/api/jarvis/tools/fetch",
            json={"id": rec.id},
        )
        assert fetch_resp.status_code == 200
        assert fetch_resp.json()["text"] == "REST search fetch integration memory."

        missing = client.post("/api/jarvis/tools/fetch", json={"id": "missing-id"})
        assert missing.status_code == 404
