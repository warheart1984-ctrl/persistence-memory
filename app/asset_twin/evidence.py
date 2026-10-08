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


class EvidenceLedger:
    def __init__(self, path: Path | None = None) -> None:
        self._path = path or (_dir() / "audit.jsonl")
        self._lock = threading.Lock()
        self._last: dict[str, str] = {}  # tenant -> digest

    def append(self, tenant: str, kind: str, body: dict) -> dict:
        with self._lock:
            prev = self._last.get(tenant, GENESIS)
            record = {
                "tenant": tenant, "kind": kind, "prev": prev,
                "body": body,
            }
            digest = "sha256:" + hashlib.sha256(canonical(record)).hexdigest()
            record["digest"] = digest
            self._last[tenant] = digest
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with self._path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n")
            return record

    def verify(self, tenant: str) -> tuple[bool, list[str]]:
        problems: list[str] = []
        prev = GENESIS
        try:
            lines = self._path.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            return True, []
        for i, line in enumerate(lines):
            if not line.strip():
                continue
            rec = json.loads(line)
            if rec.get("tenant") != tenant:
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
        return (not problems, problems)


_ledger: EvidenceLedger | None = None
_ledger_lock = threading.Lock()


def get_ledger() -> EvidenceLedger:
    global _ledger
    with _ledger_lock:
        if _ledger is None:
            _ledger = EvidenceLedger()
        return _ledger
