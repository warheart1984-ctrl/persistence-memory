"""One-way import of a JSON ledger (or the legacy JSONB blob) into the row-level schema.

    python -m app.pg_import --source PATH [--tenant operator] [--apply] [--resume]
                            [--null-dangling-supersedes] [--manifest OUT.json] [--verify]
    python -m app.pg_import --source-blob TENANT_KEY [--blob-schema public] --tenant NAME ...

* **Dry-run by default.**  The whole import runs inside one transaction, is verified, and is
  rolled back unless ``--apply`` is given - so the database itself judges every record.
* **The source is only read.**  The JSON file is never written, renamed or deleted (legacy rows
  are migrated in memory only); its hash is re-checked before commit and a change aborts.
* **Verified before commit:** record count, a canonical sha256 of every record, the board, one
  ``backfill`` history entry per inserted record and an intact history chain.  Any mismatch
  rolls everything back.
* Imported history entries are marked ``op='backfill'`` / ``actor='import:backfill'``: they are
  snapshots at import time, never presented as past edits.  Original ids, timestamps, versions and
  content hashes are preserved.

Connection: JARVIS_DATABASE_MIGRATE_URL (falling back to JARVIS_DATABASE_URL) and the optional
JARVIS_DATABASE_SCHEMA.  The connection string is never printed.
Exit codes: 0 ok, 1 verify found differences, 2 aborted / error.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import psycopg
from psycopg import sql
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from app.models import MemoryBoard, MemoryRecord
from app.pg_schema import EXPECTED_SCHEMA_VERSION, check_schema_version, validate_schema_name
from app.pg_store import _COLUMNS, _record
from app.store import parse_ledger_document
from app.store_errors import StoreUnavailableError

ACTOR = "import:backfill"


class ImportAbort(Exception):
    """Stop the import; ``problems`` are printed for the operator."""

    def __init__(self, *problems: str):
        super().__init__("; ".join(problems))
        self.problems = list(problems)


# --- helpers ----------------------------------------------------------------------------------


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:  # read-only
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _parse_ts(value: str) -> datetime:
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)  # naive timestamps are treated as UTC
    return parsed.astimezone(timezone.utc)


def canonical(rec: MemoryRecord) -> str:
    """sha256 over a normalised, order-independent rendering of a record."""
    data = rec.model_dump()
    data["created_at"] = _parse_ts(data["created_at"]).isoformat()
    data["updated_at"] = _parse_ts(data["updated_at"]).isoformat()
    data["supersedes"] = data["supersedes"] or None
    blob = json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def canonical_board(board: MemoryBoard) -> str:
    return hashlib.sha256(json.dumps(board.model_dump(), sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def local_problems(rec: MemoryRecord) -> list[str]:
    """Mirror of the database CHECK constraints, so problems are reported before any insert."""
    problems = []
    if not 1 <= len(rec.id) <= 128:
        problems.append("id must be 1-128 characters")
    if not 1 <= len(rec.content) <= 2000:
        problems.append(f"content must be 1-2000 characters (is {len(rec.content)})")
    if len(rec.source_agent) > 128:
        problems.append("source_agent longer than 128 characters")
    if not 1 <= len(rec.session_id) <= 128:
        problems.append("session_id must be 1-128 characters")
    if rec.subject is not None and len(rec.subject) > 256:
        problems.append("subject longer than 256 characters")
    if len(rec.tags) > 32:
        problems.append(f"tags has {len(rec.tags)} entries (max 32)")
    if len(rec.evidence) > 32:
        problems.append(f"evidence has {len(rec.evidence)} entries (max 32)")
    if len(rec.content_sha256) != 64 or any(c not in "0123456789abcdef" for c in rec.content_sha256):
        problems.append("content_sha256 is not a 64-character lowercase hex digest")
    for name in ("created_at", "updated_at"):
        try:
            _parse_ts(getattr(rec, name))
        except (ValueError, TypeError):
            problems.append(f"{name} is not a valid ISO-8601 timestamp")
    return problems


@dataclass
class Plan:
    board: MemoryBoard
    records: list[MemoryRecord]  # insertion order: superseded targets first
    nulled: list[tuple[str, str]] = field(default_factory=list)  # (record id, dropped pointer)


def build_plan(raw: Any, *, null_dangling: bool) -> tuple[Plan | None, list[str]]:
    """Strictly parse the document, check limits, resolve pointers and order for the FK."""
    if not isinstance(raw, dict):
        return None, ["source is not a JSON object"]
    try:
        board, memories, _dirty = parse_ledger_document(raw)
    except StoreUnavailableError as exc:
        return None, [str(exc)]
    problems: list[str] = []
    records = dict(memories)
    nulled: list[tuple[str, str]] = []
    for rec in list(records.values()):
        for text in local_problems(rec):
            problems.append(f"{rec.id}: {text}")
        if rec.supersedes and rec.supersedes != rec.id and rec.supersedes not in records:
            if null_dangling:
                nulled.append((rec.id, rec.supersedes))
                records[rec.id] = rec.model_copy(update={"supersedes": None})
            else:
                problems.append(
                    f"{rec.id}: supersedes {rec.supersedes!r}, which is not in the source "
                    "(use --null-dangling-supersedes to drop such pointers)"
                )
    ordered: list[MemoryRecord] = []
    placed: set[str] = set()
    pending = dict(records)
    while pending:
        ready = [
            r for r in pending.values()
            if not r.supersedes or r.supersedes == r.id or r.supersedes in placed or r.supersedes not in pending
        ]
        if not ready:
            problems.append(f"supersedes cycle among: {', '.join(sorted(pending))}")
            break
        for rec in sorted(ready, key=lambda r: r.id):
            ordered.append(rec)
            placed.add(rec.id)
            del pending[rec.id]
    return (None if problems else Plan(board=board, records=ordered, nulled=nulled)), problems


# --- database phase -----------------------------------------------------------------------------


@dataclass
class Result:
    inserted: int = 0
    skipped: int = 0
    differences: list[str] = field(default_factory=list)


def _insert(conn: psycopg.Connection, tenant: str, rec: MemoryRecord) -> None:
    conn.execute(
        "INSERT INTO memories (tenant_key, id, version, content, content_sha256, created_at, updated_at, "
        "source_agent, session_id, type, status, confidence, subject, supersedes, tags, evidence) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::text[],%s)",
        (
            tenant, rec.id, rec.version, rec.content, rec.content_sha256, _parse_ts(rec.created_at),
            _parse_ts(rec.updated_at), rec.source_agent, rec.session_id, rec.type, rec.status,
            rec.confidence, rec.subject, rec.supersedes or None, list(rec.tags),
            Jsonb([e.model_dump() for e in rec.evidence]),
        ),
    )


def _compare(conn: psycopg.Connection, tenant: str, plan: Plan) -> list[str]:
    """Differences between the tenant's rows/board and the plan (empty list = identical)."""
    diffs: list[str] = []
    rows = conn.execute(f"SELECT {_COLUMNS} FROM memories WHERE tenant_key = %s", (tenant,)).fetchall()
    db = {r["id"]: canonical(_record(r)) for r in rows}
    want = {r.id: canonical(r) for r in plan.records}
    for rid in sorted(set(want) - set(db)):
        diffs.append(f"{rid}: missing in the database")
    for rid in sorted(set(db) - set(want)):
        diffs.append(f"{rid}: in the database but not in the source")
    for rid in sorted(set(want) & set(db)):
        if want[rid] != db[rid]:
            diffs.append(f"{rid}: differs from the source")
    brow = conn.execute("SELECT board FROM boards WHERE tenant_key = %s", (tenant,)).fetchone()
    if brow is None or canonical_board(MemoryBoard(**brow["board"])) != canonical_board(plan.board):
        diffs.append("board: missing or differs from the source")
    return diffs


def _history_problems(conn: psycopg.Connection, tenant: str) -> list[str]:
    rows = conn.execute("SELECT memory_id, problem FROM jarvis_verify_history(%s)", (tenant,)).fetchall()
    return [f"history: {r['memory_id'] or '-'}: {r['problem']}" for r in rows]


def execute(conn: psycopg.Connection, tenant: str, plan: Plan, *, resume: bool, verify_only: bool) -> Result:
    """Run (or, for verify_only, just check) inside the caller's transaction."""
    result = Result()
    existing_rows = conn.execute(f"SELECT {_COLUMNS} FROM memories WHERE tenant_key = %s", (tenant,)).fetchall()
    existing = {r["id"]: canonical(_record(r)) for r in existing_rows}
    want = {r.id: canonical(r) for r in plan.records}

    if verify_only:
        result.differences = _compare(conn, tenant, plan) + _history_problems(conn, tenant)
        return result

    if existing and not resume:
        raise ImportAbort(
            f"target tenant {tenant!r} already has {len(existing)} record(s); refusing to import into it "
            "(use --resume only to complete an interrupted import of the same source)"
        )
    extra = sorted(set(existing) - set(want))
    if extra:
        raise ImportAbort(f"target has records that are not in the source: {', '.join(extra[:10])}")
    changed = sorted(i for i in existing if existing[i] != want[i])
    if changed:
        raise ImportAbort(*[f"{i}: already in the target but differs from the source" for i in changed[:20]])

    board_row = conn.execute("SELECT board FROM boards WHERE tenant_key = %s", (tenant,)).fetchone()
    if board_row is not None and canonical_board(MemoryBoard(**board_row["board"])) != canonical_board(plan.board):
        raise ImportAbort("target board already exists and differs from the source")

    inserted_ids: list[str] = []
    for rec in plan.records:
        if rec.id in existing:
            result.skipped += 1
            continue
        _insert(conn, tenant, rec)
        inserted_ids.append(rec.id)
    result.inserted = len(inserted_ids)
    if board_row is None:
        conn.execute(
            "INSERT INTO boards (tenant_key, board) VALUES (%s, %s)", (tenant, Jsonb(plan.board.model_dump()))
        )

    problems = _compare(conn, tenant, plan)
    if inserted_ids:
        marks = conn.execute(
            "SELECT memory_id, op, actor FROM record_history WHERE tenant_key = %s AND memory_id = ANY(%s)",
            (tenant, inserted_ids),
        ).fetchall()
        by_id: dict[str, list[tuple[str, str]]] = {}
        for m in marks:
            by_id.setdefault(m["memory_id"], []).append((m["op"], m["actor"]))
        for rid in inserted_ids:
            if by_id.get(rid) != [("backfill", ACTOR)]:
                problems.append(f"{rid}: expected exactly one backfill history entry, found {by_id.get(rid)}")
    problems += _history_problems(conn, tenant)
    if problems:
        raise ImportAbort("post-import verification failed:", *problems[:50])
    return result


# --- CLI --------------------------------------------------------------------------------------------


def _dsn() -> str:
    return (os.getenv("JARVIS_DATABASE_MIGRATE_URL") or os.getenv("JARVIS_DATABASE_URL") or "").strip()


def _use_schema(conn: psycopg.Connection, schema: str | None) -> None:
    if schema:
        conn.execute(sql.SQL("SET LOCAL search_path TO {}").format(sql.Identifier(validate_schema_name(schema))))


def _read_blob(dsn: str, tenant_key: str, schema: str) -> Any:
    with psycopg.connect(dsn, connect_timeout=5) as conn:
        conn.execute("SET TRANSACTION READ ONLY")
        _use_schema(conn, schema)
        row = conn.execute(
            "SELECT payload FROM jarvis_tenant_ledgers WHERE tenant_key = %s", (tenant_key,)
        ).fetchone()
    if row is None:
        raise ImportAbort(f"no legacy blob ledger for tenant_key {tenant_key!r}")
    return row[0]


def _describe(db_error: psycopg.Error) -> str:
    diag = getattr(db_error, "diag", None)
    primary = getattr(diag, "message_primary", None) or type(db_error).__name__
    constraint = getattr(diag, "constraint_name", None)
    return f"{primary} ({constraint})" if constraint else primary  # never row data, never the DSN


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.pg_import", description=__doc__.split("\n\n")[0])
    src = parser.add_mutually_exclusive_group(required=True)
    src.add_argument("--source", help="path of the JSON ledger (opened read-only)")
    src.add_argument("--source-blob", metavar="TENANT_KEY", help="read the legacy jarvis_tenant_ledgers row")
    parser.add_argument("--blob-schema", default="public", help="schema holding jarvis_tenant_ledgers")
    parser.add_argument("--tenant", default="operator", help="target tenant_key (default: operator)")
    parser.add_argument("--apply", action="store_true", help="commit (default is a rolled-back dry run)")
    parser.add_argument("--resume", action="store_true", help="complete an interrupted import of the same source")
    parser.add_argument("--verify", action="store_true", help="compare the database to the source; write nothing")
    parser.add_argument("--null-dangling-supersedes", action="store_true")
    parser.add_argument("--manifest", help="write counts and per-record hashes here (not next to the source)")
    args = parser.parse_args(argv)
    if args.verify and (args.apply or args.resume):
        print("--verify cannot be combined with --apply/--resume")
        return 2

    dsn = _dsn()
    schema = (os.getenv("JARVIS_DATABASE_SCHEMA") or "").strip() or None
    source_path: Path | None = None
    source_hash: str | None = None
    try:
        if args.source:
            source_path = Path(args.source)
            if args.manifest:
                out = Path(args.manifest).resolve()
                if out == source_path.resolve() or out.parent == source_path.resolve().parent:
                    raise ImportAbort("--manifest must not be written next to (or over) the source file")
            if not source_path.is_file():
                raise ImportAbort(f"source file not found: {source_path.name}")
            source_hash = _sha256_file(source_path)
            data = source_path.read_bytes()
            if hashlib.sha256(data).hexdigest() != source_hash:
                raise ImportAbort("source changed while it was being read; stop the writer and retry")
            try:
                raw = json.loads(data.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                raise ImportAbort("source is not valid UTF-8 JSON") from None
            label = source_path.name
        else:
            if not dsn:
                raise ImportAbort("set JARVIS_DATABASE_MIGRATE_URL (or JARVIS_DATABASE_URL) to read the blob table")
            raw = _read_blob(dsn, args.source_blob, args.blob_schema)
            label = f"blob:{args.source_blob}"
        plan, problems = build_plan(raw, null_dangling=args.null_dangling_supersedes)
    except ImportAbort as exc:
        for line in exc.problems:
            print(f"ABORT: {line}")
        return 2
    except psycopg.Error as exc:
        print(f"ABORT: database error while reading the source: {_describe(exc)}")
        return 2

    mode = "VERIFY" if args.verify else ("APPLY" if args.apply else "DRY RUN")
    print(f"{mode}: source={label} tenant={args.tenant}" + (f" sha256={source_hash[:12]}" if source_hash else ""))
    for line in problems:
        print(f"PROBLEM {line}")
    if plan is None:
        return 2
    statuses = ", ".join(f"{k}={v}" for k, v in sorted(Counter(r.status for r in plan.records).items()))
    types = ", ".join(f"{k}={v}" for k, v in sorted(Counter(r.type for r in plan.records).items()))
    print(f"  {len(plan.records)} records (status {statuses or '-'}; type {types or '-'})")
    for rid, dropped in plan.nulled:
        print(f"  NULLED supersedes on {rid} (was {dropped})")

    if not dsn:
        if args.apply or args.verify:
            print("ABORT: set JARVIS_DATABASE_MIGRATE_URL (or JARVIS_DATABASE_URL)")
            return 2
        print("  no database configured: analysis only (nothing was checked against Postgres)")
        return 0

    exit_code = 0
    try:
        with psycopg.connect(dsn, connect_timeout=5, row_factory=dict_row) as conn:
            _use_schema(conn, schema)
            try:
                check_schema_version(conn)
            except StoreUnavailableError as exc:
                raise ImportAbort(f"{exc} (python -m app.pg_migrate first; this import needs schema {EXPECTED_SCHEMA_VERSION})")
            conn.execute("SELECT set_config('jarvis.tenant_key', %s, true)", (args.tenant,))
            conn.execute("SELECT set_config('jarvis.actor', %s, true)", (ACTOR,))
            conn.execute("SELECT set_config('jarvis.history_op', 'backfill', true)")
            try:
                result = execute(conn, args.tenant, plan, resume=args.resume, verify_only=args.verify)
                if source_path is not None and _sha256_file(source_path) != source_hash:
                    raise ImportAbort("source file changed during the import; rolled back")
            except ImportAbort:
                conn.rollback()
                raise
            if args.verify:
                conn.rollback()
                if result.differences:
                    for line in result.differences[:50]:
                        print(f"DIFFERENCE {line}")
                    return 1
                print(f"verified: {len(plan.records)} records, board and history chain match the source")
                exit_code = 0
            elif args.apply:
                conn.commit()
                print(f"APPLIED: inserted {result.inserted}, skipped {result.skipped} already identical; "
                      "verified record hashes, board and history chain before commit")
            else:
                conn.rollback()
                print(f"  would insert {result.inserted}, skip {result.skipped}; verified record hashes, "
                      "board and history chain")
                print("  rolled back: no changes were made (re-run with --apply to commit)")
    except ImportAbort as exc:
        for line in exc.problems:
            print(f"ABORT: {line}")
        return 2
    except psycopg.Error as exc:
        print(f"ABORT: database error, rolled back: {_describe(exc)}")
        return 2

    if args.manifest and exit_code == 0:
        manifest = {
            "tenant": args.tenant,
            "source": label,
            "source_sha256": source_hash,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "schema_version": EXPECTED_SCHEMA_VERSION,
            "applied": bool(args.apply),
            "record_count": len(plan.records),
            "board_sha256": canonical_board(plan.board),
            "records": [{"id": r.id, "sha256": canonical(r)} for r in plan.records],
        }
        Path(args.manifest).write_text(json.dumps(manifest, indent=2), "utf-8")
        print(f"  manifest written: {Path(args.manifest).name}")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
