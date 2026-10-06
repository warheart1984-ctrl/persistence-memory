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

from app import evidence as evidence_objects
from app.pg_schema import check_schema_version, validate_schema_name


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
    if problems:
        for tenant, memory_id, problem in problems:
            print(f"PROBLEM tenant={tenant} record={memory_id}: {problem}")
        return 1
    note = f"; {evidence_total} evidence object(s) re-hashed" if evidence_total else ""
    print(f"ok: history intact for {len(tenants)} tenant(s){note}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
