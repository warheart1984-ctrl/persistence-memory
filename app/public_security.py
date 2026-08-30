"""Fail-closed controls for an internet-facing Memory Board deployment."""

from __future__ import annotations

import os
import time
from collections import defaultdict, deque

from fastapi import Request
from starlette.responses import JSONResponse


def is_public_deployment() -> bool:
    return (os.getenv("JARVIS_PUBLIC_MODE") or "").lower() in {"1", "true", "yes"}


def cors_origins() -> list[str]:
    configured = (os.getenv("JARVIS_CORS_ORIGINS") or "").strip()
    if configured:
        return [origin.strip() for origin in configured.split(",") if origin.strip()]
    return ["http://127.0.0.1:8001", "http://localhost:8001"]


def trusted_hosts() -> set[str]:
    configured = (os.getenv("JARVIS_TRUSTED_HOSTS") or "").strip()
    return {host.strip().lower() for host in configured.split(",") if host.strip()}


class McpRateLimiter:
    """Process-local backstop; the edge/WAF remains the primary limiter."""

    def __init__(self) -> None:
        self._requests: dict[str, deque[float]] = defaultdict(deque)

    def allow(self, client: str) -> bool:
        limit = int(os.getenv("JARVIS_MCP_RATE_LIMIT_PER_MINUTE", "60"))
        now = time.monotonic()
        window = self._requests[client]
        while window and now - window[0] >= 60:
            window.popleft()
        if len(window) >= limit:
            return False
        window.append(now)
        return True


mcp_rate_limiter = McpRateLimiter()


async def public_security_middleware(request: Request, call_next):
    if is_public_deployment():
        allowed_hosts = trusted_hosts()
        request_host = (request.url.hostname or "").lower()
        if not allowed_hosts or request_host not in allowed_hosts:
            return JSONResponse(status_code=421, content={"detail": "Untrusted Host header"})
        if request.url.path.startswith("/mcp"):
            client = request.client.host if request.client else "unknown"
            if not mcp_rate_limiter.allow(client):
                return JSONResponse(status_code=429, content={"detail": "MCP rate limit exceeded"})

    response = await call_next(request)
    response.headers.setdefault("Cache-Control", "no-store")
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
    return response
