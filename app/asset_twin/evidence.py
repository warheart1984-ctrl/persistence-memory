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


def _checked_digest(rec: dict) -> str:
    """The record's stored digest if it matches its own content, else a marker that can never equal a real head."""
    body = {k: v for k, v in rec.items() if k != "digest"}
    expect = "sha256:" + hashlib.sha256(canonical(body)).hexdigest()
    return rec.get("digest") if rec.get("digest") == expect else "<corrupt>"


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

    def _tail_digest(self, tenant: str) -> str | None:
        """Digest of the tenant's newest record in the file, read from the END (cheap), or None if it has none."""
        try:
            size = self._path.stat().st_size
            fh = self._path.open("rb")
        except FileNotFoundError:
            return None
        with fh:
            end = size
            carry = b""
            while end > 0:
                start = max(0, end - 65536)
                fh.seek(start)
                data = fh.read(end - start) + carry
                lines = data.split(b"\n")
                carry = lines[0] if start > 0 else b""
                for raw in reversed(lines[1:] if start > 0 else lines):
                    if not raw.strip():
                        continue
                    try:
                        rec = json.loads(raw)
                    except (json.JSONDecodeError, UnicodeDecodeError):
                        return "<unreadable>"
                    if isinstance(rec, dict) and rec.get("tenant") == tenant:
                        return _checked_digest(rec)
                end = start
            if carry.strip():
                try:
                    rec = json.loads(carry)
                    if isinstance(rec, dict) and rec.get("tenant") == tenant:
                        return _checked_digest(rec)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    return "<unreadable>"
        return None

    def _head(self, tenant: str) -> str:
        """The tenant's newest digest.

        After a restart `_last` is empty, so recover it from the file and verify the whole chain on the way: appending to a
        chain that does not verify would only hide the break. A cached head is not trusted blindly either: this service is the
        only writer, so the file's tail must still end in the digest it last wrote. If it does not (records removed, edited
        or added behind its back) that is a failure, not something to re-adopt: a truncated chain still verifies, so
        quietly accepting the shorter one would forgive exactly the tampering this ledger exists to expose. Restart
        recovery (above) is the only way a head is taken from disk."""
        head = self._last.get(tenant)
        if head is not None:
            on_disk = self._tail_digest(tenant) or GENESIS  # no record on disk yet is the genesis head
            if on_disk != head:
                raise EvidenceChainError(
                    f"evidence file for tenant {tenant!r} changed while the service was running "
                    f"(expected newest digest {head}, found {on_disk}); refusing to append or authorise anything"
                )
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

    def last_measurement_seq(self, tenant: str, asset_id: str) -> int | None:
        """Newest telemetry sequence this tenant's chain holds for the asset (None if none), for recovery after a restart.
        Only call after `check(tenant)`: the chain it reads has then been verified."""
        try:
            text = self._path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        newest: int | None = None
        for line in text.splitlines():
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            body = rec.get("body", {}) if isinstance(rec, dict) else {}
            if rec.get("tenant") == tenant and rec.get("kind") == "measurement" and body.get("asset") == asset_id:
                seq = body.get("seq")
                if isinstance(seq, int) and (newest is None or seq > newest):
                    newest = seq
        return newest

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
