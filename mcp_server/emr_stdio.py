#!/usr/bin/env python3
"""Stdio MCP server — EMR recall + gated write tools.

Proxies tool calls to the Jarvis Memoryboard HTTP API:
  POST {JARVIS_MEMORYBOARD_URL}/api/jarvis/tools/{emr_recall|emr_remember|emr_upsert}

Transport: JSON-RPC 2.0 over stdin/stdout (MCP 2024-11-05 / 2025-03-26 compatible).
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from mcp_server.protocol import (
    EMR_RECALL_TOOL,
    EMR_REMEMBER_TOOL,
    EMR_UPSERT_TOOL,
    MCP_TOOLS,
    dispatch_rpc,
)

NO_BASE_URL_MESSAGE = (
    "JARVIS_MEMORYBOARD_URL is not set. Set it to the ledger you mean, for example "
    "http://127.0.0.1:8011 through an SSH tunnel. There is no default address, so nothing is sent "
    "(and the API key is never sent) until you choose one."
)

__all__ = [
    "EMR_RECALL_TOOL",
    "EMR_REMEMBER_TOOL",
    "EMR_UPSERT_TOOL",
    "MCP_TOOLS",
    "handle_message",
    "handle_tools_call",
    "call_emr_tool",
    "call_emr_recall",
]


def base_url() -> str:
    url = (os.environ.get("JARVIS_MEMORYBOARD_URL") or "").strip().rstrip("/")
    if not url:
        raise RuntimeError(NO_BASE_URL_MESSAGE)
    return url


def _send(message: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(message, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def _http_post(path: str, arguments: dict[str, Any]) -> dict[str, Any]:
    url = f"{base_url()}{path}"
    parsed = urllib.parse.urlparse(url)
    payload = json.dumps(arguments).encode("utf-8")
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    # Server accepts EMR_RECALL_API_KEY when set, else JARVIS_API_KEY.
    api_key = (
        (os.environ.get("EMR_RECALL_API_KEY") or "").strip()
        or (os.environ.get("JARVIS_API_KEY") or "").strip()
    )
    if api_key:
        if parsed.scheme != "https" and (parsed.hostname or "") not in ("127.0.0.1", "localhost", "::1"):
            raise RuntimeError(
                "refusing to send the API key over plain http to a non-loopback host; "
                "use https or an SSH tunnel to 127.0.0.1"
            )
        headers["Authorization"] = f"Bearer {api_key}"
    req = urllib.request.Request(
        url,
        data=payload,
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            body = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"memoryboard HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(
            f"memoryboard unreachable at {base_url()}: {exc.reason}. "
            "Check that the ledger is running and that the SSH tunnel (if you use one) is up."
        ) from exc

    return json.loads(body)


def call_emr_tool(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """POST the named EMR tool to the memoryboard HTTP API."""
    route_map = {
        "emr_recall": "emr_recall",
        "emr_remember": "emr_remember",
        "emr_upsert": "emr_upsert",
        "search": "search",
        "fetch": "fetch",
        "emr_search": "emr_search",
        "emr_fetch": "emr_fetch",
    }
    path_name = route_map.get(name)
    if path_name is None:
        raise RuntimeError(f"unknown tool: {name}")
    return _http_post(f"/api/jarvis/tools/{path_name}", arguments)


def call_emr_recall(arguments: dict[str, Any]) -> dict[str, Any]:
    """POST emr_recall to the memoryboard HTTP API (compat)."""
    return call_emr_tool("emr_recall", arguments)


def handle_tools_call(params: dict[str, Any]) -> dict[str, Any]:
    from mcp_server.protocol import handle_tools_call as _handle

    return _handle(params, call_emr_tool)


def handle_message(message: dict[str, Any]) -> bool:
    """Handle one JSON-RPC message. Returns False when the server should exit."""
    out = dispatch_rpc(message, call_emr_tool)
    if out is not None:
        _send(out)
    return True


def run_stdio() -> None:
    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError as exc:
            print(f"invalid JSON: {exc}", file=sys.stderr, flush=True)
            continue
        if not handle_message(message):
            break


if __name__ == "__main__":
    run_stdio()
