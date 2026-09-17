"""Auth gates.

Ledger routes: JARVIS_API_KEY required by default (ApiKeyMiddleware);
JARVIS_ALLOW_UNAUTHENTICATED=1 is the local-dev opt-out.
EMR tool / MCP routes: EMR_RECALL_API_KEY (optional, hosted recall).
Write gate: JARVIS_MEMORY_WRITE_ENABLED / JARVIS_MCP_WRITE_ENABLED.
"""

from __future__ import annotations

import os
import secrets

from fastapi import Header, HTTPException, Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse, Response

from app.public_security import is_public_deployment
from app.identity import current_principal, reset_principal, set_principal
from app.oauth import READ_SCOPE, WRITE_SCOPE, auth_mode, public_base_url, validate_access_token


def emr_recall_api_key() -> str | None:
    key = (os.getenv("EMR_RECALL_API_KEY") or "").strip()
    return key or None


def memory_write_enabled() -> bool:
    """Gate for REST ledger CRUD / EMR reinforce / board mutations."""
    default = "false" if is_public_deployment() else "true"
    return os.getenv("JARVIS_MEMORY_WRITE_ENABLED", default).lower() in (
        "1",
        "true",
        "yes",
    )


def mcp_write_enabled() -> bool:
    """Gate for MCP/tool write surface (emr_remember / emr_upsert).

    Defaults to **false** so public Render stays recall-only until explicitly enabled.
    Independent of ``JARVIS_MEMORY_WRITE_ENABLED`` (REST CRUD).
    """
    return os.getenv("JARVIS_MCP_WRITE_ENABLED", "false").lower() in (
        "1",
        "true",
        "yes",
    )


def require_mcp_write() -> None:
    if oauth_enabled():
        principal = current_principal()
        if principal is None or WRITE_SCOPE not in principal.scopes:
            raise HTTPException(status_code=403, detail="OAuth access token lacks required scope: memory.write")
    if not mcp_write_enabled():
        raise HTTPException(
            status_code=403,
            detail="MCP memory writes are disabled on this deployment (set JARVIS_MCP_WRITE_ENABLED=true)",
        )


def ledger_read_protected() -> bool:
    """When true, GET /api/jarvis/memory/* requires the operator API key."""
    if not emr_recall_api_key():
        return False
    return os.getenv("JARVIS_PROTECT_LEDGER_READ", "false").lower() in (
        "1",
        "true",
        "yes",
    )


def deployment_label() -> str:
    if os.getenv("RENDER"):
        return "render"
    if os.getenv("JARVIS_DEPLOYMENT"):
        return os.getenv("JARVIS_DEPLOYMENT", "").strip() or "custom"
    return "local"


def _extract_bearer(authorization: str | None) -> str | None:
    if not authorization:
        return None
    prefix = "bearer "
    if authorization.lower().startswith(prefix):
        return authorization[len(prefix) :].strip()
    return None


def verify_operator_api_key(
    authorization: str | None,
    x_emr_recall_key: str | None,
) -> None:
    expected = emr_recall_api_key()
    if not expected:
        raise HTTPException(
            status_code=503,
            detail="Operator API key required but EMR_RECALL_API_KEY is not configured",
        )
    token = _extract_bearer(authorization) or (x_emr_recall_key or "").strip()
    if not token or not secrets.compare_digest(token, expected):
        raise HTTPException(
            status_code=401,
            detail="Invalid or missing operator API key",
        )


def require_emr_recall_api_key(
    authorization: str | None = Header(default=None),
    x_emr_recall_key: str | None = Header(default=None, alias="X-EMR-Recall-Key"),
) -> None:
    """When EMR_RECALL_API_KEY is set, require Bearer or X-EMR-Recall-Key header."""
    if oauth_enabled():
        # Identity middleware authenticated the OAuth token before this dependency.
        return
    expected = emr_recall_api_key()
    if is_public_deployment() and not expected:
        raise HTTPException(status_code=503, detail="Public deployment requires EMR_RECALL_API_KEY")
    if not expected:
        return
    verify_operator_api_key(authorization, x_emr_recall_key)


def require_ledger_read_api_key(
    authorization: str | None = Header(default=None),
    x_emr_recall_key: str | None = Header(default=None, alias="X-EMR-Recall-Key"),
) -> None:
    if not ledger_read_protected():
        return
    verify_operator_api_key(authorization, x_emr_recall_key)


def require_memory_write() -> None:
    if oauth_enabled():
        principal = current_principal()
        if principal is None or WRITE_SCOPE not in principal.scopes:
            raise HTTPException(status_code=403, detail="OAuth access token lacks required scope: memory.write")
    if not memory_write_enabled():
        raise HTTPException(
            status_code=403,
            detail="Memory writes are disabled on this deployment",
        )


LEDGER_READ_PREFIX = "/api/jarvis/memory"


async def ledger_read_protection_middleware(request: Request, call_next):
    """Gate GET ledger endpoints when JARVIS_PROTECT_LEDGER_READ is enabled."""
    if oauth_enabled():
        return await call_next(request)
    if request.method != "GET" or not request.url.path.startswith(LEDGER_READ_PREFIX):
        return await call_next(request)
    if not ledger_read_protected():
        return await call_next(request)
    try:
        verify_operator_api_key(
            request.headers.get("authorization"),
            request.headers.get("x-emr-recall-key"),
        )
    except HTTPException as exc:
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})
    return await call_next(request)


def optional_verify_operator_api_key(
    authorization: str | None,
    x_emr_recall_key: str | None,
) -> None:
    """When EMR_RECALL_API_KEY is unset, allow (local dev). When set, require match."""
    if oauth_enabled():
        # The identity middleware already validates OAuth and binds a tenant.
        # Never reinterpret a valid user token as the legacy operator secret.
        return
    if is_public_deployment() and not emr_recall_api_key():
        raise HTTPException(status_code=503, detail="Public deployment requires EMR_RECALL_API_KEY")
    if not emr_recall_api_key():
        return
    verify_operator_api_key(authorization, x_emr_recall_key)

# ---------------------------------------------------------------------------
# Ledger operator API key (required-by-default; restored from a9b992c regression)
# ---------------------------------------------------------------------------

_TRUTHY = frozenset({"1", "true", "yes", "on"})


def configured_api_key() -> str | None:
    key = (os.getenv("JARVIS_API_KEY") or "").strip()
    return key or None


def allow_unauthenticated() -> bool:
    raw = (os.getenv("JARVIS_ALLOW_UNAUTHENTICATED") or "").strip().lower()
    return raw in _TRUTHY


def extract_presented_key(request: Request) -> str | None:
    auth = request.headers.get("Authorization") or ""
    if auth.lower().startswith("bearer "):
        return auth[7:].strip() or None
    header_key = request.headers.get("X-API-Key")
    if header_key:
        return header_key.strip() or None
    return None


PUBLIC_PATHS = frozenset(
    {
        "/",
        "/health",
        "/docs",
        "/openapi.json",
        "/redoc",
        "/api/jarvis/tools",
        "/.well-known/oauth-protected-resource",
        "/.well-known/oauth-protected-resource/mcp",
        "/privacy",
        "/terms",
    }
)
# Routes that carry their own gate (EMR_RECALL_API_KEY for tools/MCP,
# JARVIS_RAG_API_KEY_FILE for RAG) and are consumed by external MCP hosts;
# the ledger key does not apply to them.
LEDGER_KEY_EXEMPT_PREFIXES = ("/api/jarvis/tools/", "/api/jarvis/rag/", "/mcp")


def path_is_public(path: str) -> bool:
    return path in PUBLIC_PATHS


def path_is_ledger_key_exempt(path: str) -> bool:
    return path_is_public(path) or path.startswith(LEDGER_KEY_EXEMPT_PREFIXES)


class ApiKeyMiddleware(BaseHTTPMiddleware):
    """Gate ledger routes: JARVIS_API_KEY required unless JARVIS_ALLOW_UNAUTHENTICATED."""

    async def dispatch(self, request: Request, call_next) -> Response:
        # OAuth mode owns identity for every public API/MCP request.  Do not
        # require the legacy operator key in addition to a valid user token.
        if oauth_enabled():
            return await call_next(request)
        # CORS preflights carry no credentials; let CORSMiddleware answer them.
        if request.method == "OPTIONS" or path_is_ledger_key_exempt(request.url.path):
            return await call_next(request)

        expected = configured_api_key()
        if expected is not None:
            presented = extract_presented_key(request)
            if presented is None or not secrets.compare_digest(presented, expected):
                return JSONResponse(
                    status_code=401,
                    content={"detail": "Invalid or missing API key"},
                )
            return await call_next(request)

        if allow_unauthenticated():
            return await call_next(request)

        return JSONResponse(
            status_code=401,
            content={
                "detail": (
                    "API key required. Set JARVIS_API_KEY, or for local dev only "
                    "set JARVIS_ALLOW_UNAUTHENTICATED=1."
                )
            },
        )


OptionalApiKeyMiddleware = ApiKeyMiddleware

def oauth_enabled() -> bool:
    return auth_mode() == "oauth"


def oauth_challenge(scope: str = READ_SCOPE) -> str:
    base = public_base_url()
    metadata = f'{base}/.well-known/oauth-protected-resource' if base else "/.well-known/oauth-protected-resource"
    return f'Bearer resource_metadata="{metadata}", scope="{scope}"'


async def identity_middleware(request: Request, call_next):
    """Authenticate public API/MCP calls and bind their OAuth subject to storage."""
    protected = request.url.path == "/mcp" or request.url.path.startswith("/api/jarvis/")
    if not protected or not oauth_enabled():
        return await call_next(request)
    authorization = request.headers.get("authorization") or ""
    if not authorization.lower().startswith("bearer "):
        return JSONResponse(
            status_code=401,
            content={"detail": "OAuth Bearer access token required"},
            headers={"WWW-Authenticate": oauth_challenge()},
        )
    try:
        principal = validate_access_token(authorization[7:].strip())
    except HTTPException as exc:
        headers = {"WWW-Authenticate": oauth_challenge()} if exc.status_code == 401 else {}
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail}, headers=headers)
    token = set_principal(principal)
    try:
        return await call_next(request)
    finally:
        reset_principal(token)
