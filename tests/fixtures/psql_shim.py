"""A stand-in for ``psql -X -At -d jarvis -c SQL`` for the anchor tests.

The deploy scripts address the ledger as the schema ``jarvis``; the tests use a throwaway schema, so every
``jarvis.`` in the SQL is rewritten to it.  Environment: SHIM_DSN, SHIM_SCHEMA.  Output follows ``psql -At``
(booleans as t/f, columns joined with ``|``); COPY ... TO STDOUT streams its rows as they are.
"""

from __future__ import annotations

import os
import sys

import psycopg


def main(argv: list[str]) -> int:
    args = argv[:]
    sql = None
    while args:
        a = args.pop(0)
        if a == "-c":
            sql = args.pop(0)
    if sql is None:
        print("psql_shim: only -c SQL is supported", file=sys.stderr)
        return 2
    schema = os.environ["SHIM_SCHEMA"]
    sql = sql.replace("jarvis.", f"{schema}.")
    with psycopg.connect(os.environ["SHIM_DSN"], autocommit=True) as conn:
        if sql.lstrip().upper().startswith("COPY"):
            with conn.cursor().copy(sql) as copy:
                for chunk in copy:
                    sys.stdout.write(bytes(chunk).decode("utf-8"))
        else:
            for row in conn.execute(sql):
                sys.stdout.write("|".join("t" if v is True else "f" if v is False else str(v) for v in row) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
