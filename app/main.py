from __future__ import annotations

import os

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware

from app.auth import ApiKeyMiddleware
from app.continuity import to_selection
from app.models import (
    BoardUpdate,
    MemoryBoard,
    MemoryCreate,
    MemoryUpdate,
    ExternalSearchRequest,
    ExternalPromotionRequest,
)
from app.nx_search_client import NxSearchClient
from app.store import get_store
from app.graph import bfs_search, shortest_path, find_related, connected_components, memory_graph_stats

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


@app.post("/api/jarvis/memory/graph/bfs")
def graph_bfs(body: dict[str, Any]):
    relations = set(body["relations"]) if body.get("relations") else None
    results = bfs_search(body.get("start_id"), body.get("depth", 2), body.get("max_nodes", 50), relations, body.get("min_confidence", 0.0), body.get("max_age_days"))
    return {"start_id": body.get("start_id"), "depth": body.get("depth", 2), "results": results, "count": len(results)}

@app.post("/api/jarvis/memory/graph/shortest-path")
def graph_shortest_path(body: dict[str, Any]):
    path = shortest_path(body.get("source"), body.get("target"), body.get("max_nodes", 1000), body.get("min_confidence", 0.0), body.get("max_age_days"))
    if path is None: raise HTTPException(status_code=404, detail="No path found between the given memories")
    return {"source": body.get("source"), "target": body.get("target"), "path": path}

@app.post("/api/jarvis/memory/graph/related")
def graph_related(body: dict[str, Any]):
    results = find_related(body.get("memory_id"), body.get("k", 10), body.get("min_distance", 1), body.get("max_distance", 3), body.get("min_confidence", 0.0), body.get("max_age_days"))
    return {"memory_id": body.get("memory_id"), "k": body.get("k", 10), "results": results, "count": len(results)}

@app.post("/api/jarvis/memory/graph/components")
def graph_components(body: dict[str, Any]):
    return {"min_size": body.get("min_size", 2), "components": connected_components(body.get("min_size", 2), body.get("min_confidence", 0.0), body.get("max_age_days"))}

@app.get("/api/jarvis/memory/graph/stats")
def graph_stats(min_confidence: float = 0.0, max_age_days: float | None = None):
    return memory_graph_stats(min_confidence=min_confidence, max_age_days=max_age_days)


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
    body: ExternalSearchRequest,
):
    """Search nx-search external memory and optionally promote results to working memory."""
    query = body.query
    name_only = body.name_only
    limit = body.limit
    auto_promote = body.auto_promote
    source_agent = body.source_agent
    session_id = body.session_id
    
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
    use_nx_fallback: bool = Query(default=True),
):
    """Search the truth ledger first; optionally augment it with untrusted NX evidence."""
    store = get_store()
    working_memories, selections, conflicts = store.retrieve(
        query=query,
        limit=limit,
        session_id=session_id,
    )
    
    external_results = {"content": [], "filenames": [], "skipped": True}
    if use_nx_fallback and not working_memories:
        external_results = NxSearchClient().search(query, limit=limit)
    
    return {
        "working_memory": {
            "memories": [m.model_dump() for m in working_memories],
            "selections": [s.model_dump() for s in selections],
            "conflicts": [c.model_dump() for c in conflicts],
        },
        "long_term_memory": external_results,
        "query": query,
        "source_agent": source_agent,
        "nx_fallback_used": use_nx_fallback and not working_memories,
    }


@app.post("/api/jarvis/memory/promote")
def promote_external_result(
    body: ExternalPromotionRequest,
):
    """Promote a specific nx-search result to structured working memory."""
    path = body.path
    snippet = body.snippet
    source_agent = body.source_agent
    session_id = body.session_id
    confidence = body.confidence
    
    store = get_store()
    nx_client = NxSearchClient()
    
    # Require the promoted record to be an actual result from the indexed
    # search, rather than accepting caller-invented filesystem evidence.
    verified_results = nx_client.search(body.query, limit=100)
    if "error" in verified_results:
        raise HTTPException(status_code=502, detail=verified_results["error"])
    if not any(
        item.get("path") == path and item.get("snippet") == snippet
        for item in verified_results.get("content", [])
    ):
        raise HTTPException(
            status_code=422,
            detail="Promotion requires an exact result returned by nx-search for the supplied query",
        )

    search_result = {"path": path, "snippet": snippet}
    memory_data = nx_client.promote_to_memory(
        search_result, source_agent, session_id, confidence
    )
    
    try:
        memory = store.create_memory(MemoryCreate(**memory_data))
        return {"memory": memory.model_dump(), "status": "promoted"}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
