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
]

REGISTRY: dict[str, ContractSpec] = {
    CONTRACT_ID: ContractSpec(
        id=CONTRACT_ID, version=CONTRACT_VERSION, status="implemented", consumer="Continuity Ledger (this service)",
        description="Rebuild the ledger's records and ordered events as of a history seq or a sealed block.",
        algorithm="ledger-state-at-seq/v1", owner="persistence-memory", determinism=_LEDGER_DETERMINISM,
        schemas=["RC.Ledger.v1.input.schema.json", "RC.Ledger.v1.state.schema.json", "RC.Ledger.v1.events.schema.json"],
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


SCHEMA_MODELS: dict[str, type[BaseModel]] = {
    "RC.Ledger.v1.input.schema.json": ReplayInput,
    "RC.Ledger.v1.state.schema.json": ReplayState,
    "RC.Ledger.v1.events.schema.json": ReplayEvents,
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


# Run in this order.  A dict so a test can swap one entry out and show the tamper it exists for goes unnoticed.
CHECKS: dict[str, Callable[[Ctx], list[Problem]]] = {
    "seq": _check_seq,
    "entry_hash": _check_entry_hash,
    "chain_link": _check_chain_link,
    "state_root": _check_state_root,
    "live_match": _check_live_match,
    "blocks": _check_blocks,
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


def verify_replay(conn: Any, tenant: str, *, at_seq: int | None = None, at_block: int | None = None,
                  expect_root: str | None = None) -> dict[str, Any]:
    """Replay the tenant's history up to a point from the raw entries and check everything that can be checked.

    ``conn`` is a psycopg connection with the ledger schema on its search path and the tenant (or a superuser) able to
    read it.  Returns {"ok", "problems", ...}; a problem names the check that found it."""
    seq = resolve_at_seq(conn, tenant, at_seq, at_block)
    ctx = Ctx(conn=conn, tenant=tenant, at_seq=seq, expect_root=expect_root)
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
    return {
        "ok": not problems, "problems": problems, "tenant": tenant, "at_seq": seq, "history_seq": ctx.counter,
        "sealed_seq": ctx.sealed_seq, "sealed": bool(covering), "state_root": ctx.root,
        "record_count": len(ctx.folded.live), "deleted_count": ctx.folded.deleted, "entry_count": len(ctx.entries),
        "block": {"height": covering[0], "block_hash": covering[1]} if covering else None,
    }


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
            result = verify_replay(conn, args.tenant, at_seq=args.at_seq, at_block=args.at_block, expect_root=args.expect_root)
        except ReplayError as exc:
            print(f"{exc.code}: {exc.message}", file=sys.stderr)
            return 2
    for p in result["problems"]:
        print(f"PROBLEM tenant={args.tenant} {p['subject']} [{p['check']}]: {p['problem']}")
    if result["problems"]:
        return 1
    where = f"block {result['block']['height']} ({result['block']['block_hash'][:16]}...)" if result["block"] else "not covered by a sealed block"
    print(f"ok: replayed {result['entry_count']} entries to seq {result['at_seq']}: {result['record_count']} record(s), "
          f"{result['deleted_count']} deleted, state root {result['state_root']}; {where}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
