"""Per-tenant jobs for the box: seal, and a one-tenant request runner for the signer.  Runs in the ``migrate`` container (the migrator role).

    python -m app.tenant_jobs seal    --tenants operator,<tenant>,... [--force] [--json]
    python -m app.tenant_jobs request --tenant T METHOD PATH [--body-stdin]        one in-process request as the operator administering tenant T
    python -m app.tenant_jobs serve   --tenant T                                   the same, JSON lines on stdin/stdout (what the host signer drives)

Why a command-line tool and not the API: the public API must never be able to select a tenant (docs/tenant_isolation.md).  Cross-tenant
administration therefore happens only here, in a process the operator started on the box.  A tenant is chosen with ``identity.acting_for``, which nothing in
the HTTP stack sets and no request parameter, header or token can set.  The work is the same code the API runs (``store.seal_blocks``, the same routes through
the same router and dependencies), just reached with a tenant bound by the operator's own process.

Rules (the live timers are NOT changed by any of this; it is a new command, ``jarvisctl tenants``):

* **One tenant cannot break the others.**  Each tenant runs on its own; an exception is logged and recorded and the loop goes on.  The report names every
  tenant that failed and the exit code is 1: a failure is never skipped silently.
* **With only the operator tenant the output is what ``seal.sh`` logs today** (same lines), and the effect is the same blocks.
* The tenant list is passed in (``--tenants``): enumerating tenants needs the superuser path the backup scripts use, not this role (the migrator cannot bypass
  row-level security).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from typing import Any, Callable
from urllib.parse import urlsplit

from app.identity import acting_for

OPERATOR = "operator"
_log = logging.getLogger("jarvis.tenant_jobs")


def _use_the_migrator_role() -> None:
    """This runs as the migrator (the DDL role: the table owner, still bound by row-level security, so it sees only the tenant it is set to)."""
    migrate = (os.getenv("JARVIS_DATABASE_MIGRATE_URL") or "").strip()
    if migrate and not (os.getenv("JARVIS_DATABASE_URL") or "").strip():
        os.environ["JARVIS_DATABASE_URL"] = migrate
    os.environ.setdefault("JARVIS_PG_STORE", "rows")


def parse_tenants(text: str) -> list[str]:
    """The tenant keys of a comma-separated list: the operator first (so its output leads), the others in the order given, no duplicates, no blanks."""
    seen: list[str] = []
    for part in (text or "").split(","):
        t = part.strip()
        if t and t not in seen:
            seen.append(t)
    return sorted(seen, key=lambda t: (t != OPERATOR, seen.index(t)))


# ------------------------------------------------------------------------------------------------------------------------- the loop
def run_for_tenants(op: Callable[[str], dict[str, Any]], tenants: list[str]) -> dict[str, Any]:
    """Run ``op`` for each tenant on its own.  An exception in one is logged and recorded and never stops the others.

    Returns {"results": [one entry per tenant, in order], "failed": [tenant, ...]}: a failure always shows up in both."""
    results: list[dict[str, Any]] = []
    failed: list[str] = []
    for tenant in tenants:
        try:
            with acting_for(tenant):
                result = op(tenant)
            results.append({"tenant": tenant, "ok": True, **result})
        except Exception as exc:  # noqa: BLE001 - one tenant's failure must never stop the others, whatever it is
            _log.error("tenant %s failed: %s: %s", tenant, type(exc).__name__, exc)
            failed.append(tenant)
            results.append({"tenant": tenant, "ok": False, "error": f"{type(exc).__name__}: {str(exc)[:300]}"})
    return {"results": results, "failed": failed}


# --------------------------------------------------------------------------------------------------------------------------- seal
def seal_one(tenant: str, *, force: bool = False) -> dict[str, Any]:
    """Seal one tenant's unsealed history (the same ``seal_blocks`` the API route calls), as that tenant's own store."""
    from app.store import get_store

    store = get_store()
    if not hasattr(store, "seal_blocks"):
        raise RuntimeError("the ledger is not on the PostgreSQL row store; there are no blocks to seal")
    done = store.seal_blocks(force=force)
    return {"sealed": [{"height": b["height"], "block_hash": b["block_hash"]} for b in done["sealed"]], "reason": done["reason"], "head": done["head"]}


def _summary(head: dict[str, Any]) -> str:
    """The line seal.sh prints after sealing: identical text."""
    tip = head.get("tip")
    return "newest block: %s, sealed through seq %s, %s entries unsealed (history at seq %s)" % (
        tip["height"] if tip else "none", head["sealed_seq"], head["unsealed_entries"], head["history_seq"])


def seal_lines(report: dict[str, Any]) -> list[str]:
    """The log lines for a seal report.  For the operator alone they are exactly the lines seal.sh logs; another tenant's carry its key."""
    lines: list[str] = []
    many = len(report["results"]) > 1
    for r in report["results"]:
        prefix = "" if r["tenant"] == OPERATOR else f"tenant {r['tenant']}: "
        if not r["ok"]:
            lines.append(f"ERROR {prefix}sealing failed: {r['error']}")
        elif r["sealed"]:
            heights = ",".join(str(b["height"]) for b in r["sealed"])
            lines.append(f"{prefix}sealed {len(r['sealed'])} block(s) (height {heights}); {_summary(r['head'])}")
        else:
            lines.append(f"{prefix}nothing sealed: {r['reason']}; {_summary(r['head'])}")
    if report["failed"]:
        lines.append(f"ERROR tenants that FAILED: {', '.join(report['failed'])} ({len(report['results']) - len(report['failed'])} of {len(report['results'])} ok)")
    elif many:
        lines.append(f"all {len(report['results'])} tenants ok")
    return lines


def seal(tenants: list[str], *, force: bool = False) -> dict[str, Any]:
    _use_the_migrator_role()
    return run_for_tenants(lambda t: seal_one(t, force=force), tenants)


# ------------------------------------------------------------------------------------------------- one request, in process, as a tenant
async def _asgi(method: str, path: str, body: dict[str, Any] | None) -> tuple[int, Any]:
    """Drive the application's own router (so its routes and dependencies run) with the request, without the HTTP middlewares and without a socket."""
    from app.main import app

    split = urlsplit(path)
    payload = json.dumps(body).encode() if body is not None else b""
    headers = [(b"content-type", b"application/json"), (b"content-length", str(len(payload)).encode())]
    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": method.upper(), "scheme": "http", "path": split.path,
             "raw_path": split.path.encode(), "query_string": split.query.encode(), "root_path": "", "headers": headers, "server": ("tenant-jobs", 0),
             "client": ("tenant-jobs", 0), "app": app, "state": {}}
    sent: list[dict[str, Any]] = []
    consumed = False

    async def receive() -> dict[str, Any]:
        nonlocal consumed
        if consumed:
            return {"type": "http.disconnect"}
        consumed = True
        return {"type": "http.request", "body": payload, "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    # the router, behind ONLY the layer that turns an HTTPException (a 404, a 403) and the app's registered handlers into responses: not the HTTP middlewares
    # (no API key, CORS or call witness: this is the operator's own process, and the tenant is bound by it)
    from starlette.middleware.exceptions import ExceptionMiddleware

    await ExceptionMiddleware(app.router, handlers=app.exception_handlers)(scope, receive, send)
    status = next((m["status"] for m in sent if m["type"] == "http.response.start"), 500)
    raw = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    try:
        return status, json.loads(raw) if raw else {}
    except ValueError:
        return status, {"detail": raw.decode("utf-8", "replace")[:300]}


def request_as(tenant: str, method: str, path: str, body: dict[str, Any] | None = None) -> tuple[int, Any]:
    """One request, as the operator administering ``tenant``.  The tenant is bound here, by this process: the request cannot name one."""
    _use_the_migrator_role()
    with acting_for(tenant):
        return asyncio.run(_asgi(method, path, body))


def serve(tenant: str, stdin=None, stdout=None) -> int:
    """JSON lines in ({"method", "path", "body"}), JSON lines out ({"status", "body"}); ends at EOF.  What the host signer's transport drives."""
    stdin, stdout = stdin or sys.stdin, stdout or sys.stdout
    for line in stdin:
        if not line.strip():
            continue
        try:
            req = json.loads(line)
            status, body = request_as(tenant, str(req["method"]), str(req["path"]), req.get("body"))
        except Exception as exc:  # noqa: BLE001 - the caller needs an answer for every line, and a bad line must not end the session
            status, body = 500, {"detail": f"{type(exc).__name__}: {str(exc)[:200]}"}
        stdout.write(json.dumps({"status": status, "body": body}) + "\n")
        stdout.flush()
    return 0


# ---------------------------------------------------------------------------------------------------------------------------- main
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.tenant_jobs", description="Per-tenant jobs for the box (the public API never selects a tenant)")
    sub = parser.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("seal")
    s.add_argument("--tenants", required=True, help="comma-separated tenant keys (the host lists them with the superuser path)")
    s.add_argument("--force", action="store_true")
    s.add_argument("--json", action="store_true", help="print the report as JSON instead of the log lines")
    r = sub.add_parser("request")
    r.add_argument("--tenant", required=True)
    r.add_argument("method")
    r.add_argument("path")
    r.add_argument("--body-stdin", action="store_true")
    v = sub.add_parser("serve")
    v.add_argument("--tenant", required=True)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stderr)

    if args.cmd == "seal":
        tenants = parse_tenants(args.tenants)
        if not tenants:
            print("no tenants given", file=sys.stderr)
            return 2
        report = seal(tenants, force=args.force)
        if args.json:
            print(json.dumps(report, indent=2))
        else:
            for line in seal_lines(report):
                print(line)
        return 1 if report["failed"] else 0
    if args.cmd == "request":
        body = json.loads(sys.stdin.read()) if args.body_stdin else None
        status, payload = request_as(args.tenant, args.method, args.path, body)
        print(json.dumps({"status": status, "body": payload}))
        return 0
    return serve(args.tenant)


if __name__ == "__main__":
    sys.exit(main())
