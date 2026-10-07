"""Replay Contracts: RC.Ledger.v1, the ledger's own deterministic reconstruction rule.

A Replay Contract is a registered, versioned rule: the same input always gives the same output and the same hash.
RC.Ledger.v1 rebuilds the ledger as of a point in its history (a ``record_history.seq``, or the end of a sealed block):

* **state**: every record as it was at that seq (deleted records excluded), committed to by a *state root*;
* **events**: the ordered history entries up to that seq (what happened, in what order, under which recorded actor,
  citing which evidence).

The state root never re-serializes anything.  A record's state at seq S is the ``row_hash`` of its newest history entry
with ``seq <= S`` (that hash already commits, through the per-record chain, to the full content).  The leaves are those
(record id, row_hash) pairs sorted by record id (bytewise), hashed RFC 6962 style, and merged into one Merkle root.
Entries are checked against the block chain (and, through the backup anchors, outside the database) by ``verify_replay``.

Clause III: replay reconstructs what happened, order, authority and evidence.  It does not reconstruct any consumer's
domain logic.  The AIKI, ARIS, SX, Lineage and Mandala contracts stay *declared*: their semantics and owners are not in
this repository.  See docs/REPLAY_CONTRACTS.md.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from pydantic import BaseModel, Field

from app import blocks as continuity_blocks
from app import evidence as evidence_objects

CONTRACT_ID = "RC.Ledger.v1"
CONTRACT_VERSION = 1
STATE_LEAF_TAG = "jarvis-state|v1|"
EMPTY_ROOT = hashlib.sha256(b"").hexdigest()  # RFC 6962: the Merkle tree hash of an empty list
GENESIS = continuity_blocks.GENESIS_HASH
MAX_PAGE = 1000

_HEX64 = re.compile(r"^[0-9a-f]{64}$")


class ReplayError(Exception):
    """A replay request that cannot be answered; ``status`` is the HTTP status the API maps it to."""

    def __init__(self, code: str, message: str, status: int = 422):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


# --- the registry ------------------------------------------------------------------------------------------------

class ContractSpec(BaseModel):
    id: str
    version: int
    status: str = Field(description="implemented | declared")
    consumer: str
    description: str
    algorithm: str | None = Field(default=None, description="the named, versioned algorithm; null while only declared")
    owner: str | None = None
    determinism: list[str] = Field(default_factory=list)
    schemas: list[str] = Field(default_factory=list, description="file names under schemas/rc/")


_LEDGER_DETERMINISM = [
    "no wall clock, no randomness, no network: the output depends only on the tenant's history entries up to at_seq",
    "records are ordered by id, bytewise (UTF-8)",
    "the state root hashes row_hash values; it never re-serializes JSON, so floats and key order cannot change it",
    "the same input gives the same output after later writes, after sealing, and after a backup and restore",
    "a receipt holds no timestamp, so the same replay at the same sealed point always gives the same receipt (the same evidence id)",
]

REGISTRY: dict[str, ContractSpec] = {
    CONTRACT_ID: ContractSpec(
        id=CONTRACT_ID, version=CONTRACT_VERSION, status="implemented", consumer="Continuity Ledger (this service)",
        description="Rebuild the ledger's records and ordered events as of a history seq or a sealed block.",
        algorithm="ledger-state-at-seq/v1", owner="persistence-memory", determinism=_LEDGER_DETERMINISM,
        schemas=["RC.Ledger.v1.input.schema.json", "RC.Ledger.v1.state.schema.json", "RC.Ledger.v1.events.schema.json",
                 "RC.Ledger.v1.receipt.schema.json", "RC.Ledger.v1.receipt-verification.schema.json"],
    ),
}
for _rc, _consumer in (("RC.AIKI.v1", "AIKI"), ("RC.ARIS.v1", "ARIS"), ("RC.SX.v1", "Sovereign X"),
                       ("RC.Lineage.v1", "Lineage"), ("RC.Mandala.v1", "Mandala")):
    REGISTRY[_rc] = ContractSpec(
        id=_rc, version=1, status="declared", consumer=_consumer,
        description=f"{_consumer} reconstruction rules. Declared only: their semantics and owner are not in this repository "
                    "(Constitutional Boundary Clause III keeps domain logic sovereign).",
    )


# --- the hashing -------------------------------------------------------------------------------------------------

def state_leaf(memory_id: str, row_hash: str) -> bytes:
    """RFC 6962 leaf: sha256(0x00 || "jarvis-state|v1|" || <id bytes>:<id> || "|" || row_hash).

    The tag keeps a state leaf from ever equalling a block-entry leaf, and the id is length-prefixed in bytes."""
    if not _HEX64.match(row_hash or ""):
        raise ValueError("row_hash must be 64 lowercase hex characters")
    ident = memory_id.encode("utf-8")
    return hashlib.sha256(b"\x00" + f"{STATE_LEAF_TAG}{len(ident)}:".encode() + ident + b"|" + row_hash.encode()).digest()


def state_root(heads: list[tuple[str, str]]) -> str:
    """The state root over (memory_id, row_hash) pairs of the records that exist at the point replayed.

    The input order does not matter: the pairs are sorted by id (bytewise) here.  The empty state has the empty-tree hash."""
    if not heads:
        return EMPTY_ROOT
    ordered = sorted(heads, key=lambda h: h[0].encode("utf-8"))
    ids = [h[0] for h in ordered]
    if len(set(ids)) != len(ids):
        raise ValueError("a record can appear only once in a state")
    return continuity_blocks.merkle_tree_hash([state_leaf(i, h) for i, h in ordered]).hex()


def entry_row_hash(prev_hash: str, op: str, version: int, before_text: str | None, after_text: str | None) -> str:
    """Recompute a history entry's row_hash (what ``jarvis_history_hash`` computes in the database) from the entry's
    fields, with ``before`` / ``after`` as the text Postgres renders for the jsonb values."""
    text = f"{prev_hash}|{op}|{version}|{before_text or ''}|{after_text or ''}"
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Entry:
    """One history entry as the verifier sees it (before/after as the database renders them)."""

    seq: int
    memory_id: str
    op: str
    version: int
    prev_hash: str
    row_hash: str
    before_text: str | None
    after_text: str | None


@dataclass
class Folded:
    live: dict[str, Entry] = field(default_factory=dict)  # id -> newest entry, for records that exist
    deleted: int = 0  # records whose newest entry is a delete


def fold_state(entries: list[Entry], at_seq: int) -> Folded:
    """Apply the entries up to ``at_seq`` in seq order: a plain fold, independent of any SQL used to serve the state."""
    newest: dict[str, Entry] = {}
    for e in sorted(entries, key=lambda e: e.seq):
        if e.seq <= at_seq:
            newest[e.memory_id] = e
    out = Folded()
    for mid, e in newest.items():
        if e.op == "delete":
            out.deleted += 1
        else:
            out.live[mid] = e
    return out


# --- the models (their JSON Schemas are published under schemas/rc/) ------------------------------------------------

class ReplayInput(BaseModel):
    """What a replay is asked for: the tenant's history up to a point.  Neither bound given means the current end."""

    at_seq: int | None = Field(default=None, ge=0, description="replay as of this record_history seq (0 = empty ledger)")
    at_block: int | None = Field(default=None, ge=1, description="replay as of the last entry of this sealed block")
    after_id: str | None = Field(default=None, description="paging cursor: return records with an id after this one (bytewise)")
    limit: int = Field(default=200, ge=1, le=MAX_PAGE)


class BlockRef(BaseModel):
    height: int
    first_seq: int
    last_seq: int
    block_hash: str
    signed: bool = Field(default=False, description="true only when a valid attestation by an authorized Mint key was verified against the pinned roots "
                                                    "(L1 or better); false when unsigned, unverifiable (no trust root) or checking is off")
    signature_level: int | None = Field(default=None, description="0 unsigned, 1 Mint-signed, 2 root-cosigned; null when signatures are not checked (JARVIS_SIGNATURES=off)")


class ReplayedRecord(BaseModel):
    id: str
    seq: int = Field(description="seq of the history entry that is this record's state at at_seq")
    version: int
    row_hash: str
    record: dict[str, Any] = Field(description="the record exactly as that history entry stored it")


class ReplayState(BaseModel):
    contract: str = CONTRACT_ID
    contract_version: int = CONTRACT_VERSION
    tenant: str
    at_seq: int
    history_seq: int = Field(description="the tenant's history counter now")
    sealed_seq: int = Field(description="the last seq covered by a sealed block")
    sealed: bool = Field(description="true when at_seq is covered by a sealed block")
    at_block_boundary: bool = Field(description="true when at_seq is the last entry of a sealed block")
    block: BlockRef | None = Field(description="the sealed block that covers at_seq, if any")
    record_count: int
    deleted_count: int
    state_root: str
    records: list[ReplayedRecord]
    next_after_id: str | None = Field(description="pass as after_id to get the next page; null on the last page")


class EvidenceStatus(BaseModel):
    kind: str
    ref: str
    note: str | None = None
    status: str = Field(description="intact | missing | tampered (evidence objects) | not-checked (every other link kind)")


class ReplayEvent(BaseModel):
    seq: int
    memory_id: str
    op: str
    version: int
    actor: str = Field(description="the recorded authority: whoever the ledger recorded as making the change")
    changed_at: str
    prev_hash: str
    row_hash: str
    before: dict[str, Any] | None
    after: dict[str, Any] | None
    evidence: list[EvidenceStatus]


class ReplayEvents(BaseModel):
    contract: str = CONTRACT_ID
    contract_version: int = CONTRACT_VERSION
    tenant: str
    from_seq: int
    to_seq: int
    history_seq: int
    events: list[ReplayEvent]
    next_from_seq: int | None


class ReceiptRequest(BaseModel):
    """Ask for a receipt of the replay at a sealed point.  Neither bound given means the end of the newest sealed block."""

    at_seq: int | None = Field(default=None, ge=1, description="a seq covered by a sealed block")
    at_block: int | None = Field(default=None, ge=1, description="the last entry of this sealed block")


class ReplayReceiptPayload(BaseModel):
    """The payload of a ``CES.Local.ReplayReceipt.v1`` evidence object: what the replay at a sealed point produced.
    No timestamps, so the same replay always gives the same receipt (the same evidence id)."""

    contract: str = CONTRACT_ID
    contract_version: int = Field(default=CONTRACT_VERSION, ge=1)
    tenant: str
    at_seq: int = Field(ge=0)
    block_height: int = Field(ge=1, description="the sealed block that covers at_seq")
    block_hash: str = Field(pattern=r"^[0-9a-f]{64}$", description="that block's hash, which the backup anchors also hold")
    state_root: str = Field(pattern=r"^[0-9a-f]{64}$")
    record_count: int = Field(ge=0)
    deleted_count: int = Field(ge=0)


class ReceiptVerification(BaseModel):
    """The result of re-deriving a receipt: the receipt is honest only if every problem list entry is absent."""

    ok: bool
    receipt_id: str
    problems: list[dict[str, str]]
    receipt: ReplayReceiptPayload | None = None
    replayed: dict[str, Any] | None = Field(default=None, description="what the replay says now (root, counts, block)")
    signatures: dict[str, Any] | None = Field(default=None, description="the receipt's and its block's signature levels (L0 unsigned, L1 Mint-signed, "
                                                                         "L2 root-cosigned) and, in JARVIS_SIGNATURES=require, whether they are enough; null when checking is off")


def receipt_payload(state: "ReplayState") -> dict[str, Any]:
    """The receipt payload for a replayed state (it must be at a sealed point: ``state.block`` is set)."""
    if state.block is None:
        raise ReplayError("replay_not_sealed", f"receipts are issued only at sealed points; the ledger is sealed through seq {state.sealed_seq}")
    return ReplayReceiptPayload(
        tenant=state.tenant, at_seq=state.at_seq, block_height=state.block.height, block_hash=state.block.block_hash,
        state_root=state.state_root, record_count=state.record_count, deleted_count=state.deleted_count,
    ).model_dump()


SCHEMA_MODELS: dict[str, type[BaseModel]] = {
    "RC.Ledger.v1.input.schema.json": ReplayInput,
    "RC.Ledger.v1.state.schema.json": ReplayState,
    "RC.Ledger.v1.events.schema.json": ReplayEvents,
    "RC.Ledger.v1.receipt.schema.json": ReplayReceiptPayload,
    "RC.Ledger.v1.receipt-verification.schema.json": ReceiptVerification,
}
SCHEMA_DIR = Path(__file__).resolve().parents[1] / "schemas" / "rc"


def schema_text(model: type[BaseModel]) -> str:
    return json.dumps(model.model_json_schema(), indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def registry_text() -> str:
    return json.dumps({k: v.model_dump() for k, v in REGISTRY.items()}, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def expected_schema_files() -> dict[str, str]:
    files = {name: schema_text(model) for name, model in SCHEMA_MODELS.items()}
    files["registry.json"] = registry_text()
    return files


def evidence_links(entry_json: dict[str, Any] | None) -> list[dict[str, Any]]:
    return [dict(x) for x in ((entry_json or {}).get("evidence") or []) if isinstance(x, dict)]


# --- the offline verifier -------------------------------------------------------------------------------------------

@dataclass
class Ctx:
    conn: Any
    tenant: str
    at_seq: int
    expect_root: str | None
    expect_block_hash: str | None = None
    expect_block_height: int | None = None
    counter: int = 0
    sealed_seq: int = 0
    entries: list[Entry] = field(default_factory=list)  # every entry with seq <= at_seq, ordered by (memory_id, seq)
    folded: Folded = field(default_factory=Folded)
    root: str = EMPTY_ROOT


Problem = dict[str, str]


def _problem(check: str, subject: str, message: str) -> Problem:
    return {"check": check, "subject": subject, "problem": message}


def _check_seq(ctx: Ctx) -> list[Problem]:
    out: list[Problem] = []
    if ctx.at_seq > ctx.counter:
        out.append(_problem("seq", "history", f"at_seq {ctx.at_seq} is beyond the history counter {ctx.counter}"))
    seqs = [e.seq for e in ctx.entries]
    if len(set(seqs)) != len(seqs):
        out.append(_problem("seq", "history", "a history seq appears more than once"))
    missing = sorted(set(range(1, ctx.at_seq + 1)) - set(seqs))
    for n in missing[:20]:
        out.append(_problem("seq", f"seq {n}", f"history entry {n} is missing (an entry was removed)"))
    return out


def _check_entry_hash(ctx: Ctx) -> list[Problem]:
    out: list[Problem] = []
    for e in ctx.entries:
        if entry_row_hash(e.prev_hash, e.op, e.version, e.before_text, e.after_text) != e.row_hash:
            out.append(_problem("entry_hash", f"seq {e.seq}", f"row_hash does not match the entry's contents (record {e.memory_id}; the entry was altered)"))
    return out


def _check_chain_link(ctx: Ctx) -> list[Problem]:
    out: list[Problem] = []
    last: dict[str, str] = {}
    for e in ctx.entries:  # ordered by (memory_id, seq)
        expected = last.get(e.memory_id, GENESIS)
        if e.prev_hash != expected:
            out.append(_problem("chain_link", f"seq {e.seq}", f"prev_hash does not match the previous entry of {e.memory_id} (an entry was removed, reordered or altered)"))
        last[e.memory_id] = e.row_hash
    return out


def _check_state_root(ctx: Ctx) -> list[Problem]:
    ctx.root = state_root([(mid, e.row_hash) for mid, e in ctx.folded.live.items()])
    if ctx.expect_root is not None and ctx.expect_root != ctx.root:
        return [_problem("state_root", "state", f"state root {ctx.root} differs from the expected root {ctx.expect_root}")]
    return []


def _check_live_match(ctx: Ctx) -> list[Problem]:
    """At the current end of the history the replayed state must equal the live table, record for record."""
    if ctx.at_seq != ctx.counter:
        return []
    live = {r[0]: r[1] for r in ctx.conn.execute(
        "SELECT m.id, jarvis_memory_json(m)::text FROM memories m WHERE m.tenant_key = %s", (ctx.tenant,))}
    out: list[Problem] = []
    for mid, e in ctx.folded.live.items():
        if mid not in live:
            out.append(_problem("live_match", mid, "the replayed state has a record that is not in the live table"))
        elif live[mid] != e.after_text:
            out.append(_problem("live_match", mid, "the live record differs from its latest history entry"))
    for mid in sorted(set(live) - set(ctx.folded.live)):
        out.append(_problem("live_match", mid, "the live table has a record that the replayed history does not"))
    return out


def _check_blocks(ctx: Ctx) -> list[Problem]:
    """Every sealed block up to the one that covers at_seq must still verify (the same checks as pg_verify)."""
    from app import pg_verify  # local import: pg_verify is a command-line module

    covering = ctx.conn.execute(
        "SELECT height FROM blocks WHERE tenant_key = %s AND first_seq <= %s AND last_seq >= %s",
        (ctx.tenant, ctx.at_seq, ctx.at_seq)).fetchone()
    if covering is None:
        return []
    out: list[Problem] = []
    for what, message in pg_verify._block_problems(ctx.conn, ctx.tenant):
        m = re.match(r"block (\d+)$", what)
        if m is None or int(m.group(1)) <= covering[0]:
            out.append(_problem("blocks", what, message))
    return out


def _check_expected_block(ctx: Ctx) -> list[Problem]:
    """The sealed block that covers at_seq must be the one expected (the anchor's, or the receipt's)."""
    if ctx.expect_block_hash is None and ctx.expect_block_height is None:
        return []
    row = ctx.conn.execute(
        "SELECT height, block_hash FROM blocks WHERE tenant_key = %s AND first_seq <= %s AND last_seq >= %s",
        (ctx.tenant, ctx.at_seq, ctx.at_seq)).fetchone()
    if row is None:
        return [_problem("expected_block", "block", f"no sealed block covers seq {ctx.at_seq}, but one was expected")]
    out: list[Problem] = []
    if ctx.expect_block_height is not None and row[0] != ctx.expect_block_height:
        out.append(_problem("expected_block", f"block {row[0]}", f"the covering block is {row[0]}, expected block {ctx.expect_block_height}"))
    if ctx.expect_block_hash is not None and row[1] != ctx.expect_block_hash:
        out.append(_problem("expected_block", f"block {row[0]}", f"the covering block's hash is {row[1]}, not the expected {ctx.expect_block_hash}"))
    return out


# Run in this order.  A dict so a test can swap one entry out and show the tamper it exists for goes unnoticed.
CHECKS: dict[str, Callable[[Ctx], list[Problem]]] = {
    "seq": _check_seq,
    "entry_hash": _check_entry_hash,
    "chain_link": _check_chain_link,
    "state_root": _check_state_root,
    "live_match": _check_live_match,
    "blocks": _check_blocks,
    "expected_block": _check_expected_block,
}


def resolve_at_seq(conn: Any, tenant: str, at_seq: int | None, at_block: int | None) -> int:
    """Turn an at_seq / at_block request into a seq (raises ReplayError); neither means the current end."""
    if at_seq is not None and at_block is not None:
        raise ReplayError("replay_bound_ambiguous", "give at_seq or at_block, not both")
    counter_row = conn.execute("SELECT last_seq FROM history_counters WHERE tenant_key = %s", (tenant,)).fetchone()
    counter = counter_row[0] if counter_row else 0
    if at_block is not None:
        row = conn.execute("SELECT last_seq FROM blocks WHERE tenant_key = %s AND height = %s", (tenant, at_block)).fetchone()
        if row is None:
            raise ReplayError("replay_block_not_found", f"there is no sealed block {at_block}", 404)
        return row[0]
    if at_seq is None:
        return counter
    if at_seq > counter:
        raise ReplayError("replay_seq_out_of_range", f"at_seq {at_seq} is beyond the history counter {counter}")
    return at_seq


def add_signatures(conn: Any, tenant: str, result: dict[str, Any], *, receipt_id: str | None = None, mode: str | None = None) -> dict[str, Any]:
    """Attach the signature levels of the sealed block (and the receipt) to a verification result, and fold what must fail into
    ``problems`` / ``ok``.  Re-derivation (``ok`` before this) is a separate question from who vouches: in ``warn`` an unsigned block
    only adds a warning; an attestation that does not verify, and anything unsigned in ``require``, make the result fail."""
    from app import attest

    block = result.get("block")
    if (mode or attest.signatures_mode()) == "off" or (block is None and receipt_id is None):
        result.setdefault("signatures", None)
        return result
    rep = attest.signature_report(conn, tenant, block_height=block["height"] if block else None, block_hash=block["block_hash"] if block else None,
                                  receipt_id=receipt_id, mode=mode)
    result["signatures"] = rep
    if block is not None:
        block["signed"] = bool(rep["verified"] and rep["block"] and rep["block"]["level"] >= 1)
        block["signature_level"] = rep["block"]["level"] if rep["block"] else None
    result["problems"] = list(result.get("problems", [])) + rep["problems"]
    result["ok"] = not result["problems"]
    return result


def verify_replay(conn: Any, tenant: str, *, at_seq: int | None = None, at_block: int | None = None,
                  expect_root: str | None = None, expect_block_hash: str | None = None,
                  expect_block_height: int | None = None, signatures: str | None = None) -> dict[str, Any]:
    """Replay the tenant's history up to a point from the raw entries and check everything that can be checked.

    ``conn`` is a psycopg connection with the ledger schema on its search path and the tenant (or a superuser) able to
    read it.  Returns {"ok", "problems", ...}; a problem names the check that found it."""
    # Row-level security binds every role but a superuser to the tenant named here; without it the ledger looks empty.
    conn.execute("SELECT set_config('jarvis.tenant_key', %s, true)", (tenant,))
    seq = resolve_at_seq(conn, tenant, at_seq, at_block)
    ctx = Ctx(conn=conn, tenant=tenant, at_seq=seq, expect_root=expect_root,
              expect_block_hash=expect_block_hash, expect_block_height=expect_block_height)
    row = conn.execute("SELECT last_seq FROM history_counters WHERE tenant_key = %s", (tenant,)).fetchone()
    ctx.counter = row[0] if row else 0
    row = conn.execute("SELECT coalesce(max(last_seq), 0) FROM blocks WHERE tenant_key = %s", (tenant,)).fetchone()
    ctx.sealed_seq = row[0]
    ctx.entries = [Entry(*r) for r in conn.execute(
        "SELECT seq, memory_id, op, version, prev_hash, row_hash, before::text, after::text FROM record_history "
        "WHERE tenant_key = %s AND seq <= %s ORDER BY memory_id COLLATE \"C\", seq", (tenant, seq))]
    ctx.folded = fold_state(ctx.entries, seq)
    problems: list[Problem] = []
    for check in list(CHECKS.values()):
        problems.extend(check(ctx))
    covering = conn.execute(
        "SELECT height, block_hash FROM blocks WHERE tenant_key = %s AND first_seq <= %s AND last_seq >= %s",
        (tenant, seq, seq)).fetchone()
    result = {
        "ok": not problems, "problems": problems, "tenant": tenant, "at_seq": seq, "history_seq": ctx.counter,
        "sealed_seq": ctx.sealed_seq, "sealed": bool(covering), "state_root": ctx.root,
        "record_count": len(ctx.folded.live), "deleted_count": ctx.folded.deleted, "entry_count": len(ctx.entries),
        "block": {"height": covering[0], "block_hash": covering[1]} if covering else None, "signatures": None,
    }
    return add_signatures(conn, tenant, result, mode=signatures) if signatures != "off" else result


def _load_receipt(conn: Any, tenant: str, receipt_id: str) -> "evidence_objects.EvidenceObject":
    if not evidence_objects.ID_RE.match(receipt_id or ""):
        raise ReplayError("receipt_id_invalid", "a receipt id looks like eo:sha256:<64 lowercase hex characters>")
    conn.execute("SELECT set_config('jarvis.tenant_key', %s, true)", (tenant,))
    row = conn.execute(
        "SELECT id, schema_id, payload, pointer, size_bytes, created_at, created_by FROM evidence_objects WHERE tenant_key = %s AND id = %s",
        (tenant, receipt_id)).fetchone()
    if row is None:
        raise ReplayError("receipt_not_found", f"there is no evidence object {receipt_id}", 404)
    obj = evidence_objects.EvidenceObject(id=row[0], schema_id=row[1], payload=row[2], pointer=row[3], size_bytes=row[4],
                                          created_at=row[5].isoformat(), created_by=row[6])
    if obj.schema_id != evidence_objects.CES_REPLAY_RECEIPT:
        raise ReplayError("not_a_replay_receipt", f"{receipt_id} is a {obj.schema_id} object, not a replay receipt")
    return obj


def verify_receipt(conn: Any, tenant: str, receipt_id: str, *, signatures: str | None = None) -> dict[str, Any]:
    """Re-derive a receipt from the raw history entries: the stored object must be intact, and replaying the ledger at the
    receipt's sealed point must give the receipt's state root, counts and covering block.  Returns a ``verify_replay``-style
    result with a ``receipt`` entry; ``ok`` is false if anything differs."""
    obj = _load_receipt(conn, tenant, receipt_id)
    problems: list[Problem] = [_problem("receipt", receipt_id, f"the stored receipt is damaged: {p}") for p in evidence_objects.verify_stored(obj)]
    if problems:
        return {"ok": False, "problems": problems, "receipt": obj.payload, "tenant": tenant}
    p = obj.payload
    if p["contract"] != CONTRACT_ID or p["contract_version"] != CONTRACT_VERSION:
        return {"ok": False, "receipt": p, "tenant": tenant,
                "problems": [_problem("receipt", receipt_id, f"this verifier knows {CONTRACT_ID} version {CONTRACT_VERSION}, not {p['contract']} version {p['contract_version']}")]}
    if p["tenant"] != tenant:
        problems.append(_problem("receipt", receipt_id, f"the receipt is for tenant {p['tenant']}, not {tenant}"))
    try:
        result = verify_replay(conn, tenant, at_seq=p["at_seq"], expect_root=p["state_root"],
                               expect_block_hash=p["block_hash"], expect_block_height=p["block_height"], signatures="off")  # signatures are added once, below
    except ReplayError as exc:
        problems.append(_problem("receipt", receipt_id, f"the receipt cannot be replayed: {exc.message}"))
        return {"ok": False, "problems": problems, "receipt": p, "tenant": tenant}
    problems.extend(result["problems"])
    for key in ("record_count", "deleted_count"):
        if result[key] != p[key]:
            problems.append(_problem("receipt", receipt_id, f"{key} is {result[key]} on replay, the receipt says {p[key]}"))
    result.update(problems=problems, ok=not problems, receipt=p, receipt_id=receipt_id)
    return add_signatures(conn, tenant, result, receipt_id=receipt_id, mode=signatures) if signatures != "off" else result


# --- command line -----------------------------------------------------------------------------------------------

def _connect(schema: str | None):
    import psycopg
    from psycopg import sql

    from app.pg_schema import check_schema_version, validate_schema_name

    dsn = (os.getenv("JARVIS_DATABASE_MIGRATE_URL") or os.getenv("JARVIS_DATABASE_URL") or "").strip()
    if not dsn:
        print("Set JARVIS_DATABASE_MIGRATE_URL (or JARVIS_DATABASE_URL).", file=sys.stderr)
        return None
    conn = psycopg.connect(dsn, connect_timeout=5)
    if schema:
        conn.execute(sql.SQL("SET LOCAL search_path TO {}").format(sql.Identifier(validate_schema_name(schema))))
    check_schema_version(conn)
    return conn


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.replay", description="RC.Ledger.v1 offline verifier and schema tools")
    sub = parser.add_subparsers(dest="cmd", required=True)
    v = sub.add_parser("verify", help="replay the history up to a point from the raw entries and check it")
    v.add_argument("--tenant", default="operator")
    g = v.add_mutually_exclusive_group()
    g.add_argument("--at-seq", type=int)
    g.add_argument("--at-block", type=int)
    v.add_argument("--expect-root", help="fail unless the replayed state root equals this value")
    v.add_argument("--expect-block-hash", help="fail unless the sealed block covering the point has this hash (an anchor's, for example)")
    v.add_argument("--receipt", help="re-derive this replay receipt (an evidence object id) from the raw history instead")
    v.add_argument("--signatures", choices=["off", "warn", "require"],
                   help="override JARVIS_SIGNATURES for this run (the signer uses off: it asks only whether the receipt re-derives, not whether it is signed yet)")
    s = sub.add_parser("schemas", help="write or check the published schemas under schemas/rc/")
    sg = s.add_mutually_exclusive_group(required=True)
    sg.add_argument("--write", action="store_true")
    sg.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)

    if args.cmd == "schemas":
        files = expected_schema_files()
        if args.write:
            SCHEMA_DIR.mkdir(parents=True, exist_ok=True)
            for name, text in files.items():
                (SCHEMA_DIR / name).write_text(text, "utf-8", newline="\n")
            print(f"wrote {len(files)} files to {SCHEMA_DIR}")
            return 0
        stale = [n for n, t in files.items() if not (SCHEMA_DIR / n).exists() or (SCHEMA_DIR / n).read_text("utf-8") != t]
        print("schemas are current" if not stale else "stale or missing: " + ", ".join(stale))
        return 1 if stale else 0

    schema = (os.getenv("JARVIS_DATABASE_SCHEMA") or "").strip() or None
    conn = _connect(schema)
    if conn is None:
        return 2
    with conn:
        try:
            if args.receipt:
                if args.at_seq is not None or args.at_block is not None or args.expect_root or args.expect_block_hash:
                    print("--receipt carries its own point, root and block; do not combine it with those options", file=sys.stderr)
                    return 2
                result = verify_receipt(conn, args.tenant, args.receipt, signatures=args.signatures)
            else:
                result = verify_replay(conn, args.tenant, at_seq=args.at_seq, at_block=args.at_block, expect_root=args.expect_root,
                                       expect_block_hash=args.expect_block_hash, signatures=args.signatures)
        except ReplayError as exc:
            print(f"{exc.code}: {exc.message}", file=sys.stderr)
            return 2
    for p in result["problems"]:
        print(f"PROBLEM tenant={args.tenant} {p['subject']} [{p['check']}]: {p['problem']}")
    def report_signatures() -> None:
        sig = result.get("signatures")
        if not sig:
            return
        parts = ", ".join(f"{name} {sig[name].get('id') or sig[name].get('height')}: {sig[name]['label']}" for name in ("block", "receipt") if sig.get(name))
        print(f"signatures ({sig['mode']}): {sig['label']}" + (f" [{parts}]" if parts else ""))
        for w in sig["warnings"]:
            print(f"WARNING: {w}")

    if result["problems"]:
        report_signatures()
        return 1
    where = f"block {result['block']['height']} ({result['block']['block_hash'][:16]}...)" if result["block"] else "not covered by a sealed block"
    if args.receipt:
        print(f"ok: receipt {args.receipt} re-derived: seq {result['at_seq']}, {result['record_count']} record(s), "
              f"state root {result['state_root']}; {where}")
    else:
        print(f"ok: replayed {result['entry_count']} entries to seq {result['at_seq']}: {result['record_count']} record(s), "
              f"{result['deleted_count']} deleted, state root {result['state_root']}; {where}")
    report_signatures()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
