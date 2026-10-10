"""Shared MCP protocol surface — stdio and Streamable HTTP.

Tools:
- ``emr_recall`` — read-only governed recall (always available when auth allows)
- ``search`` / ``fetch`` — OpenAI deep-research / company-knowledge (read-only)
- ``emr_search`` / ``emr_fetch`` — aliases of search/fetch
- ``emr_remember`` / ``emr_upsert`` — draft writes when ``JARVIS_MCP_WRITE_ENABLED``
"""

from __future__ import annotations

import json
from typing import Any, Callable

PROTOCOL_VERSION = "2025-03-26"
PROTOCOL_VERSION_LEGACY = "2024-11-05"
SERVER_NAME = "jarvis-emr"
SERVER_VERSION = "0.3.0"

# (tool_name, arguments) → JSON-serializable result dict
EmrToolCaller = Callable[[str, dict[str, Any]], dict[str, Any]]

# Backward-compatible alias used by older call sites
EmrRecallCaller = Callable[[dict[str, Any]], dict[str, Any]]

EMR_RECALL_TOOL: dict[str, Any] = {
    "name": "emr_recall",
    "description": (
        "Governed recall bundle from EMR. Returns STM-ready memories with provenance "
        "and activation scores. Does not write, reinforce, or mutate ledger truth. "
        "May abstain when evidence is insufficient; surfaces unresolved conflicts."
    ),
    "annotations": {
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
    "inputSchema": {
        "type": "object",
        "properties": {
            "intent": {
                "description": "Recall wave: operation string or structured intent object",
                "oneOf": [
                    {"type": "string"},
                    {
                        "type": "object",
                        "properties": {
                            "operation": {"type": "string"},
                            "domain": {"type": "string"},
                            "project": {"type": "string"},
                            "authority_required": {"type": "string"},
                        },
                        "required": ["operation"],
                    },
                ],
            },
            "query": {
                "type": "string",
                "description": "Natural-language recall query",
            },
            "subjects": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Subject filters (e.g. image-signature, creative-style)",
            },
            "tags_any": {
                "type": "array",
                "items": {"type": "string"},
            },
            "types": {
                "type": "array",
                "items": {"type": "string"},
            },
            "statuses": {
                "type": "array",
                "items": {"type": "string"},
            },
            "max_memories": {
                "type": "integer",
                "minimum": 1,
                "maximum": 32,
                "default": 8,
            },
            "truth_scope": {
                "type": "string",
                "default": "live",
            },
            "session_key": {
                "type": "string",
                "default": "tool-emr-recall",
            },
            "include_provenance": {
                "type": "boolean",
                "default": True,
            },
        },
        "required": ["intent", "query"],
    },
}

EMR_LATEST_TOOL: dict[str, Any] = {
    "name": "emr_latest",
    "description": (
        "Newest-first discovery over the Continuity Ledger: call it with no id and no keyword to find the most "
        "recent memory records. Read-only. Superseded, archived and ai-twin records are excluded unless asked for. "
        "Returns records (id, created_at, type, status, provenance, supersedes, superseded_by, summary), "
        "next_cursor for paging, ledger_head and a result_digest that is identical for identical ledger state."
    ),
    "annotations": {
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
    "inputSchema": {
        "type": "object",
        "properties": {
            "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 10},
            "cursor": {"type": "string", "description": "next_cursor from the previous page"},
            "include_superseded": {"type": "boolean", "default": False},
            "include_archived": {"type": "boolean", "default": False},
            "include_twin": {"type": "boolean", "default": False},
            "type": {"type": "string", "description": "Only records of this type"},
        },
        "additionalProperties": False,
    },
}

EMR_SEARCH_LEDGER_TOOL: dict[str, Any] = {
    "name": "emr_search_ledger",
    "description": (
        "Ranked full-text search over the Continuity Ledger's own records (not files). Every word of the query must "
        "appear in the record (subject, content or tags); matches are ranked subject > tags > content, then newest "
        "first. Read-only. Superseded, archived and ai-twin records are excluded unless asked for. Returns records "
        "with id, created_at, status, provenance, supersedes/superseded_by, summary and score, plus result_digest."
    ),
    "annotations": {
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
    "inputSchema": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "minLength": 1, "maxLength": 500},
            "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 10},
            "include_superseded": {"type": "boolean", "default": False},
            "include_archived": {"type": "boolean", "default": False},
            "include_twin": {"type": "boolean", "default": False},
            "type": {"type": "string", "description": "Only records of this type"},
        },
        "required": ["query"],
        "additionalProperties": False,
    },
}

EMR_REMEMBER_TOOL: dict[str, Any] = {
    "name": "emr_remember",
    "description": (
        "Create a governed durable memory record via EMR. Writes to Continuity Ledger "
        "through constitutional gatekeeping. Requires user_requested=true; always draft. "
        "Host should requireApproval. Disabled unless JARVIS_MCP_WRITE_ENABLED=true."
    ),
    "annotations": {
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": False,
    },
    "inputSchema": {
        "type": "object",
        "properties": {
            "content": {"type": "string", "description": "Memory content"},
            "source_agent": {"type": "string", "description": "Agent writing the memory"},
            "session_id": {"type": "string", "description": "Session identifier"},
            "type": {
                "type": "string",
                "description": "MemoryType literal",
                "enum": [
                    "decision",
                    "fact",
                    "task",
                    "preference",
                    "architecture",
                    "research",
                ],
            },
            "subject": {"type": "string", "description": "Subject domain"},
            "tags": {"type": "array", "items": {"type": "string"}},
            "user_requested": {
                "type": "boolean",
                "description": "Must be true — explicit user intent to store",
            },
            "user_statement": {
                "type": "string",
                "description": "Verbatim user wording requesting storage",
            },
        },
        "required": ["content", "session_id", "type", "user_requested"],
    },
}

SEARCH_TOOL: dict[str, Any] = {
    "name": "search",
    "description": (
        "Read-only company knowledge search over the Continuity Ledger (EMR recall). "
        "Returns memory ids, titles, and citation URLs for deep-research hosts. "
        "Does not write, reinforce, or mutate ledger truth."
    ),
    "annotations": {
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
    "inputSchema": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Natural-language search query",
            },
            "max_memories": {
                "type": "integer",
                "minimum": 1,
                "maximum": 16,
                "default": 12,
            },
        },
        "required": ["query"],
    },
}

FETCH_TOOL: dict[str, Any] = {
    "name": "fetch",
    "description": (
        "Read-only fetch of one Continuity Ledger memory by id (from search). "
        "Returns full text, citation URL, and metadata. Does not mutate ledger truth."
    ),
    "annotations": {
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
    "inputSchema": {
        "type": "object",
        "properties": {
            "id": {
                "type": "string",
                "description": "Memory id from a prior search result",
            },
        },
        "required": ["id"],
    },
}

EMR_SEARCH_TOOL: dict[str, Any] = {
    **SEARCH_TOOL,
    "name": "emr_search",
    "description": (
        "Alias of ``search`` — read-only EMR company-knowledge search. "
        "Returns memory ids, titles, and citation URLs."
    ),
}

EMR_FETCH_TOOL: dict[str, Any] = {
    **FETCH_TOOL,
    "name": "emr_fetch",
    "description": (
        "Alias of ``fetch`` — read-only EMR memory fetch by id with full text and metadata."
    ),
}

EMR_UPSERT_TOOL: dict[str, Any] = {
    "name": "emr_upsert",
    "description": (
        "Update or supersede an existing memory record. EMR enforces lineage, "
        "provenance, and conflict membranes (new draft + archive prior; no destructive "
        "overwrite). Requires user_requested=true. Disabled unless JARVIS_MCP_WRITE_ENABLED=true."
    ),
    "annotations": {
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": False,
    },
    "inputSchema": {
        "type": "object",
        "properties": {
            "id": {"type": "string", "description": "Existing memory id being superseded"},
            "content": {"type": "string", "description": "Updated content"},
            "supersedes": {
                "type": "string",
                "description": "Optional id of record being superseded (defaults to id)",
            },
            "source_agent": {"type": "string"},
            "session_id": {"type": "string"},
            "type": {
                "type": "string",
                "enum": [
                    "decision",
                    "fact",
                    "task",
                    "preference",
                    "architecture",
                    "research",
                ],
            },
            "subject": {"type": "string"},
            "tags": {"type": "array", "items": {"type": "string"}},
            "user_requested": {
                "type": "boolean",
                "description": "Must be true — explicit user intent",
            },
            "user_statement": {"type": "string"},
        },
        "required": ["id", "content", "user_requested"],
    },
}

MCP_TOOLS: list[dict[str, Any]] = [
    EMR_RECALL_TOOL,
    EMR_LATEST_TOOL,
    EMR_SEARCH_LEDGER_TOOL,
    SEARCH_TOOL,
    FETCH_TOOL,
    EMR_SEARCH_TOOL,
    EMR_FETCH_TOOL,
    EMR_REMEMBER_TOOL,
    EMR_UPSERT_TOOL,
]

_KNOWN_TOOLS = frozenset(t["name"] for t in MCP_TOOLS)


class ToolRefusal(Exception):
    """A tool call refused for a reason with a stable machine-readable ``code``.

    Raised by tool callers; reported as ``isError`` with ``structuredContent.error.code`` so MCP
    clients can tell an unavailable ledger, a version conflict and a denial apart.
    """

    def __init__(self, code: str, message: str, reason: str | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.reason = reason


def handle_tools_call(
    params: dict[str, Any],
    call_tool: EmrToolCaller,
) -> dict[str, Any]:
    name = params.get("name")
    arguments = params.get("arguments") or {}
    if name not in _KNOWN_TOOLS:
        return {
            "content": [{"type": "text", "text": f"unknown tool: {name}"}],
            "isError": True,
        }
    try:
        result = call_tool(str(name), arguments if isinstance(arguments, dict) else {})
    except ToolRefusal as exc:
        return {
            "content": [{"type": "text", "text": exc.message}],
            "structuredContent": {"error": {"code": exc.code, **({"reason": exc.reason} if exc.reason else {})}},
            "isError": True,
        }
    except Exception as exc:  # noqa: BLE001 — surface as tool error to host
        return {
            "content": [{"type": "text", "text": str(exc)}],
            "isError": True,
        }
    # Write refusals are structured success (accepted=false), not transport errors
    return {
        "content": [{"type": "text", "text": json.dumps(result, indent=2)}],
        "structuredContent": result,
        "isError": False,
    }


def _initialize_result(params: dict[str, Any] | None) -> dict[str, Any]:
    requested = (params or {}).get("protocolVersion")
    if requested in (PROTOCOL_VERSION, PROTOCOL_VERSION_LEGACY):
        negotiated = requested
    else:
        negotiated = PROTOCOL_VERSION
    return {
        "protocolVersion": negotiated,
        "capabilities": {
            "tools": {"listChanged": False},
        },
        "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
        "instructions": (
            "EMR constitutional memory tools. "
            "Use search/fetch (or emr_search/emr_fetch) for OpenAI deep-research style "
            "company knowledge — read-only, citation URLs on every result. "
            "Use emr_latest to find the newest memory records with no id or keyword. "
            "Use emr_search_ledger for ranked word search over the ledger's own records. "
            "Use emr_recall for governed Continuity Ledger recall bundles (may abstain). "
            "Use emr_remember / emr_upsert only when the user explicitly asked to store "
            "or update memory (user_requested=true); writes are draft-only and may be "
            "disabled by JARVIS_MCP_WRITE_ENABLED. Never invent memories."
        ),
    }


def dispatch_rpc(
    message: dict[str, Any],
    call_tool: EmrToolCaller,
) -> dict[str, Any] | None:
    """Dispatch one JSON-RPC MCP message.

    Returns a JSON-RPC response dict, or ``None`` for notifications (no body).
    """
    method = message.get("method")
    request_id = message.get("id")
    params = message.get("params") or {}

    # Notifications have no id (or explicitly null) and return no response body.
    is_notification = "id" not in message or message.get("id") is None

    if method == "notifications/initialized":
        return None
    if method == "notifications/cancelled":
        return None

    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": _initialize_result(params if isinstance(params, dict) else {}),
        }

    if method == "tools/list":
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {"tools": list(MCP_TOOLS)},
        }

    if method == "tools/call":
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": handle_tools_call(
                params if isinstance(params, dict) else {},
                call_tool,
            ),
        }

    if method == "ping":
        return {"jsonrpc": "2.0", "id": request_id, "result": {}}

    if is_notification:
        return None

    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": -32601, "message": f"Unknown method: {method}"},
    }


def is_jsonrpc_request(message: dict[str, Any]) -> bool:
    return "id" in message and message.get("id") is not None and "method" in message


def wrap_recall_caller(call_emr_recall: EmrRecallCaller) -> EmrToolCaller:
    """Adapt legacy recall-only callers to the multi-tool dispatcher."""

    def _call(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if name != "emr_recall":
            raise RuntimeError(f"tool {name} not supported by this caller")
        return call_emr_recall(arguments)

    return _call
