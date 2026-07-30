from __future__ import annotations

import tempfile
from pathlib import Path

from app.models import MemoryBoard, MemoryCreate, MemoryUpdate
from app.store import JarvisStore


def _store() -> JarvisStore:
    tmp = Path(tempfile.mktemp(suffix=".json"))
    return JarvisStore(str(tmp))


def test_default_board():
    s = _store()
    board = s.get_board()
    assert board.board_id == "default_board"
    assert isinstance(board.slots, list)
    assert isinstance(board.governance, list)


def test_set_board():
    s = _store()
    board = MemoryBoard(
        board_id="test-board",
        summary="Test board",
        linked_subsystems=["jarvis", "mrs"],
    )
    s.set_board(board)
    loaded = s.get_board()
    assert loaded.board_id == "test-board"
    assert loaded.summary == "Test board"


def test_patch_board():
    s = _store()
    s.patch_board({"summary": "Patched summary"})
    assert s.get_board().summary == "Patched summary"


def test_patch_board_ignores_none():
    s = _store()
    s.patch_board({"summary": "New", "linked_subsystems": None})
    assert s.get_board().summary == "New"


def test_create_memory():
    s = _store()
    data = MemoryCreate(content="Test memory", category="signal", tags=["test"])
    rec = s.create_memory(data)
    assert rec.id.startswith("mem-")
    assert rec.content == "Test memory"
    assert rec.category == "signal"
    assert rec.tags == ["test"]
    assert rec.scope == "session"
    assert rec.state_class == "live"
    assert rec.truth_status == "pending"
    assert rec.created_at
    assert rec.updated_at == rec.created_at


def test_get_memory():
    s = _store()
    data = MemoryCreate(content="Find me")
    rec = s.create_memory(data)
    found = s.get_memory(rec.id)
    assert found is not None
    assert found.content == "Find me"


def test_get_memory_missing():
    s = _store()
    assert s.get_memory("nonexistent") is None


def test_update_memory():
    s = _store()
    rec = s.create_memory(MemoryCreate(content="Original", tags=["a"]))
    updated = s.update_memory(rec.id, MemoryUpdate(content="Updated", tags=["a", "b"]))
    assert updated is not None
    assert updated.content == "Updated"
    assert updated.tags == ["a", "b"]
    assert updated.updated_at is not None


def test_update_memory_partial():
    s = _store()
    rec = s.create_memory(MemoryCreate(content="Original", tags=["a"]))
    updated = s.update_memory(rec.id, MemoryUpdate(content="New content only"))
    assert updated is not None
    assert updated.content == "New content only"
    assert updated.tags == ["a"]


def test_update_memory_missing():
    s = _store()
    assert s.update_memory("nope", MemoryUpdate(content="x")) is None


def test_delete_memory():
    s = _store()
    rec = s.create_memory(MemoryCreate(content="Delete me"))
    assert s.delete_memory(rec.id) is True
    assert s.get_memory(rec.id) is None


def test_delete_memory_missing():
    s = _store()
    assert s.delete_memory("nope") is False


def test_list_memories_empty():
    s = _store()
    assert s.list_memories() == []


def test_list_memories_orders_by_created_desc():
    s = _store()
    a = s.create_memory(MemoryCreate(content="A"))
    b = s.create_memory(MemoryCreate(content="B"))
    results = s.list_memories()
    assert [r.id for r in results] == [b.id, a.id]


def test_list_memories_limit():
    s = _store()
    for i in range(10):
        s.create_memory(MemoryCreate(content=f"Mem {i}"))
    assert len(s.list_memories(limit=3)) == 3


def test_list_filter_by_truth_scope():
    s = _store()
    s.create_memory(MemoryCreate(content="Live one", state_class="live"))
    s.create_memory(MemoryCreate(content="Archived one", state_class="archived"))
    live = s.list_memories(truth_scope="live")
    assert len(live) == 1
    assert live[0].content == "Live one"


def test_list_filter_by_query_content():
    s = _store()
    s.create_memory(MemoryCreate(content="Tesseract lattice is canonical", tags=["tesseract"]))
    s.create_memory(MemoryCreate(content="Prefer glass cathedrals", tags=["glass"]))
    results = s.list_memories(query="tesseract")
    assert len(results) == 1


def test_list_filter_by_query_tag():
    s = _store()
    s.create_memory(MemoryCreate(content="Some memory", tags=["tesseract", "lattice"]))
    s.create_memory(MemoryCreate(content="Other memory", tags=["glass"]))
    results = s.list_memories(query="lattice")
    assert len(results) == 1


def test_persistence():
    tmp = Path(tempfile.mktemp(suffix=".json"))
    s1 = JarvisStore(str(tmp))
    s1.create_memory(MemoryCreate(content="Survive restart"))
    del s1
    s2 = JarvisStore(str(tmp))
    results = s2.list_memories()
    assert len(results) == 1
    assert results[0].content == "Survive restart"
    tmp.unlink(missing_ok=True)


def test_load_corrupted_store():
    tmp = Path(tempfile.mktemp(suffix=".json"))
    tmp.write_text("{bad json", "utf-8")
    s = JarvisStore(str(tmp))
    board = s.get_board()
    assert board.board_id == "default_board"
    tmp.unlink(missing_ok=True)
