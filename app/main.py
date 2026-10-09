from __future__ import annotations

import json
import logging
import os
import secrets
import subprocess
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi import Path as PathParam
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from pydantic import BaseModel, Field

from app.amul import (
    anchor_memory,
    get_field,
    verify_field,
)
import app.amul_llm as amul_llm
import app.amul_rag as amul_rag
from app.amul_rag import (
    AuthorityClass,
    DocumentStatus,
    answer_query,
    get_index,
    ledger_docs,
    maintain_replay_log,
    normalize_document,
)
from app.emr import (
    CorrectRequest,
    ExpandRequest,
    ExciteRequest,
    ReinforceRequest,
    clear_stm,
    correct_memory_ids,
    emr_status,
    excite,
    expand_stm_entry,
    get_stm,
    reinforce_ids,
    resolve_record,
    stm_context_block,
)
from app.emr_tool import EmrRecallRequest, emr_recall, tool_catalog
from app.emr_write import (
    EmrRememberRequest,
    EmrUpsertRequest,
    emr_remember,
    emr_upsert,
)
from app.emr_research import (
    EmrFetchRequest,
    EmrSearchRequest,
    emr_fetch,
    emr_search,
)
from app.emr_pipeline import ConsolidationRequest, pipeline as memory_pipeline
from app.models import (
    BoardUpdate,
    MemoryBoard,
    MemoryCreate,
    MemoryUpdate,
    ExternalSearchRequest,
    ExternalPromotionRequest,
    NxAskRequest,
    NxRememberRequest,
    NxForgetRequest,
    NxDescribeRequest,
    NxSpatializeRequest,
    NxScanRequest,
    NxWatchRequest,
)
from app.nx_search_client import NxSearchClient
from app.auth import (
    deployment_label,
    emr_recall_api_key,
    ledger_read_protected,
    ApiKeyMiddleware,
    ledger_read_protection_middleware,
    mcp_write_enabled,
    memory_write_enabled,
    require_emr_recall_api_key,
    require_mcp_write_scope,
    require_memory_write,
    require_operator_read,
    verify_operator_api_key,
    identity_middleware,
    oauth_enabled,
    nx_write_enabled,
    require_nx_write,
    require_nx_enabled,
    validate_nx_path,
    nx_allowed_roots,
)
from app.identity import current_tenant_key
from app.oauth import protected_resource_metadata
from app.public_security import cors_origins, public_security_middleware
from app.refusal import DENIED, LEDGER_UNAVAILABLE, VERSION_CONFLICT, json_response, retry_after_seconds
from app import clause_v
from app.clause_v import ClauseVViolation
from app import evidence as evidence_objects
from app import replay as replay_contracts
from app import attest as attest_module
from app.evidence import EvidenceError, EvidenceObjectCreate, require_operator_write
from app.store import StoreUnavailableError, StoreVersionConflict, get_store
from app.store_errors import InvalidInputError
from app.twin import (
    DIGEST_TAG_PREFIX,
    FilteredRecords,
    generate_twin_intelligence,
    is_twin_authored,
    twin_memory_payload,
)
from app.twin_state import build_twin_state
import app.narrator as narrator
from app.narrator.base import NarratorError
import app.twinchat.service as twinchat_service
from app.twinchat.models import ChatRequest
from app.twinchat.receipts import ReceiptError, get_receipt_store
from app.graph import (
    BfsBody,
    ComponentsBody,
    RelatedBody,
    ShortestPathBody,
    bfs_search,
    connected_components,
    find_related,
    memory_graph_stats,
    shortest_path,
)
from mcp_server.mcp_http import create_mcp_router
from mcp_server.protocol import ToolRefusal

from contextlib import asynccontextmanager


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan: track and cleanup background nx-search processes."""
    # Store for background processes
    app.state.nx_background = {"watch": None, "serve": None}
    yield
    # Cleanup on shutdown
    for name, proc in app.state.nx_background.items():
        if proc and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()


app = FastAPI(
    title="Jarvis Continuity Ledger",
    description=(
        "Jarvis Memoryboard — LTM access/API over Continuity Ledger SoT. "
        "Stack: AMUL (LTM substrate) → Memoryboard → EMR → STM → LLM. "
        "EMR decides active cognition; does not invent persistent LTM."
    ),
    version="0.2.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origins(),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


app.add_middleware(ApiKeyMiddleware)


@app.middleware("http")
async def _ledger_read_auth(request: Request, call_next):
    return await ledger_read_protection_middleware(request, call_next)


@app.middleware("http")
async def _public_security(request: Request, call_next):
    return await public_security_middleware(request, call_next)


@app.middleware("http")
async def _identity(request: Request, call_next):
    return await identity_middleware(request, call_next)


@app.get("/")
def index():
    return {
        "service": "jarvis-memoryboard",
        "schema": "continuity-ledger-v1",
        "version": "0.2.0",
        "docs": "/docs",
        "maturity": {
            "continuity": "enforced",
            "replay": "enforced",
            "conflict": "enforced",
            "drift": "partial",
            "emr_stm": "partial",
        },
        "architecture": {
            "AMUL": "LTM substrate (persistence/structure/lineage) — declared/partial",
            "Memoryboard": "LTM access/API — Continuity Ledger SoT (this service)",
            "EMR": "governed activation — POST /api/jarvis/memory/emr/excite | GET /active",
            "STM": "budgeted working set — GET /api/jarvis/memory/stm (+ context/expand)",
            "LLM": "reasoning surface (consumer of STM)",
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
            "emr_stm": {
                "active": "GET /api/jarvis/memory/active",
                "excite": "POST /api/jarvis/memory/emr/excite",
                "reinforce": "POST /api/jarvis/memory/emr/reinforce",
                "correct": "POST /api/jarvis/memory/emr/correct",
                "status": "GET /api/jarvis/memory/emr/status",
                "stm": "GET /api/jarvis/memory/stm",
                "stm_context": "GET /api/jarvis/memory/stm/context",
                "expand": "POST /api/jarvis/memory/stm/expand",
                "resolve": "GET /api/jarvis/memory/{id}/resolve",
                "clear": "DELETE /api/jarvis/memory/stm",
            },
            "amul": {
                "anchor": "POST /api/jarvis/memory/amul/anchor",
                "artifact": "GET /api/jarvis/memory/amul/artifacts/{id}",
                "lineage": "GET /api/jarvis/memory/amul/lineage/{memory_id}",
                "field_status": "GET /api/jarvis/memory/amul/field/status",
                "verify": "POST /api/jarvis/memory/amul/field/verify",
            },
            "rag": {
                "documents": "POST /api/jarvis/rag/documents",
                "query": "POST /api/jarvis/rag/query",
                "log": "GET /api/jarvis/rag/log",
                "status": "GET /api/jarvis/rag/status",
                "maintenance": "POST /api/jarvis/rag/maintenance",
            },
            "tools": {
                "catalog": "GET /api/jarvis/tools",
                "emr_recall": "POST /api/jarvis/tools/emr_recall",
                "search": "POST /api/jarvis/tools/search",
                "fetch": "POST /api/jarvis/tools/fetch",
                "emr_remember": "POST /api/jarvis/tools/emr_remember",
                "emr_upsert": "POST /api/jarvis/tools/emr_upsert",
            },
            "mcp": {
                "streamable_http": "POST /mcp",
                "transport": "streamable-http",
                "tools": [
                    "emr_recall",
                    "search",
                    "fetch",
                    "emr_search",
                    "emr_fetch",
                    "emr_remember",
                    "emr_upsert",
                ],
                "mcp_write_enabled": mcp_write_enabled(),
            },
        },
    }


@app.exception_handler(StoreUnavailableError)
async def _store_unavailable(request: Request, exc: StoreUnavailableError):
    logging.getLogger("jarvis.store").error("ledger unavailable: %s", exc)
    return json_response(503, "Ledger store unavailable", code=LEDGER_UNAVAILABLE)


@app.exception_handler(ClauseVViolation)
async def _clause_v_violation(request: Request, exc: ClauseVViolation):
    # 422, no Retry-After: retrying the same write cannot succeed. The body names every reason.
    return JSONResponse(status_code=422, content=exc.body())


@app.exception_handler(attest_module.AttestError)
async def _attest_error(request: Request, exc: attest_module.AttestError):
    return JSONResponse(status_code=exc.status, content={"detail": exc.message, "code": exc.code})


@app.exception_handler(EvidenceError)
async def _evidence_error(request: Request, exc: EvidenceError):
    # 422 (413 when too large), no Retry-After: the request itself has to change.
    return JSONResponse(status_code=exc.status, content=exc.body())


@app.exception_handler(InvalidInputError)
async def _invalid_input(request: Request, exc: InvalidInputError):
    return json_response(400, str(exc), code="invalid_input")


@app.exception_handler(StoreVersionConflict)
async def _version_conflict(request: Request, exc: StoreVersionConflict):
    return json_response(409, str(exc), code=VERSION_CONFLICT)


@app.exception_handler(StarletteHTTPException)
async def _http_exception(request: Request, exc: StarletteHTTPException):
    """Same bodies as before; 401/403 gain code=denied and every 503 gains Retry-After."""
    return json_response(exc.status_code, exc.detail, headers=getattr(exc, "headers", None))


@app.get("/ready")
def ready():
    """Readiness: can the ledger be served safely right now?  503 (with Retry-After) if not.

    Checked live on every call, never cached.  For PostgreSQL: SELECT 1, schema version, the role
    is neither superuser nor BYPASSRLS, the role is *proven* unable to write the history tables, and
    no legacy blob ledger is waiting to be imported.  Failing checks are named; details stay in the log.
    """
    try:
        checks = get_store().readiness()
    except StoreUnavailableError as exc:
        logging.getLogger("jarvis.store").error("not ready: %s", exc)
        checks = {"store": "failed"}
    stack = (os.getenv("JARVIS_STACK_ID") or "").strip()  # which deployment this is; scripts/chaos/ refuses anything that is not a throwaway
    if all(state == "ok" for state in checks.values()):
        return {"status": "ready", "checks": checks, **({"stack": stack} if stack else {})}
    body = {"status": "unavailable", "code": LEDGER_UNAVAILABLE, "checks": checks, **({"stack": stack} if stack else {})}
    return JSONResponse(status_code=503, content=body, headers={"Retry-After": str(retry_after_seconds())})


@app.get("/health")
def health():
    """Liveness: the process is up.  Never touches the ledger; see /ready for readiness."""
    return {
        "status": "ok",
        "live": True,
        "service": "jarvis-memoryboard",
        "schema": "continuity-ledger-v1",
        "memory_write_enabled": memory_write_enabled(),
        "mcp_write_enabled": mcp_write_enabled(),
        "deployment": deployment_label(),
        "auth": {
            "mode": "oauth" if oauth_enabled() else "operator",
            "emr_recall_key_required": emr_recall_api_key() is not None,
            "ledger_read_protected": ledger_read_protected(),
        },
        "mcp": {
            "streamable_http": "/mcp",
            "tools": [
                "emr_recall",
                "search",
                "fetch",
                "emr_search",
                "emr_fetch",
                "emr_remember",
                "emr_upsert",
            ],
            "mcp_write_enabled": mcp_write_enabled(),
        },
        "emr_tools_http": {
            "catalog": "GET /api/jarvis/tools",
            "emr_recall": "POST /api/jarvis/tools/emr_recall",
            "search": "POST /api/jarvis/tools/search",
            "fetch": "POST /api/jarvis/tools/fetch",
            "emr_remember": "POST /api/jarvis/tools/emr_remember",
            "emr_upsert": "POST /api/jarvis/tools/emr_upsert",
        },
    }


@app.get("/.well-known/oauth-protected-resource")
@app.get("/.well-known/oauth-protected-resource/mcp")
def oauth_protected_resource():
    """RFC 9728 metadata used by ChatGPT/Codex to discover OAuth."""
    return {key: value for key, value in protected_resource_metadata().items() if value is not None}


@app.get("/privacy", include_in_schema=False)
def privacy_policy():
    return {
        "service": "Jarvis Memoryboard",
        "summary": "OAuth subject identifiers select isolated ledgers. The service stores only governed memory records submitted through its tools.",
        "retention": "Records remain until the account owner requests deletion or the service retention policy changes.",
        "contact": os.getenv("JARVIS_SUPPORT_EMAIL", "support-not-configured"),
    }


@app.get("/terms", include_in_schema=False)
def terms_of_service():
    return {
        "service": "Jarvis Memoryboard",
        "summary": "Read tools return only the authenticated subject's ledger. Write tools require explicit approval and the memory.write scope.",
        "contact": os.getenv("JARVIS_SUPPORT_EMAIL", "support-not-configured"),
    }


# --- Board endpoints ---


@app.get("/api/jarvis/memory/board")
def get_board():
    store = get_store()
    board = store.get_board()
    return {"memory_board": board.model_dump()}


@app.post("/api/jarvis/memory/board")
def set_board(body: MemoryBoard, _: None = Depends(require_memory_write)):
    store = get_store()
    board = store.set_board(body)
    return {"memory_board": board.model_dump()}


@app.patch("/api/jarvis/memory/board")
def patch_board(body: BoardUpdate, _: None = Depends(require_memory_write)):
    store = get_store()
    updates = {k: v for k, v in body.model_dump().items() if v is not None}
    board = store.patch_board(updates)
    return {"memory_board": board.model_dump()}


# --- Continuity Ledger (LTM) endpoints ---


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


def _with_clause_v_warnings(payload: dict) -> dict:
    """Add the gate's soft (warn-only) findings for this write, if any, so the caller sees them."""
    warnings = clause_v.take_warnings()
    if warnings:
        payload["clause_v_warnings"] = warnings
    return payload


@app.post("/api/jarvis/memory")
def create_memory(body: MemoryCreate, _: None = Depends(require_memory_write)):
    store = get_store()
    try:
        rec = store.create_memory(body)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _with_clause_v_warnings({"memory": rec.model_dump()})


def _env_flag(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in ("1", "true", "yes")


@app.get("/api/jarvis/twin/daily")
def twin_daily(
    request: Request,
    persist: bool = Query(default=False),
):
    """AI Twin coverage index — read-only by default, dark until JARVIS_TWIN_ENABLED.

    Disabled -> 404: the surface stays invisible rather than advertising a
    disabled feature.  ``persist=1`` writes the brief back through the normal
    MemoryCreate path and additionally requires JARVIS_TWIN_PERSIST_ENABLED
    (403 TWIN_PERSIST_DISABLED) and the same write auth as a manual write.
    This route is outside LEDGER_READ_PREFIX, so the read-protection check is
    mirrored here explicitly.
    """
    if not _env_flag("JARVIS_TWIN_ENABLED"):
        raise HTTPException(status_code=404, detail="Not found")
    if not oauth_enabled() and ledger_read_protected():
        verify_operator_api_key(
            request.headers.get("authorization"),
            request.headers.get("x-emr-recall-key"),
        )
    store = get_store()  # already tenant-scoped (RLS / tenant key)
    now = datetime.now(timezone.utc)
    records = FilteredRecords.from_records(
        store.list_memories(limit=100000, truth_scope="live")
    )
    tenant = current_tenant_key() or "operator"
    packet = generate_twin_intelligence(records, identity_id=tenant, now=now)
    digest = packet["twin_input_digest"]
    result: dict = {"twin": packet}
    if not persist:
        return result
    if not _env_flag("JARVIS_TWIN_PERSIST_ENABLED"):
        raise HTTPException(status_code=403, detail="TWIN_PERSIST_DISABLED")
    require_memory_write()  # identical gate to POST /api/jarvis/memory

    day = now.date().isoformat()
    subject = f"twin:daily:{tenant}:{day}"
    todays = [
        m for m in store.list_memories(limit=200, subject=subject)
        if is_twin_authored(m)
    ]
    digest_tag = f"{DIGEST_TAG_PREFIX}{digest}"
    for m in todays:
        if digest_tag in (m.tags or []):
            result["persisted"] = "existing"
            result["memory_id"] = m.id
            return result
    payload = twin_memory_payload(
        packet,
        session_id=f"twin-{day}",
        day=day,
        supersedes=todays[0].id if todays else None,
    )
    try:
        rec = store.create_memory(MemoryCreate(**payload))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    result["persisted"] = "created"
    result["memory_id"] = rec.id
    return _with_clause_v_warnings(result)


def _twin_guard(request: Request) -> None:
    """404 when the twin is dark; mirrors the ledger-read operator-key check."""
    if not _env_flag("JARVIS_TWIN_ENABLED"):
        raise HTTPException(status_code=404, detail="Not found")
    if not oauth_enabled() and ledger_read_protected():
        verify_operator_api_key(
            request.headers.get("authorization"),
            request.headers.get("x-emr-recall-key"),
        )


def _twin_state_for_request(request: Request):
    """Tenant-scoped state for the caller — the twin never sees other tenants."""
    _twin_guard(request)
    store = get_store()
    now = datetime.now(timezone.utc)
    records = FilteredRecords.from_records(
        store.list_memories(limit=100000, truth_scope="live")
    )
    tenant = current_tenant_key() or "operator"
    return build_twin_state(records, identity_id=tenant, now=now)


@app.get("/api/jarvis/twin/state")
def twin_state(request: Request):
    """TwinState.v1 — read-only. JARVIS_TWIN_ENABLED off -> 404."""
    return {"state": _twin_state_for_request(request)}


@app.get("/api/jarvis/twin/providers")
def twin_providers(request: Request):
    """Configured narrator providers — names and models only, never URLs/keys."""
    _twin_guard(request)
    try:
        return {"providers": narrator.provider_catalog()}
    except NarratorError as exc:
        raise HTTPException(status_code=400, detail=exc.code) from exc


@app.get("/api/jarvis/twin/narration")
def twin_narration(request: Request, provider: str = Query(default="none")):
    """Gated narration over TwinState — model text never ships unchecked.

    Requires BOTH JARVIS_TWIN_ENABLED and JARVIS_TWIN_NARRATOR_ENABLED
    (404 otherwise). Unknown provider -> 400 NARRATOR_UNKNOWN. Any model
    failure falls back to the deterministic template; the receipt records it.
    """
    _twin_guard(request)
    if not _env_flag("JARVIS_TWIN_NARRATOR_ENABLED"):
        raise HTTPException(status_code=404, detail="Not found")
    try:
        cfg, adapter = narrator.get_adapter(provider)
    except NarratorError as exc:
        raise HTTPException(status_code=400, detail=exc.code) from exc
    state = _twin_state_for_request(request)
    out = narrator.narrate_state(state, cfg, adapter)
    return {"state": state, "narration": out["sections"], "receipt": out["receipt"]}


# --- TwinChat (governed conversation; flag-dark like the rest of twin) ---


def _twin_chat_guard(request: Request) -> str:
    """twin guard + chat flag + tenant key. Returns the internal tenant key."""
    _twin_guard(request)
    if not _env_flag("JARVIS_TWIN_CHAT_ENABLED"):
        raise HTTPException(status_code=404, detail="Not found")
    return current_tenant_key() or "operator"


@app.post("/api/jarvis/twin/chat")
def twin_chat(request: Request, body: ChatRequest):
    """One governed turn: session window → recall → backend → gate → receipt.

    Dark unless JARVIS_TWIN_CHAT_ENABLED. ``persist=1`` additionally needs
    JARVIS_TWIN_CHAT_PERSIST_ENABLED (403 TWIN_CHAT_PERSIST_DISABLED) and the
    same write auth as POST /api/jarvis/memory — checked before any model
    call so a refused persist never spends a turn. A second concurrent turn
    on the same session gets 409 SESSION_BUSY; an exhausted receipt store
    gets 503 RECEIPT_STORE_FULL.
    """
    tenant = _twin_chat_guard(request)
    if body.persist:
        if not _env_flag("JARVIS_TWIN_CHAT_PERSIST_ENABLED"):
            raise HTTPException(status_code=403, detail="TWIN_CHAT_PERSIST_DISABLED")
        require_memory_write()
    try:
        return twinchat_service.run_turn(
            get_store(), body, tenant_key=tenant
        )
    except NarratorError as exc:
        raise HTTPException(status_code=400, detail=exc.code) from exc
    except ReceiptError as exc:
        status = 409 if exc.code == "SESSION_BUSY" else 503
        raise HTTPException(status_code=status, detail=exc.code) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/jarvis/twin/chat/receipts/{digest}")
def twin_chat_receipt(request: Request, digest: str):
    """One tenant-scoped receipt by digest — 404 for unknown or cross-tenant."""
    tenant = _twin_chat_guard(request)
    body = get_receipt_store().get_receipt(tenant, digest)
    if body is None:
        raise HTTPException(status_code=404, detail="RECEIPT_UNKNOWN")
    return {"receipt": body}


@app.get("/api/jarvis/twin/chat/sessions/{session_id}/turns")
def twin_chat_session_turns(request: Request, session_id: str):
    """Ordered receipt digests for a session — tenant-scoped."""
    tenant = _twin_chat_guard(request)
    return {"turns": get_receipt_store().session_turns(tenant, session_id)}


# --- Asset Twin (load-bearing diagram, simulation-only reference) ---


def _asset_twin_guard(request: Request) -> str:
    """Dark unless JARVIS_ASSET_TWIN_ENABLED; mirrors twin guard + tenant key."""
    if not _env_flag("JARVIS_ASSET_TWIN_ENABLED"):
        raise HTTPException(status_code=404, detail="Not found")
    _twin_guard(request)
    return current_tenant_key() or "operator"


@app.post("/api/jarvis/asset-twin/cycle")
def asset_twin_cycle(request: Request, body: dict):
    """One simulated cycle: telemetry -> twin -> recommendation. Never executes.

    Simulation-only: the asset is an in-process model. The response carries a
    pending veto record; movement requires a separate human approve + execute.
    """
    import app.asset_twin.service as asset_service
    from app.asset_twin.models import Telemetry

    tenant = _asset_twin_guard(request)
    try:
        telemetry = Telemetry.model_validate(body)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"bad telemetry: {exc}") from exc
    return asset_service.run_cycle(tenant, telemetry).model_dump(mode="json")


@app.post("/api/jarvis/asset-twin/decide")
def asset_twin_decide(request: Request, body: dict):
    """Human veto gate: approve | veto | hold. Veto always wins; executed is final."""
    import app.asset_twin.service as asset_service
    from app.asset_twin.models import VetoDecision

    tenant = _asset_twin_guard(request)
    try:
        verdict = VetoDecision.model_validate(body)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"bad verdict: {exc}") from exc
    if verdict.verdict == "approve":
        require_memory_write()
    try:
        return asset_service.decide_human(tenant, verdict, actor=tenant)
    except (KeyError, ValueError) as exc:
        raise HTTPException(status_code=404 if "unknown" in str(exc) else 400, detail=str(exc)) from exc


@app.post("/api/jarvis/asset-twin/execute")
def asset_twin_execute(request: Request, body: dict):
    """Simulated execution only: refuses anything not approved + unexpired."""
    import app.asset_twin.service as asset_service

    tenant = _asset_twin_guard(request)
    require_memory_write()
    decision_id = str(body.get("decision_id", ""))
    asset_id = str(body.get("asset_id", ""))
    if not decision_id or not asset_id:
        raise HTTPException(status_code=400, detail="decision_id and asset_id required")
    return asset_service.execute(tenant, decision_id, asset_id).model_dump(mode="json")


@app.get("/api/jarvis/asset-twin/audit")
def asset_twin_audit(request: Request):
    """Tenant-scoped evidence chain validity (digests + problems, no raw bus)."""
    from app.asset_twin.evidence import get_ledger

    tenant = _asset_twin_guard(request)
    ok, problems = get_ledger().verify(tenant)
    return {"chain_valid": ok, "problems": problems}


_TWIN_UI_DIR = Path(__file__).resolve().parent.parent / "ui" / "twin"
_TWIN_UI_FILES = {
    "index.html": "text/html; charset=utf-8",
    "app.js": "text/javascript; charset=utf-8",
    "styles.css": "text/css; charset=utf-8",
}


@app.get("/ui/twin", include_in_schema=False)
def twin_ui_index(request: Request):
    """Read-only Twin dashboard — dark (404) when JARVIS_TWIN_ENABLED is off."""
    _twin_guard(request)
    return FileResponse(_TWIN_UI_DIR / "index.html", media_type="text/html")


@app.get("/ui/twin/{asset}", include_in_schema=False)
def twin_ui_asset(request: Request, asset: str):
    """Static twin UI assets. Filename allowlist — no traversal, no data files."""
    _twin_guard(request)
    media = _TWIN_UI_FILES.get(asset)
    if media is None:
        raise HTTPException(status_code=404, detail="Not found")
    return FileResponse(_TWIN_UI_DIR / asset, media_type=media)


@app.post("/api/jarvis/memory/external-search", dependencies=[Depends(require_nx_enabled), Depends(require_emr_recall_api_key)])
def external_search(body: ExternalSearchRequest):
    """Search nx-search; optional promotion remains bounded and auditable."""
    if body.auto_promote:
        require_memory_write()  # promotion writes to the ledger; search alone stays read-only
    client = NxSearchClient()
    results = client.search(body.query, name_only=body.name_only, limit=body.limit)
    if "error" in results:
        raise HTTPException(status_code=502, detail=results["error"])

    promoted = []
    if body.auto_promote and results.get("content"):
        store = get_store()
        for item in results["content"][:5]:
            data = client.promote_to_memory(item, body.source_agent, body.session_id)
            try:
                promoted.append(store.create_memory(MemoryCreate(**data)).model_dump())
            except ValueError:
                continue
    return {"external_results": results, "promoted_memories": promoted, "promotion_count": len(promoted)}


@app.get("/api/jarvis/memory/unified", dependencies=[Depends(require_nx_enabled), Depends(require_emr_recall_api_key)])
def unified_search(
    query: str = Query(..., min_length=1, max_length=500),
    limit: int = Query(default=25, ge=1, le=100),
    session_id: str = Query(default="unified-search-session", min_length=1, max_length=128),
):
    """Return working-memory records alongside nx-search evidence."""
    store = get_store()
    memories, selections, conflicts = store.retrieve(query=query, limit=limit, session_id=session_id)
    external = NxSearchClient().search(query, limit=limit)
    if "error" in external:
        raise HTTPException(status_code=502, detail=external["error"])
    return {
        "working_memory": {
            "memories": [m.model_dump() for m in memories],
            "selections": [s.model_dump() for s in selections],
            "conflicts": [c.model_dump() for c in conflicts],
        },
        "long_term_memory": external,
        "query": query,
    }


@app.post("/api/jarvis/memory/promote", dependencies=[Depends(require_memory_write)])
def promote_external_result(body: ExternalPromotionRequest):
    """Promote only an exact result returned by nx-search for the query."""
    client = NxSearchClient()
    results = client.search(body.query, limit=100)
    if "error" in results:
        raise HTTPException(status_code=502, detail=results["error"])
    if not any(item.get("path") == body.path and item.get("snippet") == body.snippet
               for item in results.get("content", [])):
        raise HTTPException(status_code=422, detail="Promotion requires an exact nx-search result")

    data = client.promote_to_memory(
        {"path": body.path, "snippet": body.snippet},
        body.source_agent,
        body.session_id,
        body.confidence,
    )
    try:
        memory = get_store().create_memory(MemoryCreate(**data))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"memory": memory.model_dump(), "status": "promoted"}


# --- nx-search extended capabilities ---


@app.get("/api/jarvis/memory/external/stats", dependencies=[Depends(require_nx_enabled), Depends(require_emr_recall_api_key)])
def external_stats():
    """Get nx-search index statistics."""
    client = NxSearchClient()
    stats = client.stats()
    if "error" in stats:
        raise HTTPException(status_code=502, detail=stats["error"])
    return stats


@app.post("/api/jarvis/memory/external/ask", dependencies=[Depends(require_nx_enabled), Depends(require_emr_recall_api_key)])
def external_ask(body: NxAskRequest):
    """Ask JARVIS a natural-language question over indexed files (read-only)."""
    client = NxSearchClient()
    result = client.ask(body.question, no_stream=body.no_stream)
    if "error" in result:
        raise HTTPException(status_code=502, detail=result["error"])
    return result


@app.post("/api/jarvis/memory/external/remember", dependencies=[Depends(require_nx_enabled), Depends(require_nx_write), Depends(require_memory_write)])
def external_remember(body: NxRememberRequest, request: Request):
    """Store a persistent preference — written to BOTH nx-search AND the Continuity Ledger as evidenced record."""
    client = NxSearchClient()
    
    # 1. Write to nx-search local memory
    result = client.remember(body.key, body.value)
    if "error" in result:
        raise HTTPException(status_code=502, detail=result["error"])
    
    # 2. Also write to ledger as evidenced memory record
    store = get_store()
    try:
        tenant = current_tenant_key() or "operator"
        ledger_data = {
            "content": f"nx-search preference: {body.key} = {body.value}",
            "source_agent": "nx-search-bridge",
            "session_id": f"nx-remember-{body.key}",
            "type": "external_context",
            "confidence": 1.0,
            "evidence": [
                {"kind": "nx_memory", "ref": f"nx:{body.key}", "note": "nx-search persistent preference"}
            ],
            "subject": f"nx-preference:{body.key}",
            "tags": ["nx-search", "preference", "user-configured"],
        }
        ledger_mem = store.create_memory(MemoryCreate(**ledger_data))
        result["ledger_memory_id"] = ledger_mem.id
        result["ledger_status"] = "written"
    except Exception as e:
        result["ledger_status"] = f"failed: {e}"
    
    # Audit log
    _audit_log("nx_remember", request, {"key": body.key, "ledger_id": result.get("ledger_memory_id")})
    return result


@app.post("/api/jarvis/memory/external/forget", dependencies=[Depends(require_nx_enabled), Depends(require_nx_write), Depends(require_memory_write)])
def external_forget(body: NxForgetRequest, request: Request):
    """Forget a persistent preference — removed from nx-search AND marked archived in ledger."""
    client = NxSearchClient()
    
    # 1. Forget from nx-search
    result = client.forget(body.key)
    if "error" in result:
        raise HTTPException(status_code=502, detail=result["error"])
    
    # 2. Archive in ledger
    store = get_store()
    try:
        tenant = current_tenant_key() or "operator"
        # Find and archive the ledger record
        memories = store.list_memories(limit=100, subject=f"nx-preference:{body.key}")
        for mem in memories:
            if mem.type == "external_context" and f"nx-preference:{body.key}" in (mem.subject or ""):
                store.update_memory(mem.id, MemoryUpdate(status="archived"))
                result["ledger_archived"] = mem.id
                break
    except Exception as e:
        result["ledger_status"] = f"archive_failed: {e}"
    
    _audit_log("nx_forget", request, {"key": body.key, "ledger_archived": result.get("ledger_archived")})
    return result


@app.post("/api/jarvis/memory/external/describe", dependencies=[Depends(require_nx_enabled), Depends(require_emr_recall_api_key)])
def external_describe(body: NxDescribeRequest, request: Request):
    """Describe an image via vision (NVIDIA + HoloRT4D). Path validated against allowed roots."""
    # Validate path is within allowed roots
    validated_path = validate_nx_path(body.image_path)
    
    client = NxSearchClient()
    result = client.describe(
        str(validated_path),
        question=body.question,
        holo=body.holo,
        native=body.native,
        save=body.save,
    )
    if "error" in result:
        raise HTTPException(status_code=502, detail=result["error"])
    
    # If save=True, also write description to ledger
    if body.save and "error" not in result:
        store = get_store()
        try:
            ledger_data = {
                "content": f"Image description for {validated_path}: {result.get('output', result.get('text', ''))[:2000]}",
                "source_agent": "nx-search-vision",
                "session_id": f"nx-describe-{validated_path.name}",
                "type": "external_context",
                "confidence": 0.9,
                "evidence": [
                    {"kind": "filesystem_evidence", "ref": str(validated_path), "note": "nx-search vision description"}
                ],
                "subject": f"image:{validated_path.name}",
                "tags": ["nx-search", "vision", "image-description"],
            }
            ledger_mem = store.create_memory(MemoryCreate(**ledger_data))
            result["ledger_memory_id"] = ledger_mem.id
        except Exception as e:
            result["ledger_status"] = f"failed: {e}"
    
    _audit_log("nx_describe", request, {"path": str(validated_path), "save": body.save})
    return result


@app.post("/api/jarvis/memory/external/spatialize", dependencies=[Depends(require_nx_enabled), Depends(require_nx_write), Depends(require_memory_write)])
def external_spatialize(body: NxSpatializeRequest, request: Request):
    """Spatialize a directory of rendered frames (temporal + spatial memory). Path validated."""
    validated_path = validate_nx_path(body.directory)
    
    client = NxSearchClient()
    result = client.spatialize(
        str(validated_path),
        every_nth=body.every_nth,
        max_frames=body.max_frames,
        tag=body.tag,
    )
    if "error" in result:
        raise HTTPException(status_code=502, detail=result["error"])
    
    _audit_log("nx_spatialize", request, {"directory": str(validated_path), "every_nth": body.every_nth, "max_frames": body.max_frames})
    return result


@app.post("/api/jarvis/memory/external/scan", dependencies=[Depends(require_nx_enabled), Depends(require_nx_write), Depends(require_memory_write)])
def external_scan(body: NxScanRequest, request: Request):
    """Scan and index paths (incremental or full rebuild). Paths validated against allowed roots."""
    validated_paths = []
    if body.paths:
        for p in body.paths:
            validated_paths.append(str(validate_nx_path(p)))
    
    client = NxSearchClient()
    result = client.scan(paths=validated_paths if validated_paths else None, rebuild=body.rebuild)
    if "error" in result:
        raise HTTPException(status_code=502, detail=result["error"])
    
    _audit_log("nx_scan", request, {"paths": validated_paths, "rebuild": body.rebuild})
    return result


@app.post("/api/jarvis/memory/external/reindex", dependencies=[Depends(require_nx_enabled), Depends(require_nx_write), Depends(require_memory_write)])
def external_reindex(path: str = Query(..., min_length=1, max_length=1000), request: Request = None):
    """Incremental reindex of a single path. Path validated against allowed roots."""
    validated_path = validate_nx_path(path)
    
    client = NxSearchClient()
    result = client.reindex(str(validated_path))
    if "error" in result:
        raise HTTPException(status_code=502, detail=result["error"])
    
    _audit_log("nx_reindex", request, {"path": str(validated_path)})
    return result


@app.post("/api/jarvis/memory/external/prune", dependencies=[Depends(require_nx_enabled), Depends(require_nx_write), Depends(require_memory_write)])
def external_prune(paths: list[str] = Query(..., min_length=1, max_length=10), request: Request = None):
    """Prune missing files from index. Paths validated against allowed roots."""
    validated_paths = [str(validate_nx_path(p)) for p in paths]
    
    client = NxSearchClient()
    result = client.prune(validated_paths)
    if "error" in result:
        raise HTTPException(status_code=502, detail=result["error"])
    
    _audit_log("nx_prune", request, {"paths": validated_paths})
    return result


@app.post("/api/jarvis/memory/external/watch", dependencies=[Depends(require_nx_enabled), Depends(require_nx_write), Depends(require_memory_write)])
def external_watch(body: NxWatchRequest, request: Request):
    """Start file watcher for incremental index maintenance. One instance max, capped by feature flag."""
    # Check if already running
    existing = request.app.state.nx_background.get("watch")
    if existing and existing.poll() is None:
        raise HTTPException(status_code=409, detail="Watch already running (stop it first or wait for shutdown)")
    
    validated_paths = [str(validate_nx_path(p)) for p in body.paths]
    
    client = NxSearchClient()
    proc = client.watch(validated_paths, debounce_ms=body.debounce_ms, no_reconcile=body.no_reconcile)
    
    request.app.state.nx_background["watch"] = proc
    
    _audit_log("nx_watch_start", request, {"paths": validated_paths, "debounce_ms": body.debounce_ms, "pid": proc.pid})
    return {"status": "started", "pid": proc.pid, "paths": validated_paths, "debounce_ms": body.debounce_ms}


@app.post("/api/jarvis/memory/external/watch/stop", dependencies=[Depends(require_nx_enabled), Depends(require_nx_write), Depends(require_memory_write)])
def external_watch_stop(request: Request):
    """Stop the file watcher."""
    existing = request.app.state.nx_background.get("watch")
    if not existing or existing.poll() is not None:
        return {"status": "not_running"}
    
    existing.terminate()
    try:
        existing.wait(timeout=5)
    except subprocess.TimeoutExpired:
        existing.kill()
        existing.wait()
    
    request.app.state.nx_background["watch"] = None
    
    _audit_log("nx_watch_stop", request, {})
    return {"status": "stopped"}


@app.post("/api/jarvis/memory/external/serve", dependencies=[Depends(require_nx_enabled), Depends(require_nx_write), Depends(require_memory_write)])
def external_serve(port: int = Query(default=7788, ge=1024, le=65535), request: Request = None):
    """Start web UI server on specified port. One instance max, capped by feature flag."""
    existing = request.app.state.nx_background.get("serve")
    if existing and existing.poll() is None:
        raise HTTPException(status_code=409, detail="Serve already running on another port (stop it first)")
    
    client = NxSearchClient()
    proc = client.serve(port)
    
    request.app.state.nx_background["serve"] = proc
    
    _audit_log("nx_serve_start", request, {"port": port, "pid": proc.pid})
    return {"status": "started", "pid": proc.pid, "port": port, "url": f"http://127.0.0.1:{port}"}


@app.post("/api/jarvis/memory/external/serve/stop", dependencies=[Depends(require_nx_enabled), Depends(require_nx_write), Depends(require_memory_write)])
def external_serve_stop(request: Request):
    """Stop the web UI server."""
    existing = request.app.state.nx_background.get("serve")
    if not existing or existing.poll() is not None:
        return {"status": "not_running"}
    
    existing.terminate()
    try:
        existing.wait(timeout=5)
    except subprocess.TimeoutExpired:
        existing.kill()
        existing.wait()
    
    request.app.state.nx_background["serve"] = None
    
    _audit_log("nx_serve_stop", request, {})
    return {"status": "stopped"}


def _audit_log(action: str, request: Request | None, details: dict):
    """Log nx-search operations for audit trail."""
    import logging
    logger = logging.getLogger("jarvis.nx_audit")
    client_ip = request.client.host if request and request.client else "unknown"
    auth = "oauth" if oauth_enabled() else "apikey"
    logger.info(f"nx_audit action={action} ip={client_ip} auth={auth} details={details}")


# --- EMR / STM (LTM stays the store; STM is an activated view) ---


@app.post("/api/jarvis/tools/search", dependencies=[Depends(require_emr_recall_api_key)])
def tool_search(body: EmrSearchRequest):
    """OpenAI company-knowledge search — read-only EMR recall → citation results."""
    store = get_store()
    try:
        return emr_search(store, body)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/jarvis/tools/fetch", dependencies=[Depends(require_emr_recall_api_key)])
def tool_fetch(body: EmrFetchRequest):
    """OpenAI company-knowledge fetch — full LTM record by id (read-only)."""
    store = get_store()
    try:
        return emr_fetch(store, body)
    except ValueError as exc:
        if "not found" in str(exc).lower():
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/jarvis/tools/emr_search", dependencies=[Depends(require_emr_recall_api_key)])
def tool_emr_search(body: EmrSearchRequest):
    """Alias of ``search`` for hosts that namespace EMR tools."""
    return tool_search(body)


@app.post("/api/jarvis/tools/emr_fetch", dependencies=[Depends(require_emr_recall_api_key)])
def tool_emr_fetch(body: EmrFetchRequest):
    """Alias of ``fetch`` for hosts that namespace EMR tools."""
    return tool_fetch(body)


@app.post("/api/jarvis/tools/emr_recall", dependencies=[Depends(require_emr_recall_api_key)])
def tool_emr_recall(body: EmrRecallRequest):
    """Read-only EMR Recall Protocol — governed bundle for agent tool calling."""
    store = get_store()
    try:
        result = emr_recall(store, body)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return result.model_dump()


@app.post(
    "/api/jarvis/tools/emr_remember",
    dependencies=[Depends(require_emr_recall_api_key), Depends(require_mcp_write_scope)],
)
def tool_emr_remember(body: EmrRememberRequest):
    """Governed create via EMR — draft-only; gated by JARVIS_MCP_WRITE_ENABLED."""
    store = get_store()
    return emr_remember(store, body).model_dump()


@app.post(
    "/api/jarvis/tools/emr_upsert",
    dependencies=[Depends(require_emr_recall_api_key), Depends(require_mcp_write_scope)],
)
def tool_emr_upsert(body: EmrUpsertRequest):
    """Governed supersede via EMR — new draft + archive prior; gated by JARVIS_MCP_WRITE_ENABLED."""
    store = get_store()
    return emr_upsert(store, body).model_dump()


@app.get("/api/jarvis/tools")
def list_tools():
    """Tool catalog (OpenAI-compatible function schemas)."""
    return tool_catalog()


@app.get("/api/jarvis/memory/emr/status")
def get_emr_status():
    return emr_status()


@app.post("/api/jarvis/memory/emr/excite", dependencies=[Depends(require_memory_write)])
def emr_excite(body: ExciteRequest):
    """Governed recall: score LTM → bundle → promote/evict STM under budget."""
    store = get_store()
    candidates = store.list_memories(
        truth_scope=body.truth_scope,
        # Filters must see the whole local ledger cohort before candidate_limit
        # is applied; otherwise older exact metadata matches can be hidden by
        # the store's recency ordering and graph traversal becomes incomplete.
        limit=9999,
    )
    try:
        result = excite(candidates, body)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return result.model_dump()


@app.post("/api/jarvis/memory/emr/reinforce", dependencies=[Depends(require_memory_write)])
def emr_reinforce(body: ReinforceRequest):
    """Bounded reinforcement of retrievability (Q+, D−).

    Constitutional guard: mutates only the EMR dynamics overlay. LTM fields
    carrying truth/authority (status, confidence, content, content_sha256)
    are never written by this endpoint.
    """
    store = get_store()
    known: set[str] = set()
    for mid in body.memory_ids:
        if store.get_memory(mid) is not None:
            known.add(mid)
    reinforced, unknown, replayed = reinforce_ids(
        known,
        body.memory_ids,
        outcome=body.outcome,
    )
    return {
        "reinforced": [r.model_dump() for r in reinforced],
        "unknown_ids": unknown,
        "replayed_memory_ids": replayed,
        "ltm_mutations": 0,
        "rule": (
            "Reinforcement requires an explicit positive outcome signal and "
            "strengthens retrievability within separate and combined hard caps; "
            "truth/authority remain independently certified by the Continuity "
            "Ledger and are never mutated."
        ),
    }


@app.post("/api/jarvis/memory/emr/correct", dependencies=[Depends(require_memory_write)])
def emr_correct(body: CorrectRequest):
    """Operator correction: immediately reset reinforcement overlay.

    Clears salience and decay damping on corrected memories so wrong recall
    cannot slowly outcompete a replacement. Never mutates LTM truth fields.
    """
    store = get_store()
    known: set[str] = set()
    for mid in body.memory_ids:
        if store.get_memory(mid) is not None:
            known.add(mid)
    corrected, unknown, replayed = correct_memory_ids(
        known,
        body.memory_ids,
        correction=body.correction,
    )
    return {
        "corrected": [r.model_dump() for r in corrected],
        "unknown_ids": unknown,
        "replayed_correction_ids": replayed,
        "ltm_mutations": 0,
        "rule": (
            "Operator correction resets reinforcement overlay immediately; "
            "LTM truth/authority remain independently certified."
        ),
    }


@app.get("/api/jarvis/memory/active", dependencies=[Depends(require_memory_write)])
def active_stm(
    query: str = Query(..., min_length=1, max_length=2000),
    session_key: str = Query(default="default"),
    token_budget: int = Query(default=512, ge=32, le=8000),
    theta_promote: float = Query(default=0.12, ge=0.0, le=1.0),
    theta_evict: float = Query(default=0.04, ge=0.0, le=1.0),
    truth_scope: str = Query(default="live"),
    candidate_limit: int = Query(default=200, ge=1, le=2000),
    trajectory: list[str] | None = Query(default=None),
):
    """Contract surface: EMR excite → budgeted STM view in one GET."""
    store = get_store()
    body = ExciteRequest(
        query=query,
        trajectory=trajectory or [],
        token_budget=token_budget,
        theta_promote=theta_promote,
        theta_evict=theta_evict,
        truth_scope=truth_scope,
        candidate_limit=candidate_limit,
        session_key=session_key,
    )
    candidates = store.list_memories(
        truth_scope=body.truth_scope,
        limit=9999,
    )
    result = excite(candidates, body)
    return result.model_dump()


@app.get("/api/jarvis/memory/stm")
def read_stm(session_key: str = Query(default="default")):
    entries = get_stm(session_key)
    return {
        "session_key": session_key,
        "stm": [e.model_dump() for e in entries],
        "budget_used": sum(e.token_cost for e in entries),
        "count": len(entries),
    }


@app.get("/api/jarvis/memory/stm/context")
def read_stm_context(session_key: str = Query(default="default")):
    """LLM-ready STM block (summaries + LTM provenance ids)."""
    return {
        "session_key": session_key,
        "context": stm_context_block(session_key),
    }


@app.post("/api/jarvis/memory/stm/expand", dependencies=[Depends(require_memory_write)])
def stm_expand(body: ExpandRequest):
    """Raise resolution summary→detail→evidence; payload still points at LTM."""
    store = get_store()
    rec = store.get_memory(body.memory_id)
    if not rec:
        raise HTTPException(status_code=404, detail="LTM memory not found")
    if body.memory_id not in {e.memory_id for e in get_stm(body.session_key)}:
        raise HTTPException(
            status_code=400,
            detail="Memory not in STM; POST /emr/excite first to promote",
        )
    updated = expand_stm_entry({body.memory_id: rec}, body)
    if updated is None:
        raise HTTPException(status_code=404, detail="STM entry not found")
    return {"stm_entry": updated.model_dump()}


@app.delete("/api/jarvis/memory/stm", dependencies=[Depends(require_memory_write)])
def stm_clear(session_key: str | None = Query(default=None)):
    clear_stm(session_key)
    return {"status": "cleared", "session_key": session_key}


@app.post("/api/jarvis/memory/pipeline", dependencies=[Depends(require_memory_write)])
def memory_pipeline_endpoint(body: ConsolidationRequest):
    """EMR -> STM -> LTM governed pipeline: excite workers -> draft-consolidate to ledger.

    Reads LTM, builds the STM working set, then materialises ONE governed DRAFT
    summary record via the emr_write gateway (conflict-checked, draft-only,
    provenance-preserving). The pipeline never auto-verifies.
    """
    store = get_store()
    try:
        trace = memory_pipeline(store, body)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return trace.model_dump()


@app.get("/api/jarvis/memory/history/verify")
def verify_history():
    """Recompute the record-history hash chain (PostgreSQL row store only)."""
    try:
        problems = get_store().verify_history()
    except NotImplementedError as exc:
        raise HTTPException(status_code=501, detail=str(exc)) from exc
    return {"ok": not problems, "problems": problems}


@app.get("/api/jarvis/memory/{memory_id}/history")
def memory_history(memory_id: str, limit: int = Query(default=200, ge=1, le=1000)):
    """Append-only change log for one record, including after deletion (row store only)."""
    try:
        return {"memory_id": memory_id, "history": get_store().history(memory_id, limit)}
    except NotImplementedError as exc:
        raise HTTPException(status_code=501, detail=str(exc)) from exc


@app.get("/api/jarvis/memory/{memory_id}/resolve")
def resolve_memory(
    memory_id: str,
    resolution: str = Query(default="summary", pattern="^(summary|detail|evidence)$"),
):
    """Expand one LTM particle to summary|detail|evidence with provenance."""
    store = get_store()
    rec = store.get_memory(memory_id)
    if not rec:
        raise HTTPException(status_code=404, detail="LTM memory not found")
    return resolve_record(rec, resolution)  # type: ignore[arg-type]


@app.get("/api/jarvis/memory/{memory_id}")
def get_memory(memory_id: str):
    store = get_store()
    rec = store.get_memory(memory_id)
    if not rec:
        raise HTTPException(status_code=404, detail="Memory not found")
    from app.continuity import to_selection

    sel = to_selection(rec)
    return {"memory": rec.model_dump(), "selection": sel.model_dump()}


@app.patch("/api/jarvis/memory/{memory_id}", dependencies=[Depends(require_memory_write)])
def update_memory(memory_id: str, body: MemoryUpdate):
    store = get_store()
    try:
        rec = store.update_memory(memory_id, body)
    except ValueError as exc:  # StoreVersionConflict is not a ValueError: it reaches the 409 handler
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not rec:
        raise HTTPException(status_code=404, detail="Memory not found")
    return _with_clause_v_warnings({"memory": rec.model_dump()})


@app.delete("/api/jarvis/memory/{memory_id}", dependencies=[Depends(require_memory_write)])
def delete_memory(memory_id: str):
    store = get_store()
    ok = store.delete_memory(memory_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Memory not found")
    return {"status": "deleted", "id": memory_id}


# --- Evidence Objects (content-addressed, immutable, hashes only; created with the operator key only) ---


def _evidence_id(evidence_id: str) -> str:
    if not evidence_objects.ID_RE.match(evidence_id):
        raise EvidenceError("evidence_id_invalid", "an evidence id looks like eo:sha256:<64 lowercase hex characters>")
    return evidence_id


# --- Continuity Blocks (operator key only; PostgreSQL row store only) ---

class SealBlocksBody(BaseModel):
    """Seal rules.  Defaults: seal at 500 unsealed entries or when the oldest is an hour old."""

    force: bool = False
    min_entries: int = Field(default=500, ge=1, le=1_000_000)
    max_age_seconds: int = Field(default=3600, ge=0, le=31_536_000)
    max_entries: int = Field(default=10_000, ge=1, le=100_000)


def _blocks_call(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except NotImplementedError as exc:
        raise HTTPException(status_code=501, detail=str(exc)) from exc


@app.post("/api/jarvis/blocks/seal", dependencies=[Depends(require_operator_write)])
def seal_blocks(body: SealBlocksBody = SealBlocksBody()):
    """Seal the unsealed history into blocks (what the seal timer calls).  Sealing is idempotent and changes no record."""
    return _blocks_call(get_store().seal_blocks, force=body.force, min_entries=body.min_entries,
                        max_age_seconds=body.max_age_seconds, max_entries=body.max_entries)


@app.get("/api/jarvis/blocks", dependencies=[Depends(require_operator_read)])
def list_blocks(after_height: int = Query(default=0, ge=0), limit: int = Query(default=100, ge=1, le=1000)):
    blocks = _blocks_call(get_store().list_blocks, after_height, limit)
    return {"blocks": blocks, "count": len(blocks)}


@app.get("/api/jarvis/blocks/head", dependencies=[Depends(require_operator_read)])
def blocks_head():
    """The newest block and how much history is not sealed yet."""
    return _blocks_call(get_store().block_head)


@app.get("/api/jarvis/blocks/verify", dependencies=[Depends(require_operator_read)])
def verify_blocks():
    """Recompute every block (database verifier + independent recomputation + cited evidence)."""
    problems = _blocks_call(get_store().verify_blocks)
    return {"ok": not problems, "problems": problems}


@app.get("/api/jarvis/blocks/{height}", dependencies=[Depends(require_operator_read)])
def get_block(height: int = PathParam(ge=1)):
    block = _blocks_call(get_store().get_block, height)
    if block is None:
        raise HTTPException(status_code=404, detail="no such block")
    return {"block": block}


# --- Replay Contracts (operator key only; PostgreSQL row store only) ---

def _replay_call(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except NotImplementedError as exc:
        raise HTTPException(status_code=501, detail=str(exc)) from exc
    except replay_contracts.ReplayError as exc:
        raise HTTPException(status_code=exc.status, detail=f"{exc.code}: {exc.message}") from exc


@app.get("/api/jarvis/replay/contracts", dependencies=[Depends(require_operator_read)])
def replay_contracts_registry():
    """The registered Replay Contracts and their status (only RC.Ledger.v1 is implemented; the rest are declared)."""
    return {"contracts": [spec.model_dump() for spec in replay_contracts.REGISTRY.values()]}


@app.get("/api/jarvis/replay/state", dependencies=[Depends(require_operator_read)])
def replay_state(
    at_seq: int | None = Query(default=None, ge=0),
    at_block: int | None = Query(default=None, ge=1),
    after_id: str | None = Query(default=None, max_length=128),
    limit: int = Query(default=200, ge=1, le=replay_contracts.MAX_PAGE),
):
    """RC.Ledger.v1: the ledger's records as of a history seq (or the end of a sealed block), with the state root."""
    return _replay_call(get_store().replay_state, at_seq=at_seq, at_block=at_block, after_id=after_id, limit=limit).model_dump()


@app.get("/api/jarvis/replay/events", dependencies=[Depends(require_operator_read)])
def replay_events(
    from_seq: int = Query(default=1, ge=1),
    to_seq: int | None = Query(default=None, ge=1),
    limit: int = Query(default=200, ge=1, le=replay_contracts.MAX_PAGE),
):
    """RC.Ledger.v1: the ordered history entries (what happened, in what order, under which recorded actor and evidence)."""
    return _replay_call(get_store().replay_events, from_seq=from_seq, to_seq=to_seq, limit=limit).model_dump()


@app.post("/api/jarvis/replay/receipts", dependencies=[Depends(require_operator_write)])
def create_replay_receipt(body: replay_contracts.ReceiptRequest = replay_contracts.ReceiptRequest()):
    """Replay at a SEALED point and store the result as a receipt (a CES.Local.ReplayReceipt.v1 evidence object).

    Idempotent: the same replay gives the same receipt.  A point no sealed block covers is refused."""
    obj, created, state = _replay_call(get_store().create_replay_receipt, at_seq=body.at_seq, at_block=body.at_block)
    return {"receipt": obj.model_dump(), "created": created,
            "state": state.model_dump(exclude={"records", "next_after_id"})}


@app.get("/api/jarvis/replay/receipts", dependencies=[Depends(require_operator_read)])
def list_replay_receipts(limit: int = Query(default=100, ge=1, le=1000)):
    receipts = _replay_call(get_store().list_replay_receipts, limit)
    return {"receipts": [r.model_dump() for r in receipts], "count": len(receipts)}


@app.get("/api/jarvis/replay/receipts/{receipt_id}", dependencies=[Depends(require_operator_read)])
def get_replay_receipt(receipt_id: str):
    return {"receipt": _replay_call(get_store().get_replay_receipt, receipt_id).model_dump()}


@app.get("/api/jarvis/replay/receipts/{receipt_id}/verify", dependencies=[Depends(require_operator_read)])
def verify_replay_receipt(receipt_id: str):
    """Re-derive a receipt: intact object, same state root, same counts, same sealed block (a receipt is only a claim until this passes)."""
    return _replay_call(get_store().verify_replay_receipt, receipt_id).model_dump()


# --- Signatures: attestations and trust statements (operator key only; PostgreSQL row store only; verification side, nothing here signs) ---

class AttestationBody(BaseModel):
    """An attestation made elsewhere (by the signer, on the host).  The service verifies it before it stores it."""

    kind: str = Field(min_length=1, max_length=20)
    subject: str = Field(min_length=1, max_length=300)
    subject_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    signer_seq: int = Field(ge=1)
    prev_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    key_id: str = Field(pattern=r"^SHA256:[A-Za-z0-9+/]{43}$")
    signed_at: str = Field(pattern=r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$")
    signature: str = Field(min_length=100, max_length=4000)


class TrustStatementBody(BaseModel):
    """A trust statement signed by a root key (authorize or revoke a signing key, add a root, cosign a checkpoint)."""

    kind: str = Field(min_length=1, max_length=20)
    key_id: str = Field(pattern=r"^SHA256:[A-Za-z0-9+/]{43}$")
    pubkey: str | None = Field(default=None, max_length=600)
    arg: int | None = Field(default=None, ge=0)
    subject_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    stmt_seq: int = Field(ge=1)
    prev_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    signed_by: str = Field(pattern=r"^SHA256:[A-Za-z0-9+/]{43}$")
    signature: str = Field(min_length=100, max_length=4000)


def _sig_call(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except NotImplementedError as exc:
        raise HTTPException(status_code=501, detail=str(exc)) from exc


@app.get("/api/jarvis/attestations", dependencies=[Depends(require_operator_read)])
def list_attestations(after_seq: int = Query(default=0, ge=0), limit: int = Query(default=100, ge=1, le=1000)):
    rows = _sig_call(get_store().list_attestations, after_seq, limit)
    return {"attestations": rows, "count": len(rows)}


@app.get("/api/jarvis/attestations/head", dependencies=[Depends(require_operator_read)])
def attestation_head():
    """Where the signing log ends: the next signer_seq and the prev_hash the next attestation must name."""
    return _sig_call(get_store().attestation_head)


@app.get("/api/jarvis/attestations/pending", dependencies=[Depends(require_operator_read)])
def pending_attestations():
    """Sealed blocks and receipts that have no attestation yet (what a signer would sign next)."""
    return _sig_call(get_store().pending_attestations)


@app.get("/api/jarvis/attestations/verify", dependencies=[Depends(require_operator_read)])
def verify_signatures():
    """Check every attestation and trust statement against the pinned roots (never reports success without a trust root)."""
    result = _sig_call(get_store().verify_signatures)
    return {"ok": not result["problems"], **result}


@app.post("/api/jarvis/attestations", dependencies=[Depends(require_operator_write)])
def store_attestation(body: AttestationBody):
    """Store an attestation after verifying its position in the log, its signature, the key's authorization and its subject."""
    return _sig_call(get_store().store_attestation, body.model_dump())


@app.get("/api/jarvis/trust", dependencies=[Depends(require_operator_read)])
def trust_state():
    """The pinned roots, the roots added since, the authorized signing keys (with revocation cutoffs) and the cosigned checkpoints."""
    return _sig_call(get_store().trust_state)


@app.get("/api/jarvis/trust/statements", dependencies=[Depends(require_operator_read)])
def list_trust_statements(after_seq: int = Query(default=0, ge=0), limit: int = Query(default=100, ge=1, le=1000)):
    rows = _sig_call(get_store().list_trust_statements, after_seq, limit)
    return {"statements": rows, "count": len(rows)}


@app.post("/api/jarvis/trust/statements", dependencies=[Depends(require_operator_write)])
def store_trust_statement(body: TrustStatementBody):
    """Store a root-signed statement after verifying it against the pinned roots and the statement log so far."""
    return _sig_call(get_store().store_trust_statement, body.model_dump())


@app.post("/api/jarvis/evidence", dependencies=[Depends(require_operator_write)])
def create_evidence(body: EvidenceObjectCreate):
    """Store an evidence object. Idempotent: the same content is the same id (``created`` says which happened)."""
    if body.schema_id == evidence_objects.CES_REPLAY_RECEIPT:
        raise EvidenceError("evidence_schema_reserved", f"{body.schema_id} objects are created only by POST /api/jarvis/replay/receipts, which derives them from a replay")
    try:
        obj, created = get_store().put_evidence_object(body)
    except NotImplementedError as exc:
        raise HTTPException(status_code=501, detail=str(exc)) from exc
    return {"evidence": obj.model_dump(), "created": created}


@app.get("/api/jarvis/evidence/{evidence_id}")
def get_evidence(evidence_id: str):
    try:
        obj = get_store().get_evidence_object(_evidence_id(evidence_id))
    except NotImplementedError as exc:
        raise HTTPException(status_code=501, detail=str(exc)) from exc
    if obj is None:
        raise HTTPException(status_code=404, detail="Evidence object not found")
    return {"evidence": obj.model_dump()}


@app.get("/api/jarvis/evidence/{evidence_id}/verify")
def verify_evidence(evidence_id: str):
    """Recompute the hash and re-check the schema of a stored object. A pointer's external content is NOT fetched."""
    try:
        obj = get_store().get_evidence_object(_evidence_id(evidence_id))
    except NotImplementedError as exc:
        raise HTTPException(status_code=501, detail=str(exc)) from exc
    if obj is None:
        raise HTTPException(status_code=404, detail="Evidence object not found")
    problems = evidence_objects.verify_stored(obj)
    return {
        "id": obj.id,
        "ok": not problems,
        "problems": problems,
        "schema_id": obj.schema_id,
        "pointer": {
            "present": obj.pointer is not None,
            "content_hash_checked": False,
            "note": "the service does not fetch pointed-at content; pointer.sha256 is recorded, not verified",
        },
    }


# --- AMUL RAG (adaptive retrieval + evidence gate + replay) ---


class RagDocumentInput(BaseModel):
    id: str | None = Field(default=None, min_length=1, max_length=256)
    title: str = Field(default="", max_length=512)
    body: str = Field(..., min_length=1, max_length=200_000)
    source: str = Field(default="unknown", min_length=1, max_length=128)
    tags: list[str] = Field(default_factory=list, max_length=64)
    authority_class: AuthorityClass = "untrusted"
    status: DocumentStatus = "draft"
    subject: str | None = Field(default=None, max_length=256)
    supersedes: str | None = Field(default=None, max_length=256)
    conflict_ids: list[str] = Field(default_factory=list, max_length=64)


class RagDocumentsBody(BaseModel):
    documents: list[RagDocumentInput] = Field(..., min_length=1, max_length=256)


class RagQueryBody(BaseModel):
    query: str = Field(..., min_length=1, max_length=4000)


class RagMaintenanceBody(BaseModel):
    apply: bool = False


def require_rag_api_key(x_jarvis_rag_key: str | None = Header(default=None)) -> None:
    """Protect RAG content, queries, and replay data with a local secret file."""
    key_path = Path(amul_rag.RAG_API_KEY_FILE) if amul_rag.RAG_API_KEY_FILE else None
    try:
        expected = key_path.read_text(encoding="utf-8").strip() if key_path else ""
    except OSError as exc:
        raise HTTPException(status_code=503, detail="RAG access key is unavailable") from exc
    if not expected:
        raise HTTPException(status_code=503, detail="RAG access control is not configured")
    if not x_jarvis_rag_key or not secrets.compare_digest(x_jarvis_rag_key, expected):
        raise HTTPException(status_code=401, detail="Invalid RAG access key")


@app.post("/api/jarvis/rag/documents", dependencies=[Depends(require_rag_api_key)])
def rag_documents(body: RagDocumentsBody):
    index = get_index()
    documents = []
    for item in body.documents:
        raw = item.model_dump(exclude_none=True)
        requested_id = str(raw.get("id") or "")
        existing = index.docs.get(requested_id) if requested_id else None
        document = normalize_document(
            raw,
            existing_version=existing.version if existing else 0,
        )
        try:
            index.add(document, persist=True)
        except OSError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        documents.append(document.model_dump())
    return {"documents": documents, "count": len(documents)}


@app.post("/api/jarvis/rag/query", dependencies=[Depends(require_rag_api_key)])
def rag_query(body: RagQueryBody):
    try:
        record = answer_query(
            body.query,
            get_index(),
            extra_docs=ledger_docs(get_store()),
        )
    except OSError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return record.model_dump()


@app.get("/api/jarvis/rag/log", dependencies=[Depends(require_rag_api_key)])
def rag_log(limit: int = Query(default=50, ge=1, le=1000)):
    records: list[dict] = []
    path = Path(amul_rag.RAG_LOG_PATH)
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines()[-limit:]:
            try:
                records.append(json.loads(line))
            except (json.JSONDecodeError, TypeError):
                continue
    return {"records": records, "count": len(records)}


@app.get("/api/jarvis/rag/status", dependencies=[Depends(require_rag_api_key)])
def get_rag_status():
    return amul_rag.rag_status()


@app.post("/api/jarvis/rag/maintenance", dependencies=[Depends(require_rag_api_key)])
def rag_maintenance(body: RagMaintenanceBody):
    return maintain_replay_log(apply=body.apply)


# --- AMUL LLM (governed generation; ledger key applies via ApiKeyMiddleware) ---


@app.get("/api/jarvis/llm/status")
def get_llm_status():
    return amul_llm.llm_status()


class LlmGenerateBody(amul_llm.PromptContract):
    # Recall from the ledger through EMR (abstention and conflicts enforced)
    # and put what it returns ahead of the caller's context.
    recall: bool = True
    recall_query: str | None = Field(default=None, max_length=2000)  # default: `user`
    recall_intent: str = Field(default="chat", min_length=1, max_length=128)
    subjects: list[str] = Field(default_factory=list, max_length=32)
    max_memories: int = Field(default=6, ge=1, le=32)
    truth_scope: str = Field(default="live", max_length=32)


_CONTEXT_LIMIT = 32000  # PromptContract.context max_length


def _recalled_context(result) -> str:
    lines = []
    if result.bundle:
        lines.append(
            "Memories recalled from the Continuity Ledger. These are recorded claims "
            "with provenance, not verified truth unless status=verified. Cite the "
            "[memory id] of any memory you rely on."
        )
    for item in result.bundle:
        subject = f" subject={item.subject}" if item.subject else ""
        lines.append(
            f"- [{item.memory_id}] type={item.type} status={item.status} "
            f"confidence={item.confidence}{subject}: {item.content}"
        )
    if result.conflicts:
        subjects = ", ".join(sorted({c.subject for c in result.conflicts}))
        lines.append(
            f"Unresolved conflicts are recorded for: {subjects}. Do not pick a side; "
            "say that the recorded claims conflict."
        )
    return "\n".join(lines)


@app.post("/api/jarvis/llm/generate")
def llm_generate(body: LlmGenerateBody):
    """One governed generation: recall -> intent -> mode -> backend -> policy check.

    Always returns the replay record (R-B), with `recall` saying which memories
    were put into context. `metadata.model_version` says which backend
    answered; `echo-stub-v0` means the backend was unreachable or refused,
    not that a model replied.
    """
    contract = amul_llm.PromptContract(
        system=body.system, user=body.user, context=body.context, mode=body.mode
    )
    recall = None
    if body.recall:
        query = (body.recall_query or body.user)[:2000]
        try:
            result = emr_recall(
                get_store(),
                EmrRecallRequest(
                    intent=body.recall_intent,
                    query=query,
                    subjects=body.subjects,
                    max_memories=body.max_memories,
                    truth_scope=body.truth_scope,
                    session_key="llm-generate",
                    include_provenance=False,
                ),
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        recall = {
            "query": query,
            "memory_ids": [item.memory_id for item in result.bundle],
            "abstained": result.abstained,
            "abstention_reason": result.abstention_reason,
            "conflict_subjects": sorted({c.subject for c in result.conflicts}),
        }
        # Conflicting memories are held out of the bundle by the conflict
        # membrane; the model still has to hear that the claims conflict.
        if result.bundle or result.conflicts:
            recalled = _recalled_context(result)
            merged = f"{recalled}\n\n{body.context}" if body.context else recalled
            contract = contract.model_copy(update={"context": merged[:_CONTEXT_LIMIT]})
    try:
        return amul_llm.generate(contract, recall=recall)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


# --- AMUL Architect (LTM substrate: append-only field, lineage, drift) ---


class AnchorBody(BaseModel):
    memory_id: str | None = None
    anchor_all: bool = False
    actor: str = Field(default="amul", max_length=64)


@app.post("/api/jarvis/memory/amul/anchor", dependencies=[Depends(require_memory_write)])
def amul_anchor(body: AnchorBody):
    """Anchor ledger truth into immutable AMUL artifacts (idempotent)."""
    store = get_store()
    field = get_field()
    if body.anchor_all:
        records = store.list_memories(limit=9999)
        reports = [anchor_memory(r, field, body.actor) for r in records]
        return {
            "anchored": len(reports),
            "created_artifacts": sum(len(r.created) for r in reports),
            "unchanged_resolutions": sum(len(r.unchanged) for r in reports),
            "field_count": field.count,
        }
    if not body.memory_id:
        raise HTTPException(status_code=400, detail="memory_id or anchor_all required")
    rec = store.get_memory(body.memory_id)
    if not rec:
        raise HTTPException(status_code=404, detail="LTM memory not found")
    report = anchor_memory(rec, field, body.actor)
    return report.model_dump()


@app.get("/api/jarvis/memory/amul/artifacts/{artifact_id}")
def amul_artifact(artifact_id: str):
    art = get_field().get(artifact_id)
    if not art:
        raise HTTPException(status_code=404, detail="Artifact not found")
    return {"artifact": art.model_dump()}


@app.get("/api/jarvis/memory/amul/lineage/{memory_id}")
def amul_lineage(memory_id: str):
    lineage = get_field().lineage(memory_id)
    if lineage["depth"] == 0:
        raise HTTPException(status_code=404, detail="No artifacts anchored for this memory")
    return lineage


@app.get("/api/jarvis/memory/amul/field/status")
def amul_field_status():
    field = get_field()
    by_res: dict[str, int] = {}
    for a in field.all():
        by_res[a.resolution] = by_res.get(a.resolution, 0) + 1
    return {
        "schema": "amul-artifact-v1",
        "path": field.path,
        "artifact_count": field.count,
        "by_resolution": by_res,
        "append_only": True,
        "role": "AMUL LTM substrate beneath the Continuity Ledger (ledger = truth SoT)",
        "maturity": {
            "persistence": "enforced",
            "resolution_artifacts": "enforced",
            "lineage_provenance": "enforced",
            "verify_drift": "enforced",
            "scale_gc_index": "declared",
        },
    }


@app.post("/api/jarvis/memory/amul/field/verify")
def amul_field_verify():
    """Rehash the whole field + detect ledger drift since last anchors."""
    store = get_store()
    report = verify_field(get_field(), store.list_memories(limit=9999))
    return report.model_dump()


_log = logging.getLogger("jarvis.store")


def _invoke_emr_tool(name: str, arguments: dict) -> dict:
    """In-process EMR tools for MCP Streamable HTTP (same path as REST tools).

    Store failures are logged in full but reported to the MCP client generically,
    matching the HTTP 503 handler (the detail can name record ids).
    """
    try:
        return _invoke_emr_tool_unguarded(name, arguments)
    except StoreUnavailableError as exc:
        _log.error("MCP tool %s failed: %s", name, exc)
        raise ToolRefusal(LEDGER_UNAVAILABLE, "Ledger store unavailable") from None
    except StoreVersionConflict as exc:
        raise ToolRefusal(VERSION_CONFLICT, str(exc)) from None
    except HTTPException as exc:
        if exc.status_code in (401, 403):
            raise ToolRefusal(DENIED, str(exc.detail)) from None
        raise


def _invoke_emr_tool_unguarded(name: str, arguments: dict) -> dict:
    store = get_store()
    if name in ("search", "emr_search"):
        body = EmrSearchRequest.model_validate(arguments)
        return emr_search(store, body)
    if name in ("fetch", "emr_fetch"):
        body = EmrFetchRequest.model_validate(arguments)
        return emr_fetch(store, body)
    if name == "emr_recall":
        body = EmrRecallRequest.model_validate(arguments)
        return emr_recall(store, body).model_dump()
    if name in ("emr_remember", "emr_upsert"):
        require_mcp_write_scope()
    if name == "emr_remember":
        body = EmrRememberRequest.model_validate(arguments)
        return emr_remember(store, body).model_dump()
    if name == "emr_upsert":
        body = EmrUpsertRequest.model_validate(arguments)
        return emr_upsert(store, body).model_dump()
    raise RuntimeError(f"unknown tool: {name}")


app.include_router(create_mcp_router(_invoke_emr_tool), prefix="/mcp", tags=["mcp"])

# Relationship graph endpoints (full graph <=8k, sparse index above).
@app.post("/api/jarvis/memory/graph/bfs")
def graph_bfs(body: BfsBody):
    relations = set(body.relations) if body.relations else None
    results = bfs_search(body.start_id, body.depth, body.max_nodes, relations, body.min_confidence, body.max_age_days)
    return {"start_id": body.start_id, "depth": body.depth, "results": results, "count": len(results)}

@app.post("/api/jarvis/memory/graph/shortest-path")
def graph_shortest_path(body: ShortestPathBody):
    path = shortest_path(body.source, body.target, body.max_nodes, body.min_confidence, body.max_age_days)
    if path is None: raise HTTPException(status_code=404, detail="No path found between the given memories")
    return {"source": body.source, "target": body.target, "path": path}

@app.post("/api/jarvis/memory/graph/related")
def graph_related(body: RelatedBody):
    results = find_related(body.memory_id, body.k, body.min_distance, body.max_distance, body.min_confidence, body.max_age_days)
    return {"memory_id": body.memory_id, "k": body.k, "results": results, "count": len(results)}

@app.get("/api/jarvis/memory/graph/stats")
def graph_stats(min_confidence: float = 0.0, max_age_days: float | None = None):
    return memory_graph_stats(min_confidence=min_confidence, max_age_days=max_age_days)

@app.post("/api/jarvis/memory/graph/components")
def graph_components(body: ComponentsBody):
    found = connected_components(body.min_size, body.min_confidence, body.max_age_days)
    return {
        "min_size": body.min_size,
        "components": found[: body.limit],
        "total_components": len(found),
        "truncated": len(found) > body.limit,
    }
