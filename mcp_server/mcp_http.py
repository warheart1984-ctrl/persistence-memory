"""Streamable HTTP MCP transport — mount at ``/mcp`` on the FastAPI app.

Stateless JSON responses (no SSE session manager).
Tools: ``emr_recall``, ``emr_remember``, ``emr_upsert`` (writes gated server-side).
Spec: MCP Streamable HTTP (2025-03-26).
"""

from __future__ import annotations

import json
import secrets
from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import JSONResponse

from app import call_witness
from app.auth import mcp_credential
from app.identity import request_tenant
from mcp_server.protocol import (
    MCP_TOOLS,
    PROTOCOL_VERSION,
    RECALL_READ_TOOLS,
    WRITER_TOOLS,
    EmrToolCaller,
    dispatch_rpc,
    is_jsonrpc_request,
)

SESSION_HEADER = "mcp-session-id"


def _require_mcp_auth(request: Request) -> str:
    """Authenticate the request; returns which credential it carries (``operator``: the ledger key, or ``recall``: read tools only)."""
    return mcp_credential(
        request.headers.get("authorization"),
        request.headers.get("x-emr-recall-key"),
        request.headers.get("x-api-key"),
        request.headers.get("x-emr-writer-key"),
    )


_SCOPES = {"recall": (RECALL_READ_TOOLS, "recall-credential-read-only"), "writer": (WRITER_TOOLS, "writer-credential-scope")}


def _scoped_dispatch(message: dict[str, Any], call_tool: EmrToolCaller, credential: str) -> dict[str, Any] | None:
    """dispatch_rpc, narrowed for a scoped credential: ``recall`` lists, and may call, only RECALL_READ_TOOLS; ``writer`` only WRITER_TOOLS. Anything else is refused as a tool error."""
    scope = _SCOPES.get(credential)
    if scope is None:
        return dispatch_rpc(message, call_tool)
    allowed, reason = scope
    method = message.get("method")
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": message.get("id"), "result": {"tools": [t for t in MCP_TOOLS if t["name"] in allowed]}}
    if method == "tools/call":
        params = message.get("params")
        name = params.get("name") if isinstance(params, dict) else None
        if name not in allowed:
            return {"jsonrpc": "2.0", "id": message.get("id"), "result": {
                "content": [{"type": "text", "text": f"tool not permitted for this credential: {name}"}],
                "structuredContent": {"error": {"code": "AUTHORITY_DENIED", "reason": reason}},
                "isError": True}}
    return dispatch_rpc(message, call_tool)


def _parse_body(raw: bytes) -> list[dict[str, Any]]:
    if not raw:
        raise HTTPException(status_code=400, detail="Empty MCP request body")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail=f"Invalid JSON: {exc}") from exc
    if isinstance(payload, dict):
        return [payload]
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    raise HTTPException(status_code=400, detail="MCP body must be object or array")


def _validate_accept(request: Request) -> None:
    accept = request.headers.get("accept", "")
    if not accept:
        return
    if "application/json" in accept or "text/event-stream" in accept or "*/*" in accept:
        return
    raise HTTPException(
        status_code=406,
        detail="Accept must include application/json or text/event-stream",
    )


def create_mcp_router(call_tool: EmrToolCaller) -> APIRouter:
    router = APIRouter()

    @router.post("")
    @router.post("/")
    async def mcp_post(request: Request) -> Response:
        _validate_accept(request)
        try:
            credential = _require_mcp_auth(request)
        except HTTPException:
            call_witness.log_unauthenticated_mcp(request.headers.get("user-agent"))
            raise

        session_id = request.headers.get(SESSION_HEADER)
        messages = _parse_body(await request.body())

        requests = [m for m in messages if is_jsonrpc_request(m)]
        notifications = [m for m in messages if not is_jsonrpc_request(m)]

        for note in notifications:
            _scoped_dispatch(note, call_tool, credential)

        if not requests:
            return Response(status_code=202)

        responses: list[dict[str, Any]] = []
        new_session: str | None = None
        for msg in requests:
            if msg.get("method") == "initialize" and not session_id:
                new_session = secrets.token_urlsafe(24)
            if msg.get("method") == "initialize":
                call_witness.remember_client(new_session or session_id, msg.get("params"))
            out = call_witness.witnessed_dispatch(
                msg, lambda m: _scoped_dispatch(m, call_tool, credential), session_id=session_id or new_session,
                tenant=request_tenant(), user_agent=request.headers.get("user-agent"),
            )
            if out is not None:
                responses.append(out)

        if len(responses) == 1:
            body = responses[0]
        else:
            body = responses

        headers: dict[str, str] = {}
        if new_session:
            headers[SESSION_HEADER] = new_session
        headers["MCP-Protocol-Version"] = PROTOCOL_VERSION

        return JSONResponse(content=body, headers=headers)

    @router.get("")
    @router.get("/")
    async def mcp_get() -> Response:
        return Response(status_code=405, headers={"Allow": "POST, DELETE"})

    @router.delete("")
    @router.delete("/")
    async def mcp_delete() -> Response:
        return Response(status_code=204)

    return router
