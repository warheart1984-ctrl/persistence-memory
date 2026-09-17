"""Tests for unified memory system (nx-search integration)."""
import sys
import os

# Add parent directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.nx_search_client import NxSearchClient
from app.models import MemoryCreate


def test_nx_search_client_init():
    """Test NxSearchClient initialization."""
    client = NxSearchClient()
    assert client.nx_path == "G:/nx-search"
    
    custom_client = NxSearchClient(nx_path="custom/path")
    assert custom_client.nx_path == "custom/path"


def test_nx_search_client_error_handling():
    """Test error handling in nx-search client."""
    client = NxSearchClient(nx_path="invalid/path")
    
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