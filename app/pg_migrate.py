"""Apply the ledger schema:  python -m app.pg_migrate

Uses JARVIS_DATABASE_MIGRATE_URL (a role allowed to run DDL), falling back to
JARVIS_DATABASE_URL.  Optional: JARVIS_DATABASE_SCHEMA, JARVIS_DATABASE_APP_ROLE.
Never prints the connection string.
"""

from __future__ import annotations

import os
import sys

from app.pg_schema import migrate


def main() -> int:
    dsn = (os.getenv("JARVIS_DATABASE_MIGRATE_URL") or os.getenv("JARVIS_DATABASE_URL") or "").strip()
    if not dsn:
        print("Set JARVIS_DATABASE_MIGRATE_URL (or JARVIS_DATABASE_URL).", file=sys.stderr)
        return 2
    version = migrate(
        dsn,
        schema=(os.getenv("JARVIS_DATABASE_SCHEMA") or "").strip() or None,
        app_role=(os.getenv("JARVIS_DATABASE_APP_ROLE") or "").strip() or None,
    )
    print(f"ledger schema is at version {version}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
