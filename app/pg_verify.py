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
    if problems:
        for tenant, memory_id, problem in problems:
            print(f"PROBLEM tenant={tenant} record={memory_id}: {problem}")
        return 1
    print(f"ok: history intact for {len(tenants)} tenant(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
