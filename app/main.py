from __future__ import annotations

import os

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Body
from fastapi.middleware.cors import CORSMiddleware

from app.auth import ApiKeyMiddleware
from app.continuity import to_selection
from app.models import (
    BoardUpdate,
    MemoryBoard,
    MemoryCreate,
    MemoryUpdate,
)
from app.nx_search_client import NxSearchClient
from app.store import get_store

load_dotenv()

app = FastAPI(
    title="Jarvis Continuity Ledger",
    description=(
        "Evidence-backed Continuity Ledger (persistence-memory). "
        "Stores decisions/facts with provenance — not conversation dumps. "
        "Consumers read the same ledger and decide independently what to use."
    ),
    version="0.2.0",
)

cors_origins = (os.getenv("JARVIS_CORS_ORIGINS") or "*").split(",")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in cors_origins if o.strip()],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(ApiKeyMiddleware)


@app.get("/")
def index():
    return {
        "service": "jarvis-memoryboard",
        "distribution": "persistence-memory",
        "schema": "continuity-ledger-v1",
        "version": "0.2.0",
        "docs": "/docs",
        "maturity": {
            "continuity": "enforced",
            "replay": "enforced",
            "conflict": "enforced",
            "drift": "partial",
        },
        "endpoints": {
            "board": {
                "GET": "/api/jarvis/memory/board",
                "POST": "/api/jarvis/memory/board",
                "PATCH": "/api/jarvis/memory/board",
            },
            "memories": {
                "list": "GET /api/jarvis/memory",
                "retrieve": "GET /api/jarvis/memory/retrieve",
                "conflicts": "GET /api/jarvis/memory/conflicts",
                "create": "POST /api/jarvis/memory",
                "read": "GET /api/jarvis/memory/{id}",
                "update": "PATCH /api/jarvis/memory/{id}",
                "delete": "DELETE /api/jarvis/memory/{id}",
            },
            "unified_memory": {
                "external_search": "POST /api/jarvis/memory/external-search",
                "unified_search": "GET /api/jarvis/memory/unified",
                "promote": "POST /api/jarvis/memory/promote",
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
        "schema": "continuity-ledger-v1",
        "memory_count": len(store.list_memories(limit=9999)),
        "board_id": board.board_id,
        "memory_write_enabled": True,
    }


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


@app.get("/api/jarvis/memory/retrieve")
def retrieve_memories(
    truth_scope: str | None = Query(default=None),
    query: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    type: str | None = Query(default=None, alias="type"),
    status: str | None = Query(default=None),
    session_id: str | None = Query(default=None),
    subject: str | None = Query(default=None),
):
    """Replay-grade retrieval: memories + why/where/when/session + conflicts."""
    store = get_store()
    memories, selections, conflicts = store.retrieve(
        truth_scope=truth_scope,
        query=query,
        limit=limit,
        memory_type=type,
        status=status,
        session_id=session_id,
        subject=subject,
    )
    return {
        "memories": [m.model_dump() for m in memories],
        "selections": [s.model_dump() for s in selections],
        "conflicts": [c.model_dump() for c in conflicts],
    }


@app.get("/api/jarvis/memory/conflicts")
def list_conflicts(subject: str | None = Query(default=None)):
    store = get_store()
    conflicts = store.conflicts(subject=subject)
    return {"conflicts": [c.model_dump() for c in conflicts]}


@app.get("/api/jarvis/memory")
def list_memories(
    truth_scope: str | None = Query(default=None),
    query: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    type: str | None = Query(default=None, alias="type"),
    status: str | None = Query(default=None),
    session_id: str | None = Query(default=None),
    subject: str | None = Query(default=None),
    with_provenance: bool = Query(default=True),
):
    """List memories. By default includes selection provenance (Replay Test)."""
    store = get_store()
    if with_provenance:
        memories, selections, conflicts = store.retrieve(
            truth_scope=truth_scope,
            query=query,
            limit=limit,
            memory_type=type,
            status=status,
            session_id=session_id,
            subject=subject,
        )
        return {
            "memories": [m.model_dump() for m in memories],
            "selections": [s.model_dump() for s in selections],
            "conflicts": [c.model_dump() for c in conflicts],
        }
    memories = store.list_memories(
        truth_scope=truth_scope,
        query=query,
        limit=limit,
        memory_type=type,
        status=status,
        session_id=session_id,
        subject=subject,
    )
    return {"memories": [m.model_dump() for m in memories]}


@app.post("/api/jarvis/memory")
def create_memory(body: MemoryCreate):
    store = get_store()
    try:
        rec = store.create_memory(body)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"memory": rec.model_dump()}


@app.get("/api/jarvis/memory/{memory_id}")
def get_memory(memory_id: str):
    store = get_store()
    rec = store.get_memory(memory_id)
    if not rec:
        raise HTTPException(status_code=404, detail="Memory not found")
    sel = to_selection(rec)
    return {"memory": rec.model_dump(), "selection": sel.model_dump()}


@app.patch("/api/jarvis/memory/{memory_id}")
def update_memory(memory_id: str, body: MemoryUpdate):
    store = get_store()
    try:
        rec = store.update_memory(memory_id, body)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
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


# --- Unified Memory System endpoints (nx-search integration) ---

@app.post("/api/jarvis/memory/external-search")
def external_search(
    body: dict = Body(...),
):
    """Search nx-search external memory and optionally promote results to working memory."""
    query = body.get("query", "")
    name_only = body.get("name_only", False)
    limit = body.get("limit", 25)
    auto_promote = body.get("auto_promote", False)
    source_agent = body.get("source_agent", "unified-memory-system")
    session_id = body.get("session_id", "external-search-session")
    
    nx_client = NxSearchClient()
    search_results = nx_client.search(query, name_only=name_only, limit=limit)
    
    if "error" in search_results:
        raise HTTPException(status_code=500, detail=search_results["error"])
    
    promoted_memories = []
    if auto_promote and search_results.get("content"):
        store = get_store()
        for result in search_results["content"][:5]:  # Promote top 5 results
            memory_data = nx_client.promote_to_memory(
                result, source_agent, session_id, confidence=0.7
            )
            try:
                memory = store.create_memory(MemoryCreate(**memory_data))
                promoted_memories.append(memory.model_dump())
            except ValueError:
                pass  # Skip invalid promotions
    
    return {
        "external_results": search_results,
        "promoted_memories": promoted_memories,
        "promotion_count": len(promoted_memories),
    }


@app.get("/api/jarvis/memory/unified")
def unified_search(
    query: str = Query(..., min_length=1),
    limit: int = Query(default=25, ge=1, le=100),
    source_agent: str = Query(default="unified-memory-system"),
    session_id: str = Query(default="unified-search-session"),
):
    """Search both working memory (Jarvis) and long-term memory (nx-search) simultaneously."""
    # Search working memory
    store = get_store()
    working_memories, selections, conflicts = store.retrieve(
        query=query,
        limit=limit,
        session_id=session_id,
    )
    
    # Search long-term memory (nx-search)
    nx_client = NxSearchClient()
    external_results = nx_client.search(query, limit=limit)
    
    return {
        "working_memory": {
            "memories": [m.model_dump() for m in working_memories],
            "selections": [s.model_dump() for s in selections],
            "conflicts": [c.model_dump() for c in conflicts],
        },
        "long_term_memory": external_results,
        "query": query,
        "source_agent": source_agent,
    }


@app.post("/api/jarvis/memory/promote")
def promote_external_result(
    body: dict = Body(...),
):
    """Promote a specific nx-search result to structured working memory."""
    path = body.get("path", "")
    snippet = body.get("snippet", "")
    source_agent = body.get("source_agent", "unified-memory-system")
    session_id = body.get("session_id", "promotion-session")
    confidence = body.get("confidence", 0.7)
    
    store = get_store()
    nx_client = NxSearchClient()
    
    search_result = {"path": path, "snippet": snippet}
    memory_data = nx_client.promote_to_memory(
        search_result, source_agent, session_id, confidence
    )
    
    try:
        memory = store.create_memory(MemoryCreate(**memory_data))
        return {"memory": memory.model_dump(), "status": "promoted"}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
