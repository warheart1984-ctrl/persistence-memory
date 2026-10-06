"""Evidence Objects: content-addressed, immutable evidence the ledger can resolve and re-verify.

An evidence *link* (``EvidenceLink``) is only a pointer: nothing proves the thing it points at existed or stayed
unchanged. An Evidence Object is the thing itself, small enough to store:

    id = "eo:sha256:" + sha256(canonical JSON of {"schema_id", "payload", "pointer"?})

* **Content-addressed.** The id IS the hash. The same content always has the same id; changing one byte gives a
  different id. Anyone can recompute it.
* **Immutable.** Objects are never updated or deleted by the application (the database refuses it too).
* **Hashes only.** There are no signatures yet; signing is a root-authority question that is deliberately not answered
  here. An object proves *what was recorded*, not who vouches for it.
* **Small by design.** The canonical envelope may be at most 64 KB. Anything larger is recorded as a ``pointer``
  (uri + sha256 + size of the external content). The service does NOT fetch a pointer, so it reports the pointed-at
  content hash as "not checked".
* **Typed by a minimal local CES** (``CES.Local.DecisionEvidence.v1`` and ``CES.Local.FactEvidence.v1``). These are
  local to this ledger; they are not the CCS charter's registered CES.* schemas, which are not in this repository.

Only the operator key may create objects. Records link to them with an evidence link
``{"kind": "evidence-object", "ref": "eo:sha256:<hash>"}``; the ledger refuses a link that does not resolve, and for
``fact`` / ``architecture`` / ``research`` records the link counts as evidence only when the object is a FactEvidence.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

from fastapi import HTTPException
from pydantic import BaseModel, Field

ID_PREFIX = "eo:sha256:"
ID_RE = re.compile(r"^eo:sha256:[0-9a-f]{64}$")
LINK_KIND = "evidence-object"
INLINE_LIMIT_BYTES = 64 * 1024

CES_DECISION = "CES.Local.DecisionEvidence.v1"
CES_FACT = "CES.Local.FactEvidence.v1"
# A replay receipt (Replay Contracts, RC.Ledger.v1): what a replay at a sealed point produced.  Only the replay endpoint
# creates one (the generic create route refuses this schema), and a receipt is a claim until it is re-derived.
CES_REPLAY_RECEIPT = "CES.Local.ReplayReceipt.v1"
KNOWN_SCHEMAS = (CES_DECISION, CES_FACT, CES_REPLAY_RECEIPT)
_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")

# How a fact was observed: the same checkable kinds the Clause V gate accepts for link evidence.
FACT_METHODS = frozenset({"file", "url", "commit", "test", "receipt", "command", "document", "doc", "issue", "pr", "log"})

_MAX_DEPTH = 8
_MAX_NODES = 2000
_SHA_RE = re.compile(r"^[0-9a-f]{64}$")


class EvidenceError(Exception):
    """A refusal with a stable machine-readable code (HTTP 422 unless a status is given)."""

    def __init__(self, code: str, message: str, reasons: list[dict[str, Any]] | None = None, status: int = 422):
        super().__init__(message)
        self.code = code
        self.message = message
        self.reasons = reasons or []
        self.status = status

    def body(self) -> dict[str, Any]:
        out: dict[str, Any] = {"detail": self.message, "code": self.code}
        if self.reasons:
            out["reasons"] = self.reasons
        return out


class EvidenceObject(BaseModel):
    id: str
    schema_id: str
    payload: dict[str, Any]
    pointer: dict[str, Any] | None = None
    size_bytes: int
    created_at: str
    created_by: str


class EvidenceObjectCreate(BaseModel):
    schema_id: str = Field(..., min_length=1, max_length=128)
    payload: dict[str, Any]
    pointer: dict[str, Any] | None = None
    source_agent: str = Field(default="unknown", min_length=1, max_length=128)
    # Optional: the id the caller expects. A mismatch is refused (a cheap end-to-end check).
    id: str | None = Field(default=None, max_length=100)


@dataclass(frozen=True)
class EvidenceInfo:
    """What a link resolved to."""

    id: str
    schema_id: str


# --- canonical form and hash --------------------------------------------------------------------------------

def _walk(value: Any, depth: int, counter: list[int], problems: list[str], path: str) -> None:
    counter[0] += 1
    if counter[0] > _MAX_NODES:
        problems.append("payload has too many values")
        return
    if depth > _MAX_DEPTH:
        problems.append(f"{path}: nested too deeply (limit {_MAX_DEPTH})")
        return
    if value is None or isinstance(value, (bool, str)):
        return
    if isinstance(value, int):
        return
    if isinstance(value, float):
        what = "NaN/Infinity" if (math.isnan(value) or math.isinf(value)) else "a floating-point number"
        problems.append(f"{path}: {what} is not allowed (use a string or an integer so the hash is stable)")
        return
    if isinstance(value, list):
        for i, item in enumerate(value):
            _walk(item, depth + 1, counter, problems, f"{path}[{i}]")
        return
    if isinstance(value, dict):
        for k, v in value.items():
            if not isinstance(k, str):
                problems.append(f"{path}: object keys must be strings")
                continue
            _walk(v, depth + 1, counter, problems, f"{path}.{k}")
        return
    problems.append(f"{path}: unsupported value type {type(value).__name__}")


def canonical_bytes(schema_id: str, payload: dict[str, Any], pointer: dict[str, Any] | None = None) -> bytes:
    """The exact bytes that are hashed: sorted keys, no whitespace, UTF-8, no NaN."""
    envelope: dict[str, Any] = {"schema_id": schema_id, "payload": payload}
    if pointer is not None:
        envelope["pointer"] = pointer
    return json.dumps(envelope, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")


def object_id(schema_id: str, payload: dict[str, Any], pointer: dict[str, Any] | None = None) -> str:
    return ID_PREFIX + hashlib.sha256(canonical_bytes(schema_id, payload, pointer)).hexdigest()


# --- the minimal local CES ----------------------------------------------------------------------------------

def _is_iso(value: Any) -> bool:
    if not isinstance(value, str) or not value:
        return False
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return True


def _text(payload: dict[str, Any], key: str, limit: int, problems: list[str], required: bool) -> None:
    value = payload.get(key)
    if value is None:
        if required:
            problems.append(f"payload.{key} is required")
        return
    if not isinstance(value, str) or not value.strip():
        problems.append(f"payload.{key} must be a non-empty string")
    elif len(value) > limit:
        problems.append(f"payload.{key} is longer than {limit} characters")


def validate_payload(schema_id: str, payload: dict[str, Any]) -> list[str]:
    """Problems with ``payload`` under the minimal local CES ``schema_id`` (empty list = valid)."""
    problems: list[str] = []
    if schema_id == CES_DECISION:
        _text(payload, "statement", 4000, problems, True)    # the decision, in the decider's words
        _text(payload, "authority", 200, problems, True)     # who decided
        _text(payload, "source", 1000, problems, True)       # where it was stated
        if "decided_at" in payload and not _is_iso(payload["decided_at"]):
            problems.append("payload.decided_at must be an ISO-8601 timestamp")
    elif schema_id == CES_FACT:
        _text(payload, "observation", 4000, problems, True)  # what was observed
        _text(payload, "source", 1000, problems, True)       # the path, URL, command or receipt it came from
        _text(payload, "excerpt", 4000, problems, False)
        method = payload.get("method")
        if method is None:
            problems.append("payload.method is required")
        elif method not in FACT_METHODS:
            problems.append(f"payload.method must be one of: {', '.join(sorted(FACT_METHODS))}")
        if "observed_at" in payload and not _is_iso(payload["observed_at"]):
            problems.append("payload.observed_at must be an ISO-8601 timestamp")
    elif schema_id == CES_REPLAY_RECEIPT:
        _text(payload, "contract", 64, problems, True)
        _text(payload, "tenant", 128, problems, True)
        for key, floor in (("contract_version", 1), ("at_seq", 0), ("block_height", 1), ("record_count", 0), ("deleted_count", 0)):
            value = payload.get(key)
            if isinstance(value, bool) or not isinstance(value, int) or value < floor:
                problems.append(f"payload.{key} must be an integer of at least {floor}")
        for key in ("block_hash", "state_root"):
            value = payload.get(key)
            if not isinstance(value, str) or not _HEX64_RE.match(value):
                problems.append(f"payload.{key} must be 64 lowercase hex characters")
        extra = sorted(set(payload) - {"contract", "contract_version", "tenant", "at_seq", "block_height", "block_hash", "state_root", "record_count", "deleted_count"})
        if extra:
            problems.append(f"payload has fields a receipt does not have: {', '.join(extra)}")
    else:
        problems.append(f"unknown schema_id {schema_id!r}")
    return problems


def validate_pointer(pointer: dict[str, Any]) -> list[str]:
    problems: list[str] = []
    allowed = {"uri", "sha256", "size_bytes", "media_type"}
    for extra in sorted(set(pointer) - allowed):
        problems.append(f"pointer.{extra} is not allowed")
    uri = pointer.get("uri")
    if not isinstance(uri, str) or not (1 <= len(uri) <= 1000):
        problems.append("pointer.uri must be a string of 1 to 1000 characters")
    digest = pointer.get("sha256")
    if not isinstance(digest, str) or not _SHA_RE.match(digest):
        problems.append("pointer.sha256 must be 64 lowercase hex characters (the hash of the external content)")
    size = pointer.get("size_bytes")
    if isinstance(size, bool) or not isinstance(size, int) or size < 0:
        problems.append("pointer.size_bytes must be an integer >= 0")
    media = pointer.get("media_type")
    if media is not None and (not isinstance(media, str) or len(media) > 200):
        problems.append("pointer.media_type must be a string of at most 200 characters")
    return problems


def _problem_reasons(problems: list[str]) -> list[dict[str, Any]]:
    return [{"code": "evidence_payload_invalid", "message": p} for p in problems]


def build_object(req: EvidenceObjectCreate) -> tuple[str, bytes]:
    """Validate a create request and return (id, canonical bytes). Raises EvidenceError."""
    if req.schema_id not in KNOWN_SCHEMAS:
        raise EvidenceError(
            "evidence_schema_unknown",
            f"unknown schema_id {req.schema_id!r}; known: {', '.join(KNOWN_SCHEMAS)}",
        )
    problems: list[str] = []
    _walk(req.payload, 0, [0], problems, "payload")
    if req.pointer is not None:
        _walk(req.pointer, 0, [0], problems, "pointer")
    if problems:
        raise EvidenceError("evidence_payload_invalid", "the payload cannot be hashed stably", _problem_reasons(problems))
    problems = validate_payload(req.schema_id, req.payload)
    if req.pointer is not None:
        problems += validate_pointer(req.pointer)
    if problems:
        raise EvidenceError("evidence_payload_invalid", f"the payload does not satisfy {req.schema_id}", _problem_reasons(problems))
    data = canonical_bytes(req.schema_id, req.payload, req.pointer)
    if len(data) > INLINE_LIMIT_BYTES:
        raise EvidenceError(
            "evidence_too_large",
            f"the evidence is {len(data)} bytes; the inline limit is {INLINE_LIMIT_BYTES}. Record large content as a "
            "pointer: keep the description in the payload and give pointer.uri, pointer.sha256 and pointer.size_bytes.",
            status=413,
        )
    oid = ID_PREFIX + hashlib.sha256(data).hexdigest()
    if req.id is not None and req.id != oid:
        raise EvidenceError("evidence_hash_mismatch", "the id you supplied is not the hash of this content", [{"code": "evidence_hash_mismatch", "message": f"expected {oid}"}])
    return oid, data


def verify_stored(obj: EvidenceObject) -> list[str]:
    """Problems found when re-checking a stored object (empty list = intact)."""
    problems: list[str] = []
    if not ID_RE.match(obj.id):
        problems.append("the id is not of the form eo:sha256:<64 hex>")
        return problems
    walk: list[str] = []
    _walk(obj.payload, 0, [0], walk, "payload")
    if walk:
        problems.append("the stored payload cannot be canonicalised: " + "; ".join(walk))
        return problems
    try:
        recomputed = object_id(obj.schema_id, obj.payload, obj.pointer)
    except (TypeError, ValueError) as exc:
        return [f"the stored content cannot be canonicalised ({exc})"]
    if recomputed != obj.id:
        problems.append(f"hash mismatch: the content hashes to {recomputed}, not {obj.id}")
    problems += [f"schema: {p}" for p in validate_payload(obj.schema_id, obj.payload)]
    if obj.pointer is not None:
        problems += [f"schema: {p}" for p in validate_pointer(obj.pointer)]
    return problems


# --- links from records -------------------------------------------------------------------------------------

Resolver = Callable[[str], "EvidenceInfo | None"]


def _link_kind(link: Any) -> str:
    return str((link.get("kind") if isinstance(link, dict) else getattr(link, "kind", "")) or "").strip().lower()


def _link_ref(link: Any) -> str:
    return str((link.get("ref") if isinstance(link, dict) else getattr(link, "ref", "")) or "").strip()


def check_links(evidence: Iterable[Any] | None, resolve: Resolver) -> None:
    """Refuse a record whose evidence-object links do not resolve to an intact stored object."""
    reasons: list[dict[str, Any]] = []
    for link in evidence or []:
        if _link_kind(link) != LINK_KIND:
            continue
        ref = _link_ref(link)
        if not ID_RE.match(ref):
            reasons.append({"code": "evidence_object_unresolved", "ref": ref[:120], "message": "not a valid evidence-object id (eo:sha256:<64 hex>)"})
            continue
        try:
            info = resolve(ref)
        except EvidenceError as exc:
            reasons.extend(exc.reasons or [{"code": exc.code, "ref": ref, "message": exc.message}])
            continue
        if info is None:
            reasons.append({"code": "evidence_object_unresolved", "ref": ref, "message": "no such evidence object in this ledger"})
    if reasons:
        raise EvidenceError("evidence_object_invalid", "an evidence link does not resolve to an intact evidence object", reasons)


def counts_as_fact_evidence(link: Any, resolve: Resolver | None) -> bool:
    """Does this evidence-object link prove a fact? Only an intact FactEvidence object does."""
    if resolve is None or _link_kind(link) != LINK_KIND:
        return False
    try:
        info = resolve(_link_ref(link))
    except EvidenceError:
        return False
    return info is not None and info.schema_id == CES_FACT


# --- the API's creation guard -------------------------------------------------------------------------------

def require_operator_write() -> None:
    """Evidence Objects are created with the operator key only: never through an OAuth user token."""
    from app import auth

    if auth.oauth_enabled():
        raise HTTPException(status_code=403, detail="Evidence Objects can be created with the operator key only")
    auth.require_memory_write()


# --- the JSON-file backend (the PostgreSQL row store has its own table) -------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class FileEvidenceStore:
    """Append-only JSON-lines file next to the JSON ledger: used by the local/test store, not by the Mint ledger."""

    def __init__(self, ledger_path: Path):
        self.path = ledger_path.with_name(ledger_path.stem + ".evidence.jsonl")

    def _read(self) -> dict[str, EvidenceObject]:
        out: dict[str, EvidenceObject] = {}
        if not self.path.exists():
            return out
        for line in self.path.read_text("utf-8").splitlines():
            if line.strip():
                obj = EvidenceObject(**json.loads(line))
                out[obj.id] = obj
        return out

    def put(self, oid: str, req: EvidenceObjectCreate, size: int) -> tuple[EvidenceObject, bool]:
        existing = self._read().get(oid)
        if existing is not None:
            return existing, False
        obj = EvidenceObject(id=oid, schema_id=req.schema_id, payload=req.payload, pointer=req.pointer, size_bytes=size, created_at=_now_iso(), created_by=req.source_agent)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(obj.model_dump(), sort_keys=True, ensure_ascii=False) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        return obj, True

    def get(self, oid: str) -> EvidenceObject | None:
        return self._read().get(oid)

    def all(self) -> list[EvidenceObject]:
        return list(self._read().values())
