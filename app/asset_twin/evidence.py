"""Evidence, Governance, Traceability: append-only hash-chained receipt log.

Covers the bottom bar of the diagram: time-stamped data, model/config
versions, assumptions & limits, requirements/constraints (policy id),
validation note, approval record, audit trail, change history.

Tenant-scoped, file-backed JSONL (like twinchat receipts). No prompt,
no credential, no raw control bus — digests + counters + versions only.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
from pathlib import Path

from .twin_core import canonical

GENESIS = "sha256:genesis"


def _dir() -> Path:
    root = Path(os.getenv("JARVIS_ASSET_TWIN_DIR", "data/asset-twin"))
    root.mkdir(parents=True, exist_ok=True)
    return root


class EvidenceChainError(RuntimeError):
    """The persisted chain for a tenant does not verify; nothing is appended on top of a broken chain."""


def _scan(text: str, tenant: str) -> tuple[list[str], str]:
    """Walk one tenant's records in file order; returns (problems, last digest)."""
    problems: list[str] = []
    prev = GENESIS
    for i, line in enumerate(text.splitlines()):
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            problems.append(f"line {i}: unreadable")
            break
        if not isinstance(rec, dict) or rec.get("tenant") != tenant:
            continue
        if rec.get("prev") != prev:
            problems.append(f"line {i}: prev mismatch")
            break
        digest = rec.pop("digest", "")
        expect = "sha256:" + hashlib.sha256(canonical(rec)).hexdigest()
        if digest != expect:
            problems.append(f"line {i}: digest mismatch")
            break
        prev = digest
    return problems, prev


class EvidenceLedger:
    def __init__(self, path: Path | None = None) -> None:
        self._path = path or (_dir() / "audit.jsonl")
        self._lock = threading.Lock()
        self._last: dict[str, str] = {}  # tenant -> digest of its newest record

    def _head(self, tenant: str) -> str:
        """The tenant's newest digest. After a restart `_last` is empty, so recover it from the file and verify the
        whole chain on the way: appending to a chain that does not verify would only hide the break."""
        head = self._last.get(tenant)
        if head is not None:
            return head
        try:
            text = self._path.read_text(encoding="utf-8")
        except FileNotFoundError:
            text = ""
        problems, head = _scan(text, tenant)
        if problems:
            raise EvidenceChainError(f"evidence chain for tenant {tenant!r} is broken ({'; '.join(problems)}); refusing to append")
        self._last[tenant] = head
        return head

    def check(self, tenant: str) -> None:
        """Raise EvidenceChainError if this tenant's chain cannot be appended to. Called BEFORE anything acts, so a broken
        audit trail stops the action instead of letting it happen unrecorded."""
        with self._lock:
            self._head(tenant)

    def append(self, tenant: str, kind: str, body: dict) -> dict:
        with self._lock:
            prev = self._head(tenant)
            record = {
                "tenant": tenant, "kind": kind, "prev": prev,
                "body": body,
            }
            digest = "sha256:" + hashlib.sha256(canonical(record)).hexdigest()
            record["digest"] = digest
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with self._path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n")
            self._last[tenant] = digest  # only once the record is on disk
            return record

    def verify(self, tenant: str) -> tuple[bool, list[str]]:
        try:
            text = self._path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return True, []
        problems, _ = _scan(text, tenant)
        return (not problems, problems)


_ledger: EvidenceLedger | None = None
_ledger_lock = threading.Lock()


def get_ledger() -> EvidenceLedger:
    global _ledger
    with _ledger_lock:
        if _ledger is None:
            _ledger = EvidenceLedger()
        return _ledger
