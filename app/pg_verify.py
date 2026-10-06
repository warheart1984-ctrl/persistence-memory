"""Verify the record-history hash chain:  python -m app.pg_verify [--tenant T | --all]

Uses JARVIS_DATABASE_MIGRATE_URL (falling back to JARVIS_DATABASE_URL) and the optional
JARVIS_DATABASE_SCHEMA.  ``--all`` iterates every tenant and needs a role that can see all
rows (superuser/BYPASSRLS); otherwise verify one tenant at a time.  Exit 0 = intact,
1 = problems found, 2 = misconfiguration.  Never prints the connection string.
"""

from __future__ import annotations

import argparse
import os
import sys

import psycopg
from psycopg import sql

from app import blocks as continuity_blocks
from app import evidence as evidence_objects
from app.pg_schema import check_schema_version, validate_schema_name

# A tail of unsealed history older than this is reported (a warning, not a failure): the seal timer may have stopped.
STALE_TAIL_HOURS = 24


def _block_problems(conn: psycopg.Connection, tenant: str) -> list[tuple[str, str]]:
    """Everything wrong with the tenant's sealed blocks: the database's own verifier first, then an independent
    recomputation here (a second implementation, so a bug or a tampered function in one is caught by the other)."""
    out: list[tuple[str, str]] = []
    for height, problem in conn.execute("SELECT height, problem FROM jarvis_verify_blocks(%s)", (tenant,)):
        out.append((f"block {height}" if height is not None else "blocks", problem))
    prev_hash, prev_last, expected_height = continuity_blocks.GENESIS_HASH, 0, 1
    rows = conn.execute(
        "SELECT height, first_seq, last_seq, entry_count, prev_block_hash, entries_root, block_hash, format "
        "FROM blocks WHERE tenant_key = %s ORDER BY height", (tenant,)
    ).fetchall()
    for height, first_seq, last_seq, count, prev, root, bhash, fmt in rows:
        label = f"block {height}"
        if height != expected_height:
            out.append((label, f"(recompute) expected height {expected_height}: a block is missing"))
        expected_height = height + 1
        if prev != prev_hash:
            out.append((label, "(recompute) prev_block_hash does not match the previous block"))
        if first_seq != prev_last + 1:
            out.append((label, f"(recompute) block does not continue the history (expected first_seq {prev_last + 1})"))
        leaves = [r[0] for r in conn.execute(
            "SELECT row_hash FROM record_history WHERE tenant_key = %s AND seq BETWEEN %s AND %s ORDER BY seq",
            (tenant, first_seq, last_seq))]
        if len(leaves) != count or count != last_seq - first_seq + 1:
            out.append((label, f"(recompute) entry count does not match the history in range (block says {count}, found {len(leaves)})"))
        elif continuity_blocks.merkle_root(leaves) != root:
            out.append((label, "(recompute) entries_root does not match the history entries in range"))
        if continuity_blocks.block_hash(
            tenant=tenant, height=height, first_seq=first_seq, last_seq=last_seq, entry_count=count,
            prev_block_hash=prev, entries_root=root, fmt=fmt,
        ) != bhash:
            out.append((label, "(recompute) block_hash does not match the block's contents"))
        prev_hash, prev_last = bhash, last_seq
    return out


def _block_evidence_problems(conn: psycopg.Connection, tenant: str) -> list[tuple[str, str]]:
    """Every evidence object cited by a sealed history entry must still exist and still hash to its id."""
    out: list[tuple[str, str]] = []
    for height, first_seq, last_seq in conn.execute(
        "SELECT height, first_seq, last_seq FROM blocks WHERE tenant_key = %s ORDER BY height", (tenant,)
    ).fetchall():
        refs = {r[0] for r in conn.execute(
            "SELECT DISTINCT e->>'ref' FROM record_history h, "
            "LATERAL (SELECT * FROM jsonb_array_elements(coalesce(h.after -> 'evidence', '[]'::jsonb)) "
            "          UNION ALL SELECT * FROM jsonb_array_elements(coalesce(h.before -> 'evidence', '[]'::jsonb))) AS x(e) "
            "WHERE h.tenant_key = %s AND h.seq BETWEEN %s AND %s AND e->>'kind' = 'evidence-object'",
            (tenant, first_seq, last_seq))}
        for ref in sorted(r for r in refs if r):
            row = conn.execute(
                "SELECT id, schema_id, payload, pointer, size_bytes, created_at, created_by "
                "FROM evidence_objects WHERE tenant_key = %s AND id = %s", (tenant, ref)).fetchone()
            if row is None:
                out.append((f"block {height}", f"cites evidence object {ref}, which does not exist"))
                continue
            obj = evidence_objects.EvidenceObject(
                id=row[0], schema_id=row[1], payload=row[2], pointer=row[3], size_bytes=row[4],
                created_at=row[5].isoformat(), created_by=row[6])
            for problem in evidence_objects.verify_stored(obj):
                out.append((f"block {height}", f"cites evidence object {ref}: {problem}"))
    return out


def _block_status(conn: psycopg.Connection, tenant: str) -> dict[str, object]:
    sealed_count, sealed_seq = conn.execute(
        "SELECT count(*), coalesce(max(last_seq), 0) FROM blocks WHERE tenant_key = %s", (tenant,)).fetchone()
    unsealed, age_hours = conn.execute(
        "SELECT count(*), extract(epoch FROM (now() - min(changed_at))) / 3600 "
        "FROM record_history WHERE tenant_key = %s AND seq > %s", (tenant, sealed_seq)).fetchone()
    return {"blocks": sealed_count, "sealed_seq": sealed_seq, "unsealed": unsealed, "oldest_unsealed_hours": age_hours}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.pg_verify")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--tenant", default="operator", help="tenant_key to verify (default: operator)")
    group.add_argument("--all", action="store_true", help="verify every tenant (needs a RLS-bypassing role)")
    args = parser.parse_args(argv)

    dsn = (os.getenv("JARVIS_DATABASE_MIGRATE_URL") or os.getenv("JARVIS_DATABASE_URL") or "").strip()
    if not dsn:
        print("Set JARVIS_DATABASE_MIGRATE_URL (or JARVIS_DATABASE_URL).", file=sys.stderr)
        return 2
    schema = (os.getenv("JARVIS_DATABASE_SCHEMA") or "").strip() or None

    problems: list[tuple[str, str, str]] = []
    evidence_total = 0
    block_notes: list[str] = []
    warnings: list[str] = []
    with psycopg.connect(dsn, connect_timeout=5) as conn:
        if schema:
            conn.execute(sql.SQL("SET LOCAL search_path TO {}").format(sql.Identifier(validate_schema_name(schema))))
        check_schema_version(conn)
        if args.all:
            conn.execute("SET LOCAL row_security = off")
            tenants = [r[0] for r in conn.execute("SELECT DISTINCT tenant_key FROM record_history ORDER BY 1")]
        else:
            tenants = [args.tenant]
        for tenant in tenants:
            conn.execute("SELECT set_config('jarvis.tenant_key', %s, true)", (tenant,))
            for _hid, memory_id, problem in conn.execute(
                "SELECT history_id, memory_id, problem FROM jarvis_verify_history(%s)", (tenant,)
            ):
                problems.append((tenant, memory_id, problem))
            if conn.execute("SELECT to_regclass('evidence_objects') IS NOT NULL").fetchone()[0]:
                for row in conn.execute(
                    "SELECT id, schema_id, payload, pointer, size_bytes, created_at, created_by "
                    "FROM evidence_objects WHERE tenant_key = %s ORDER BY id", (tenant,)
                ).fetchall():
                    obj = evidence_objects.EvidenceObject(
                        id=row[0], schema_id=row[1], payload=row[2], pointer=row[3], size_bytes=row[4],
                        created_at=row[5].isoformat(), created_by=row[6],
                    )
                    for problem in evidence_objects.verify_stored(obj):
                        problems.append((tenant, obj.id, f"evidence object: {problem}"))
                evidence_total += conn.execute(
                    "SELECT count(*) FROM evidence_objects WHERE tenant_key = %s", (tenant,)
                ).fetchone()[0]
            if conn.execute("SELECT to_regclass('blocks') IS NOT NULL").fetchone()[0]:
                for what, problem in _block_problems(conn, tenant):
                    problems.append((tenant, what, f"blocks: {problem}"))
                if conn.execute("SELECT to_regclass('evidence_objects') IS NOT NULL").fetchone()[0]:
                    for what, problem in _block_evidence_problems(conn, tenant):
                        problems.append((tenant, what, f"blocks: {problem}"))
                st = _block_status(conn, tenant)
                block_notes.append(
                    f"{st['blocks']} block(s) sealed through seq {st['sealed_seq']}, {st['unsealed']} entr{'y' if st['unsealed'] == 1 else 'ies'} unsealed")
                if st["unsealed"] and st["oldest_unsealed_hours"] is not None and st["oldest_unsealed_hours"] > STALE_TAIL_HOURS:
                    warnings.append(
                        f"WARNING tenant={tenant}: {st['unsealed']} unsealed entries, the oldest {st['oldest_unsealed_hours']:.0f}h old "
                        "(the seal timer may have stopped)")
    if problems:
        for tenant, memory_id, problem in problems:
            print(f"PROBLEM tenant={tenant} record={memory_id}: {problem}")
        return 1
    note = f"; {evidence_total} evidence object(s) re-hashed" if evidence_total else ""
    if block_notes:
        note += "; blocks intact: " + ("; ".join(block_notes) if len(tenants) > 1 else block_notes[0])
    print(f"ok: history intact for {len(tenants)} tenant(s){note}")
    for line in warnings:
        print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
