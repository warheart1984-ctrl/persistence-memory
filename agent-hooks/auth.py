"""API-key gate for Continuity Ledger deployments.

Default (secure): JARVIS_API_KEY must be set; protected routes require
Authorization: Bearer <key> or X-API-Key.

Local-dev opt-out: set JARVIS_ALLOW_UNAUTHENTICATED=1 to serve without a key
(open auth). Do not use the opt-out on shared or port-forwarded hosts.
"""

from __future__ import annotations

import hmac
import os

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

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


def path_is_public(path: str) -> bool:
    return path in {"/", "/health", "/docs", "/openapi.json", "/redoc"}


class ApiKeyMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next) -> Response:
        if path_is_public(request.url.path):
            return await call_next(request)

        expected = configured_api_key()
        if expected is not None:
            presented = extract_presented_key(request)
            if presented is None or not hmac.compare_digest(presented, expected):
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


# Backward-compatible alias (middleware renamed from optional → required-by-default).
OptionalApiKeyMiddleware = ApiKeyMiddleware
