"""Fail-closed controls for an internet-facing Memory Board deployment."""

from __future__ import annotations

import os
import threading
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
    """Process-local fixed-window limiter (the edge/WAF remains the primary limiter). Buckets idle for a whole window are evicted, so a stream of distinct subjects cannot grow the table forever."""

    MAX_BUCKETS = 10_000

    def __init__(self, window: float = 60.0) -> None:
        self._requests: dict[str, deque[float]] = defaultdict(deque)
        self._window = window
        self._lock = threading.Lock()

    def allow(self, subject: str, limit: int | None = None) -> bool:
        limit = int(os.getenv("JARVIS_MCP_RATE_LIMIT_PER_MINUTE", "60")) if limit is None else limit
        now = time.monotonic()
        with self._lock:
            if len(self._requests) >= self.MAX_BUCKETS and subject not in self._requests:
                self._evict(now)
            window = self._requests[subject]
            while window and now - window[0] >= self._window:
                window.popleft()
            if len(window) >= limit:
                return False
            window.append(now)
            return True

    def reserve(self, subject: str, limit: int) -> bool:
        """Atomically count one in-flight attempt against `subject` unless it is already at `limit`. The caller `release`s it if the attempt turns out not to be a failure. Counting BEFORE the
        expensive work means a burst of concurrent attempts cannot all pass a check made against an empty bucket. Buckets at their limit are never evicted to make room (that would reset
        the throttle); when the table is full of such buckets a new subject is refused instead."""
        now = time.monotonic()
        with self._lock:
            self._protect_at = limit
            if len(self._requests) >= self.MAX_BUCKETS and subject not in self._requests:
                self._evict(now)
                if len(self._requests) >= self.MAX_BUCKETS:
                    return False
            window = self._requests[subject]
            while window and now - window[0] >= self._window:
                window.popleft()
            if len(window) >= limit:
                return False
            window.append(now)
            return True

    def release(self, subject: str) -> None:
        with self._lock:
            window = self._requests.get(subject)
            if window:
                window.pop()

    def _evict(self, now: float) -> None:
        for key in [k for k, w in self._requests.items() if not w or now - w[-1] >= self._window]:
            del self._requests[key]
        if len(self._requests) >= self.MAX_BUCKETS:             # every bucket is live: drop the oldest-touched half, but never one that is at its limit (a throttle must survive a flood of other subjects)
            keep_at = getattr(self, "_protect_at", None)
            droppable = [(k, w) for k, w in self._requests.items() if keep_at is None or len(w) < keep_at]
            for key, _ in sorted(droppable, key=lambda kv: kv[1][-1])[: self.MAX_BUCKETS // 2]:
                del self._requests[key]


mcp_rate_limiter = McpRateLimiter()
key_failure_limiter = McpRateLimiter()          # failed tenant-key attempts, keyed (client address, key id); see key_auth.reserve_attempt


def rate_limit_subject(request: Request) -> str:
    """Who a request is charged to: its authenticated credential when the identity layer has bound one (a tenant key or OAuth subject is a tenant of its own; the operator is one subject),
    else the client address. Tenants behind one proxy address therefore no longer share a bucket. This runs inside the identity middleware, so a failed authentication never reaches it
    (see key_auth.failure_limiter for those)."""
    from app.identity import current_principal

    principal = current_principal()
    if principal is not None:
        if principal.kind == "operator":
            return "operator"
        who = principal.credential_id or principal.tenant_key or principal.subject
        return f"tenant:{principal.issuer}:{who}"
    return "ip:" + (request.client.host if request.client else "unknown")


def worker_count() -> int:
    for name in ("WEB_CONCURRENCY", "UVICORN_WORKERS", "GUNICORN_WORKERS"):
        raw = (os.getenv(name) or "").strip()
        if raw.isdigit():
            return int(raw)
    return 1


def config_problems() -> list[str]:
    """The limiters are per process. With more than one worker each tenant's quota silently multiplies by the worker count, so that is a startup refusal unless a shared backend is declared
    (JARVIS_RATE_LIMIT_SHARED_BACKEND names one; the limiters here do not implement it, so declaring it states that the edge enforces the real limit)."""
    if worker_count() > 1 and not (os.getenv("JARVIS_RATE_LIMIT_SHARED_BACKEND") or "").strip():
        return [f"{worker_count()} workers are configured but the rate limiters are per process (each tenant's quota would multiply by the worker count): run one worker, "
                "or set JARVIS_RATE_LIMIT_SHARED_BACKEND to name the shared limiter that enforces the real limit"]
    return []


def assert_config() -> None:
    problems = config_problems()
    if problems:
        raise RuntimeError("rate limiting refuses to start: " + "; ".join(problems))


async def public_security_middleware(request: Request, call_next):
    if is_public_deployment():
        allowed_hosts = trusted_hosts()
        request_host = (request.url.hostname or "").lower()
        if not allowed_hosts or request_host not in allowed_hosts:
            return JSONResponse(status_code=421, content={"detail": "Untrusted Host header"})
        if request.url.path.startswith("/mcp"):
            if not mcp_rate_limiter.allow(rate_limit_subject(request)):
                return JSONResponse(status_code=429, content={"detail": "MCP rate limit exceeded"})

    response = await call_next(request)
    response.headers.setdefault("Cache-Control", "no-store")
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
    return response
