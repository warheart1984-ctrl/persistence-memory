"""Stdio MCP server for the Jarvis Continuity Ledger's plain API: health, recall, get, and a guarded write.

For agents that can run a local MCP server (Grok, Codex, Cursor, ...). It talks to the ledger over HTTP:

    JARVIS_MEMORYBOARD_URL   required; there is NO default address (e.g. http://127.0.0.1:8011 through a tunnel)
    JARVIS_API_KEY_FILE      the key file (first line), or JARVIS_API_KEY for the key itself
    JARVIS_LEDGER_MCP_WRITE  set to 1 to offer the write tool at all (default: off)

Fail closed: without the URL or the key every tool refuses and no request is made. The key is only ever sent
over https or to this machine (loopback), is never printed, and never appears in a tool result or an error.

Tools (namespaced by the host with the server name, e.g. ``jarvis-ledger__recall``):

    health   liveness and readiness
    recall   list memories (default 50, up to 200); live ones only unless truth_scope says otherwise
    get      one memory by id
    write    store ONE draft DECISION (nothing else: Clause V keeps preferences, tasks and chat out of the ledger).
             Only offered when JARVIS_LEDGER_MCP_WRITE=1; needs the user's own wording in ``user_requested``, which is
             kept as the decision's evidence; refuses anything that looks like a credential; source_agent is fixed to
             JARVIS_LEDGER_MCP_SOURCE (default ``grok-bot``). The ledger applies its own Clause V gate on top.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

try:
    from mcp_server.jarvis_keyfile import KeyFileError, read_key_file
except ImportError:  # run as a script (python mcp_server/ledger_stdio.py): load the sibling module
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from jarvis_keyfile import KeyFileError, read_key_file  # type: ignore[no-redef]

PROTOCOL_VERSION = "2025-03-26"
PROTOCOL_VERSION_LEGACY = "2024-11-05"
SERVER_NAME = "jarvis-ledger"
SERVER_VERSION = "1.0.0"

TIMEOUT_SEC = 20.0
MAX_LIMIT = 200
DEFAULT_LIMIT = 50
TRUTH_SCOPES = ("live", "all", "archived")  # live = everything except archived records (the default)
MAX_CONTENT = 1900  # the API limit is 2000
_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

NO_URL_MESSAGE = (
    "JARVIS_MEMORYBOARD_URL is not set. Set it to the ledger you mean, for example http://127.0.0.1:8011 "
    "through an SSH tunnel. There is no default address, so nothing is sent (and the API key is never sent) "
    "until you choose one."
)
NO_KEY_MESSAGE = (
    "No API key: set JARVIS_API_KEY_FILE to the ledger key file (or JARVIS_API_KEY). Nothing was sent."
)


class Refusal(Exception):
    """A tool call refused before or after talking to the ledger, with a stable code."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


# --- configuration (read at call time, so a fixed environment is honoured and tests can change it) ---------

def _base_url() -> str:
    url = (os.environ.get("JARVIS_MEMORYBOARD_URL") or "").strip().rstrip("/")
    if not url:
        raise Refusal("no_url", NO_URL_MESSAGE)
    return url


def _api_key() -> str:
    key = (os.environ.get("JARVIS_API_KEY") or "").strip()
    if not key:
        path = (os.environ.get("JARVIS_API_KEY_FILE") or "").strip()
        if path:
            try:
                key = read_key_file(path)
            except KeyFileError as exc:
                raise Refusal("no_key", f"JARVIS_API_KEY_FILE names {path!r}, which {exc}. Nothing was sent.") from None
    if not key:
        raise Refusal("no_key", NO_KEY_MESSAGE)
    return key


def write_enabled() -> bool:
    return os.environ.get("JARVIS_LEDGER_MCP_WRITE", "").strip().lower() in ("1", "true", "yes", "on")


def _source_agent() -> str:
    return (os.environ.get("JARVIS_LEDGER_MCP_SOURCE") or "grok-bot").strip()[:128] or "grok-bot"


def _key_may_travel(url: str) -> bool:
    parsed = urllib.parse.urlparse(url)
    return parsed.scheme == "https" or (parsed.hostname or "") in ("127.0.0.1", "localhost", "::1")


# --- who is calling (self-reported; the ledger's call log records it as such) ------------------------------------------------
_CLIENT = {"name": "ledger_stdio", "version": None}


def _client_header() -> str:
    """'name/version' for X-Jarvis-MCP-Client: the host's MCP clientInfo when it sent one, else this adapter's own name."""
    def clean(text: object) -> str:
        return "".join(ch if ch.isprintable() and not ch.isspace() else "_" for ch in str(text or ""))[:64].encode("latin-1", "replace").decode("latin-1")

    name = clean(_CLIENT["name"]) or "ledger_stdio"
    version = clean(_CLIENT["version"])
    return f"{name}/{version}" if version else name


def _note_client(message: dict[str, Any]) -> None:
    if message.get("method") == "initialize":
        info = (message.get("params") or {}).get("clientInfo")
        if isinstance(info, dict) and info.get("name"):
            _CLIENT["name"], _CLIENT["version"] = info.get("name"), info.get("version")


# --- HTTP ---------------------------------------------------------------------------------------------------

def _request(method: str, path: str, body: dict[str, Any] | None = None, *, tolerate: tuple[int, ...] = ()) -> tuple[int, Any]:
    """One request to the ledger. Checks URL and key first; never puts the key in an error."""
    base = _base_url()
    key = _api_key()
    url = f"{base}{path}"
    if not _key_may_travel(url):
        raise Refusal(
            "key_not_allowed",
            "refusing to send the API key over plain http to a non-loopback host; use https or an SSH tunnel to 127.0.0.1",
        )
    headers = {"Accept": "application/json", "X-API-Key": key, "X-Jarvis-MCP-Transport": "stdio", "X-Jarvis-MCP-Client": _client_header()}
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_SEC) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        text = exc.read().decode("utf-8", errors="replace")
        if exc.code == 422:
            try:
                refusal = json.loads(text)
            except json.JSONDecodeError:
                refusal = {}
            if isinstance(refusal, dict) and refusal.get("code") == "clause_v_violation":
                reasons = ", ".join(str(r.get("code")) for r in refusal.get("reasons", []) if isinstance(r, dict))
                raise Refusal("clause_v_violation", f"the ledger refused this write under Clause V ({reasons}); nothing was stored") from None
        if exc.code in tolerate:
            try:
                return exc.code, json.loads(text)
            except json.JSONDecodeError:
                return exc.code, {"detail": text[:300]}
        raise Refusal(f"http_{exc.code}", f"ledger returned HTTP {exc.code}: {_scrub(text[:300], key)}") from None
    except urllib.error.URLError as exc:
        raise Refusal(
            "unreachable",
            f"ledger unreachable at {base}: {_scrub(str(exc.reason), key)}. Is the SSH tunnel up?",
        ) from None


def _scrub(text: str, key: str) -> str:
    return text.replace(key, "<key>") if key else text


# --- tools --------------------------------------------------------------------------------------------------

def _truncate(text: str, limit: int) -> str:
    if limit <= 0 or len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"


def tool_health(_args: dict[str, Any]) -> dict[str, Any]:
    _, health = _request("GET", "/health")
    status, ready = _request("GET", "/ready", tolerate=(503,))
    return {
        "health": health.get("status"),
        "ready": status == 200 and ready.get("status") == "ready",
        "ready_detail": ready,
        "memory_write_enabled": health.get("memory_write_enabled"),
    }


def tool_recall(args: dict[str, Any]) -> dict[str, Any]:
    limit = args.get("limit", DEFAULT_LIMIT)
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_LIMIT:
        raise Refusal("bad_argument", f"limit must be an integer from 1 to {MAX_LIMIT}")
    scope = args.get("truth_scope", "live")
    if scope not in TRUTH_SCOPES:
        raise Refusal("bad_argument", f"truth_scope must be one of {', '.join(TRUTH_SCOPES)}")
    params: dict[str, str] = {"limit": str(limit), "with_provenance": "false"}
    if scope != "all":  # live (the default) leaves archived records out; archived returns only those
        params["truth_scope"] = scope
    for name in ("query", "type", "status", "subject"):
        value = args.get(name)
        if value is not None:
            if not isinstance(value, str) or len(value) > 500:
                raise Refusal("bad_argument", f"{name} must be a string of at most 500 characters")
            params[name] = value
    chars = args.get("content_chars", 400)
    if isinstance(chars, bool) or not isinstance(chars, int) or chars < 0:
        raise Refusal("bad_argument", "content_chars must be an integer >= 0 (0 means full text)")
    _, body = _request("GET", "/api/jarvis/memory?" + urllib.parse.urlencode(params))
    memories = body.get("memories", [])
    records = []
    for m in memories:
        record = {k: m.get(k) for k in ("id", "type", "status", "source_agent", "subject", "tags", "confidence", "created_at", "version", "content_sha256")}
        content = m.get("content") or ""
        record["content"] = _truncate(content, chars)
        record["truncated"] = bool(chars) and len(content) > chars
        records.append(record)
    return {"count": len(records), "limit": limit, "capped": len(records) >= limit, "truth_scope": scope, "memories": records}


def tool_get(args: dict[str, Any]) -> dict[str, Any]:
    memory_id = args.get("id")
    if not isinstance(memory_id, str) or not _ID_RE.match(memory_id):
        raise Refusal("bad_argument", "id must be a memory id such as mem-1a2b3c4d5e6f")
    status, body = _request("GET", f"/api/jarvis/memory/{urllib.parse.quote(memory_id, safe='')}", tolerate=(404,))
    if status == 404:
        raise Refusal("not_found", f"no memory with id {memory_id}")
    return {"memory": body.get("memory")}


def _find_secrets(text: str) -> list[str]:
    """The hooks' secret filter, loaded from agent-hooks/ (a folder name Python cannot import by name)."""
    path = Path(__file__).resolve().parents[1] / "agent-hooks" / "jarvis_common.py"
    spec = importlib.util.spec_from_file_location("jarvis_hooks_common_for_mcp", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("secret filter not found")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.find_secrets(text)


def tool_write(args: dict[str, Any]) -> dict[str, Any]:
    if not write_enabled():
        raise Refusal("write_disabled", "writing is switched off (JARVIS_LEDGER_MCP_WRITE is not 1); nothing was sent")
    content = args.get("content")
    mem_type = args.get("type")
    wording = args.get("user_requested")
    session_id = args.get("session_id") or "grok-bot-session"
    if not isinstance(content, str) or not content.strip() or len(content) > MAX_CONTENT:
        raise Refusal("bad_argument", f"content must be a non-empty string of at most {MAX_CONTENT} characters")
    if mem_type != "decision":
        raise Refusal("bad_argument", "type must be 'decision': the ledger keeps decisions and evidence, not facts, preferences or notes")
    if not isinstance(wording, str) or len(wording.strip()) < 8:
        raise Refusal("user_request_required", "user_requested must quote the user's own words asking to store this; nothing was sent")
    if not isinstance(session_id, str) or not session_id.strip() or len(session_id) > 128:
        raise Refusal("bad_argument", "session_id must be a string of at most 128 characters")
    try:
        matched = _find_secrets("\n".join([content, wording, session_id]))
    except Exception:  # noqa: BLE001 - the filter must never be the reason a secret is sent
        matched = ["filter-error"]
    if matched:
        raise Refusal("secret_refused", f"not stored: the text looks like it contains a credential ({', '.join(matched)}); nothing was sent")
    body = {
        "content": content.strip(),
        "source_agent": _source_agent(),
        "session_id": session_id.strip(),
        "type": mem_type,
        "confidence": 0.5,
        "status": "draft",
        "tags": [_source_agent(), "user-requested"],
        "evidence": [{"kind": "user-request", "ref": "mcp:jarvis-ledger/write", "note": wording.strip()[:400]}],
    }
    _, created = _request("POST", "/api/jarvis/memory", body)
    memory = created.get("memory", {})
    out = {"stored": True, "id": memory.get("id"), "type": memory.get("type"), "status": memory.get("status"), "source_agent": memory.get("source_agent")}
    if created.get("clause_v_warnings"):  # warn-only findings from the ledger's Clause V gate, passed on to the caller
        out["clause_v_warnings"] = created["clause_v_warnings"]
    return out


_READ_ONLY = {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}

TOOLS: dict[str, dict[str, Any]] = {
    "health": {
        "description": "Is the Jarvis Continuity Ledger up and ready? Read-only.",
        "annotations": _READ_ONLY,
        "inputSchema": {"type": "object", "properties": {}},
        "run": tool_health,
    },
    "recall": {
        "description": (
            "List live memories from the Jarvis Continuity Ledger, newest first. Archived records are left out unless "
            "you ask for truth_scope 'all' or 'archived'. Default 50, at most 200. Optional filters: query, type, "
            "status, subject. Content is shortened to content_chars (default 400; 0 = full text). Read-only. Say so "
            "when the result is capped."
        ),
        "annotations": _READ_ONLY,
        "inputSchema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "minimum": 1, "maximum": MAX_LIMIT, "default": DEFAULT_LIMIT},
                "query": {"type": "string", "description": "Optional text to match"},
                "type": {"type": "string", "description": "Optional memory type, e.g. fact, decision"},
                "status": {"type": "string", "description": "Optional status, e.g. draft, verified"},
                "subject": {"type": "string"},
                "content_chars": {"type": "integer", "minimum": 0, "default": 400},
                "truth_scope": {"type": "string", "enum": list(TRUTH_SCOPES), "default": "live"},
            },
        },
        "run": tool_recall,
    },
    "get": {
        "description": "Fetch one memory by id (for example from a recall result). Read-only.",
        "annotations": _READ_ONLY,
        "inputSchema": {
            "type": "object",
            "properties": {"id": {"type": "string", "description": "Memory id, e.g. mem-1a2b3c4d5e6f"}},
            "required": ["id"],
        },
        "run": tool_get,
    },
    "write": {
        "description": (
            "Store ONE draft DECISION in the ledger (decisions only; not facts, preferences, tasks or conversation). Use "
            "ONLY when the user explicitly asked you to store a decision, and quote their words in user_requested. "
            "Never store credentials. Never invent decisions. Refused unless the user has switched writing on."
        ),
        "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": False},
        "inputSchema": {
            "type": "object",
            "properties": {
                "content": {"type": "string", "maxLength": MAX_CONTENT},
                "type": {"type": "string", "enum": ["decision"]},
                "user_requested": {"type": "string", "description": "The user's own words asking you to store this"},
                "session_id": {"type": "string", "description": "Optional conversation or session id"},
            },
            "required": ["content", "type", "user_requested"],
        },
        "run": tool_write,
    },
}


def _listed_tools() -> list[dict[str, Any]]:
    tools = []
    for name, spec in TOOLS.items():
        if name == "write" and not write_enabled():
            continue  # not even offered unless the user switched writing on
        tools.append({k: v for k, v in spec.items() if k != "run"} | {"name": name})
    return tools


def handle_tools_call(params: dict[str, Any]) -> dict[str, Any]:
    name = params.get("name")
    arguments = params.get("arguments") or {}
    spec = TOOLS.get(name) if isinstance(name, str) else None
    if spec is None or (name == "write" and not write_enabled()):
        return {"content": [{"type": "text", "text": f"unknown or disabled tool: {name}"}], "isError": True}
    try:
        result = spec["run"](arguments if isinstance(arguments, dict) else {})
    except Refusal as exc:
        return {
            "content": [{"type": "text", "text": exc.message}],
            "structuredContent": {"error": {"code": exc.code}},
            "isError": True,
        }
    except Exception as exc:  # noqa: BLE001 - surface as a tool error to the host
        return {"content": [{"type": "text", "text": f"{type(exc).__name__}: {exc}"}], "isError": True}
    return {"content": [{"type": "text", "text": json.dumps(result, indent=2)}], "structuredContent": result, "isError": False}


def dispatch(message: dict[str, Any]) -> dict[str, Any] | None:
    _note_client(message)
    method = message.get("method")
    request_id = message.get("id")
    params = message.get("params") if isinstance(message.get("params"), dict) else {}
    if request_id is None:
        return None  # notifications get no reply
    if method == "initialize":
        requested = params.get("protocolVersion")
        negotiated = requested if requested in (PROTOCOL_VERSION, PROTOCOL_VERSION_LEGACY) else PROTOCOL_VERSION
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {
                "protocolVersion": negotiated,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
                "instructions": (
                    "Jarvis Continuity Ledger. Use recall to read memories and get to fetch one by id. Only use "
                    "write when the user explicitly asks you to store something, and never store credentials."
                ),
            },
        }
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": request_id, "result": {"tools": _listed_tools()}}
    if method == "tools/call":
        return {"jsonrpc": "2.0", "id": request_id, "result": handle_tools_call(params)}
    if method == "ping":
        return {"jsonrpc": "2.0", "id": request_id, "result": {}}
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32601, "message": f"Unknown method: {method}"}}


def _send(message: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(message, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def run_stdio() -> None:
    try:
        _base_url()
        _api_key()
    except Refusal as exc:
        print(f"jarvis-ledger MCP: {exc.message}", file=sys.stderr, flush=True)  # still serve: tools will refuse
    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError as exc:
            print(f"invalid JSON: {exc}", file=sys.stderr, flush=True)
            continue
        if not isinstance(message, dict):
            continue
        response = dispatch(message)
        if response is not None:
            _send(response)


if __name__ == "__main__":
    run_stdio()
