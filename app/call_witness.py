"""Wiring for the call log (app/call_log.py): an ASGI middleware for the HTTP routes and a wrapper for MCP ``tools/call``.

What is witnessed:
* every call to ``/api/jarvis/tools/*`` (transport ``http-tool``, or ``mcp-stdio`` when the stdio proxy says so),
* every non-GET call under ``/api/jarvis/`` (transport ``http-api``),
* every ``tools/call`` on ``/mcp`` (transport ``mcp-http``), one entry per call even when a request carries a batch.

Not witnessed (documented in docs/call_log.md): GET reads of ``/api/jarvis/memory/*``, the log's own endpoints, and health/ready.

Client identity is whatever the client says about itself (``clientInfo`` on MCP initialize, the ``X-Jarvis-MCP-Client`` header, or the
User-Agent); the server records it labelled as self-reported.  The entry is appended BEFORE the response is sent, so a client that
has the answer can already find its call in the log.
"""

from __future__ import annotations

import json
import re
import time
from collections import OrderedDict
from typing import Any, Callable
from urllib.parse import parse_qsl

from app import call_log
from app.refusal import error_body, error_headers

TOOLS_PREFIX = "/api/jarvis/tools/"
OWN_PREFIXES = ("/api/jarvis/tools/calls",)  # the log's own endpoints are not tool calls
READ_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
MAX_RESPONSE_BUFFER = 2 * 1024 * 1024
_ID_SEGMENTS = (
    (re.compile(r"^mem-[0-9a-f]+$"), "{id}"),
    (re.compile(r"^eo:sha256:[0-9a-f]+$"), "{eo}"),
    (re.compile(r"^[0-9]+$"), "{n}"),
)
_CODE_SAFE = re.compile(r"[^A-Za-z0-9_:.\-]")


def normalize_route(path: str) -> tuple[str, str | None]:
    """Replace record ids and the like with placeholders; return (route template, the id that was in the path)."""
    parts, target = [], None
    for seg in path.split("/"):
        for rx, label in _ID_SEGMENTS:
            if rx.match(seg):
                target = target or seg
                seg = label
                break
        parts.append(seg)
    return "/".join(parts), target


def classify(method: str, path: str) -> dict[str, Any] | None:
    """What to log for this request, or None if it is not witnessed."""
    if any(path == p or path.startswith(p + "/") for p in OWN_PREFIXES):
        return None
    if path.startswith(TOOLS_PREFIX):
        name = path[len(TOOLS_PREFIX):].strip("/") or "(none)"
        return {"kind": "tool", "tool": name[:64], "route": path, "target": None, "write": name in call_log.WRITE_TOOLS}
    if path.startswith("/api/jarvis/") and method not in READ_METHODS:
        route, target = normalize_route(path)
        write = (route == "/api/jarvis/memory" and method == "POST") or (route == "/api/jarvis/memory/{id}" and method in ("PATCH", "PUT", "DELETE"))
        return {"kind": "api", "tool": f"{method} {route}", "route": route, "target": target, "write": write}
    return None


def _code(status: int, body: Any) -> str | None:
    if status < 400:
        return None
    raw = None
    if isinstance(body, dict):
        raw = body.get("reason") or body.get("code")
    return _CODE_SAFE.sub("", str(raw or f"HTTP_{status}"))[:64]


def _outcome(status: int) -> str:
    if status < 400:
        return "ok"
    return "denied" if status in (401, 403) else "error"


def _json_or_none(raw: bytes) -> Any:
    if not raw.strip():
        return None
    try:
        return json.loads(raw.decode("utf-8"))
    except ValueError:
        return {"_unparseable_body_sha256": call_log.sha256_hex(raw)}  # still a stable fingerprint, never the content


def request_args(body: Any, query_string: bytes) -> Any:
    query = dict(parse_qsl(query_string.decode("latin-1"), keep_blank_values=True)) if query_string else None
    if body is not None and query:
        return {"body": body, "query": query}
    return body if body is not None else (query or {})


class CallWitnessMiddleware:
    """Pure ASGI, so the request body can be replayed and the response held until its log entry is written."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not call_log.enabled():
            return await self.app(scope, receive, send)
        method, path = scope["method"], scope["path"]
        plan = classify(method, path)
        if plan is None:
            return await self.app(scope, receive, send)

        log = call_log.get_call_log()
        started = time.perf_counter()

        chunks: list[bytes] = []
        while True:
            message = await receive()
            if message["type"] == "http.request":
                chunks.append(message.get("body", b""))
                if not message.get("more_body"):
                    break
            else:  # disconnect before the body arrived: nothing to serve
                return
        body_bytes = b"".join(chunks)
        replayed = False

        async def replay():
            nonlocal replayed
            if not replayed:
                replayed = True
                return {"type": "http.request", "body": body_bytes, "more_body": False}
            return await receive()

        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
        if plan["write"]:
            try:
                log.preflight()
            except call_log.CallLogError as exc:
                log._mark_degraded(str(exc))
                payload = json.dumps(error_body(503, "the call log is unavailable, so ledger writes are refused", code="unavailable", reason="CALL_LOG_UNAVAILABLE")).encode()
                await send({"type": "http.response.start", "status": 503, "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(payload)).encode()),
                                                                                     *[(k.lower().encode(), v.encode()) for k, v in (error_headers(503) or {}).items()]]})
                await send({"type": "http.response.body", "body": payload})
                return

        held: list[dict[str, Any]] = []
        buffered = 0
        status = 500
        overflow = False
        resp_body = bytearray()

        async def capture(message):
            nonlocal buffered, status, overflow
            if message["type"] == "http.response.start":
                status = message["status"]
                held.append(message)
                return
            if overflow:
                await send(message)
                return
            held.append(message)
            if message["type"] == "http.response.body":
                chunk = message.get("body", b"")
                buffered += len(chunk)
                resp_body.extend(chunk)
                if buffered > MAX_RESPONSE_BUFFER:  # too big to hold: stream it, log without a digest
                    overflow = True
                    for m in held:
                        await send(m)
                    held.clear()
                    return
                if not message.get("more_body"):
                    await finish()

        sent_seq: list[int] = []

        async def finish():
            digest = None
            parsed = None
            if not overflow:
                try:
                    parsed = json.loads(bytes(resp_body).decode("utf-8")) if resp_body else None
                except ValueError:
                    parsed = None
                if isinstance(parsed, dict):
                    digest = parsed.get("result_digest")
            client_header = headers.get("x-jarvis-mcp-client") or headers.get("user-agent")
            transport = "mcp-stdio" if headers.get("x-jarvis-mcp-transport", "").strip().lower() == "stdio" else ("http-tool" if plan["kind"] == "tool" else "http-api")
            fields = call_log.make_fields(
                transport=transport, tool=plan["tool"], args=request_args(_json_or_none(body_bytes), scope.get("query_string", b"")), outcome=_outcome(status),
                started=started, client_header=client_header, tenant=scope.get("jarvis_tenant"), error_code=_code(status, parsed), status_code=status,
                result_digest=digest, route=plan["route"], method=method, target=plan["target"],
            )
            entry = log.record(fields)
            if entry and "seq" in entry:
                sent_seq.append(entry["seq"])
            for m in held:
                if m["type"] == "http.response.start":
                    hdrs = list(m.get("headers", []))
                    if sent_seq:
                        hdrs.append((b"x-jarvis-call-seq", str(sent_seq[0]).encode()))
                    elif entry is None:
                        hdrs.append((b"x-jarvis-call-log", b"degraded"))
                    m = {**m, "headers": hdrs}
                await send(m)
            held.clear()

        try:
            await self.app(scope, replay, capture)
        except Exception:
            log.record(call_log.make_fields(transport="http-tool" if plan["kind"] == "tool" else "http-api", tool=plan["tool"], args=request_args(_json_or_none(body_bytes), scope.get("query_string", b"")),
                                            outcome="error", started=started, client_header=headers.get("x-jarvis-mcp-client") or headers.get("user-agent"),
                                            tenant=scope.get("jarvis_tenant"), error_code="INTERNAL", status_code=500, route=plan["route"], method=method, target=plan["target"]))
            raise
        if held:  # the app ended without a final body chunk
            await finish()
        if overflow:
            call_fields = call_log.make_fields(
                transport="http-tool" if plan["kind"] == "tool" else "http-api", tool=plan["tool"], args=request_args(_json_or_none(body_bytes), scope.get("query_string", b"")),
                outcome=_outcome(status), started=started, client_header=headers.get("x-jarvis-mcp-client") or headers.get("user-agent"),
                tenant=scope.get("jarvis_tenant"), error_code=_code(status, None), status_code=status, route=plan["route"], method=method, target=plan["target"])
            log.record(call_fields)


# ------------------------------------------------------------------------------------------------------------- MCP

_sessions: "OrderedDict[str, tuple[str | None, str | None]]" = OrderedDict()
MAX_SESSIONS = 2048


def remember_client(session_id: str | None, params: dict[str, Any] | None) -> None:
    if not session_id:
        return
    info = (params or {}).get("clientInfo") or {}
    if not isinstance(info, dict):
        return
    _sessions[session_id] = (call_log.clean_client(info.get("name")), call_log.clean_client(info.get("version"), 64))
    while len(_sessions) > MAX_SESSIONS:
        _sessions.popitem(last=False)


def client_for(session_id: str | None) -> tuple[str | None, str | None]:
    return _sessions.get(session_id or "", (None, None))


def _mcp_result(response: dict[str, Any] | None) -> tuple[str, str | None, str | None]:
    """(outcome, error code, result_digest) from a JSON-RPC response to tools/call."""
    result = (response or {}).get("result") or {}
    error = (response or {}).get("error")
    if error:
        return "error", _CODE_SAFE.sub("", str(error.get("code", "RPC_ERROR")))[:64], None
    if result.get("isError"):
        info = ((result.get("structuredContent") or {}).get("error")) or {}
        code = str(info.get("reason") or info.get("code") or "TOOL_ERROR")
        return ("denied" if info.get("code") == "denied" else "error"), _CODE_SAFE.sub("", code)[:64], None
    digest = (result.get("structuredContent") or {}).get("result_digest")
    return "ok", None, digest if isinstance(digest, str) else None


def witnessed_dispatch(message: dict[str, Any], dispatch: Callable[[dict[str, Any]], dict[str, Any] | None], *, session_id: str | None, tenant: str | None, user_agent: str | None = None) -> dict[str, Any] | None:
    """Run one JSON-RPC message through ``dispatch``; if it is a tools/call, write exactly one log entry for it."""
    if not call_log.enabled() or message.get("method") != "tools/call":
        return dispatch(message)
    params = message.get("params") or {}
    name = str(params.get("name") or "(none)")[:64]
    arguments = params.get("arguments") if isinstance(params.get("arguments"), dict) else {}
    log = call_log.get_call_log()
    started = time.perf_counter()
    client_name, client_version = client_for(session_id)
    if client_name is None:  # no initialize seen for this session: fall back to the User-Agent (also self-reported)
        client_name, client_version = call_log.split_client(user_agent)

    def entry(outcome: str, code: str | None, digest: str | None) -> None:
        log.record(call_log.make_fields(transport="mcp-http", tool=name, args=arguments, outcome=outcome, started=started, client_name=client_name,
                                        client_version=client_version, tenant=tenant, error_code=code, result_digest=digest))

    if name in call_log.WRITE_TOOLS:
        try:
            log.preflight()
        except call_log.CallLogError as exc:
            log._mark_degraded(str(exc))
            return {"jsonrpc": "2.0", "id": message.get("id"), "result": {
                "content": [{"type": "text", "text": "the call log is unavailable, so ledger writes are refused"}],
                "structuredContent": {"error": {"code": "unavailable", "reason": "CALL_LOG_UNAVAILABLE"}}, "isError": True}}
    try:
        response = dispatch(message)
    except Exception:
        entry("error", "INTERNAL", None)
        raise
    outcome, code, digest = _mcp_result(response)
    entry(outcome, code, digest)
    return response


def log_unauthenticated_mcp(client_header: str | None) -> None:
    if not call_log.enabled():
        return
    started = time.perf_counter()
    call_log.get_call_log().record(call_log.make_fields(transport="mcp-http", tool="(unauthenticated)", args={}, outcome="denied", started=started,
                                                        client_header=client_header, error_code="UNAUTHENTICATED"))
