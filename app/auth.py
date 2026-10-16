"""Auth gates.

Ledger routes: JARVIS_API_KEY required by default (ApiKeyMiddleware);
JARVIS_ALLOW_UNAUTHENTICATED=1 is the local-dev opt-out.
EMR tool / MCP routes: EMR_RECALL_API_KEY (optional, hosted recall).
Write gate: JARVIS_MEMORY_WRITE_ENABLED / JARVIS_MCP_WRITE_ENABLED.
NX Search write gate: JARVIS_NX_WRITE_ENABLED (off by default).
"""

from __future__ import annotations

import os
import secrets
from pathlib import Path

from fastapi import Header, HTTPException, Request
from starlette.concurrency import run_in_threadpool
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse, Response

from app.public_security import is_public_deployment
from app.refusal import json_response
from app import key_auth
from app.identity import (OPERATOR_TENANT, Principal, current_principal, current_tenant_key, in_operator_job, is_key_tenant, is_operator, multi_tenant_mode, operator_principal,
                          reset_principal, set_principal)
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


def nx_write_enabled() -> bool:
    """Gate for nx-search mutating operations (scan, reindex, prune, remember, forget,
    describe --save, spatialize, watch, serve).

    Defaults to **false** so local file index mutations are opt-in.
    Independent of JARVIS_MEMORY_WRITE_ENABLED and JARVIS_MCP_WRITE_ENABLED.
    """
    return os.getenv("JARVIS_NX_WRITE_ENABLED", "false").lower() in (
        "1",
        "true",
        "yes",
    )


def nx_enabled() -> bool:
    """Master switch for all nx-search integration.
    
    Defaults to false on public deployments (Render, etc.), true for local dev.
    Set JARVIS_NX_ENABLED=true to explicitly enable.
    """
    default = "false" if is_public_deployment() else "true"
    return os.getenv("JARVIS_NX_ENABLED", default).lower() in (
        "1",
        "true",
        "yes",
    )


def require_nx_enabled() -> None:
    """Require nx-search integration to be enabled."""
    if not nx_enabled():
        raise HTTPException(
            status_code=403,
            detail="NX search integration is disabled on this deployment (set JARVIS_NX_ENABLED=true)",
        )


def nx_allowed_roots() -> list[Path]:
    """Paths that nx-search operations may read/write. Defaults to indexed drives from NX_SCAN_ROOTS."""
    roots_env = os.getenv("NX_SCAN_ROOTS", "").strip()
    if roots_env:
        return [Path(r.strip()) for r in roots_env.split(";") if r.strip()]
    # Default to common Windows drives if not configured
    return [Path("D:/"), Path("F:/"), Path("G:/")]


def require_nx_write() -> None:
    """Require nx-write feature flag and memory write auth."""
    if tenant_identity_in_force():
        principal = current_principal()
        if principal is None or WRITE_SCOPE not in principal.scopes:
            raise HTTPException(status_code=403, detail=_scope_denied(WRITE_SCOPE))
    if not nx_write_enabled():
        raise HTTPException(
            status_code=403,
            detail="NX search writes are disabled on this deployment (set JARVIS_NX_WRITE_ENABLED=true)",
        )


def validate_nx_path(path: str, allowed_roots: list[Path] | None = None) -> Path:
    """Validate that a path is within allowed roots. Raises HTTPException if not."""
    if allowed_roots is None:
        allowed_roots = nx_allowed_roots()
    
    try:
        target = Path(path).resolve()
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Invalid path: {e}")
    
    # Check path traversal
    try:
        target.relative_to(target.anchor)
    except ValueError:
        raise HTTPException(status_code=400, detail="Path traversal not allowed")
    
    # Check against allowed roots
    for root in allowed_roots:
        try:
            target.relative_to(root.resolve())
            return target
        except ValueError:
            continue
    
    allowed = ", ".join(str(r) for r in allowed_roots)
    raise HTTPException(status_code=403, detail=f"Path outside allowed roots: {allowed}")


def require_mcp_write_scope() -> None:
    """OAuth scope half of the MCP write gate (flag handled by the write tools)."""
    if tenant_identity_in_force():
        principal = current_principal()
        if principal is None or WRITE_SCOPE not in principal.scopes:
            raise HTTPException(status_code=403, detail=_scope_denied(WRITE_SCOPE))


def require_mcp_write() -> None:
    require_mcp_write_scope()
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
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> None:
    """Require EMR_RECALL_API_KEY when set; else JARVIS_API_KEY unless JARVIS_ALLOW_UNAUTHENTICATED."""
    if tenant_identity_in_force():
        # Identity middleware authenticated the OAuth token / tenant key before this dependency.
        return
    expected = emr_recall_api_key()
    if is_public_deployment() and not expected:
        raise HTTPException(status_code=503, detail="Public deployment requires EMR_RECALL_API_KEY")
    if not expected:
        verify_ledger_key_fallback(authorization, x_api_key)
        return
    verify_operator_api_key(authorization, x_emr_recall_key)


def credential_kind(authorization: str | None, x_emr_recall_key: str | None, x_api_key: str | None) -> str | None:
    """Which credential does a tool or MCP request carry?  ``operator`` (the ledger key), ``recall`` (EMR_RECALL_API_KEY: read tools only), or None.

    The recall key is a *scoped* credential: it never opens call log, drift, writes, administration or any REST ledger route (see docs/recall_credential.md).
    The ledger key keeps working on the tool routes when a recall key is configured, so installing one breaks no existing client.  On a PUBLIC deployment the
    old rule stays: the recall key is the one operator credential there (the ledger key is not accepted on the tool routes)."""
    recall = emr_recall_api_key()
    ledger = configured_api_key()
    bearer = _extract_bearer(authorization)
    recall_token = bearer or (x_emr_recall_key or "").strip()
    recall_match = bool(recall and recall_token and secrets.compare_digest(recall_token, recall))
    if is_public_deployment():
        return "operator" if recall_match else None            # the old rule: on a public deployment the recall key is the operator credential
    if ledger is None:
        if recall:
            return "recall" if recall_match else None          # still read-only with no ledger key configured: operator-only operations fail closed (nobody is the operator)
        return "operator" if allow_unauthenticated() else None
    if any(t and secrets.compare_digest(t, ledger) for t in (bearer, (x_api_key or "").strip())):   # (either one: see presented_ledger_candidates)
        return "operator"                                      # (the ledger key wins when both are presented)
    return "recall" if recall_match else None


def require_tools_credential(
    request: Request,
    authorization: str | None = Header(default=None),
    x_emr_recall_key: str | None = Header(default=None, alias="X-EMR-Recall-Key"),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> None:
    """The read tool routes: the ledger key (operator) or, when configured, the recall key (read-only scope). Records which one on the request."""
    if tenant_identity_in_force():
        return                            # the identity middleware authenticated the OAuth token / tenant key before this dependency
    if is_public_deployment() and not emr_recall_api_key():
        raise HTTPException(status_code=503, detail="Public deployment requires EMR_RECALL_API_KEY")
    kind = credential_kind(authorization, x_emr_recall_key, x_api_key)
    if kind is None:
        raise HTTPException(status_code=401, detail="Invalid or missing operator API key" if emr_recall_api_key() else "Invalid or missing API key")
    request.scope["jarvis_credential"] = kind


def require_ledger_operator_key(
    authorization: str | None = Header(default=None),
    x_emr_recall_key: str | None = Header(default=None, alias="X-EMR-Recall-Key"),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> None:
    """Call log, drift, tool writes and the nx routes: the LEDGER key, never the recall key (a recall-only caller is refused 401).

    With no recall key configured this is exactly what it was (the ledger key).  On a public deployment the recall key stays the operator credential."""
    if tenant_identity_in_force():
        return                            # (a tenant reaches the operator gates below this and is refused there, 403)
    if credential_kind(authorization, x_emr_recall_key, x_api_key) == "operator":
        return
    if is_public_deployment() and not emr_recall_api_key():
        raise HTTPException(status_code=503, detail="Public deployment requires EMR_RECALL_API_KEY")
    raise HTTPException(status_code=401, detail="Invalid or missing operator API key" if emr_recall_api_key() else "Invalid or missing API key")


def mcp_credential(authorization: str | None, x_emr_recall_key: str | None, x_api_key: str | None) -> str:
    """The MCP endpoint's check: raises like the tool routes do, and returns which credential the request carries (``operator`` or ``recall``)."""
    if tenant_identity_in_force():
        return "operator"                 # (as before: the identity middleware validated the token and bound the tenant; "operator" here only means "not recall-scoped")
    if is_public_deployment() and not emr_recall_api_key():
        raise HTTPException(status_code=503, detail="Public deployment requires EMR_RECALL_API_KEY")
    kind = credential_kind(authorization, x_emr_recall_key, x_api_key)
    if kind is None:
        raise HTTPException(status_code=401, detail="Invalid or missing operator API key" if emr_recall_api_key() else "Invalid or missing API key")
    return kind


def require_ledger_read_api_key(
    authorization: str | None = Header(default=None),
    x_emr_recall_key: str | None = Header(default=None, alias="X-EMR-Recall-Key"),
) -> None:
    if not ledger_read_protected():
        return
    verify_operator_api_key(authorization, x_emr_recall_key)


def require_memory_write() -> None:
    if tenant_identity_in_force():
        principal = current_principal()
        if principal is None or WRITE_SCOPE not in principal.scopes:
            raise HTTPException(status_code=403, detail=_scope_denied(WRITE_SCOPE))
    if not memory_write_enabled():
        raise HTTPException(
            status_code=403,
            detail="Memory writes are disabled on this deployment",
        )


def multi_tenant_config_problems() -> list[str]:
    """What JARVIS_MULTI_TENANT refuses to run with: anything that could turn "no identity" into an operator, or a store that is not tenant-isolated."""
    problems: list[str] = []
    if allow_unauthenticated():
        problems.append("JARVIS_ALLOW_UNAUTHENTICATED is set (every caller would be the operator)")
    if not (os.getenv("JARVIS_DATABASE_URL") or "").strip():
        problems.append("no PostgreSQL store is configured (JARVIS_DATABASE_URL): the JSON file store has no row-level isolation")
    elif (os.getenv("JARVIS_PG_STORE") or "rows").strip().lower() != "rows":
        problems.append("JARVIS_PG_STORE is not 'rows': only the row store has per-tenant rows and forced row-level security")
    if not oauth_enabled() and configured_api_key() is None:
        problems.append("no credential is configured (set JARVIS_API_KEY for the operator, or JARVIS_AUTH_MODE=oauth)")
    return problems


def assert_multi_tenant_config() -> None:
    """Called at startup: in JARVIS_MULTI_TENANT mode, refuse to serve with a configuration that could fail open."""
    if not multi_tenant_mode():
        return
    problems = multi_tenant_config_problems()
    if problems:
        raise RuntimeError("JARVIS_MULTI_TENANT refuses to start: " + "; ".join(problems))


def caller_is_operator() -> bool:
    """May this request use an operator-only surface?  An OAuth tenant never may.  In JARVIS_MULTI_TENANT mode the caller must also BE the operator (an
    authenticated operator principal): no identity is not the operator.  Outside that mode the shared-key deployment's rule holds: a request only gets here
    after the key middleware accepted it."""
    if in_operator_job():          # the box's own per-tenant job (identity.acting_for), not a request: allowed in OAuth deployments too
        return True
    principal = current_principal()
    if principal is not None and principal.kind == "tenant":
        return False               # a tenant is never the operator, whatever the deployment mode (a per-tenant key in a shared-key deployment included)
    if oauth_enabled():
        return False
    if multi_tenant_mode():
        return is_operator()
    return True


def require_operator_read() -> None:
    """Continuity Block reads are for the operator key only: never through an OAuth user token."""
    if not caller_is_operator():
        raise HTTPException(status_code=403, detail="Continuity Blocks can be read with the operator key only")


def require_nx_operator() -> None:
    """nx-search reads and indexes the host filesystem under deployment-wide roots, with no per-tenant scoping.

    Until there is a real multi-tenant user and per-tenant roots, every nx route is operator-only: an OAuth user token
    (even with memory.read) is refused.
    """
    if not caller_is_operator():
        raise HTTPException(status_code=403, detail="nx-search routes touch the host filesystem and are operator-only")


LEDGER_READ_PREFIX = "/api/jarvis/memory"


async def ledger_read_protection_middleware(request: Request, call_next):
    """Gate GET ledger endpoints when JARVIS_PROTECT_LEDGER_READ is enabled."""
    if tenant_identity_in_force():
        return await call_next(request)
    if request.method != "GET" or not request.url.path.startswith(LEDGER_READ_PREFIX):
        return await call_next(request)
    if not ledger_read_protected():
        return await call_next(request)
    try:
        # The operator credential for a ledger read is the LEDGER key (the recall key is read-only and never opens a REST ledger route). On a public
        # deployment the recall key stays the operator credential, as before.
        if is_public_deployment() and not emr_recall_api_key():
            raise HTTPException(status_code=503, detail="Public deployment requires EMR_RECALL_API_KEY")
        if credential_kind(request.headers.get("authorization"), request.headers.get("x-emr-recall-key"), request.headers.get("x-api-key")) != "operator":
            raise HTTPException(status_code=401, detail="Invalid or missing operator API key")
    except HTTPException as exc:
        return json_response(exc.status_code, exc.detail, headers=getattr(exc, "headers", None), path=request.url.path)
    return await call_next(request)


def verify_ledger_key_fallback(authorization: str | None, x_api_key: str | None) -> None:
    """No recall key configured: require JARVIS_API_KEY unless the local-dev opt-out is set."""
    expected = configured_api_key()
    if expected is None:
        if allow_unauthenticated():
            return
        raise HTTPException(
            status_code=401,
            detail=(
                "API key required. Set EMR_RECALL_API_KEY or JARVIS_API_KEY, or for "
                "local dev only set JARVIS_ALLOW_UNAUTHENTICATED=1."
            ),
        )
    presented = _extract_bearer(authorization) or (x_api_key or "").strip()
    if not presented or not secrets.compare_digest(presented, expected):
        raise HTTPException(status_code=401, detail="Invalid or missing API key")


def optional_verify_operator_api_key(
    authorization: str | None,
    x_emr_recall_key: str | None,
    x_api_key: str | None = None,
) -> None:
    """EMR_RECALL_API_KEY when set; otherwise JARVIS_API_KEY / explicit local-dev opt-out."""
    if tenant_identity_in_force():
        # The identity middleware already validates OAuth and binds a tenant.
        # Never reinterpret a valid user token as the legacy operator secret.
        return
    if is_public_deployment() and not emr_recall_api_key():
        raise HTTPException(status_code=503, detail="Public deployment requires EMR_RECALL_API_KEY")
    if not emr_recall_api_key():
        verify_ledger_key_fallback(authorization, x_api_key)
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


def presented_ledger_candidates(headers) -> list[str]:
    """Every value a request offers as the ledger key: the Bearer token and X-API-Key. Either one being the ledger key is enough (a request may carry the
    recall key as its Bearer and the ledger key in X-API-Key; the ledger key must win, in the middleware, the identity binding and credential_kind alike)."""
    out = []
    bearer = _extract_bearer(headers.get("authorization") or headers.get("Authorization"))
    if bearer:
        out.append(bearer)
    header_key = (headers.get("x-api-key") or headers.get("X-API-Key") or "").strip()
    if header_key:
        out.append(header_key)
    return out


PUBLIC_PATHS = frozenset(
    {
        "/",
        "/health",
        "/ready",
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
        if tenant_identity_in_force():
            return await call_next(request)          # OAuth, or a per-tenant key the identity middleware (which runs first) has already authenticated and bound
        # CORS preflights carry no credentials; let CORSMiddleware answer them.
        if request.method == "OPTIONS" or path_is_ledger_key_exempt(request.url.path):
            return await call_next(request)

        expected = configured_api_key()
        if expected is not None:
            if not any(secrets.compare_digest(p, expected) for p in presented_ledger_candidates(request.headers)):
                return json_response(401, "Invalid or missing API key", path=request.url.path)
            return await call_next(request)

        if allow_unauthenticated():
            return await call_next(request)

        return json_response(
            401,
            "API key required. Set JARVIS_API_KEY, or for local dev only set JARVIS_ALLOW_UNAUTHENTICATED=1.",
            path=request.url.path,
        )


OptionalApiKeyMiddleware = ApiKeyMiddleware

def oauth_enabled() -> bool:
    return auth_mode() == "oauth"


def tenant_identity_in_force() -> bool:
    """Is this request's identity decided by a tenant principal the identity middleware bound (an OAuth token, or a per-tenant API key) rather than by the shared key?"""
    return oauth_enabled() or is_key_tenant()


def _scope_denied(scope: str) -> str:
    return f"OAuth access token lacks required scope: {scope}" if oauth_enabled() else f"API key lacks required scope: {scope}"


def oauth_challenge(scope: str = READ_SCOPE) -> str:
    base = public_base_url()
    metadata = f'{base}/.well-known/oauth-protected-resource' if base else "/.well-known/oauth-protected-resource"
    return f'Bearer resource_metadata="{metadata}", scope="{scope}"'


def credential_is_operator(headers, path: str) -> bool:
    """Does this request carry the operator credential?  The same rules the gates apply, evaluated once so the identity can be bound for the whole request:
    the tool, MCP and call-log routes take EMR_RECALL_API_KEY when it is set and the ledger key otherwise; the ledger routes take the ledger key; the
    RAG routes have a key of their own (not the operator's); with no key configured only the explicit local-dev opt-out is the operator."""
    if oauth_enabled() or path.startswith("/api/jarvis/rag/"):
        return False
    bearer = _extract_bearer(headers.get("authorization"))
    if path.startswith(("/api/jarvis/tools/", "/mcp")):
        recall = emr_recall_api_key()
        if recall:
            token = bearer or (headers.get("x-emr-recall-key") or "").strip()
            if token and secrets.compare_digest(token, recall):
                return True                  # the recall key is a request identity here (the operator's ledger); WHAT it may call is decided by credential_kind
            if is_public_deployment():
                return False
        elif is_public_deployment():
            return False
    expected = configured_api_key()
    if expected is None:
        return allow_unauthenticated()
    return any(secrets.compare_digest(p, expected) for p in presented_ledger_candidates(headers))


async def identity_middleware(request: Request, call_next):
    """Authenticate public API/MCP calls and bind their identity to the request: an OAuth subject (a tenant), or the operator when the operator
    credential was presented.  Everything downstream reads the principal; nothing infers "operator" from the absence of one."""
    path = request.url.path
    protected = path == "/mcp" or path.startswith(("/mcp/", "/api/jarvis/"))
    if protected and not oauth_enabled() and credential_is_operator(request.headers, path):
        token = set_principal(operator_principal())
        request.scope["jarvis_tenant"] = OPERATOR_TENANT
        try:
            return await call_next(request)
        finally:
            reset_principal(token)
    if protected and not oauth_enabled() and key_auth.key_tenants_enabled():
        candidates = key_auth.token_candidates(request.headers)
        if candidates:
            # A per-tenant API key (JARVIS_KEY_TENANTS): authenticated against the control schema, bound to the key's own tenant. Every way of failing is the one 401 a wrong ledger
            # key gets; an unreachable control schema is a 503 (never an admission); a valid key of a tenant that is not active is a 503 for that tenant only.
            try:
                resolved = await run_in_threadpool(key_auth.authenticate, candidates)
            except key_auth.KeyAuthUnavailable:
                return json_response(503, "Ledger unavailable", path=path)
            if resolved is None:
                return json_response(401, "Invalid or missing API key", path=path)
            if resolved.tenant_status != "active":
                return json_response(503, "tenant_unavailable", path=path)
            if READ_SCOPE not in resolved.scopes:
                return json_response(403, f"API key lacks required scope: {READ_SCOPE}", path=path)
            token = set_principal(Principal(subject=resolved.key_id, scopes=resolved.scopes, issuer="jarvis-key", kind="tenant", tenant_key=resolved.tenant_key,
                                            credential_type="api_key", credential_id=resolved.key_id))
            request.scope["jarvis_tenant"] = resolved.tenant_key      # the call log reads this from the outermost layer
            try:
                return await call_next(request)
            finally:
                reset_principal(token)
    if not protected or not oauth_enabled():
        return await call_next(request)
    authorization = request.headers.get("authorization") or ""
    if not authorization.lower().startswith("bearer "):
        return json_response(
            401, "OAuth Bearer access token required", headers={"WWW-Authenticate": oauth_challenge()}, path=path
        )
    try:
        principal = validate_access_token(authorization[7:].strip())
    except HTTPException as exc:
        headers = {"WWW-Authenticate": oauth_challenge()} if exc.status_code == 401 else {}
        return json_response(exc.status_code, exc.detail, headers=headers, path=path)
    token = set_principal(principal)
    request.scope["jarvis_tenant"] = current_tenant_key()  # the call log reads this from the outermost layer
    try:
        return await call_next(request)
    finally:
        reset_principal(token)
