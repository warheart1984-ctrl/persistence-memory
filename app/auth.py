"""Optional API-key gate for operator deployments.

When JARVIS_API_KEY is unset, all routes remain open (local/dev default).
When set, non-public routes require Bearer or X-API-Key.
"""

from __future__ import annotations

import hmac
import os

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response


def configured_api_key() -> str | None:
    key = (os.getenv("JARVIS_API_KEY") or "").strip()
    return key or None


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


class OptionalApiKeyMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next) -> Response:
        expected = configured_api_key()
        if expected is None or path_is_public(request.url.path):
            return await call_next(request)

        presented = extract_presented_key(request)
        if presented is None or not hmac.compare_digest(presented, expected):
            return JSONResponse(
                status_code=401,
                content={"detail": "Invalid or missing API key"},
            )
        return await call_next(request)
