#!/usr/bin/env python3
"""Cross-agent acceptance check for ``emr_latest`` on a SCRATCH stack (never a live ledger).

What it proves: two independent MCP clients, connecting the way Devin and OpenCode do (JSON-RPC over POST /mcp, each with its
own session and ``clientInfo``), both call ``emr_latest`` with no id and no keyword and get the same top record and the same
``result_digest``; after one new record is written, both report the new record as newest, with matching digests that differ
from round one.

Safety: this script starts its OWN throwaway server on an ephemeral loopback port, in a temporary directory, with a freshly
generated API key.  It has no option to point at an existing server, and it refuses the live ledger's ports (8011, and the
older 8001/8002) on every spelling.  Postgres runs only when JARVIS_TEST_PG_DSN names a throwaway server on this machine,
and the report says plainly whether it ran; a skipped backend is never reported as a pass.

Usage:  python scripts/emr_latest_crosscheck.py [--out report.json] [--backend json|postgres|all]
Exit status: 0 only if every backend that ran passed (and at least one ran; ``--backend postgres`` fails if it could not run).
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

REPO = Path(__file__).resolve().parents[1]
LIVE_PORTS = frozenset({8011, 8001, 8002})
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", "[::1]"})
CLIENTS = ("devin", "opencode")
SEEDED = 5


class CrosscheckError(Exception):
    pass


# ----------------------------------------------------------------------------------------------------- guards


def refuse_live(target: str) -> None:
    """Raise if ``target`` (a URL, ``host:port`` or bare port) names a live-ledger port.  Every spelling of loopback counts."""
    text = str(target).strip()
    if re.fullmatch(r"\d+", text):
        port = int(text)
    else:
        parts = urlsplit(text if "://" in text else f"//{text}")
        try:
            port = parts.port
        except ValueError:
            port = None
        if port is None and parts.netloc:
            m = re.search(r":(\d+)$", parts.netloc)
            port = int(m.group(1)) if m else None
    if port in LIVE_PORTS:
        raise CrosscheckError(f"refusing {target!r}: port {port} belongs to a live ledger; this check only runs on a scratch stack")


def scratch_dsn_ok(dsn: str) -> tuple[bool, str]:
    """A scratch Postgres must be on this machine (loopback), so the check can never touch a remote or live database."""
    from psycopg.conninfo import conninfo_to_dict

    info = conninfo_to_dict(dsn)
    host = (info.get("host") or "localhost").split(",")[0]
    if host not in LOOPBACK_HOSTS and not host.startswith("/"):
        return False, f"JARVIS_TEST_PG_DSN host {host!r} is not loopback; refusing to treat it as a scratch database"
    port = info.get("port") or "5432"
    try:
        refuse_live(str(port))
    except CrosscheckError as exc:
        return False, str(exc)
    return True, ""


# ----------------------------------------------------------------------------------------------------- the scratch server


def free_port() -> int:
    for _ in range(50):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        if port not in LIVE_PORTS:
            return port
    raise CrosscheckError("could not find a free scratch port")


def http(method: str, url: str, *, headers: dict[str, str] | None = None, body: Any = None, timeout: float = 30.0) -> tuple[int, dict[str, str], Any]:
    refuse_live(url)
    data = None if body is None else json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, method=method, headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
            return resp.status, {k.lower(): v for k, v in resp.headers.items()}, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            parsed: Any = json.loads(raw)
        except ValueError:
            parsed = raw
        return exc.code, {k.lower(): v for k, v in exc.headers.items()}, parsed


@contextlib.contextmanager
def scratch_server(extra_env: dict[str, str]):
    """Run the app on an ephemeral loopback port in a temp dir; yield (base_url, api_key)."""
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    refuse_live(base)
    api_key = secrets.token_hex(16)
    tmp = tempfile.mkdtemp(prefix="emr-latest-xcheck-")
    try:
        tmp_path = Path(tmp)
        env = {k: v for k, v in os.environ.items() if not k.startswith(("JARVIS_", "EMR_"))}
        env.update(
            {
                "JARVIS_API_KEY": api_key,
                "JARVIS_STORE_PATH": str(tmp_path / "store.json"),
                "JARVIS_TENANT_STORE_DIR": str(tmp_path / "tenants"),
                "JARVIS_STORE_BOOTSTRAP": "1",
                "JARVIS_MEMORY_WRITE_ENABLED": "true",
                "JARVIS_CLAUSE_V": "off",  # seed plain facts without evidence on this throwaway stack
                "PYTHONPATH": str(REPO),
                "PYTHONUTF8": "1",
                **extra_env,
            }
        )
        log_path = tmp_path / "server.log"
        with open(log_path, "wb") as log:
            proc = subprocess.Popen(
                [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning"],
                cwd=tmp, env=env, stdout=log, stderr=subprocess.STDOUT,
            )
        try:
            deadline = time.time() + 60
            while True:
                if proc.poll() is not None:
                    raise CrosscheckError("scratch server exited early:\n" + log_path.read_text(encoding="utf-8", errors="replace")[-2000:])
                try:
                    status, _, _ = http("GET", f"{base}/health", timeout=2)
                    if status == 200:
                        break
                except (urllib.error.URLError, OSError, ConnectionError):
                    pass
                if time.time() > deadline:
                    raise CrosscheckError("scratch server did not become healthy in 60s")
                time.sleep(0.3)
            yield base, api_key
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=10)
    finally:
        for _ in range(20):  # Windows can hold the log file for a moment after the process exits
            try:
                shutil.rmtree(tmp)
                break
            except OSError:
                time.sleep(0.25)


# ----------------------------------------------------------------------------------------------------- an MCP client


class McpClient:
    """A minimal independent MCP client: its own session, its own clientInfo, key sent the way the stdio proxies send it."""

    def __init__(self, name: str, base: str, api_key: str):
        self.name, self.base = name, base
        self.headers = {"Accept": "application/json, text/event-stream", "X-API-Key": api_key}
        self._id = 0
        status, hdrs, body = self._rpc("initialize", {"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": name, "version": "1.0"}})
        if status != 200 or "result" not in (body or {}):
            raise CrosscheckError(f"{name}: initialize failed: {status} {body}")
        session = hdrs.get("mcp-session-id")
        if session:
            self.headers["Mcp-Session-Id"] = session

    def _rpc(self, method: str, params: dict[str, Any]) -> tuple[int, dict[str, str], Any]:
        self._id += 1
        return http("POST", f"{self.base}/mcp", headers=self.headers, body={"jsonrpc": "2.0", "id": self._id, "method": method, "params": params})

    def call(self, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
        status, _, body = self._rpc("tools/call", {"name": tool, "arguments": arguments})
        result = (body or {}).get("result") if isinstance(body, dict) else None
        if status != 200 or result is None or result.get("isError"):
            raise CrosscheckError(f"{self.name}: {tool} failed: {status} {body}")
        return result["structuredContent"]

    def tool_names(self) -> list[str]:
        _, _, body = self._rpc("tools/list", {})
        return [t["name"] for t in body["result"]["tools"]]


# ----------------------------------------------------------------------------------------------------- the check


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def write_record(base: str, api_key: str, content: str) -> str:
    status, _, body = http(
        "POST", f"{base}/api/jarvis/memory", headers={"X-API-Key": api_key},
        body={"content": content, "source_agent": "emr-latest-crosscheck", "session_id": "crosscheck", "type": "fact"},
    )
    if status != 200:
        raise CrosscheckError(f"seeding failed: {status} {body}")
    time.sleep(0.01)
    return body["memory"]["id"]


def run_round(clients: list[McpClient]) -> list[dict[str, Any]]:
    rows = []
    for client in clients:
        out = client.call("emr_latest", {})  # no id, no keyword, no parameters at all
        if not out["records"]:
            raise CrosscheckError(f"{client.name}: emr_latest returned no records")
        rows.append(
            {
                "client": client.name,
                "top_id": out["records"][0]["id"],
                "top_created_at": out["records"][0]["created_at"],
                "result_digest": out["result_digest"],
                "ledger_head": out["ledger_head"],
                "record_count": len(out["records"]),
                "timestamp": now_iso(),
            }
        )
    return rows


def check_backend(name: str, extra_env: dict[str, str]) -> dict[str, Any]:
    report: dict[str, Any] = {"backend": name, "ran": True, "ok": False, "rounds": [], "failures": []}
    with scratch_server(extra_env) as (base, api_key):
        report["scratch_port"] = int(base.rsplit(":", 1)[1])
        clients = [McpClient(n, base, api_key) for n in CLIENTS]
        for c in clients:
            if "emr_latest" not in c.tool_names():
                raise CrosscheckError(f"{c.name}: emr_latest is not in tools/list")
        seeded = [write_record(base, api_key, f"crosscheck seed {i} on {name}") for i in range(SEEDED)]
        report["seeded"] = SEEDED

        round1 = run_round(clients)
        report["rounds"].append({"round": 1, "clients": round1})
        if len({r["top_id"] for r in round1}) != 1:
            report["failures"].append("round 1: the clients disagree on the newest record")
        if len({r["result_digest"] for r in round1}) != 1:
            report["failures"].append("round 1: the clients disagree on result_digest")
        if round1[0]["top_id"] != seeded[-1]:
            report["failures"].append("round 1: the newest record is not the last one seeded")

        new_id = write_record(base, api_key, f"crosscheck NEW record on {name}")
        report["new_record"] = new_id
        round2 = run_round(clients)
        report["rounds"].append({"round": 2, "clients": round2})
        if {r["top_id"] for r in round2} != {new_id}:
            report["failures"].append("round 2: not both clients report the new record as newest")
        if len({r["result_digest"] for r in round2}) != 1:
            report["failures"].append("round 2: the clients disagree on result_digest")
        if round2[0]["result_digest"] == round1[0]["result_digest"]:
            report["failures"].append("round 2: the digest did not change after a write")
    report["ok"] = not report["failures"]
    return report


def run_postgres(dsn: str) -> dict[str, Any]:
    import psycopg
    from psycopg.conninfo import make_conninfo

    sys.path.insert(0, str(REPO))
    from app.pg_schema import migrate

    schema, role, password = f"xchk_{uuid.uuid4().hex[:12]}", f"jarvis_xchk_{uuid.uuid4().hex[:8]}", secrets.token_hex(12)
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(f"CREATE ROLE \"{role}\" LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE PASSWORD '{password}'")
        conn.execute(f'CREATE SCHEMA "{schema}"')
    try:
        migrate(dsn, schema=schema, app_role=role)
        app_dsn = make_conninfo(dsn, user=role, password=password)
        return check_backend(
            "postgres",
            {"JARVIS_DATABASE_URL": app_dsn, "JARVIS_DATABASE_SCHEMA": schema, "JARVIS_PG_STORE": "rows"},
        )
    finally:
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
            conn.execute(f'DROP OWNED BY "{role}"')
            conn.execute(f'DROP ROLE IF EXISTS "{role}"')


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--out", help="also write the JSON report to this file (utf-8)")
    ap.add_argument("--backend", choices=("json", "postgres", "all"), default="all")
    args = ap.parse_args(argv)

    report: dict[str, Any] = {"check": "emr_latest cross-agent", "scratch_stack_only": True, "started": now_iso(), "backends": []}
    try:
        if args.backend in ("json", "all"):
            report["backends"].append(check_backend("json", {}))
        if args.backend in ("postgres", "all"):
            dsn = (os.environ.get("JARVIS_TEST_PG_DSN") or "").strip()
            if not dsn:
                report["backends"].append({"backend": "postgres", "ran": False, "ok": False, "reason": "JARVIS_TEST_PG_DSN is not set (no scratch Postgres)"})
            else:
                allowed, why = scratch_dsn_ok(dsn)
                report["backends"].append(run_postgres(dsn) if allowed else {"backend": "postgres", "ran": False, "ok": False, "reason": why})
    except CrosscheckError as exc:
        report["error"] = str(exc)
    report["finished"] = now_iso()
    ran = [b for b in report["backends"] if b["ran"]]
    wanted_postgres_missing = args.backend == "postgres" and not any(b["backend"] == "postgres" and b["ran"] for b in report["backends"])
    report["postgres"] = next(
        ({"ran": b["ran"], "ok": b["ok"], **({"reason": b["reason"]} if not b["ran"] else {})} for b in report["backends"] if b["backend"] == "postgres"),
        {"ran": False, "ok": False, "reason": "not requested"},
    )
    report["ok"] = bool(ran) and "error" not in report and all(b["ok"] for b in ran) and not wanted_postgres_missing
    text = json.dumps(report, indent=2, ensure_ascii=False)
    print(text)
    if args.out:
        Path(args.out).write_text(text + "\n", encoding="utf-8")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
