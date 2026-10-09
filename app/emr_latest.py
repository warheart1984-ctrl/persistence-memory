"""``emr_latest``: newest-memory discovery.  One service function behind both the HTTP route and the MCP tool.

Ordering, filtering, paging, status derivation, the cursor and the digest all live here and nowhere else.  The
operation is read-only: it calls only ``list_latest`` (and, for the head, a read-only counter) on the tenant's store.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.models import MemoryRecord
from app.refusal import AUTHORITY_DENIED, LATEST_PATHS  # noqa: F401  (re-exported)
from app.ts import norm_ts, parse_utc

DEFAULT_LIMIT = 10
MIN_LIMIT = 1
MAX_LIMIT = 50
CURSOR_INFO = b"emr-latest-cursor-v1"

# Uppercase spec reasons travel in ``error.reason``; the stable lowercase ``code`` is unchanged.
LIMIT_OUT_OF_RANGE = "LIMIT_OUT_OF_RANGE"
CURSOR_INVALID = "CURSOR_INVALID"
TENANT_UNRESOLVED = "TENANT_UNRESOLVED"
CURSOR_KEY_UNAVAILABLE = "CURSOR_KEY_UNAVAILABLE"



class LatestError(Exception):
    """A refusal with HTTP status, the stable lowercase ``code`` and the uppercase ``reason``."""

    def __init__(self, status: int, code: str, reason: str, detail: str):
        super().__init__(detail)
        self.status, self.code, self.reason, self.detail = status, code, reason, detail


@dataclass(frozen=True)
class LatestParams:
    limit: int = DEFAULT_LIMIT
    cursor: str | None = None
    include_superseded: bool = False
    include_archived: bool = False
    include_twin: bool = False
    type: str | None = None


def parse_limit(value: Any) -> int:
    """Strict: an int (not a bool, not a numeric string) in 1..50, else ``LIMIT_OUT_OF_RANGE``; never clamped."""
    if value is None:
        return DEFAULT_LIMIT
    if isinstance(value, bool) or not isinstance(value, int) or not MIN_LIMIT <= value <= MAX_LIMIT:
        raise LatestError(422, "invalid_request", LIMIT_OUT_OF_RANGE, f"limit must be an integer from {MIN_LIMIT} to {MAX_LIMIT}")
    return value


# ---------------------------------------------------------------------------------------------------------------- cursor


def _hkdf_sha256(secret: bytes, info: bytes, length: int = 32) -> bytes:
    """RFC 5869 extract-and-expand (no salt), so the cursor key is bound to this one purpose."""
    prk = hmac.new(b"\x00" * 32, secret, hashlib.sha256).digest()
    okm, block, counter = b"", b"", 1
    while len(okm) < length:
        block = hmac.new(prk, block + info + bytes([counter]), hashlib.sha256).digest()
        okm += block
        counter += 1
    return okm[:length]


def cursor_key() -> bytes:
    """JARVIS_CURSOR_HMAC_KEY (or ``_FILE``), else derived from the API key for cursors only; fail closed if neither."""
    explicit = (os.getenv("JARVIS_CURSOR_HMAC_KEY") or "").strip()
    key_file = (os.getenv("JARVIS_CURSOR_HMAC_KEY_FILE") or "").strip()
    if not explicit and key_file:
        try:
            explicit = Path(key_file).read_text(encoding="utf-8").strip()
        except OSError:
            explicit = ""
    if explicit:
        return _hkdf_sha256(explicit.encode("utf-8"), CURSOR_INFO)
    from app.auth import configured_api_key  # local: auth imports the stores' consumers

    api_key = configured_api_key()
    if api_key:
        return _hkdf_sha256(api_key.encode("utf-8"), CURSOR_INFO)
    raise LatestError(
        503, "unavailable", CURSOR_KEY_UNAVAILABLE,
        "emr_latest needs JARVIS_CURSOR_HMAC_KEY (or JARVIS_API_KEY) to sign cursors",
    )


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _scope(tenant: str, p: LatestParams) -> str:
    """Binds a cursor to the tenant and the filters it was issued for."""
    blob = json.dumps(
        [tenant, p.type, p.include_superseded, p.include_archived, p.include_twin], separators=(",", ":"), ensure_ascii=False
    )
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:24]


def encode_cursor(key: bytes, tenant: str, p: LatestParams, created_at: str, record_id: str) -> str:
    body = _b64(json.dumps({"c": norm_ts(created_at), "i": record_id, "s": _scope(tenant, p)}, separators=(",", ":")).encode())
    return f"{body}.{_b64(hmac.new(key, body.encode('ascii'), hashlib.sha256).digest())}"


def decode_cursor(key: bytes, tenant: str, p: LatestParams, token: str) -> tuple[Any, str]:
    bad = LatestError(422, "invalid_request", CURSOR_INVALID, "cursor is invalid")
    try:
        body, tag = token.split(".")
        expected = hmac.new(key, body.encode("ascii"), hashlib.sha256).digest()
        if not hmac.compare_digest(expected, _unb64(tag)):
            raise bad
        data = json.loads(_unb64(body))
        if not isinstance(data, dict) or data.get("s") != _scope(tenant, p):
            raise bad
        record_id = data["i"]
        if not isinstance(record_id, str) or not record_id:
            raise bad
        return parse_utc(data["c"]), record_id
    except LatestError:
        raise
    except Exception as exc:  # malformed base64/JSON/timestamp: all the same refusal
        raise bad from exc


# ---------------------------------------------------------------------------------------------------------------- shaping


def result_digest(rows: list[tuple[str, str, str]]) -> str:
    """sha256 over canonical JSON of ``[(id, created_at, status)]`` in returned order."""
    blob = json.dumps([list(r) for r in rows], separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _summary(rec: MemoryRecord) -> str:
    if rec.subject and rec.subject.strip():
        return rec.subject.strip()[:200]
    for line in rec.content.splitlines():
        if line.strip():
            return line.strip()[:200]
    return ""


def _status(rec: MemoryRecord, superseded_by: str | None) -> str:
    if rec.status == "archived":
        return "archived"
    return "superseded" if superseded_by else "active"


def _shape(rec: MemoryRecord, superseded_by: str | None) -> dict[str, Any]:
    return {
        "id": rec.id,
        "created_at": norm_ts(rec.created_at),
        "type": rec.type,
        "status": _status(rec, superseded_by),
        "provenance": {
            "source_agent": rec.source_agent or None,
            "actor": None,  # not stored on a record
            "method": None,  # not stored on a record
            "evidence_refs": [e.ref for e in (rec.evidence or [])],
        },
        "supersedes": rec.supersedes or None,
        "superseded_by": superseded_by,
        "summary": _summary(rec),
    }


def ledger_head(store: Any, *, operator: bool) -> str | None:
    """The newest block hash (operator key only), else this tenant's history sequence; ``None`` on the JSON store.

    OAuth tenants never get block data (blocks are operator-only), only their own history counter.
    """
    seq_fn = getattr(store, "history_seq", None)
    if seq_fn is None:
        return None
    if operator and hasattr(store, "block_head"):
        head = store.block_head()
        tip = head.get("tip")
        if tip:
            return f"block:{tip['block_hash']}"
        return f"seq:{head['history_seq']}"
    return f"seq:{seq_fn()}"


def latest_memories(store: Any, *, tenant: str | None, params: LatestParams, operator: bool) -> dict[str, Any]:
    """The single implementation of newest-first discovery.  ``store`` must already be scoped to ``tenant``."""
    if not tenant:
        raise LatestError(403, "denied", TENANT_UNRESOLVED, "tenant could not be resolved")
    limit = parse_limit(params.limit)
    key = cursor_key()
    after = decode_cursor(key, tenant, params, params.cursor) if params.cursor else None

    rows = store.list_latest(
        limit=limit + 1,
        after=after,
        memory_type=params.type,
        include_superseded=params.include_superseded,
        include_archived=params.include_archived,
        include_twin=params.include_twin,
    )
    page, more = rows[:limit], len(rows) > limit
    records = [_shape(rec, succ) for rec, succ in page]
    next_cursor = None
    if more and page:
        last = page[-1][0]
        next_cursor = encode_cursor(key, tenant, params, last.created_at, last.id)
    return {
        "records": records,
        "next_cursor": next_cursor,
        "tenant": tenant,
        "ledger_head": ledger_head(store, operator=operator),
        "result_digest": result_digest([(r["id"], r["created_at"], r["status"]) for r in records]),
        "provenance": "ledger",
    }
