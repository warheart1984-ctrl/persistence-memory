"""Tests for unified memory system (nx-search integration)."""
import sys
import os
from pathlib import Path

# Add parent directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.nx_search_client import NxSearchClient
from app.models import MemoryCreate, NxAskRequest, NxRememberRequest, NxDescribeRequest, NxSpatializeRequest, NxWatchRequest
from app.auth import nx_allowed_roots, validate_nx_path, nx_write_enabled, require_nx_write
from fastapi import HTTPException


def test_nx_search_client_init():
    """Test NxSearchClient initialization."""
    client = NxSearchClient(require_available=False)
    assert client.nx_path == "G:/nx-search"
    
    custom_client = NxSearchClient(nx_path="custom/path", require_available=False)
    assert custom_client.nx_path == "custom/path"


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
    client = NxSearchClient()
    
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
    client = NxSearchClient()
    
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
    client = NxSearchClient()
    
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
    client = NxSearchClient()
    
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


def test_validate_nx_path_allows_allowed_root():
    """Test validate_nx_path allows paths within allowed roots."""
    roots = [Path("G:/project")]
    result = validate_nx_path("G:/project/file.py", roots)
    assert isinstance(result, Path)
    assert str(result) == "G:\\project\\file.py"


def test_validate_nx_path_rejects_traversal():
    """Test validate_nx_path rejects path traversal attempts."""
    roots = [Path("G:/project")]
    
    # Test ../ traversal
    try:
        validate_nx_path("G:/project/../../etc/passwd", roots)
        assert False, "Should have raised HTTPException"
    except HTTPException as e:
        assert e.status_code == 403
        assert "outside allowed roots" in e.detail or "traversal" in e.detail.lower()


def test_validate_nx_path_rejects_outside_root():
    """Test validate_nx_path rejects paths outside allowed roots."""
    roots = [Path("G:/project")]
    
    try:
        validate_nx_path("C:/Windows/System32", roots)
        assert False, "Should have raised HTTPException"
    except HTTPException as e:
        assert e.status_code == 403
        assert "outside allowed roots" in e.detail


def test_validate_nx_path_rejects_absolute_outside():
    """Test validate_nx_path rejects absolute paths outside roots on Windows."""
    roots = [Path("G:/project")]
    
    try:
        validate_nx_path("D:/other/file.txt", roots)
        assert False, "Should have raised HTTPException"
    except HTTPException as e:
        assert e.status_code == 403


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