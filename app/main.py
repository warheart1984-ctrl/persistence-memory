from __future__ import annotations

import os

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware

from app.models import (
    BoardUpdate,
    MemoryBoard,
    MemoryCreate,
    MemoryUpdate,
    MemoryRecord,
)
from app.store import get_store

app = FastAPI(
    title="Jarvis Memory Board",
    description=(
        "Persistent read/write memory board for MRS agents. "
        "Stores board context and memory records for cross-session continuity."
    ),
    version="0.1.0",
)

cors_origins = (os.getenv("JARVIS_CORS_ORIGINS") or "*").split(",")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in cors_origins],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/")
def index():
    return {
        "service": "jarvis-memoryboard",
        "version": "0.1.0",
        "docs": "/docs",
        "endpoints": {
            "board": {
                "GET": "/api/jarvis/memory/board",
                "POST": "/api/jarvis/memory/board",
                "PATCH": "/api/jarvis/memory/board",
            },
            "memories": {
                "list": "GET /api/jarvis/memory",
                "create": "POST /api/jarvis/memory",
                "read": "GET /api/jarvis/memory/{id}",
                "update": "PATCH /api/jarvis/memory/{id}",
                "delete": "DELETE /api/jarvis/memory/{id}",
            },
        },
    }


@app.get("/health")
def health():
    store = get_store()
    board = store.get_board()
    return {
        "status": "ok",
        "service": "jarvis-memoryboard",
        "memory_count": len(store.list_memories(limit=9999)),
        "board_id": board.board_id,
        "memory_write_enabled": True,
    }


# --- Board endpoints ---

@app.get("/api/jarvis/memory/board")
def get_board():
    store = get_store()
    board = store.get_board()
    return {"memory_board": board.model_dump()}


@app.post("/api/jarvis/memory/board")
def set_board(body: MemoryBoard):
    store = get_store()
    board = store.set_board(body)
    return {"memory_board": board.model_dump()}


@app.patch("/api/jarvis/memory/board")
def patch_board(body: BoardUpdate):
    store = get_store()
    updates = {k: v for k, v in body.model_dump().items() if v is not None}
    board = store.patch_board(updates)
    return {"memory_board": board.model_dump()}


# --- Memory endpoints ---

@app.get("/api/jarvis/memory")
def list_memories(
    truth_scope: str | None = Query(default=None),
    query: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
):
    store = get_store()
    memories = store.list_memories(truth_scope=truth_scope, query=query, limit=limit)
    return {"memories": [m.model_dump() for m in memories]}


@app.post("/api/jarvis/memory")
def create_memory(body: MemoryCreate):
    store = get_store()
    rec = store.create_memory(body)
    return {"memory": rec.model_dump()}


@app.get("/api/jarvis/memory/{memory_id}")
def get_memory(memory_id: str):
    store = get_store()
    rec = store.get_memory(memory_id)
    if not rec:
        raise HTTPException(status_code=404, detail="Memory not found")
    return {"memory": rec.model_dump()}


@app.patch("/api/jarvis/memory/{memory_id}")
def update_memory(memory_id: str, body: MemoryUpdate):
    store = get_store()
    rec = store.update_memory(memory_id, body)
    if not rec:
        raise HTTPException(status_code=404, detail="Memory not found")
    return {"memory": rec.model_dump()}


@app.delete("/api/jarvis/memory/{memory_id}")
def delete_memory(memory_id: str):
    store = get_store()
    ok = store.delete_memory(memory_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Memory not found")
    return {"status": "deleted", "id": memory_id}
