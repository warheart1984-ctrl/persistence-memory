"""Wiring for the call log (app/call_log.py): an ASGI middleware for the HTTP routes and a wrapper for MCP ``tools/call``.

What is witnessed:
* every call to ``/api/jarvis/tools/*`` (transport ``http-tool``, or ``mcp-stdio`` when the stdio proxy says so),
* every non-GET call under ``/api/jarvis/`` (transport ``http-api``),
* every ``tools/call`` on ``/mcp`` (transport ``mcp-http``), one entry per call even when a request carries a batch.

* every GET read of ``/api/jarvis/memory`` and ``/api/jarvis/memory/*`` (transport ``http-api``, or ``mcp-stdio`` from a stdio adapter), up to
  JARVIS_CALL_LOG_READ_CAP_PER_MIN a minute; beyond that the reads are counted into one ``read_suppressed`` summary entry, never dropped silently.
  JARVIS_CALL_LOG_READS=0 turns read witnessing off.

Not witnessed (documented in docs/call_log.md): the log's own endpoints, health/ready, and GET reads outside ``/api/jarvis/memory``.

Client identity is whatever the client says about itself (``clientInfo`` on MCP initialize, the ``X-Jarvis-MCP-Client`` header, or the
User-Agent); the server records it labelled as self-reported.  The entry is appended BEFORE the response is sent, so a client that
has the answer can already find its call in the log.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from collections import OrderedDict
from typing import Any, Callable
from urllib.parse import parse_qsl

from app import call_log
from app.refusal import error_body, error_headers

TOOLS_PREFIX = "/api/jarvis/tools/"
MEMORY_PREFIX = "/api/jarvis/memory"
OWN_PREFIXES = ("/api/jarvis/tools/calls",)  # the log's own endpoints are not tool calls
READ_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
MAX_RESPONSE_BUFFER = 2 * 1024 * 1024


def max_body_bytes() -> int:
    """The most of a request body the witness buffers (JARVIS_CALL_LOG_MAX_BODY_BYTES, default 4 MiB); the rest of a larger one streams through, hashed."""
    try:
        return max(1024, int((os.environ.get("JARVIS_CALL_LOG_MAX_BODY_BYTES") or "").strip() or 4 * 1024 * 1024))
    except ValueError:
        return 4 * 1024 * 1024
_ID_SEGMENTS = (
    (re.compile(r"^mem-[0-9a-f]+$"), "{id}"),
    (re.compile(r"^eo:sha256:[0-9a-f]+$"), "{eo}"),
    (re.compile(r"^[0-9]+$"), "{n}"),
)
_CODE_SAFE = re.compile(r"[^A-Za-z0-9_:.\-]")
# What a path parameter must look like, exactly as the app generates it, to be recorded as a target (anything else is only ever hashed):
# a memory id is mem-<12 hex> (app/store.py new_id), an AMUL artifact id is art-<12 hex>-<s|d|e><seq> (app/amul.py: the resolution is summary, detail or evidence).
_MEMORY_TARGETS = (re.compile(r"^mem-[0-9a-f]{12}$"), re.compile(r"^art-[0-9a-f]{12}-[sde][0-9]{1,9}$"))
# The route templates under /api/jarvis/memory, per method (relative; "{id}" is any one segment). A path is templated by matching it position by
# position against the templates of ITS method, preferring the one with the most literal segments: a route that exists only for POST (promote,
# pipeline, external-search ...) is not a literal for a GET, which the app serves as a record lookup. A segment that lands on a "{id}" is a path
# parameter and is never written (the target is recorded only if it really is a record id); a path that matches nothing is logged as .../{other}.
_GET_ROUTES = ("", "board", "retrieve", "conflicts", "search", "latest", "unified", "active", "stm", "stm/context", "history/verify", "{id}", "{id}/history",
               "{id}/resolve", "external/stats", "emr/status", "amul/artifacts/{id}", "amul/lineage/{id}", "amul/field/status", "graph/stats")
_POST_ROUTES = ("", "board", "external-search", "promote", "pipeline", "stm/expand", "external/ask", "external/remember", "external/forget", "external/describe",
                "external/spatialize", "external/scan", "external/reindex", "external/prune", "external/watch", "external/watch/stop", "external/serve",
                "external/serve/stop", "emr/excite", "emr/reinforce", "emr/correct", "amul/anchor", "amul/field/verify", "graph/bfs", "graph/shortest-path",
                "graph/related", "graph/components")
MEMORY_TEMPLATES: dict[str, tuple[tuple[str, ...], ...]] = {
    method: tuple(tuple(x for x in route.split("/") if x) for route in routes)
    for method, routes in {"GET": _GET_ROUTES, "POST": _POST_ROUTES, "PATCH": ("board", "{id}"), "PUT": ("{id}",), "DELETE": ("stm", "{id}")}.items()
}


def normalize_memory_route(method: str, path: str) -> tuple[str, str | None, list[str]]:
    """Route template for a request under /api/jarvis/memory, taken from the route table by method and position, never from the typed path.
    Also returns the values that sat in the path-parameter slots: they are never stored, but they go into the argument hash (see ``witness_args``)."""
    segs = [s for s in path[len(MEMORY_PREFIX):].split("/") if s]
    best: tuple[str, ...] | None = None
    for tpl in MEMORY_TEMPLATES.get(method.upper(), ()):
        if len(tpl) != len(segs) or any(p != "{id}" and p != s for p, s in zip(tpl, segs)):
            continue
        if best is None or sum(p != "{id}" for p in tpl) > sum(p != "{id}" for p in best):
            best = tpl
    if best is None:
        return MEMORY_PREFIX + "/{other}", None, segs
    params = [s for p, s in zip(best, segs) if p == "{id}"]
    target = next((s for s in params if any(rx.match(s) for rx in _MEMORY_TARGETS)), None)
    return "/".join([MEMORY_PREFIX, *best]), target, params


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
    if method == "GET" and (path == MEMORY_PREFIX or path.startswith(MEMORY_PREFIX + "/")) and call_log.reads_enabled():
        route, target, params = normalize_memory_route("GET", path)
        return {"kind": "api", "tool": f"GET {route}", "route": route, "target": target, "write": False, "path_params": params}
    if path.startswith("/api/jarvis/") and method not in READ_METHODS:
        params: list[str] = []
        if path == MEMORY_PREFIX or path.startswith(MEMORY_PREFIX + "/"):
            route, target, params = normalize_memory_route(method, path)
        else:
            route, target = normalize_route(path)
        write = (route == "/api/jarvis/memory" and method == "POST") or (route == "/api/jarvis/memory/{id}" and method in ("PATCH", "PUT", "DELETE"))
        return {"kind": "api", "tool": f"{method} {route}", "route": route, "target": target, "write": write, "path_params": params}
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


def _query_dict(query_string: bytes) -> dict[str, Any] | None:
    """The query as the app sees it: a key given more than once keeps every value, in order (``?t=a&t=b`` is not ``?t=b``)."""
    if not query_string:
        return None
    out: dict[str, Any] = {}
    for key, value in parse_qsl(query_string.decode("latin-1"), keep_blank_values=True):
        if key in out:
            out[key] = [*out[key], value] if isinstance(out[key], list) else [out[key], value]
        else:
            out[key] = value
    return out


def _transport(plan: dict[str, Any], headers: dict[str, str]) -> str:
    """mcp-stdio when a stdio adapter says so, else http-tool / http-api: the same label on every path (completion, refusal, error, overflow)."""
    if headers.get("x-jarvis-mcp-transport", "").strip().lower() == "stdio":
        return "mcp-stdio"
    return "http-tool" if plan["kind"] == "tool" else "http-api"


def _witness_headers(entry: dict[str, Any] | None) -> list[tuple[bytes, bytes]]:
    """The response header that tells the caller where its call is in the log, or that the log could not record it."""
    if entry and "seq" in entry:
        return [(b"x-jarvis-call-seq", str(entry["seq"]).encode())]
    return [(b"x-jarvis-call-log", b"degraded")] if entry is None else []


class BodyStream:
    """A request body too big to buffer: only its length and sha256 are kept, as the app reads it."""

    def __init__(self) -> None:
        self._h = hashlib.sha256()
        self.size = 0

    def add(self, chunk: bytes) -> None:
        self._h.update(chunk)
        self.size += len(chunk)

    def as_args(self) -> dict[str, Any]:
        return {"_streamed_body_sha256": self._h.hexdigest(), "_streamed_body_bytes": self.size}


def witness_args(plan: dict[str, Any], body_bytes: bytes | None, query_string: bytes, stream: "BodyStream | None" = None) -> Any:
    """What the argument hash is taken over: the body and query, plus the path parameters (hashed, never stored) so that a read of one record
    or artifact is not indistinguishable from a read of another. A route with no path parameter hashes exactly as before."""
    args = request_args(stream.as_args() if stream is not None else _json_or_none(body_bytes or b""), query_string)
    params = plan.get("path_params")
    return {"args": args, "path": params} if params else args


def request_args(body: Any, query_string: bytes) -> Any:
    query = _query_dict(query_string)
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

        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
        stream: BodyStream | None = None  # set only when the body is too big to buffer
        if method in READ_METHODS:
            # A read has no body worth witnessing: do not buffer one (nothing a client sends here may grow this process), pass the request through as it came.
            body_bytes = b""
            replay = receive
        else:
            # Buffer the body only up to a bound (the witness needs it to hash the arguments). A larger body is NOT refused (that would impose a
            # smaller limit than the API's own on routes such as bulk document import): what has been read is replayed, the rest streams through to
            # the app untouched while it is hashed on the way, so this process never holds more than the bound.
            limit = max_body_bytes()
            chunks: list[bytes] = []
            received = 0
            complete = False
            while True:
                message = await receive()
                if message["type"] != "http.request":  # disconnect before the body arrived: nothing to serve
                    return
                chunk = message.get("body", b"")
                chunks.append(chunk)
                received += len(chunk)
                if not message.get("more_body"):
                    complete = True
                    break
                if received > limit:
                    break
            if complete:
                body_bytes = b"".join(chunks)
                replayed = False

                async def replay():
                    nonlocal replayed
                    if not replayed:
                        replayed = True
                        return {"type": "http.request", "body": body_bytes, "more_body": False}
                    return await receive()
            else:
                body_bytes = None
                stream = BodyStream()
                for c in chunks:
                    stream.add(c)
                pending = list(chunks)

                async def replay():
                    if pending:
                        return {"type": "http.request", "body": pending.pop(0), "more_body": True}
                    message = await receive()
                    if message["type"] == "http.request":
                        stream.add(message.get("body", b""))
                    return message

        if plan["write"]:
            try:
                log.preflight()
            except call_log.CallLogError as exc:
                log._mark_degraded(str(exc), unlogged=0, refused=1)
                payload = json.dumps(error_body(503, "the call log is unavailable, so ledger writes are refused", code="unavailable", reason="CALL_LOG_UNAVAILABLE")).encode()
                await send({"type": "http.response.start", "status": 503, "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(payload)).encode()),
                                                                                     *[(k.lower().encode(), v.encode()) for k, v in (error_headers(503) or {}).items()]]})
                await send({"type": "http.response.body", "body": payload})
                return

        held: list[dict[str, Any]] = []
        buffered = 0
        status = 500
        overflow = False
        logged = False
        resp_body = bytearray()

        async def capture(message):
            nonlocal buffered, status, overflow, logged
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
                if buffered + len(chunk) > MAX_RESPONSE_BUFFER:  # checked BEFORE copying the chunk, so one huge body is never duplicated here
                    # too big to hold: stream it, logged without a digest, but still BEFORE its headers are released
                    overflow = True
                    entry = log.record(call_log.make_fields(
                        transport=_transport(plan, headers), tool=plan["tool"], args=witness_args(plan, body_bytes, scope.get("query_string", b""), stream),
                        outcome=_outcome(status), started=started, client_header=headers.get("x-jarvis-mcp-client") or headers.get("user-agent"),
                        tenant=scope.get("jarvis_tenant"), error_code=_code(status, None), status_code=status, route=plan["route"], method=method, target=plan["target"]))
                    logged = True
                    for m in held:
                        if m["type"] == "http.response.start":
                            m = {**m, "headers": [*m.get("headers", []), *_witness_headers(entry)]}
                        await send(m)
                    held.clear()
                    return
                buffered += len(chunk)
                resp_body.extend(chunk)
                if not message.get("more_body"):
                    await finish()

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
            transport = _transport(plan, headers)
            fields = call_log.make_fields(
                transport=transport, tool=plan["tool"], args=witness_args(plan, body_bytes, scope.get("query_string", b""), stream), outcome=_outcome(status),
                started=started, client_header=client_header, tenant=scope.get("jarvis_tenant"), error_code=_code(status, parsed), status_code=status,
                result_digest=digest, route=plan["route"], method=method, target=plan["target"],
            )
            nonlocal logged
            entry = log.record(fields)
            logged = True
            for m in held:
                if m["type"] == "http.response.start":
                    m = {**m, "headers": [*m.get("headers", []), *_witness_headers(entry)]}
                await send(m)
            held.clear()

        try:
            await self.app(scope, replay, capture)
        except Exception:
            if not logged:  # (an overflow or finished response was already witnessed)
                log.record(call_log.make_fields(transport=_transport(plan, headers), tool=plan["tool"], args=witness_args(plan, body_bytes, scope.get("query_string", b""), stream),
                                                outcome="error", started=started, client_header=headers.get("x-jarvis-mcp-client") or headers.get("user-agent"),
                                                tenant=scope.get("jarvis_tenant"), error_code="INTERNAL", status_code=500, route=plan["route"], method=method, target=plan["target"]))
            raise
        if held:  # the app ended without a final body chunk
            await finish()


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
            log._mark_degraded(str(exc), unlogged=0, refused=1)
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
