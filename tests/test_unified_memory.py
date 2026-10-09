"""Tests for unified memory system (nx-search integration)."""
import sys
import os
import platform
import shutil
import subprocess
import threading
from pathlib import Path

import pytest

# Add parent directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.nx_search_client import NxSearchClient
from app.models import MemoryCreate, NxAskRequest, NxRememberRequest, NxDescribeRequest, NxSpatializeRequest, NxWatchRequest
from app.auth import nx_allowed_roots, validate_nx_path, nx_write_enabled, require_nx_write
from fastapi import HTTPException


def test_nx_search_client_init(monkeypatch):
    """The default install path depends on the platform; an explicit path always wins."""
    monkeypatch.delenv("NX_SEARCH_PATH", raising=False)
    client = NxSearchClient(require_available=False)
    expected = "G:/nx-search" if platform.system().lower() == "windows" else str(Path.home() / ".local" / "share" / "nx-search")
    assert client.nx_path == expected

    custom_client = NxSearchClient(nx_path="custom/path", require_available=False)
    assert custom_client.nx_path == "custom/path"


def test_default_nx_path_per_platform(monkeypatch):
    import app.nx_search_client as mod

    monkeypatch.delenv("NX_SEARCH_PATH", raising=False)
    monkeypatch.setattr(mod.platform, "system", lambda: "Windows")
    assert NxSearchClient(require_available=False).nx_path == "G:/nx-search"
    for system in ("Linux", "Darwin"):
        monkeypatch.setattr(mod.platform, "system", lambda system=system: system)
        assert NxSearchClient(require_available=False).nx_path == str(Path.home() / ".local" / "share" / "nx-search")
    monkeypatch.setenv("NX_SEARCH_PATH", "/opt/nx")
    assert NxSearchClient(require_available=False).nx_path == "/opt/nx"


def test_nx_search_client_error_handling():
    """Test error handling in nx-search client."""
    client = NxSearchClient(nx_path="invalid/path", require_available=False)
    
    # Should handle invalid path gracefully
    result = client.search("test")
    assert "error" in result
    assert result["content"] == []
    assert result["filenames"] == []


def test_promote_to_memory_format():
    """Test promotion of nx-search results to memory format."""
    client = NxSearchClient(require_available=False)
    
    search_result = {
        "path": "G:\\test\\file.py",
        "snippet": "def test_function(): pass",
        "rank": -10.5
    }
    
    memory_data = client.promote_to_memory(
        search_result,
        source_agent="test-agent",
        session_id="test-session",
        confidence=0.8
    )
    
    assert memory_data["content"].startswith("External context from")
    assert "test_function" in memory_data["content"]
    assert memory_data["source_agent"] == "test-agent"
    assert memory_data["session_id"] == "test-session"
    assert memory_data["type"] == "external_context"
    assert memory_data["confidence"] == 0.8
    assert len(memory_data["evidence"]) == 1
    assert memory_data["evidence"][0]["kind"] == "filesystem_evidence"
    assert memory_data["evidence"][0]["ref"] == "G:\\test\\file.py"
    assert "nx-search" in memory_data["tags"]


def test_memory_create_with_external_context():
    """Test MemoryCreate accepts external_context type."""
    memory_data = {
        "content": "Test external context",
        "source_agent": "test-agent",
        "session_id": "test-session",
        "type": "external_context",
        "confidence": 0.7,
        "evidence": [{"kind": "filesystem_evidence", "ref": "test/path"}],
        "subject": "test",
        "tags": ["test"],
    }
    
    memory = MemoryCreate(**memory_data)
    assert memory.type == "external_context"
    assert memory.confidence == 0.7
    assert len(memory.evidence) == 1


def test_promote_empty_snippet():
    """Test promotion with empty snippet."""
    client = NxSearchClient(require_available=False)
    
    search_result = {
        "path": "G:\\test\\file.py",
        "snippet": "",
    }
    
    memory_data = client.promote_to_memory(
        search_result,
        source_agent="test-agent",
        session_id="test-session",
    )
    
    assert memory_data["content"].startswith("External context from")
    assert "G:\\test\\file.py" in memory_data["content"]


def test_promote_windows_path():
    """Test promotion handles Windows paths correctly."""
    client = NxSearchClient(require_available=False)
    
    search_result = {
        "path": "D:\\New Project\\test.py",
        "snippet": "test content",
    }
    
    memory_data = client.promote_to_memory(
        search_result,
        source_agent="test-agent",
        session_id="test-session",
    )
    
    assert memory_data["subject"] == "test.py"
    assert "D:\\New Project\\test.py" in memory_data["content"]


def test_nx_ask_request_model():
    """Test NxAskRequest model validation."""
    req = NxAskRequest(question="What is evolving AI?", no_stream=True)
    assert req.question == "What is evolving AI?"
    assert req.no_stream is True
    
    req_default = NxAskRequest(question="test")
    assert req_default.no_stream is False


def test_nx_remember_request_model():
    """Test NxRememberRequest model validation."""
    req = NxRememberRequest(key="preference:theme", value="dark mode")
    assert req.key == "preference:theme"
    assert req.value == "dark mode"


def test_nx_describe_request_model():
    """Test NxDescribeRequest model validation."""
    req = NxDescribeRequest(
        image_path="G:/images/photo.png",
        question="What is in this image?",
        holo=True,
        native=False,
        save=True,
    )
    assert req.image_path == "G:/images/photo.png"
    assert req.question == "What is in this image?"
    assert req.holo is True
    assert req.native is False
    assert req.save is True


def test_nx_spatialize_request_model():
    """Test NxSpatializeRequest model validation."""
    req = NxSpatializeRequest(
        directory="G:/renders/frame_sequence",
        every_nth=2,
        max_frames=100,
        tag="project-alpha",
    )
    assert req.directory == "G:/renders/frame_sequence"
    assert req.every_nth == 2
    assert req.max_frames == 100
    assert req.tag == "project-alpha"


def test_nx_watch_request_model():
    """Test NxWatchRequest model validation."""
    req = NxWatchRequest(
        paths=["G:/project1", "F:/data"],
        debounce_ms=1000,
        no_reconcile=True,
    )
    assert req.paths == ["G:/project1", "F:/data"]
    assert req.debounce_ms == 1000
    assert req.no_reconcile is True


def test_client_methods_exist():
    """Test that new client methods exist and are callable."""
    client = NxSearchClient(require_available=False)
    
    # These methods should exist (they'll return errors without nx-search running, but shouldn't crash)
    assert hasattr(client, 'ask')
    assert hasattr(client, 'remember')
    assert hasattr(client, 'forget')
    assert hasattr(client, 'describe')
    assert hasattr(client, 'spatialize')
    assert hasattr(client, 'scan')
    assert hasattr(client, 'reindex')
    assert hasattr(client, 'prune')
    assert hasattr(client, 'serve')
    assert hasattr(client, 'watch')
    assert hasattr(client, 'stats')
    assert hasattr(client, 'close')
    assert hasattr(client, '__enter__')
    assert hasattr(client, '__exit__')


# --- Security/Validation Tests ---


def test_nx_allowed_roots_default():
    """Test default allowed roots are configured."""
    roots = nx_allowed_roots()
    assert isinstance(roots, list)
    assert len(roots) > 0
    # Default should include common Windows drives
    assert any("D:" in str(r) for r in roots)


def test_validate_nx_path_allows_allowed_root(tmp_path):
    """validate_nx_path allows paths within allowed roots."""
    root = tmp_path / "project"
    root.mkdir()
    (root / "file.py").write_text("x")
    result = validate_nx_path(str(root / "file.py"), [root])
    assert isinstance(result, Path)
    assert result == (root / "file.py").resolve()


def test_validate_nx_path_rejects_traversal(tmp_path):
    """validate_nx_path rejects ../ traversal out of the root."""
    root = tmp_path / "project"
    root.mkdir()
    with pytest.raises(HTTPException) as err:
        validate_nx_path(str(root / ".." / ".." / "etc" / "passwd"), [root])
    assert err.value.status_code == 403
    assert "outside allowed roots" in err.value.detail


def test_validate_nx_path_rejects_outside_root(tmp_path):
    """validate_nx_path rejects a sibling directory of the root."""
    root = tmp_path / "project"
    other = tmp_path / "other"
    root.mkdir()
    other.mkdir()
    with pytest.raises(HTTPException) as err:
        validate_nx_path(str(other / "file.txt"), [root])
    assert err.value.status_code == 403
    assert "outside allowed roots" in err.value.detail


def test_validate_nx_path_rejects_a_root_prefix_lookalike(tmp_path):
    """/x/project-evil is not inside /x/project."""
    root = tmp_path / "project"
    evil = tmp_path / "project-evil"
    root.mkdir()
    evil.mkdir()
    with pytest.raises(HTTPException) as err:
        validate_nx_path(str(evil / "f.txt"), [root])
    assert err.value.status_code == 403


@pytest.mark.skipif(sys.platform != "win32", reason="drive-letter path semantics")
def test_validate_nx_path_windows_forms():
    roots = [Path("G:/project")]
    assert str(validate_nx_path("G:/project/file.py", roots)) == "G:\\project\\file.py"
    for bad in ("G:/project/../../etc/passwd", "C:/Windows/System32", "D:/other/file.txt"):
        with pytest.raises(HTTPException) as err:
            validate_nx_path(bad, roots)
        assert err.value.status_code == 403


def test_nx_write_enabled_default_false():
    """Test nx_write_enabled defaults to False (secure by default)."""
    # Don't rely on env, test the function logic
    import os
    original = os.environ.get("JARVIS_NX_WRITE_ENABLED")
    os.environ["JARVIS_NX_WRITE_ENABLED"] = "false"
    try:
        assert nx_write_enabled() is False
    finally:
        if original is not None:
            os.environ["JARVIS_NX_WRITE_ENABLED"] = original
        else:
            os.environ.pop("JARVIS_NX_WRITE_ENABLED", None)


def test_nx_write_enabled_true_when_set():
    """Test nx_write_enabled returns True when env var is set."""
    import os
    original = os.environ.get("JARVIS_NX_WRITE_ENABLED")
    os.environ["JARVIS_NX_WRITE_ENABLED"] = "true"
    try:
        assert nx_write_enabled() is True
    finally:
        if original is not None:
            os.environ["JARVIS_NX_WRITE_ENABLED"] = original
        else:
            os.environ.pop("JARVIS_NX_WRITE_ENABLED", None)


def test_nx_enabled_default_false_on_public():
    """Test nx_enabled defaults to False on public deployments."""
    import os
    import importlib
    import app.auth
    import app.public_security
    
    original_public = os.environ.get("JARVIS_PUBLIC_MODE")
    original_nx = os.environ.get("JARVIS_NX_ENABLED")
    try:
        os.environ["JARVIS_PUBLIC_MODE"] = "true"
        os.environ.pop("JARVIS_NX_ENABLED", None)
        # Need to re-import to pick up new env
        importlib.reload(app.public_security)
        importlib.reload(app.auth)
        from app.auth import nx_enabled
        assert nx_enabled() is False
    finally:
        if original_public is not None:
            os.environ["JARVIS_PUBLIC_MODE"] = original_public
        else:
            os.environ.pop("JARVIS_PUBLIC_MODE", None)
        if original_nx is not None:
            os.environ["JARVIS_NX_ENABLED"] = original_nx
        else:
            os.environ.pop("JARVIS_NX_ENABLED", None)
        importlib.reload(app.public_security)
        importlib.reload(app.auth)


def test_nx_enabled_true_when_set():
    """Test nx_enabled returns True when explicitly set."""
    import os
    import importlib
    import app.auth
    
    original = os.environ.get("JARVIS_NX_ENABLED")
    os.environ["JARVIS_NX_ENABLED"] = "true"
    try:
        importlib.reload(app.auth)
        from app.auth import nx_enabled
        assert nx_enabled() is True
    finally:
        if original is not None:
            os.environ["JARVIS_NX_ENABLED"] = original
        else:
            os.environ.pop("JARVIS_NX_ENABLED", None)
        importlib.reload(app.auth)

# --- describe: save=true is a write, and is gated like one --------------------------------------------------------

class _RecordingClient:
    """Stands in for NxSearchClient in app.main; records whether it was ever asked to do anything."""

    instances: list["_RecordingClient"] = []

    def __init__(self, *args, **kwargs):
        self.calls: list[tuple] = []
        _RecordingClient.instances.append(self)

    def describe(self, *args, **kwargs):
        self.calls.append(("describe", args, kwargs))
        return {"output": "a described image"}

    def close(self):
        pass


@pytest.fixture()
def describe_env(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    import app.main as main

    image = tmp_path / "pic.png"
    image.write_bytes(b"\x89PNG")
    monkeypatch.setenv("NX_SCAN_ROOTS", str(tmp_path))
    monkeypatch.setenv("JARVIS_NX_ENABLED", "true")
    monkeypatch.setenv("JARVIS_MEMORY_WRITE_ENABLED", "true")
    monkeypatch.delenv("JARVIS_NX_WRITE_ENABLED", raising=False)
    _RecordingClient.instances = []
    monkeypatch.setattr(main, "NxSearchClient", _RecordingClient)
    return TestClient(main.app), str(image), main


def test_describe_without_save_needs_no_write_permission(describe_env):
    http, image, _ = describe_env
    response = http.post("/api/jarvis/memory/external/describe", json={"image_path": image, "save": False})
    assert response.status_code == 200, response.text
    assert [c[0] for i in _RecordingClient.instances for c in i.calls] == ["describe"]


def test_describe_with_save_is_refused_when_nx_writes_are_off_and_nothing_runs(describe_env):
    http, image, _ = describe_env
    response = http.post("/api/jarvis/memory/external/describe", json={"image_path": image, "save": True})
    assert response.status_code == 403
    assert "JARVIS_NX_WRITE_ENABLED" in response.text
    assert [i for i in _RecordingClient.instances if i.calls] == [], "nx-search must not be invoked, nor a ledger record written"


def test_describe_with_save_is_refused_when_memory_writes_are_off(describe_env, monkeypatch):
    http, image, _ = describe_env
    monkeypatch.setenv("JARVIS_NX_WRITE_ENABLED", "true")
    monkeypatch.setenv("JARVIS_MEMORY_WRITE_ENABLED", "false")
    response = http.post("/api/jarvis/memory/external/describe", json={"image_path": image, "save": True})
    assert response.status_code == 403
    assert [i for i in _RecordingClient.instances if i.calls] == []


def test_describe_with_save_runs_when_both_writes_are_allowed(describe_env, monkeypatch):
    http, image, main = describe_env
    monkeypatch.setenv("JARVIS_NX_WRITE_ENABLED", "true")
    saved = []

    class Store:
        def create_memory(self, data):
            saved.append(data)
            return type("M", (), {"id": "mem-1"})()

    monkeypatch.setattr(main, "get_store", lambda: Store())
    response = http.post("/api/jarvis/memory/external/describe", json={"image_path": image, "save": True})
    assert response.status_code == 200, response.text
    assert response.json()["ledger_memory_id"] == "mem-1" and len(saved) == 1
    assert [c[2]["save"] for i in _RecordingClient.instances for c in i.calls] == [True]
