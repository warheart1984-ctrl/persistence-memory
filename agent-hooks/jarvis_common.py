"""Shared helpers for Jarvis Continuity Ledger Cursor hooks."""

from __future__ import annotations

import json
import math
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

TIMEOUT_SEC = 4.0
MAX_CONTEXT_CHARS = 12000
MAX_MEMORY_CONTENT = 1900  # API limit is 2000


class BaseURLNotSet(RuntimeError):
    """No ledger URL was configured. There is deliberately no default."""


NO_BASE_URL_MESSAGE = (
    "JARVIS_MEMORYBOARD_URL is not set. Set it to the ledger you mean, for example "
    "http://127.0.0.1:8011 through an SSH tunnel. There is no default address, so nothing is sent "
    "(and the API key is never sent) until you choose one."
)


def configured_base_url() -> str | None:
    """The explicitly configured ledger URL, or None. Never guesses."""
    url = (
        os.environ.get("JARVIS_MEMORYBOARD_URL")
        or os.environ.get("DIRECTOR_MEMORYBOARD_BASE_URL")
        or ""
    ).strip()
    return url.rstrip("/") or None


def base_url() -> str:
    url = configured_base_url()
    if url is None:
        raise BaseURLNotSet(NO_BASE_URL_MESSAGE)
    return url


def _read_key_file(path: str) -> str | None:
    """First line of a key file saved as UTF-8, UTF-8 with BOM or UTF-16 (what Windows tools write), or None.

    The same logic as mcp_server/jarvis_keyfile.py (a test keeps them in step). Anything unusable gives None: the
    hook then sends no key rather than a wrong one, and never crashes on the file's encoding.
    """
    try:
        raw = Path(path).read_bytes()
        if raw.startswith(b"\xef\xbb\xbf"):
            text = raw.decode("utf-8-sig")
        elif raw.startswith((b"\xff\xfe", b"\xfe\xff")):
            text = raw.decode("utf-16")
        elif b"\x00" in raw:
            if len(raw) >= 2 and raw[0] != 0 and raw[1] == 0:
                text = raw.decode("utf-16-le")
            elif len(raw) >= 2 and raw[0] == 0 and raw[1] != 0:
                text = raw.decode("utf-16-be")
            else:
                return None
        else:
            text = raw.decode("utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    lines = text.lstrip("\ufeff").strip().splitlines()
    key = lines[0].strip() if lines else ""
    if not key or not key.isascii() or not key.isprintable() or any(ch.isspace() for ch in key):
        return None
    return key


def api_key() -> str | None:
    """The ledger API key, if configured: JARVIS_API_KEY, else the first line of JARVIS_API_KEY_FILE."""
    key = (os.environ.get("JARVIS_API_KEY") or "").strip()
    if key:
        return key
    path = (os.environ.get("JARVIS_API_KEY_FILE") or "").strip()
    if path:
        return _read_key_file(path)
    return None


def _key_may_travel(url: str) -> bool:
    """A secret only goes over https, or to this machine (e.g. through an SSH tunnel)."""
    parsed = urllib.parse.urlparse(url)
    return parsed.scheme == "https" or (parsed.hostname or "") in ("127.0.0.1", "localhost", "::1")


# --- secret filter for what the hooks send to the ledger ----------------------------------------------------
# A tripwire, not a guarantee: it recognises the common shapes of credentials (and this ledger's own API key,
# exactly). It returns the NAMES of the patterns that matched, never the matched text, so a refusal notice
# cannot leak the thing it refused. The ledger MCP server's write tool refuses to store anything that matches.

_SECRET_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("private-key-block", re.compile(r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY(?: BLOCK)?-----")),
    ("age-secret-key", re.compile(r"AGE-SECRET-KEY-1[A-Z0-9]{20,}")),
    ("openai-style-key", re.compile(r"\bsk-(?:ant-|proj-)?[A-Za-z0-9_-]{20,}")),
    ("github-token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,})")),
    ("gitlab-token", re.compile(r"\bglpat-[A-Za-z0-9_-]{20,}")),
    ("slack-token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}")),
    ("aws-access-key-id", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("google-api-key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}")),
    ("bearer-token", re.compile(r"\bbearer\s+[A-Za-z0-9._~+/=-]{20,}", re.IGNORECASE)),
    ("authorization-header", re.compile(r"\bauthorization\s*:\s*(?:basic|bearer|token)\s+\S{8,}", re.IGNORECASE)),
    (
        "secret-assignment",
        re.compile(
            r"""\b(?:api[_-]?key|secret(?:[_-]?key)?|access[_-]?token|auth[_-]?token|token|passw(?:or)?d|passwd|pwd|"""
            r"""client[_-]?secret|private[_-]?key)\b["']?\s*[:=]\s*["']?[^\s"',;]{6,}""",
            re.IGNORECASE,
        ),
    ),
    ("url-credentials", re.compile(r"\b[a-z][a-z0-9+.-]*://[^/\s:@]+:[^/\s@]{3,}@", re.IGNORECASE)),
]

_ENTROPY_CANDIDATE = re.compile(r"[A-Za-z0-9+/_-]{40,}")


def _shannon_entropy(text: str) -> float:
    counts: dict[str, int] = {}
    for ch in text:
        counts[ch] = counts.get(ch, 0) + 1
    return -sum((n / len(text)) * math.log2(n / len(text)) for n in counts.values())


def _own_secrets() -> list[str]:
    """This ledger's own credentials, so they are caught even in a shape no pattern knows."""
    values = [
        os.environ.get("JARVIS_API_KEY"),
        os.environ.get("EMR_RECALL_API_KEY"),
        api_key(),
    ]
    return sorted({v.strip() for v in values if v and len(v.strip()) >= 8})


def find_secrets(text: str, *, entropy: bool | None = None) -> list[str]:
    """Names of the secret patterns found in ``text`` (empty list: nothing matched). Never returns the matches.

    The high-entropy rule is off unless ``entropy=True`` or JARVIS_HOOK_SECRET_ENTROPY=1: long random-looking
    strings are legitimate in a ledger (hashes, ids), so it is opt-in. Pure hex is never flagged by it.
    """
    found: set[str] = set()
    for name, pattern in _SECRET_PATTERNS:
        if pattern.search(text):
            found.add(name)
    if any(secret in text for secret in _own_secrets()):
        found.add("ledger-api-key")
    if entropy is None:
        entropy = os.environ.get("JARVIS_HOOK_SECRET_ENTROPY", "").strip().lower() in ("1", "true", "yes", "on")
    if entropy:
        for candidate in _ENTROPY_CANDIDATE.findall(text):
            if re.fullmatch(r"[0-9a-fA-F]+", candidate):
                continue
            if _shannon_entropy(candidate) >= 4.3 and any(c.isdigit() for c in candidate) and any(c.isalpha() for c in candidate):
                found.add("high-entropy-string")
                break
    return sorted(found)


def refusal_log_path() -> Path:
    return state_dir() / "jarvis-hook-refusals.log"


def log_refusal(hook: str, session_id: str, names: list[str]) -> str:
    """Record that a hook refused to send something. Writes pattern names only, never the matched text."""
    safe_session = re.sub(r"[^A-Za-z0-9_.-]", "_", str(session_id))[:64]
    if find_secrets(str(session_id)) or find_secrets(safe_session):
        safe_session = "<redacted>"  # the id is caller-supplied text: it must not carry a secret into the log
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    line = f"{stamp} {hook} session={safe_session} not sent; matched: {', '.join(names)}"
    try:
        with refusal_log_path().open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    except OSError:
        pass
    return line


def repo_root() -> Path:
    # agent-hooks/ -> the repository root -> the folder that holds the checkout (where a workspace's .cursor/ lives)
    return Path(__file__).resolve().parents[2]


def state_dir() -> Path:
    d = repo_root() / ".cursor" / "hooks" / "state"
    d.mkdir(parents=True, exist_ok=True)
    return d


def context_path() -> Path:
    return state_dir() / "jarvis-live-context.md"


def session_meta_path() -> Path:
    return state_dir() / "jarvis-session-meta.json"


def read_stdin_json() -> dict[str, Any]:
    raw = sys.stdin.read()
    if not raw.strip():
        return {}
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError:
        return {}


def http_json(method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
    url = f"{base_url()}{path}"
    data = None
    # Self-reported, so the ledger's call log can say which hook made the call.
    headers = {"Accept": "application/json", "X-Jarvis-MCP-Client": "jarvis-hook-" + (os.path.splitext(os.path.basename(sys.argv[0] or "hook"))[0] or "hook")[:48]}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    key = api_key()
    if key:
        if not _key_may_travel(url):
            raise RuntimeError(
                "refusing to send the API key over plain http to a non-loopback host; "
                "use https or an SSH tunnel to 127.0.0.1"
            )
        headers["X-API-Key"] = key
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=TIMEOUT_SEC) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
        return payload if isinstance(payload, dict) else {}


def try_http_json(
    method: str, path: str, body: dict[str, Any] | None = None
) -> tuple[dict[str, Any] | None, str | None]:
    try:
        return http_json(method, path, body), None
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:300]
        return None, f"HTTP {exc.code}: {detail}"
    except Exception as exc:  # noqa: BLE001 — fail-open for hooks
        return None, str(exc)


def emit(obj: dict[str, Any]) -> None:
    # Windows hook hosts often use cp1252; force UTF-8 for JSON payloads.
    payload = json.dumps(obj, ensure_ascii=False)
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        pass
    try:
        sys.stdout.write(payload)
    except UnicodeEncodeError:
        sys.stdout.buffer.write(payload.encode("utf-8"))
    sys.stdout.flush()


def truncate(text: str, limit: int) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 20)].rstrip() + "\n…[truncated]"


def format_live_context(
    board: dict[str, Any] | None,
    memories: list[dict[str, Any]],
    *,
    selections: list[dict[str, Any]] | None = None,
    conflicts: list[dict[str, Any]] | None = None,
    error: str | None = None,
) -> str:
    lines: list[str] = [
        "# Jarvis Continuity Ledger — live cross-chat context",
        "",
        "Evidence-backed decisions/facts (not chat dumps). Consumers decide independently.",
        "Prefer POST type=decision with evidence + session_id; resolve conflicts via supersedes/status.",
        "",
        f"Base URL: `{configured_base_url() or '(not set)'}`",
        "",
    ]
    if error:
        lines.append(f"**Service unavailable:** {error}")
        if configured_base_url() is not None:
            lines.append("Check that the ledger is running and that the SSH tunnel (if you use one) is up.")
        lines.append("")
        return "\n".join(lines)

    board_obj = (board or {}).get("memory_board") or board or {}
    summary = (board_obj.get("summary") or "").strip() or "(empty)"
    board_id = board_obj.get("board_id") or "default_board"
    lines.extend(
        [
            f"## Board (`{board_id}`)",
            "",
            summary,
            "",
            f"## Live ledger entries ({len(memories)})",
            "",
        ]
    )
    if not memories:
        lines.append("_No live memories._")
    else:
        sel_by_id = {
            s.get("memory_id"): s for s in (selections or []) if isinstance(s, dict)
        }
        for mem in memories:
            mid = mem.get("id", "?")
            mtype = mem.get("type") or mem.get("category") or "?"
            status = mem.get("status") or "?"
            src = mem.get("source_agent") or "?"
            sess = mem.get("session_id") or "?"
            content = (mem.get("content") or "").replace("\n", " ").strip()
            if len(content) > 220:
                content = content[:217] + "..."
            why = (sel_by_id.get(mid) or {}).get("why_selected") or ""
            why_bit = f" | why: {why[:120]}" if why else ""
            lines.append(
                f"- `{mid}` ({mtype}/{status} from {src} sess={sess}): {content}{why_bit}"
            )
    if conflicts:
        lines.extend(["", "## Unresolved conflicts (do not merge)", ""])
        for c in conflicts:
            if not c.get("unresolved"):
                continue
            subj = c.get("subject") or "?"
            ids = ", ".join(m.get("id", "?") for m in (c.get("memories") or []))
            lines.append(
                f"- subject=`{subj}` ids=[{ids}] — resolve via supersedes or archive"
            )
    lines.extend(
        [
            "",
            "## Agent obligations",
            "",
            "1. Prefer verified decisions/architecture over draft session facts.",
            "2. Before ending substantive work, POST type=decision with source_agent, session_id, evidence.",
            "3. Never silently merge conflicts; use supersedes / status=archived.",
            "4. PATCH `/api/jarvis/memory/board` when durable workspace state changes.",
            "",
        ]
    )
    return truncate("\n".join(lines), MAX_CONTEXT_CHARS)


def write_context_file(text: str) -> Path:
    path = context_path()
    path.write_text(text, encoding="utf-8")
    return path
