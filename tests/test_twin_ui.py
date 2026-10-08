"""Twin UI serving tests — dark by default, allowlisted assets, no data leaks."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import _TWIN_UI_DIR, app
from app.models import MemoryCreate
from app.store import get_store

client = TestClient(app)

UI = Path(__file__).resolve().parent.parent / "ui" / "twin"


def _flags(monkeypatch, twin="1", narrator_flag="1"):
    monkeypatch.setenv("JARVIS_TWIN_ENABLED", twin)
    monkeypatch.setenv("JARVIS_TWIN_NARRATOR_ENABLED", narrator_flag)


def _store_write(**kw):
    base = dict(content="x", source_agent="devin", session_id="s1",
                type="fact", confidence=0.5, status="draft")
    base.update(kw)
    return get_store().create_memory(MemoryCreate(**base))


# --- dark by default ---

def test_ui_404_when_twin_disabled(monkeypatch):
    _flags(monkeypatch, twin="0")
    assert client.get("/ui/twin").status_code == 404
    assert client.get("/ui/twin/app.js").status_code == 404
    assert client.get("/ui/twin/styles.css").status_code == 404


# --- serving ---

def test_ui_serves_index_and_assets(monkeypatch):
    _flags(monkeypatch)
    for path, ctype in (
        ("/ui/twin", "text/html"),
        ("/ui/twin/index.html", "text/html"),
        ("/ui/twin/app.js", "javascript"),
        ("/ui/twin/styles.css", "text/css"),
    ):
        r = client.get(path)
        assert r.status_code == 200, path
        assert ctype in r.headers["content-type"]
    assert "<title>AI Twin" in client.get("/ui/twin").text


def test_ui_asset_allowlist_blocks_everything_else(monkeypatch):
    _flags(monkeypatch)
    for bad in ("../main.py", "..%2f..%2fapp%2fmain.py", "config.json",
                "index.html.bak", "app.js/extra"):
        assert client.get(f"/ui/twin/{bad}").status_code == 404, bad


def test_ui_dir_matches_repo_layout():
    assert _TWIN_UI_DIR.is_dir()
    for f in ("index.html", "app.js", "styles.css"):
        assert (_TWIN_UI_DIR / f).is_file()


# --- the page carries no data and no secrets ---

def test_ui_files_embed_no_secrets_or_data():
    """The page fetches everything — nothing baked in."""
    html = (UI / "index.html").read_text()
    js = (UI / "app.js").read_text()
    for token in ("api_key", "Authorization", "Bearer", "JARVIS_TWIN_ALLOWED"):
        assert token not in html, token
        assert token not in js, token
    # JS never writes innerHTML with data — all text goes through textContent
    assert "innerHTML" not in js
    assert "/api/jarvis/twin/state" in js
    assert "/api/jarvis/twin/narration" in js


# --- dropped model text can never reach the page ---

def test_receipt_dropped_entries_carry_no_text(monkeypatch):
    """PINNED: the receipt's drop records are {section,index,reason} only —
    the UI has nothing to accidentally render."""
    _flags(monkeypatch)
    _store_write(content="seed", status="verified", tags=["g"])
    r = client.get("/api/jarvis/twin/narration?provider=none")
    assert r.status_code == 200
    for d in r.json()["receipt"]["dropped"]:
        assert set(d) <= {"section", "index", "reason"}
        assert "text" not in d


def test_dropped_model_sentence_absent_from_response(monkeypatch):
    """A gated-away sentence is in neither `narration` nor `receipt` text."""
    _flags(monkeypatch)
    _store_write(content="seed", status="verified", tags=["g"])
    evil = json.dumps({"sections": {
        "assessment": [{"text": "Coverage index is 0.99 and let's assume the rest",
                        "cites": ["coverage_index"]}],
        "opportunity": [], "risk": [], "next_action": [], "explanation": []}})

    class FakeAdapter:
        name = "fake"

        def narrate(self, req):
            from app.narrator.base import NarrationResponse
            return NarrationResponse(text=evil, provider="fake",
                                     model="fake-1", latency_ms=1)

    import app.narrator as narrator_mod
    cfg = type("C", (), {"adapter": "ollama", "model": "x", "name": "fake"})()
    monkeypatch.setattr(narrator_mod, "get_adapter", lambda name: (cfg, FakeAdapter()))
    r = client.get("/api/jarvis/twin/narration?provider=fake")
    body = r.json()
    assert r.status_code == 200
    # the dropped sentence's unique text appears nowhere the UI can render
    joined = json.dumps(body["narration"]) + json.dumps(body["receipt"])
    assert "assume the rest" not in joined
    assert "0.99" not in json.dumps(body["narration"])
    assert body["receipt"]["dropped"][0]["reason"] == "HEDGE_CLAUSE"
