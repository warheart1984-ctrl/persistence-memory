#!/usr/bin/env python3
"""CL_CHAOS_100x: 100 rounds of the same probe list against a THROWAWAY clone of the Jarvis ledger stack.

    scripts/chaos/throwaway_stack.sh up                 # its own project, containers, images, volumes, port, secrets, TEST keys
    scripts/chaos/cl_chaos_100x.py --rounds 1           # a smoke round first
    scripts/chaos/cl_chaos_100x.py --rounds 100
    scripts/chaos/throwaway_stack.sh down

Safety, in this order, and never relaxed by the probes themselves:

* The default target is the throwaway stack described by ``<stack dir>/stack.json`` (written by ``throwaway_stack.sh up``).
* It REFUSES (exit 3) any target on port 8011 (or the port in deploy/mint/.env), any non-loopback host, and any target whose
  ``/ready`` does not report a ``chaos-throwaway:`` stack identity: the live stack reports ``jarvis-live`` once it runs a build
  that has the field, and none before; an unproven identity is refused, never assumed.  ``--i-know-this-is-live`` exists for a
  human who really means it; this task never passes it, and even with it the destructive probes need the throwaway proof.
* Destructive probes (database down, schema mismatch, pool flood, concurrent force-seal, the tamper-and-re-seal copy) run only
  when the containers named in stack.json carry the throwaway project label and the names ``chaos100x-*``.  They are SKIPPED, and
  reported as skipped, otherwise.
* Keys are test keys created inside the throwaway directory; the live key custody directory is never read.
* The ledger is bounded: small batches, a hard cap on history entries (``--max-history``), and a cap on blocks per probe.

The per-round probe count is ``len(PROBES)``; every number in the docs, the sample log and the tests is derived from it
(``--count`` prints it).  Standard library only (plus ``app.attest`` / ``app.signer`` / ``app.blocks``, which are stdlib-only).
"""

from __future__ import annotations

import argparse
import concurrent.futures
import dataclasses
from datetime import datetime
import hashlib
import http.client
import json
import os
import random
import re
import socket
import statistics
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from pathlib import Path
from typing import Any, Callable

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

LIVE_PORT = 8011
THROWAWAY_PREFIX = "chaos-throwaway:"
THROWAWAY_PROJECT = "jarvis-chaos100x"
THROWAWAY_NAME_PREFIX = "chaos100x-"
LIVE_NAMES = ("jarvis-db", "jarvis-app", "jarvis-migrate")
TENANT = "operator"
EXIT_OK, EXIT_PROBE_FAILURES, EXIT_USAGE, EXIT_REFUSED = 0, 1, 2, 3


# --- the guard ------------------------------------------------------------------------------------------------------------------------------

class Refusal(Exception):
    """The target is not provably a throwaway stack."""


def live_ports() -> set[int]:
    ports = {LIVE_PORT}
    env_file = REPO / "deploy" / "mint" / ".env"
    try:
        for line in env_file.read_text().splitlines():
            m = re.match(r"\s*JARVIS_APP_PORT\s*=\s*(\d+)", line)
            if m:
                ports.add(int(m.group(1)))
    except OSError:
        pass
    try:
        ports.add(int(os.environ.get("JARVIS_APP_PORT", "")))
    except ValueError:
        pass
    return ports


def _addresses(host: str) -> set[str]:
    try:
        return {info[4][0] for info in socket.getaddrinfo(host, None)}
    except OSError:
        return set()


def _is_loopback(address: str) -> bool:
    return address == "::1" or address.startswith("127.") or address == "::ffff:127.0.0.1"


def assess_target(url: str, fetch_ready: Callable[[str], tuple[int, dict[str, Any] | None]], *, allow_live: bool = False,
                  ports: set[int] | None = None) -> dict[str, Any]:
    """Decide whether ``url`` may be hammered.  Raises Refusal.  Returns {"url", "host", "port", "stack"}.

    Refusals (each lifted ONLY by allow_live, except the loopback rule, which nothing lifts): a non-local host; a live port; a
    /ready that cannot be read; one with no stack identity; one with an identity that is not a throwaway's."""
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "http" or not parsed.hostname:
        raise Refusal(f"{url!r} is not an http://host:port URL")
    host, port = parsed.hostname, parsed.port or 80
    addresses = _addresses(host)
    if not addresses or not all(_is_loopback(a) for a in addresses):
        raise Refusal(f"{host} is not a loopback address; the hammer only runs against a local throwaway stack")
    live = ports if ports is not None else live_ports()
    if port in live and not allow_live:
        raise Refusal(f"port {port} is the live stack's port; refusing (use a throwaway stack: scripts/chaos/throwaway_stack.sh up)")
    status, body = fetch_ready(url)
    if body is None:
        raise Refusal(f"{url}/ready could not be read (HTTP {status}); cannot prove the target is a throwaway stack")
    stack = body.get("stack")
    if not allow_live:
        if not stack:
            raise Refusal(f"{url}/ready reports no stack identity, so it cannot be proven to be a throwaway stack (an older build, or the live one)")
        if not str(stack).startswith(THROWAWAY_PREFIX):
            raise Refusal(f"{url}/ready reports the stack identity {stack!r}, which is not a throwaway ({THROWAWAY_PREFIX}...)")
    return {"url": url.rstrip("/"), "host": host, "port": port, "stack": stack}


def fetch_ready(url: str) -> tuple[int, dict[str, Any] | None]:
    try:
        with urllib.request.urlopen(url.rstrip("/") + "/ready", timeout=10) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read() or b"{}")
        except ValueError:
            return exc.code, None
    except (OSError, ValueError):
        return 0, None


def load_stack(directory: str | Path) -> dict[str, Any]:
    path = Path(directory) / "stack.json"
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise Refusal(f"no throwaway stack at {directory} ({exc}); run scripts/chaos/throwaway_stack.sh up") from None


def run_cmd(args: list[str], *, timeout: float = 120, input: str | None = None, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout, input=input, env=env)


def throwaway_proof(stack: dict[str, Any], target: dict[str, Any], runner: Callable[..., subprocess.CompletedProcess] = run_cmd) -> str | None:
    """None if the destructive probes may run, else why not: the containers must be the throwaway's by name AND by compose label, and
    the target must be the stack's own published port."""
    if target["port"] != stack.get("port") or target["port"] in live_ports():
        return "the target is not the throwaway stack's own port"
    for role, name in stack.get("containers", {}).items():
        if not name.startswith(THROWAWAY_NAME_PREFIX) or name in LIVE_NAMES:
            return f"container {name} is not a throwaway name"
        r = runner(["docker", "inspect", "-f", '{{index .Config.Labels "com.docker.compose.project"}}', name], timeout=30)
        if r.returncode != 0 or r.stdout.strip() != THROWAWAY_PROJECT:
            return f"container {name} does not carry the compose project label {THROWAWAY_PROJECT}"
    if Path(stack.get("keys_dir", "")).resolve().is_relative_to((Path.home() / "jarvis-ledger").resolve()):
        return "the test keys live inside the live ledger home"
    return None


# --- the client and the metrics ------------------------------------------------------------------------------------------------------------

@dataclasses.dataclass
class Response:
    status: int  # 0 = no connection, -1 = timeout
    json: Any
    headers: dict[str, str]
    ms: float
    text: str = ""


class Stats:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.requests = 0
        self.statuses: Counter = Counter()
        self.latencies: dict[str, list[float]] = {}
        self.five_xx: list[dict[str, Any]] = []
        self.current = {"round": 0, "probe": "-", "expect": ()}

    def record(self, method: str, path: str, resp: Response) -> None:
        with self.lock:
            probe, expect = self.current["probe"], self.current["expect"]
            self.requests += 1
            self.statuses[resp.status] += 1
            self.latencies.setdefault(probe, []).append(resp.ms)
            if resp.status >= 500 or resp.status in (0, -1):
                self.five_xx.append({"round": self.current["round"], "probe": probe, "method": method, "path": path.split("?")[0][:80],
                                     "status": resp.status, "expected": resp.status in expect})


def pct(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(round(q * (len(ordered) - 1))))]


class Client:
    def __init__(self, base: str, api_key: str, stats: Stats):
        self.base, self._key, self.stats = base.rstrip("/"), api_key, stats

    def request(self, method: str, path: str, body: Any = None, *, key: str | None | bool = True, headers: dict[str, str] | None = None,
                timeout: float = 30, raw: bytes | None = None) -> Response:
        h = {"Accept": "application/json"}
        if key is True:
            h["X-API-Key"] = self._key
        elif key:
            h["X-API-Key"] = str(key)
        data = raw
        if body is not None:
            data = json.dumps(body).encode()
            h["Content-Type"] = "application/json"
        elif raw is not None:
            h["Content-Type"] = "application/json"
        h.update(headers or {})
        req = urllib.request.Request(self.base + path, data=data, method=method, headers=h)
        t0 = time.perf_counter()
        status, payload, rh = 0, b"", {}
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                status, payload, rh = r.status, r.read(), dict(r.headers)
        except urllib.error.HTTPError as exc:
            status, payload, rh = exc.code, exc.read(), dict(exc.headers)
        except TimeoutError:
            status = -1
        except (urllib.error.URLError, OSError, http.client.HTTPException) as exc:    # includes a response cut off by a server that died mid-reply
            status = -1 if "timed out" in str(exc) else 0
        ms = (time.perf_counter() - t0) * 1000
        try:
            parsed = json.loads(payload) if payload else None
        except ValueError:
            parsed = None
        resp = Response(status, parsed, rh, ms, payload[:300].decode("utf-8", "replace") if parsed is None else "")
        self.stats.record(method, path, resp)
        return resp

    def get(self, path: str, **kw: Any) -> Response:
        return self.request("GET", path, **kw)

    def post(self, path: str, body: Any = None, **kw: Any) -> Response:
        return self.request("POST", path, body, **kw)


# --- the probe framework --------------------------------------------------------------------------------------------------------------------

class ProbeFail(Exception):
    pass


class Skip(Exception):
    pass


def check(cond: Any, message: str) -> None:
    if not cond:
        raise ProbeFail(message)


def status_is(resp: Response, *codes: int, what: str = "") -> None:
    if resp.status not in codes:
        detail = (resp.json if isinstance(resp.json, (dict, list)) else resp.text)
        raise ProbeFail(f"{what or 'request'} returned HTTP {resp.status}, expected {'/'.join(map(str, codes))}: {str(detail)[:200]}")


@dataclasses.dataclass(frozen=True)
class Probe:
    id: str
    phase: str
    title: str
    fn: Callable[["Ctx"], None]
    destructive: bool = False
    expect_5xx: tuple[int, ...] = ()


PROBES: list[Probe] = []


def probe(pid: str, phase: str, title: str, *, destructive: bool = False, expect_5xx: tuple[int, ...] = ()):
    def register(fn: Callable[["Ctx"], None]):
        PROBES.append(Probe(pid, phase, title, fn, destructive, expect_5xx))
        return fn
    return register


PHASES = {
    "A": "records and evidence objects", "B": "continuity blocks", "C": "auth and request guards", "D": "retrieval and hostile input",
    "F": "replay receipts", "G": "signatures (warn mode)", "E": "database role and row-level security", "H": "destructive: outages, floods, concurrent seals",
    "I": "ugly conditions: the application is killed (kill -9) mid-write", "J": "ugly conditions: the application is partitioned from the database mid-transaction",
    "K": "ugly conditions: the database volume is full",
}


class Ctx:
    """What a probe gets: the client, the stack, per-round unique names, and state that survives between rounds."""

    def __init__(self, client: Client, stats: Stats, stack: dict[str, Any] | None, target: dict[str, Any], destructive_ok: str | None,
                 state: dict[str, Any], rnd: int, rng: random.Random, max_history: int):
        self.client, self.stats, self.stack, self.target = client, stats, stack, target
        self.destructive_blocked = destructive_ok  # None = allowed, else the reason it is not
        self.state, self.round, self.rng, self.max_history = state, rnd, rng, max_history
        self.tag = f"chaos{rnd:03d}x{rng.randrange(16**6):06x}"
        self.metrics: dict[str, Any] = state.setdefault("metrics", {})

    # -- helpers --
    def content(self, what: str = "probe") -> str:
        return f"chaos100x {what} {self.tag} {self.rng.randrange(10**9)}"

    def create(self, *, type: str = "decision", subject: str | None = None, content: str | None = None, evidence: list | None = None,
               expect: tuple[int, ...] = (200,)) -> dict[str, Any]:
        body = {"content": content or self.content(), "source_agent": "chaos100x", "session_id": f"chaos-{self.round:03d}", "type": type,
                "evidence": evidence if evidence is not None else [{"kind": "user-request", "ref": f"chaos100x:round-{self.round}", "note": "created by CL_CHAOS_100x on a throwaway stack"}]}
        if subject:
            body["subject"] = subject
        r = self.client.post("/api/jarvis/memory", body)
        status_is(r, *expect, what="create record")
        with self.stats.lock:
            self.state["writes"] = self.state.get("writes", 0) + (1 if r.status == 200 else 0)
        return r.json["memory"] if r.status == 200 else r.json

    def head(self) -> dict[str, Any]:
        r = self.client.get("/api/jarvis/blocks/head")
        status_is(r, 200, what="blocks head")
        return r.json

    def seal(self, max_entries: int = 5) -> dict[str, Any]:
        r = self.client.post("/api/jarvis/blocks/seal", {"force": True, "min_entries": 1, "max_entries": max_entries})
        status_is(r, 200, what="seal")
        return r.json

    def docker(self, *args: str, timeout: float = 120) -> subprocess.CompletedProcess:
        return run_cmd(["docker", *args], timeout=timeout)

    def need_destructive(self) -> dict[str, Any]:
        if self.destructive_blocked or not self.stack:
            raise Skip(self.destructive_blocked or "no throwaway stack description (stack.json)")
        return self.stack

    def psql(self, sql: str, *, db: str = "jarvis", schema: str | None = "jarvis", check_rc: bool = True) -> str:
        stack = self.need_destructive()
        pre = f"SET search_path TO {schema}, public; " if schema else ""
        r = run_cmd(["docker", "exec", "-i", "-u", "postgres", stack["containers"]["db"], "psql", "-X", "-q", "-t", "-A", "-v", "ON_ERROR_STOP=1", "-d", db],
                    input=pre + sql, timeout=120)
        if check_rc and r.returncode != 0:
            raise ProbeFail(f"psql failed: {r.stderr.strip()[:200]}")
        return r.stdout.strip()

    def wait_ready(self, want: int, seconds: float, step: float = 0.5) -> float:
        t0 = time.perf_counter()
        while time.perf_counter() - t0 < seconds:
            r = self.client.get("/ready", key=False, timeout=5)
            if r.status == want:
                return time.perf_counter() - t0
            time.sleep(step)
        raise ProbeFail(f"/ready did not become {want} within {seconds:g}s")

    def ensure_receipt(self) -> dict[str, Any]:
        """A replay receipt taken BEFORE a fault (the one phase F made this round, or one made now) for the gates to re-derive afterwards."""
        if self.state.get("f1_round") != self.round:                          # a receipt of THIS round, taken before this round's faults
            self.create(content=self.content("receipt anchor"))
            self.seal(max_entries=5)
            tip = self.head()["tip"]
            r = self.client.post("/api/jarvis/replay/receipts", {"at_block": tip["height"]})
            status_is(r, 200, what="issue the receipt the fault gates will re-derive")
            self.state["f1"] = {"id": r.json["receipt"]["id"], "block": tip["height"], "at_seq": tip["last_seq"], "root": r.json["receipt"]["payload"]["state_root"]}
            self.state["f1_round"] = self.round
        return self.state["f1"]

    def mint_script(self, name: str, *args: str, timeout: float = 600) -> subprocess.CompletedProcess:
        stack = self.need_destructive()
        env = dict(os.environ, JARVIS_HOME=f"{stack['dir']}/home")
        return run_cmd([f"{stack['dir']}/mint/bin/{name}", *args], timeout=timeout, env=env)


def evidence_payload(ctx: Ctx, label: str) -> dict[str, Any]:
    return {"statement": f"chaos100x decision {label} {ctx.tag}", "authority": "chaos100x harness", "source": f"chaos100x:round-{ctx.round}"}


# === Phase A: records and evidence objects ================================================================================================

@probe("A1", "A", "create a record, read it back, see its history")
def a1(ctx: Ctx) -> None:
    rec = ctx.create(content=ctx.content("a1"))
    check(rec["id"].startswith("mem-"), f"unexpected id {rec['id']!r}")
    got = ctx.client.get(f"/api/jarvis/memory/{rec['id']}")
    status_is(got, 200, what="read back")
    check(got.json["memory"]["content"] == rec["content"], "content differs on read back")
    check(got.json["memory"].get("content_sha256") == hashlib.sha256(rec["content"].encode()).hexdigest(), "content_sha256 is not the sha256 of the content")
    hist = ctx.client.get(f"/api/jarvis/memory/{rec['id']}/history")
    status_is(hist, 200, what="history")
    check([h["op"] for h in hist.json["history"]] == ["create"], f"history ops {[h['op'] for h in hist.json['history']]}")
    ctx.state["a1_id"] = rec["id"]


@probe("A2", "A", "an evidence object is content-addressed: the same content returns the same id, created once")
def a2(ctx: Ctx) -> None:
    body = {"schema_id": "CES.Local.DecisionEvidence.v1", "payload": evidence_payload(ctx, "a2"), "source_agent": "chaos100x"}
    first = ctx.client.post("/api/jarvis/evidence", body)
    status_is(first, 200, what="first create")
    second = ctx.client.post("/api/jarvis/evidence", body)
    status_is(second, 200, what="second create (idempotent, not a rejection)")
    check(first.json["created"] is True and second.json["created"] is False, f"created flags {first.json['created']}, {second.json['created']}")
    eid = first.json["evidence"]["id"]
    check(second.json["evidence"]["id"] == eid and re.fullmatch(r"eo:sha256:[0-9a-f]{64}", eid), "the id changed or is malformed")
    got = ctx.client.get(f"/api/jarvis/evidence/{eid}")
    status_is(got, 200, what="get evidence")
    check(got.json["evidence"]["payload"] == body["payload"], "stored payload differs")
    ctx.state["a2_eid"] = eid


@probe("A3", "A", "an evidence object verifies; an unknown id is a 404 and a malformed id a 422")
def a3(ctx: Ctx) -> None:
    v = ctx.client.get(f"/api/jarvis/evidence/{ctx.state['a2_eid']}/verify")
    status_is(v, 200, what="verify")
    check(v.json["ok"] is True and v.json["problems"] == [], f"verify said {v.json}")
    status_is(ctx.client.get("/api/jarvis/evidence/eo:sha256:" + "0" * 64), 404, what="unknown id")
    status_is(ctx.client.get("/api/jarvis/evidence/not-an-id"), 422, what="malformed id")


@probe("A4", "A", "a fact may cite an evidence object; a dangling link and a fact with no evidence are refused")
def a4(ctx: Ctx) -> None:
    fact_ev = ctx.client.post("/api/jarvis/evidence", {"schema_id": "CES.Local.FactEvidence.v1", "source_agent": "chaos100x", "payload": {
        "observation": f"chaos100x observed {ctx.tag}", "source": f"chaos100x:round-{ctx.round}", "method": "command"}})
    status_is(fact_ev, 200, what="fact evidence object")
    ok = ctx.create(type="fact", content=ctx.content("a4 fact"), evidence=[{"kind": "evidence-object", "ref": fact_ev.json["evidence"]["id"]}])
    check(ok["type"] == "fact", "the fact was not stored as a fact")
    dangling = ctx.client.post("/api/jarvis/memory", {"content": ctx.content("a4 dangling"), "source_agent": "chaos100x", "session_id": "chaos", "type": "fact",
                                                      "evidence": [{"kind": "evidence-object", "ref": "eo:sha256:" + "f" * 64}]})
    status_is(dangling, 422, what="dangling evidence link")
    bare = ctx.client.post("/api/jarvis/memory", {"content": ctx.content("a4 bare"), "source_agent": "chaos100x", "session_id": "chaos", "type": "fact", "evidence": []})
    status_is(bare, 422, what="fact without evidence")
    check((bare.json or {}).get("code") == "clause_v_violation", f"code {(bare.json or {}).get('code')!r}")


@probe("A5", "A", "Clause V refuses types the ledger does not accept (preference, task)")
def a5(ctx: Ctx) -> None:
    for t in ("preference", "task"):
        r = ctx.client.post("/api/jarvis/memory", {"content": ctx.content(f"a5 {t}"), "source_agent": "chaos100x", "session_id": "chaos", "type": t,
                                                   "evidence": [{"kind": "user-request", "ref": "chaos100x"}]})
        status_is(r, 422, what=f"type {t}")
        check((r.json or {}).get("code") == "clause_v_violation", f"{t}: code {(r.json or {}).get('code')!r}")


@probe("A6", "A", "the same evidence content in another key order and from another agent returns the same id")
def a6(ctx: Ctx) -> None:
    payload = evidence_payload(ctx, "a6")
    one = ctx.client.post("/api/jarvis/evidence", {"schema_id": "CES.Local.DecisionEvidence.v1", "payload": payload, "source_agent": "chaos100x"})
    status_is(one, 200, what="create")
    reordered = dict(reversed(list(payload.items())))
    raw = ('{"source_agent": "someone-else", "payload": ' + json.dumps(reordered, indent=3) + ', "schema_id": "CES.Local.DecisionEvidence.v1"}').encode()
    two = ctx.client.request("POST", "/api/jarvis/evidence", raw=raw)
    status_is(two, 200, what="the same content, reordered (idempotent, not a rejection)")
    check(two.json["evidence"]["id"] == one.json["evidence"]["id"], "reordering the keys changed the id")
    check(two.json["created"] is False, "the reordered copy was reported as newly created")


@probe("A7", "A", "optimistic locking: a current expected_version updates, a stale one is a 409")
def a7(ctx: Ctx) -> None:
    rec = ctx.create(content=ctx.content("a7"))
    upd = ctx.client.request("PATCH", f"/api/jarvis/memory/{rec['id']}", {"subject": f"chaos-{ctx.tag}", "expected_version": rec["version"]})
    status_is(upd, 200, what="update with the current version")
    check(upd.json["memory"]["version"] == rec["version"] + 1, "the version did not advance by one")
    stale = ctx.client.request("PATCH", f"/api/jarvis/memory/{rec['id']}", {"subject": "stale", "expected_version": rec["version"]})
    status_is(stale, 409, what="update with a stale version")


@probe("A8", "A", "delete leaves a delete entry in history; the record is gone; a second delete is a 404")
def a8(ctx: Ctx) -> None:
    rec = ctx.create(content=ctx.content("a8"))
    status_is(ctx.client.request("DELETE", f"/api/jarvis/memory/{rec['id']}"), 200, what="delete")
    status_is(ctx.client.get(f"/api/jarvis/memory/{rec['id']}"), 404, what="read after delete")
    status_is(ctx.client.request("DELETE", f"/api/jarvis/memory/{rec['id']}"), 404, what="second delete")
    hist = ctx.client.get(f"/api/jarvis/memory/{rec['id']}/history")
    status_is(hist, 200, what="history after delete")
    check([h["op"] for h in hist.json["history"]] == ["create", "delete"], f"history ops {[h['op'] for h in hist.json['history']]}")


@probe("A9", "A", "the record-history hash chain verifies")
def a9(ctx: Ctx) -> None:
    r = ctx.client.get("/api/jarvis/memory/history/verify")
    status_is(r, 200, what="history verify")
    check(r.json["ok"] is True, f"history problems: {r.json['problems'][:2]}")


# === Phase B: continuity blocks (the routes are live) =====================================================================================

@probe("B1", "B", "seal a SMALL batch: five new records, force-seal at most five entries per block")
def b1(ctx: Ctx) -> None:
    before = ctx.head()
    for i in range(5):
        ctx.create(content=ctx.content(f"b1-{i}"))
    sealed = ctx.seal(max_entries=5)
    check(sealed["sealed"], f"nothing was sealed ({sealed['reason']})")
    check(all(b["entry_count"] <= 5 for b in sealed["sealed"]), f"a block holds more than five entries: {[b['entry_count'] for b in sealed['sealed']]}")
    total = sum(b["entry_count"] for b in sealed["sealed"])
    check(5 <= total <= 30, f"sealed {total} entries in a round step that wrote five records (the batch is small, not hundreds)")
    after = ctx.head()
    check(after["tip"]["height"] > (before["tip"]["height"] if before["tip"] else 0), "the tip did not advance")
    check(after["unsealed_entries"] == 0, f"{after['unsealed_entries']} entries are still unsealed after a force seal")
    ctx.metrics["blocks_sealed_by_b1"] = ctx.metrics.get("blocks_sealed_by_b1", 0) + len(sealed["sealed"])


@probe("B2", "B", "sealing again with the default rules seals nothing (idempotent)")
def b2(ctx: Ctx) -> None:
    r = ctx.client.post("/api/jarvis/blocks/seal", {})
    status_is(r, 200, what="seal with defaults")
    check(r.json["sealed"] == [], f"sealed {len(r.json['sealed'])} block(s) with nothing due")


@probe("B3", "B", "the blocks verify (database verifier and independent recomputation)")
def b3(ctx: Ctx) -> None:
    r = ctx.client.get("/api/jarvis/blocks/verify")
    status_is(r, 200, what="blocks verify")
    check(r.json["ok"] is True, f"block problems: {r.json['problems'][:2]}")


@probe("B4", "B", "the newest blocks chain correctly and each hash recomputes on this side")
def b4(ctx: Ctx) -> None:
    from app import blocks as blk

    tip = ctx.head()["tip"]
    start = max(0, tip["height"] - 9)
    r = ctx.client.get(f"/api/jarvis/blocks?after_height={start}&limit=20")
    status_is(r, 200, what="list blocks")
    rows = r.json["blocks"]
    check(rows and rows[-1]["height"] == tip["height"], "the list does not end at the tip")
    for prev, cur in zip(rows, rows[1:]):
        check(cur["height"] == prev["height"] + 1 and cur["prev_block_hash"] == prev["block_hash"], f"block {cur['height']} does not chain to {prev['height']}")
        check(cur["first_seq"] == prev["last_seq"] + 1, f"block {cur['height']} leaves a gap in the sealed range")
    for b in rows:
        again = blk.block_hash(tenant=TENANT, height=b["height"], first_seq=b["first_seq"], last_seq=b["last_seq"], entry_count=b["entry_count"],
                               prev_block_hash=b["prev_block_hash"], entries_root=b["entries_root"], fmt=b["format"])
        check(again == b["block_hash"], f"block {b['height']}'s hash does not recompute")


@probe("B5", "B", "block listing paginates consistently and a missing block is a 404")
def b5(ctx: Ctx) -> None:
    first = ctx.client.get("/api/jarvis/blocks?limit=2")
    status_is(first, 200, what="first page")
    heights = [b["height"] for b in first.json["blocks"]]
    check(heights == sorted(heights) and heights[0] == 1, f"first page {heights}")
    nxt = ctx.client.get(f"/api/jarvis/blocks?after_height={heights[-1]}&limit=2")
    status_is(nxt, 200, what="next page")
    check(all(b["height"] > heights[-1] for b in nxt.json["blocks"]), "the next page repeats a block")
    status_is(ctx.client.get("/api/jarvis/blocks/99999999"), 404, what="a block that does not exist")
    status_is(ctx.client.get("/api/jarvis/blocks/0"), 422, what="height 0")


@probe("B6", "B", "sealing needs the operator key; a bad body is a 422")
def b6(ctx: Ctx) -> None:
    status_is(ctx.client.post("/api/jarvis/blocks/seal", {}, key=False), 401, what="no key")
    status_is(ctx.client.post("/api/jarvis/blocks/seal", {}, key="wrong-key"), 401, what="wrong key")
    status_is(ctx.client.post("/api/jarvis/blocks/seal", {"min_entries": 0}), 422, what="min_entries 0")
    status_is(ctx.client.post("/api/jarvis/blocks/seal", {"max_entries": 100001}), 422, what="max_entries too large")


# === Phase C: auth and request guards ====================================================================================================

@probe("C1", "C", "no key and a wrong key are refused on the ledger routes")
def c1(ctx: Ctx) -> None:
    for path in ("/api/jarvis/memory", "/api/jarvis/memory/retrieve", "/api/jarvis/evidence/eo:sha256:" + "0" * 64, "/api/jarvis/blocks/head",
                 "/api/jarvis/replay/receipts", "/api/jarvis/attestations/head", "/api/jarvis/trust"):
        status_is(ctx.client.get(path, key=False), 401, what=f"{path} without a key")
        status_is(ctx.client.get(path, key="chaos-wrong-key"), 401, what=f"{path} with a wrong key")


@probe("C2", "C", "malformed bodies are a 422 or 400, never a 5xx")
def c2(ctx: Ctx) -> None:
    nul = b'{"content": "a\\u0000b", "source_agent": "chaos100x", "session_id": "chaos", "type": "decision", "evidence": [{"kind": "user-request", "ref": "x"}]}'
    for raw in (b"{not json", b"[]", b'{"content": 5}', b'{"content": "x"}', b"\xff\xfe\x00", b"", nul):
        r = ctx.client.request("POST", "/api/jarvis/memory", raw=raw)
        status_is(r, 400, 422, what=f"body {raw[:20]!r}")


@probe("C3", "C", "limits hold: 2001-character content and a 2 MB body are refused")
def c3(ctx: Ctx) -> None:
    r = ctx.client.post("/api/jarvis/memory", {"content": "x" * 2001, "source_agent": "chaos100x", "session_id": "chaos", "type": "decision",
                                               "evidence": [{"kind": "user-request", "ref": "chaos100x"}]})
    status_is(r, 422, what="2001 characters")
    big = ctx.client.request("POST", "/api/jarvis/memory", raw=b'{"content": "' + b"y" * (2 * 1024 * 1024) + b'"}')
    status_is(big, 400, 413, 422, what="a 2 MB body")


@probe("C4", "C", "hostile ids are a 404 or 422 and never reach a 5xx")
def c4(ctx: Ctx) -> None:
    for ident in ("..%2f..%2fetc%2fpasswd", "a" * 5000, "%E2%9D%A4%EF%B8%8F", "mem-%00", "mem-0'%3B--", "%25%25", "..", "mem-" + "0" * 40):
        r = ctx.client.get(f"/api/jarvis/memory/{ident}")
        status_is(r, 400, 404, 422, what=f"id {ident[:24]!r}")


@probe("C5", "C", "unknown routes are a 404 and wrong methods a 405")
def c5(ctx: Ctx) -> None:
    status_is(ctx.client.get("/api/jarvis/nope"), 404, 401, what="unknown route")
    status_is(ctx.client.request("PUT", "/api/jarvis/memory", {"a": 1}), 405, 401, what="PUT on a collection")


@probe("C6", "C", "liveness is up; readiness is ready, names every check ok and reports the throwaway identity")
def c6(ctx: Ctx) -> None:
    h = ctx.client.get("/health", key=False)
    status_is(h, 200, what="health")
    check(h.json.get("live") is True, "health does not say live")
    r = ctx.client.get("/ready", key=False)
    status_is(r, 200, what="ready")
    check(r.json["status"] == "ready" and all(v == "ok" for v in r.json["checks"].values()), f"ready said {r.json}")
    check(str(r.json.get("stack", "")).startswith(THROWAWAY_PREFIX), f"/ready stack identity is {r.json.get('stack')!r}")
    check(set(r.json["checks"]) >= {"database", "schema_version", "role", "history_write_denied", "legacy_data"}, "a readiness check is missing")


@probe("C7", "C", "the schema is v7 and the signature, replay and blocks routes exist")
def c7(ctx: Ctx) -> None:
    status_is(ctx.client.get("/api/jarvis/attestations/head"), 200, what="attestations head (schema v7 only)")
    for path in ("/api/jarvis/replay/contracts", "/api/jarvis/blocks/head", "/api/jarvis/trust"):
        status_is(ctx.client.get(path), 200, what=path)
    if ctx.stack and not ctx.destructive_blocked:
        v = ctx.psql("SELECT max(version) FROM schema_version;")
        check(v == "7", f"schema_version is {v}, expected 7")


# === Phase D: retrieval and hostile input ================================================================================================

@probe("D1", "D", "retrieve finds a record by a distinctive word")
def d1(ctx: Ctx) -> None:
    rec = ctx.create(content=f"chaos100x finder {ctx.tag}")
    r = ctx.client.get("/api/jarvis/memory/retrieve?" + urllib.parse.urlencode({"query": ctx.tag, "limit": 10}))
    status_is(r, 200, what="retrieve")
    check(rec["id"] in [m["id"] for m in r.json["memories"]], "the new record was not retrieved by its tag")


INJECTIONS = [
    "'; DROP TABLE memories; --", "' OR '1'='1", "1; SELECT pg_sleep(10)--", "\" OR \"\"=\"", "%' UNION SELECT NULL,NULL--",
    "'; UPDATE record_history SET op='x'; --", "$(sleep 10)", "`id`", "{{7*7}}", "${jndi:ldap://127.0.0.1/x}", "../../etc/passwd",
    "\u0000", "a\u0000b", "\U0001F4A5" * 50, "x" * 4000,
]


@probe("D2", "D", "hostile query strings match nothing, raise no error, and no SQL from the input runs")
def d2(ctx: Ctx) -> None:
    status_is(ctx.client.get("/api/jarvis/memory/history/verify"), 200, what="history verify before")
    seq_before = ctx.head()["history_seq"]
    for q in INJECTIONS:
        t0 = time.perf_counter()
        r = ctx.client.get("/api/jarvis/memory/retrieve?" + urllib.parse.urlencode({"query": q, "limit": 5}))
        status_is(r, 200, what=f"query {q[:24]!r}")
        check(r.json["memories"] == [], f"query {q[:24]!r} matched {len(r.json['memories'])} record(s)")
        check(time.perf_counter() - t0 < 5, f"query {q[:24]!r} took {time.perf_counter() - t0:.1f}s (did a pg_sleep run?)")
    after = ctx.client.get("/api/jarvis/memory/history/verify")
    check(after.status == 200 and after.json["ok"] is True, "the history chain no longer verifies after the hostile queries")
    check(ctx.head()["history_seq"] == seq_before, "the history counter moved: something wrote while only reading was asked for")
    if ctx.stack and not ctx.destructive_blocked:
        check(ctx.psql("SELECT count(*) FROM memories WHERE content LIKE '%DROP TABLE%' OR content LIKE '%pg_sleep%';") == "0", "a record containing the injected text exists")
        check(ctx.psql("SELECT to_regclass('memories') IS NOT NULL;") == "t", "the memories table is gone")


@probe("D3", "D", "retrieve filters accept valid values and refuse invalid ones with a 422, not a 5xx")
def d3(ctx: Ctx) -> None:
    for qs in ("type=decision&limit=3", "status=draft&limit=3", f"session_id=chaos-{ctx.round:03d}&limit=3", "limit=1", "subject=nothing-has-this-subject"):
        status_is(ctx.client.get("/api/jarvis/memory/retrieve?" + qs), 200, what=qs)
    for qs in ("limit=0", "limit=100000", "limit=abc", "type=nonsense", "status=nonsense"):
        status_is(ctx.client.get("/api/jarvis/memory/retrieve?" + qs), 200, 422, what=qs)


@probe("D4", "D", "two records with one subject and different content surface as a conflict, never merged")
def d4(ctx: Ctx) -> None:
    subject = f"chaos-subject-{ctx.tag}"
    a, b = ctx.create(subject=subject, content=ctx.content("d4 one")), ctx.create(subject=subject, content=ctx.content("d4 two"))
    r = ctx.client.get("/api/jarvis/memory/conflicts?" + urllib.parse.urlencode({"subject": subject}))
    status_is(r, 200, what="conflicts")
    ids = set(re.findall(r"mem-[0-9a-f]+", json.dumps(r.json["conflicts"])))
    check(a["id"] in ids and b["id"] in ids, "the conflict between the two records was not surfaced")
    for rec in (a, b):
        status_is(ctx.client.get(f"/api/jarvis/memory/{rec['id']}"), 200, what="a conflicting record (silently merged away?)")


# === Phase F: replay receipts =============================================================================================================

@probe("F1", "F", "issue a receipt at a sealed point; it names the covering block and the replayed state")
def f1(ctx: Ctx) -> None:
    ctx.create(content=ctx.content("f1"))
    ctx.seal(max_entries=5)
    tip = ctx.head()["tip"]
    r = ctx.client.post("/api/jarvis/replay/receipts", {"at_block": tip["height"]})
    status_is(r, 200, what="issue receipt")
    p = r.json["receipt"]["payload"]
    check(p["block_height"] == tip["height"] and p["block_hash"] == tip["block_hash"] and p["at_seq"] == tip["last_seq"], f"receipt payload {p}")
    check(r.json["receipt"]["schema_id"] == "CES.Local.ReplayReceipt.v1", "wrong schema id")
    ctx.state["f1"] = {"id": r.json["receipt"]["id"], "block": tip["height"], "at_seq": tip["last_seq"], "root": p["state_root"]}
    ctx.state["f1_round"] = ctx.round


@probe("F2", "F", "issuing the same receipt again is idempotent: same id, not created twice")
def f2(ctx: Ctx) -> None:
    f1_ = ctx.state["f1"]
    r = ctx.client.post("/api/jarvis/replay/receipts", {"at_block": f1_["block"]})
    status_is(r, 200, what="issue again")
    check(r.json["receipt"]["id"] == f1_["id"] and r.json["created"] is False, f"second issue gave {r.json['receipt']['id'][:20]} created={r.json['created']}")
    by_seq = ctx.client.post("/api/jarvis/replay/receipts", {"at_seq": f1_["at_seq"]})
    status_is(by_seq, 200, what="issue by at_seq")
    check(by_seq.json["receipt"]["id"] == f1_["id"], "the same point by seq gave another receipt")
    lst = ctx.client.get("/api/jarvis/replay/receipts?limit=1000")
    status_is(lst, 200, what="list receipts")
    check([x["id"] for x in lst.json["receipts"]].count(f1_["id"]) == 1, "the receipt is listed more than once")


@probe("F3", "F", "a point no sealed block covers is refused, and no receipt appears")
def f3(ctx: Ctx) -> None:
    before = ctx.client.get("/api/jarvis/replay/receipts?limit=1000").json["count"]
    ctx.create(content=ctx.content("f3 unsealed tail"))
    head = ctx.head()
    check(head["unsealed_entries"] >= 1, "there is no unsealed tail to test with")
    for body in ({"at_seq": head["history_seq"]}, {"at_block": head["tip"]["height"] + 1000}, {"at_seq": head["history_seq"] + 1000}):
        status_is(ctx.client.post("/api/jarvis/replay/receipts", body), 404, 409, 422, what=f"receipt at {body}")
    r = ctx.client.post("/api/jarvis/replay/receipts", {"at_seq": head["history_seq"]})
    check("replay_not_sealed" in str((r.json or {}).get("detail")), f"detail {(r.json or {}).get('detail')!r}")
    status_is(ctx.client.post("/api/jarvis/replay/receipts", {"at_seq": 1, "at_block": 1}), 422, what="at_seq and at_block together")
    check(ctx.client.get("/api/jarvis/replay/receipts?limit=1000").json["count"] == before, "a receipt was created for an unsealed point")
    ctx.seal(max_entries=5)


@probe("F4", "F", "the service re-derives the receipt: same root, same counts, same block")
def f4(ctx: Ctx) -> None:
    f1_ = ctx.state["f1"]
    v = ctx.client.get(f"/api/jarvis/replay/receipts/{f1_['id']}/verify")
    status_is(v, 200, what="verify receipt")
    check(v.json["ok"] is True and v.json["problems"] == [], f"problems: {v.json['problems'][:2]}")
    st = ctx.client.get(f"/api/jarvis/replay/state?at_block={f1_['block']}&limit=1")
    status_is(st, 200, what="replay state")
    check(st.json["state_root"] == f1_["root"] and st.json["sealed"] is True and st.json["block"]["height"] == f1_["block"], "the replayed state differs from the receipt")
    check("signed" in st.json["block"], "block.signed is missing from the replay state")


@probe("F5", "F", "the offline verifier re-derives the receipt from the raw rows in a one-off container", destructive=True)
def f5(ctx: Ctx) -> None:
    f1_ = ctx.state["f1"]
    ctx.need_destructive()
    r = ctx.mint_script("replay.sh", "verify", "--receipt", f1_["id"], "--signatures", "off")
    check(r.returncode == 0 and r.stdout.startswith("ok: receipt"), f"offline verify rc={r.returncode}: {(r.stdout + r.stderr).strip()[-200:]}")


HBA = "/etc/postgresql/pg_hba.conf"


def _scratch_url(ctx: Ctx, name: str) -> str:
    """The migrate DSN with the database name swapped for the scratch copy (read from the throwaway secrets, never printed)."""
    text = Path(ctx.stack["secrets_dir"], "migrate.env").read_text()
    m = re.search(r"^JARVIS_DATABASE_MIGRATE_URL=(.+)$", text, re.M)
    check(m, "no migrate DSN in the throwaway secrets")
    return re.sub(r"/[A-Za-z0-9_]+$", f"/{name}", m.group(1).strip())


def _scratch_hba(ctx: Ctx, db: str, allow: bool) -> None:
    """The throwaway database image lets the migrator role into the `jarvis` database only.  For the scratch copy, add one temporary rule
    on the THROWAWAY database container (and always take it out again): the migrator may reach the scratch database from the stack's network."""
    stack = ctx.need_destructive()
    c = stack["containers"]["db"]
    script = (f"set -e; grep -v ' {db} ' {HBA} > /tmp/hba.new || true; "
              + (f"awk '/^host[ ]+all[ ]+all[ ]+all[ ]+reject/ {{print \"host {db} jarvis_migrator samenet scram-sha-256\"}} {{print}}' /tmp/hba.new > /tmp/hba.new2 && mv /tmp/hba.new2 /tmp/hba.new; " if allow else "")
              + f"cat /tmp/hba.new > {HBA}")
    r = run_cmd(["docker", "exec", "-u", "root", c, "sh", "-c", script], timeout=60)
    check(r.returncode == 0, f"could not change the throwaway pg_hba: {r.stderr[:120]}")
    ctx.psql("SELECT pg_reload_conf();", db="postgres", schema=None)
    time.sleep(0.5)


def _offline_verify_scratch(ctx: Ctx, receipt_id: str, db: str) -> subprocess.CompletedProcess:
    stack = ctx.need_destructive()
    return run_cmd(["docker", "compose", "-p", stack["project"], "-f", stack["compose_file"], "run", "--rm", "--no-deps", "-T",
                    "-e", f"JARVIS_DATABASE_MIGRATE_URL={_scratch_url(ctx, db)}", "migrate", "python", "-m", "app.replay", "verify",
                    "--tenant", TENANT, "--receipt", receipt_id, "--signatures", "off"], timeout=300)


@probe("F6", "F", "a rewrite plus a full re-seal passes the database's own checks but re-derivation catches it (on a scratch copy)", destructive=True)
def f6(ctx: Ctx) -> None:
    from app import blocks as blk

    stack = ctx.need_destructive()
    db_c, scratch = stack["containers"]["db"], "chaos_scratch"
    f1_ = ctx.state["f1"]
    ctx.psql(f"DROP DATABASE IF EXISTS {scratch};", db="postgres", schema=None)
    ctx.psql(f"CREATE DATABASE {scratch};", db="postgres", schema=None)
    _scratch_hba(ctx, scratch, True)
    try:
        dump = run_cmd(["docker", "exec", "-u", "postgres", db_c, "pg_dump", "-d", "jarvis"], timeout=300)
        check(dump.returncode == 0, f"pg_dump failed: {dump.stderr[:150]}")
        load = run_cmd(["docker", "exec", "-i", "-u", "postgres", db_c, "psql", "-X", "-q", "-v", "ON_ERROR_STOP=1", "-d", scratch], input=dump.stdout, timeout=300)
        check(load.returncode == 0, f"restore into the scratch database failed: {load.stderr[:200]}")
        if ctx.round % 10 == 1:  # the control (the untouched copy re-derives) on rounds 1, 11, 21, ...: it costs a container start
            control = _offline_verify_scratch(ctx, f1_["id"], scratch)
            check(control.returncode == 0, f"the untouched scratch copy does not verify: {(control.stdout + control.stderr).strip()[-200:]}")
        row = ctx.psql(f"SELECT memory_id || '|' || max(seq) FROM record_history WHERE tenant_key = '{TENANT}' GROUP BY memory_id "
                       f"HAVING max(seq) <= {f1_['at_seq']} AND bool_and(op <> 'delete') AND max(seq) > 1 ORDER BY max(seq) DESC LIMIT 1;", db=scratch)
        check(row, "no live record ends inside the receipt's range")
        mid, seq = row.split("|")
        forged = f"forged by the chaos harness {ctx.tag}"
        sha = hashlib.sha256(forged.encode()).hexdigest()
        ctx.psql("ALTER TABLE record_history DISABLE TRIGGER record_history_no_update; ALTER TABLE memories DISABLE TRIGGER memories_history; "
                 "ALTER TABLE memories DISABLE TRIGGER memories_bump_version; "
                 f"UPDATE memories SET content = '{forged}', content_sha256 = '{sha}' WHERE id = '{mid}'; "
                 f"UPDATE record_history h SET after = jarvis_memory_json(m) FROM memories m WHERE h.seq = {seq} AND m.id = '{mid}'; "
                 f"UPDATE record_history SET row_hash = jarvis_history_hash(prev_hash, op, version, before, after) WHERE seq = {seq}; "
                 f"UPDATE chain_heads SET last_hash = (SELECT row_hash FROM record_history WHERE seq = {seq}) WHERE id = '{mid}'; "
                 "ALTER TABLE record_history ENABLE TRIGGER record_history_no_update; ALTER TABLE memories ENABLE TRIGGER memories_history; "
                 "ALTER TABLE memories ENABLE TRIGGER memories_bump_version;", db=scratch)
        # re-seal every block: recompute each entries_root from the (rewritten) row hashes and re-chain the block hashes (two queries, one batch)
        rows = [r.split("|") for r in ctx.psql(f"SELECT height, first_seq, last_seq, entry_count, format FROM blocks WHERE tenant_key = '{TENANT}' ORDER BY height;", db=scratch).splitlines()]
        hashes_by_seq = {int(a): b for a, b in (r.split("|") for r in ctx.psql(f"SELECT seq, row_hash FROM record_history WHERE tenant_key = '{TENANT}' ORDER BY seq;", db=scratch).splitlines())}
        prev, updates = blk.GENESIS_HASH, []
        for height, first, last, count, fmt in rows:
            root = blk.merkle_root([hashes_by_seq[i] for i in range(int(first), int(last) + 1)])
            bh = blk.block_hash(tenant=TENANT, height=int(height), first_seq=int(first), last_seq=int(last), entry_count=int(count), prev_block_hash=prev,
                                entries_root=root, fmt=int(fmt))
            updates.append(f"UPDATE blocks SET prev_block_hash = '{prev}', entries_root = '{root}', block_hash = '{bh}' WHERE tenant_key = '{TENANT}' AND height = {height};")
            prev = bh
        ctx.psql("ALTER TABLE blocks DISABLE TRIGGER blocks_no_update; " + " ".join(updates) + " ALTER TABLE blocks ENABLE TRIGGER blocks_no_update;", db=scratch)
        # the database's own checks are satisfied ...
        check(ctx.psql(f"SELECT count(*) FROM jarvis_verify_blocks('{TENANT}');", db=scratch) == "0", "the re-sealed copy still fails the database's block verifier (the forgery was clumsy)")
        check(ctx.psql(f"SELECT count(*) FROM jarvis_verify_history('{TENANT}');", db=scratch) == "0", "the rewritten copy fails the database's history verifier (the forgery was clumsy)")
        # ... and re-derivation of the receipt taken BEFORE the rewrite is not
        caught = _offline_verify_scratch(ctx, f1_["id"], scratch)
        out = caught.stdout + caught.stderr
        check(caught.returncode == 1 and "PROBLEM" in out, f"re-derivation did not catch the rewrite plus re-seal (rc={caught.returncode}): {out.strip()[-200:]}")
        check("state_root" in out or "expected_block" in out, f"the problem is not the expected one: {out.strip()[-200:]}")
    finally:
        try:
            _scratch_hba(ctx, scratch, False)  # always take the temporary rule out again
        finally:
            run_cmd(["docker", "exec", "-u", "postgres", db_c, "psql", "-X", "-q", "-d", "postgres", "-c", f"DROP DATABASE IF EXISTS {scratch};"], timeout=60)
    left = run_cmd(["docker", "exec", db_c, "grep", "-c", scratch, HBA], timeout=30)
    check(left.stdout.strip() == "0", "the temporary pg_hba rule for the scratch database is still there")


# === Phase G: signatures, warn mode, test keys on the throwaway stack only ==================================================================

def _keys(ctx: Ctx) -> dict[str, Path]:
    d = Path(ctx.stack["keys_dir"]) if ctx.stack else None
    if d is None or not d.is_dir() or any(not (d / n).exists() for n in ("root", "mint", "stranger")):
        raise Skip("no test keys in the throwaway stack directory")
    if d.resolve().is_relative_to((Path.home() / "jarvis-ledger").resolve()):
        raise Skip("refusing keys inside the live ledger home")
    return {n: d / n for n in ("root", "mint", "stranger")}


def _ssh_sign(key: Path, message: str) -> str:
    from app import attest

    with tempfile.TemporaryDirectory() as tmp:
        f = Path(tmp) / "m"
        f.write_text(message)
        r = run_cmd(["ssh-keygen", "-Y", "sign", "-f", str(key), "-n", attest.NAMESPACE, str(f)], timeout=30)
        if r.returncode != 0:
            raise ProbeFail("ssh-keygen could not sign with a test key")
        return attest.normalize_signature((Path(tmp) / "m.sig").read_text())


def _key_id(key: Path) -> str:
    from app import attest

    return attest.parse_public_key(Path(str(key) + ".pub").read_text()).key_id


def _pub(key: Path) -> str:
    from app import attest

    return attest.parse_public_key(Path(str(key) + ".pub").read_text()).text()


def _statement(ctx: Ctx, root: Path, kind: str, key_id: str, *, pubkey: str | None = None, arg: int | None = None, subject_hash: str | None = None,
               signer: Path | None = None) -> Response:
    from app import attest

    head = ctx.client.get("/api/jarvis/attestations/head").json
    seq, prev = head["next_stmt_seq"], head["trust_head_hash"]
    by = signer or root
    message = attest.trust_message(kind, TENANT, key_id, arg, subject_hash, seq, prev)
    return ctx.client.post("/api/jarvis/trust/statements", {
        "kind": kind, "key_id": key_id, "pubkey": pubkey, "arg": arg, "subject_hash": subject_hash, "stmt_seq": seq, "prev_hash": prev,
        "signed_by": _key_id(by), "signature": _ssh_sign(by, message)})


def _attestation(ctx: Ctx, signer: Path, kind: str, subject: str, subject_hash: str, *, key_id: str | None = None, sign_hash: str | None = None,
                 seq: int | None = None) -> Response:
    from app import attest

    head = ctx.client.get("/api/jarvis/attestations/head").json
    seq = seq or head["next_signer_seq"]
    prev = head["prev_hash"]
    signed_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    message = attest.attestation_message(kind, TENANT, subject, sign_hash or subject_hash, seq, prev, signed_at)
    return ctx.client.post("/api/jarvis/attestations", {
        "kind": kind, "subject": subject, "subject_hash": subject_hash, "signer_seq": seq, "prev_hash": prev,
        "key_id": key_id or _key_id(signer), "signed_at": signed_at, "signature": _ssh_sign(signer, message)})


def _log_size(ctx: Ctx) -> tuple[int, int]:
    h = ctx.client.get("/api/jarvis/attestations/head").json
    return h["head_seq"], h["trust_head_seq"]


def _call(ctx: Ctx, method: str, path: str, body: Any) -> tuple[int, Any]:
    r = ctx.client.request(method, path, body)
    return r.status, (r.json if r.json is not None else {})


@probe("G0", "G", "ceremony on the throwaway stack: a root authorizes the test signing key; the mode is warn")
def g0(ctx: Ctx) -> None:
    k = _keys(ctx)
    trust = ctx.client.get("/api/jarvis/trust")
    status_is(trust, 200, what="trust state")
    check(trust.json["trust_roots_configured"] is True, "no trust root is configured in the throwaway stack")
    mint_id = _key_id(k["mint"])
    if not any(x["key_id"] == mint_id for x in trust.json["keys"]):
        status_is(_statement(ctx, k["root"], "key", mint_id, pubkey=_pub(k["mint"]), arg=1), 200, what="authorize the test signing key")
    trust = ctx.client.get("/api/jarvis/trust").json
    check(any(x["key_id"] == mint_id and x["revoked_after_signer_seq"] is None for x in trust["keys"]), "the test signing key is not authorized")
    f1_ = ctx.state["f1"]
    v = ctx.client.get(f"/api/jarvis/replay/receipts/{f1_['id']}/verify").json
    check(v["signatures"] and v["signatures"]["mode"] == "warn", f"signature mode is {(v.get('signatures') or {}).get('mode')!r}, expected warn")


@probe("G1", "G", "the real signer signs pending blocks and receipts (re-derived first) and the service verifies the log", destructive=True)
def g1(ctx: Ctx) -> None:
    from app import signer

    ctx.need_destructive()
    k = _keys(ctx)
    os.chmod(k["mint"], 0o600)
    api = signer.Api("http://throwaway", "unused", lambda method, path, body: _call(ctx, method, path, body))
    key = signer.preflight_key(k["mint"], mounts=lambda: [])

    def offline(*args: str) -> None:
        r = ctx.mint_script("replay.sh", "verify", *args, "--signatures", "off")
        if r.returncode != 0:
            raise signer.SignerError("pre_sign_verify_failed", (r.stdout + r.stderr).strip()[-160:], signer.EXIT_UNHEALTHY)

    result = signer.run_sign(api, key, verify_block=lambda h, bh: offline("--at-block", str(h), "--expect-block-hash", bh),
                             verify_receipt=lambda rid: offline("--receipt", rid))
    check(not result["refused_receipts"], f"the signer refused receipts: {result['refused_receipts']}")
    v = ctx.client.get("/api/jarvis/attestations/verify")
    status_is(v, 200, what="attestations verify")
    check(v.json["ok"] is True, f"the signing log has problems: {v.json['problems'][:2]}")
    ctx.metrics["blocks_signed"] = ctx.metrics.get("blocks_signed", 0) + len(result["signed_blocks"])
    ctx.metrics["receipts_signed"] = ctx.metrics.get("receipts_signed", 0) + len(result["signed_receipts"])


@probe("G2", "G", "a forged signature (the signing key's name, someone else's signature) is rejected and stores nothing")
def g2(ctx: Ctx) -> None:
    k = _keys(ctx)
    block = ctx.head()["tip"]
    before = _log_size(ctx)
    status_is(_attestation(ctx, k["stranger"], "block", f"block:{block['height']}", block["block_hash"], key_id=_key_id(k["mint"])), 409, 422, what="forged block attestation")
    status_is(_attestation(ctx, k["stranger"], "checkpoint", f"block:{block['height']}", block["block_hash"], key_id=_key_id(k["mint"])), 409, 422, what="forged checkpoint attestation")
    check(_log_size(ctx) == before, "a forged attestation was stored")


@probe("G3", "G", "a signature by a key no root authorized is rejected; a signing key cannot authorize another key")
def g3(ctx: Ctx) -> None:
    k = _keys(ctx)
    block = ctx.head()["tip"]
    before = _log_size(ctx)
    status_is(_attestation(ctx, k["stranger"], "block", f"block:{block['height']}", block["block_hash"]), 409, 422, what="attestation by an unauthorized key")
    status_is(_statement(ctx, k["root"], "key", _key_id(k["stranger"]), pubkey=_pub(k["stranger"]), arg=1, signer=k["mint"]), 422, what="a signing key authorizing another key (only a root may)")
    check(_log_size(ctx) == before, "something was stored")


@probe("G4", "G", "a key a root revoked cannot sign after the cutoff")
def g4(ctx: Ctx) -> None:
    k = _keys(ctx)
    d = Path(ctx.stack["keys_dir"]) / f"revoke-{ctx.tag}"  # unique per run and round: ssh-keygen would otherwise ask before overwriting
    made = run_cmd(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", f"chaos100x-revoked-{ctx.round}", "-f", str(d)], timeout=30, input="")
    check(made.returncode == 0, f"could not make the short-lived test key: {made.stderr[:100]}")
    kid = _key_id(d)
    nxt = ctx.client.get("/api/jarvis/attestations/head").json["next_signer_seq"]
    status_is(_statement(ctx, k["root"], "key", kid, pubkey=_pub(d), arg=nxt), 200, what="authorize the short-lived key")
    status_is(_statement(ctx, k["root"], "revoke", kid, arg=nxt - 1), 200, 422, what="revoke it with a cutoff before any use")
    block = ctx.head()["tip"]
    before = _log_size(ctx)
    status_is(_attestation(ctx, d, "block", f"block:{block['height']}", block["block_hash"]), 409, 422, what="an attestation by the revoked key")
    check(_log_size(ctx)[0] == before[0], "the revoked key's attestation was stored")


@probe("G5", "G", "a valid signature over different content, or at a wrong position, is rejected")
def g5(ctx: Ctx) -> None:
    k = _keys(ctx)
    block = ctx.head()["tip"]
    before = _log_size(ctx)
    other = hashlib.sha256(b"not the block").hexdigest()
    status_is(_attestation(ctx, k["mint"], "block", f"block:{block['height']}", other), 409, 422, what="a hash that is not the block's (equivocation if the block is already attested)")
    status_is(_attestation(ctx, k["mint"], "block", f"block:{block['height']}", block["block_hash"], sign_hash=other), 409, 422, what="a signature over a different hash")
    status_is(_attestation(ctx, k["mint"], "block", f"block:{block['height']}", block["block_hash"], seq=before[0] + 5), 409, what="a wrong position")
    status_is(_attestation(ctx, k["mint"], "block", "block:99999999", block["block_hash"]), 422, what="a block that does not exist")
    check(_log_size(ctx) == before, "something was stored")


@probe("G6", "G", "verify never reports success with no trust root: an unreadable file refuses, an empty one says 'not verified', and require fails", destructive=True)
def g6(ctx: Ctx) -> None:
    stack = ctx.need_destructive()
    ok = ctx.client.get("/api/jarvis/attestations/verify")
    status_is(ok, 200, what="verify with roots")
    check(ok.json["ok"] is True and any("attestation(s)" in n for n in ok.json["notes"]), "the control verify did not report the signing log")
    script = ('JARVIS_TRUST_ROOTS_FILE=/nonexistent python -m app.attest verify --tenant operator; echo "RC_MISSING=$?"; '
              'JARVIS_TRUST_ROOTS_FILE=/dev/null python -m app.attest verify --tenant operator; echo "RC_EMPTY=$?"; '
              'JARVIS_TRUST_ROOTS_FILE=/dev/null python -m app.attest verify --tenant operator --mode require; echo "RC_REQUIRE=$?"')
    r = run_cmd(["docker", "compose", "-p", stack["project"], "-f", stack["compose_file"], "run", "--rm", "--no-deps", "-T", "migrate", "sh", "-c", script], timeout=300)
    out = r.stdout + r.stderr
    rc = dict(re.findall(r"RC_(\w+)=(\d+)", out))
    check(set(rc) == {"MISSING", "EMPTY", "REQUIRE"}, f"the three runs did not all finish: {out.strip()[-200:]}")
    check(rc["MISSING"] != "0" and "trust_roots_unreadable" in out, f"an unreadable roots file did not refuse (rc {rc['MISSING']})")
    check("signatures not verified" in out, "an empty roots file did not say the signatures are not verified")
    check("attestation(s) up to" not in out, "a summary of signed objects was printed with no trust root (reported success)")
    check(rc["REQUIRE"] == "1", f"require with no trust root exited {rc['REQUIRE']}, expected 1")


@probe("G7", "G", "a receipt and its block signed by the test key report L1 in warn mode, never more")
def g7(ctx: Ctx) -> None:
    f1_ = ctx.state["f1"]
    v = ctx.client.get(f"/api/jarvis/replay/receipts/{f1_['id']}/verify")
    status_is(v, 200, what="verify receipt")
    s = v.json["signatures"]
    check(v.json["ok"] is True and s["mode"] == "warn", f"mode {s['mode']}, ok {v.json['ok']}")
    check(s["level"] == 1 and s["block"]["level"] == 1 and s["receipt"]["level"] == 1, f"levels {s['level']}/{s['block']['level']}/{s['receipt']['level']}, expected 1 (no root cosigned a checkpoint)")
    st = ctx.client.get(f"/api/jarvis/replay/state?at_block={f1_['block']}&limit=1").json
    check(st["block"]["signed"] is True and st["block"]["signature_level"] == 1, f"block.signed {st['block']['signed']} level {st['block']['signature_level']}")


# === Phase E: the database role and row-level security (a non-superuser, as the application role) ===========================================

def _app_psql(ctx: Ctx, script: str) -> subprocess.CompletedProcess:
    stack = ctx.need_destructive()
    text = Path(stack["secrets_dir"], "app.env").read_text()
    m = re.search(r"^JARVIS_DATABASE_URL=postgresql://([^:]+):([^@]+)@", text, re.M)
    check(m, "no application DSN in the throwaway secrets")
    user, password = m.group(1), m.group(2)
    return run_cmd(["docker", "exec", "-i", "-e", f"PGPASSWORD={password}", stack["containers"]["db"], "psql", "-X", "-q", "-t", "-A", "-h", "127.0.0.1",
                    "-U", user, "-d", "jarvis"], input="SET search_path TO jarvis, public;\n" + script, timeout=60)


def _as_tenant(tenant: str | None, statement: str) -> str:
    guc = f"SELECT set_config('jarvis.tenant_key', '{tenant}', false);\n" if tenant else ""
    return guc + statement


def _last(r: subprocess.CompletedProcess) -> str:
    lines = [l for l in r.stdout.strip().splitlines() if l.strip() and not l.startswith("SET")]
    return lines[-1] if lines else ""


@probe("E1", "E", "the application role is not a superuser and cannot bypass row-level security", destructive=True)
def e1(ctx: Ctx) -> None:
    r = _app_psql(ctx, "SELECT rolsuper::text || '/' || rolbypassrls::text FROM pg_roles WHERE rolname = current_user;")
    check(r.returncode == 0 and _last(r) == "false/false", f"the application role: {_last(r)} {r.stderr.strip()[:100]}")


@probe("E2", "E", "as the ordinary role with NO tenant set, the ledger tables look empty", destructive=True)
def e2(ctx: Ctx) -> None:
    ctx.need_destructive()
    for table in ("memories", "record_history", "blocks", "evidence_objects", "attestations", "trust_statements", "history_counters"):
        r = _app_psql(ctx, f"SELECT count(*) FROM {table};")
        denied = r.returncode != 0 and ("permission denied" in r.stderr or "unrecognized configuration parameter" in r.stderr or "row-level security" in r.stderr)
        check(_last(r) == "0" or denied, f"{table}: visible to a role with no tenant set (count {_last(r)!r}, {r.stderr.strip()[:80]})")


@probe("E3", "E", "as the ordinary role with the operator tenant set it sees exactly the operator's rows and no one else's", destructive=True)
def e3(ctx: Ctx) -> None:
    ctx.need_destructive()
    total = ctx.psql(f"SELECT count(*) FROM memories WHERE tenant_key = '{TENANT}';")
    check(int(total) > 0, "there are no operator records to look at")
    seen = _app_psql(ctx, _as_tenant(TENANT, "SELECT count(*) FROM memories;"))
    check(seen.returncode == 0 and _last(seen) == total, f"the role sees {_last(seen)} of {total}")
    foreign = _app_psql(ctx, _as_tenant(TENANT, "SELECT count(*) FROM memories WHERE tenant_key <> 'operator';"))
    check(_last(foreign) == "0", "the role sees another tenant's rows")
    other = _app_psql(ctx, _as_tenant("chaos-other-tenant", "SELECT (SELECT count(*) FROM memories) || '/' || (SELECT count(*) FROM record_history) || '/' || (SELECT count(*) FROM blocks);"))
    check(_last(other) == "0/0/0", f"another tenant sees {_last(other)}")


@probe("E4", "E", "as the ordinary role history, blocks, signatures and receipts cannot be changed or removed", destructive=True)
def e4(ctx: Ctx) -> None:
    ctx.need_destructive()
    attempts = ["UPDATE record_history SET op = op", "DELETE FROM record_history", "TRUNCATE record_history", "UPDATE blocks SET block_hash = block_hash",
                "DELETE FROM blocks", "UPDATE attestations SET key_id = key_id", "DELETE FROM attestations", "UPDATE trust_statements SET kind = kind",
                "DELETE FROM trust_statements", "UPDATE evidence_objects SET payload = payload", "DELETE FROM evidence_objects", "UPDATE history_counters SET last_seq = 0",
                "INSERT INTO blocks (tenant_key, height) VALUES ('operator', 999999)"]
    for sql in attempts:
        r = _app_psql(ctx, "BEGIN;\n" + _as_tenant(TENANT, sql + ";") + "\nROLLBACK;")
        check(r.returncode != 0 or "ERROR" in r.stderr, f"the application role was allowed: {sql}")
        check("permission denied" in r.stderr or "append-only" in r.stderr or "row-level security" in r.stderr or "violates" in r.stderr,
              f"{sql}: refused for an unexpected reason: {r.stderr.strip()[:120]}")


@probe("E5", "E", "row-level security binds a non-superuser: a row for another tenant cannot be written even with a tenant set", destructive=True)
def e5(ctx: Ctx) -> None:
    ctx.need_destructive()
    r = _app_psql(ctx, "BEGIN;\n" + _as_tenant(TENANT, "INSERT INTO boards (tenant_key, board) VALUES ('chaos-other-tenant', '{}'::jsonb);") + "\nROLLBACK;")
    check(r.returncode != 0 or "ERROR" in r.stderr, "a row for another tenant was written by the application role")
    check("row-level security" in r.stderr or "permission denied" in r.stderr, f"refused for an unexpected reason: {r.stderr.strip()[:140]}")
    check(ctx.psql("SELECT count(*) FROM boards WHERE tenant_key = 'chaos-other-tenant';") == "0", "the foreign-tenant row is in the database")
    hostile = _app_psql(ctx, "SELECT set_config('jarvis.tenant_key', $q$operator'; DROP TABLE memories; --$q$, false);\nSELECT count(*) FROM memories;")
    check(ctx.psql("SELECT to_regclass('memories') IS NOT NULL;") == "t", "a hostile tenant string dropped a table")
    check(_last(hostile) in ("0", ""), "a hostile tenant string showed rows")


# === Phase H: destructive probes, throwaway stack only =====================================================================================

@probe("H1", "H", "database down: the service fails closed with 503 and Retry-After, liveness stays up, and it recovers with the data intact", destructive=True, expect_5xx=(503,))
def h1(ctx: Ctx) -> None:
    stack = ctx.need_destructive()
    db = stack["containers"]["db"]
    rec = ctx.create(content=ctx.content("h1 survives the outage"))
    stopped = ctx.docker("stop", "-t", "3", db, timeout=60)
    check(stopped.returncode == 0, f"could not stop the throwaway database: {stopped.stderr[:100]}")
    try:
        down = ctx.wait_ready(503, 30)
        r = ctx.client.get("/ready", key=False, timeout=15)
        status_is(r, 503, what="ready during the outage")
        check(r.headers.get("Retry-After") or r.headers.get("retry-after"), "no Retry-After on the 503")
        w = ctx.client.post("/api/jarvis/memory", {"content": ctx.content("h1 during outage"), "source_agent": "chaos100x", "session_id": "chaos", "type": "decision",
                                                   "evidence": [{"kind": "user-request", "ref": "chaos100x"}]}, timeout=40)
        status_is(w, 503, what="a write while the database is down (fail closed, not 500, not a silent success)")
        status_is(ctx.client.get(f"/api/jarvis/memory/{rec['id']}", timeout=40), 503, what="a read while the database is down")
        status_is(ctx.client.get("/health", key=False), 200, what="liveness during the outage")
        ctx.metrics.setdefault("outage_detect_seconds", []).append(round(down, 2))
    finally:
        ctx.docker("start", db, timeout=60)
    back = ctx.wait_ready(200, 120)
    ctx.metrics.setdefault("recovery_seconds", []).append(round(back, 2))
    status_is(ctx.client.get(f"/api/jarvis/memory/{rec['id']}"), 200, what="the record written before the outage")
    ctx.create(content=ctx.content("h1 after recovery"))


@probe("H2", "H", "schema mismatch: readiness fails and names schema_version; removing the mismatch restores it", destructive=True, expect_5xx=(503,))
def h2(ctx: Ctx) -> None:
    ctx.need_destructive()
    ctx.psql("INSERT INTO schema_version (version) VALUES (99);")
    try:
        ctx.wait_ready(503, 20)
        r = ctx.client.get("/ready", key=False)
        status_is(r, 503, what="ready with a mismatched schema")
        check(r.json["checks"].get("schema_version") == "failed", f"checks {r.json['checks']}")
        check(all(v == "ok" for k, v in r.json["checks"].items() if k != "schema_version"), f"another check failed too: {r.json['checks']}")
        check("99" not in json.dumps(r.json), "the response leaks the schema version")
    finally:
        ctx.psql("DELETE FROM schema_version WHERE version = 99;")
    ctx.metrics.setdefault("schema_recovery_seconds", []).append(round(ctx.wait_ready(200, 30), 2))


@probe("H3", "H", "pool flood: 160 concurrent reads end in 200 or a 503 with Retry-After, never a 500, and readiness recovers", destructive=True, expect_5xx=(503,))
def h3(ctx: Ctx) -> None:
    ctx.need_destructive()
    path = "/api/jarvis/memory/retrieve?" + urllib.parse.urlencode({"query": "chaos100x", "limit": 50})
    t0 = time.perf_counter()
    with concurrent.futures.ThreadPoolExecutor(max_workers=40) as pool:
        results = list(pool.map(lambda _: ctx.client.get(path, timeout=60), range(160)))
    elapsed = time.perf_counter() - t0
    codes = Counter(r.status for r in results)
    bad = {c: n for c, n in codes.items() if c not in (200, 503)}
    check(not bad, f"unexpected statuses under flood: {bad}")
    check(all((r.headers.get("Retry-After") or r.headers.get("retry-after")) for r in results if r.status == 503), "a 503 without Retry-After")
    check(codes[200] > 0, "every request failed")
    ctx.metrics.setdefault("flood", []).append({"ok": codes[200], "503": codes[503], "seconds": round(elapsed, 2), "p99_ms": round(pct([r.ms for r in results], 0.99), 1)})
    ctx.wait_ready(200, 30)


@probe("H4", "H", "concurrent writers and force-seals (12 callers on a pool of 10): nothing is lost or corrupted, any refusal is a clean 503, blocks stay contiguous, chain and blocks verify", destructive=True, expect_5xx=(503,))
def h4(ctx: Ctx) -> None:
    """Twelve callers share a pool of ten connections with a one-second wait, so an occasional request is shed: that is the design (a 503 with Retry-After),
    not a failure.  What must never happen is a refusal that is anything else, an acknowledged write that is not in the ledger, or a chain that does not verify."""
    ctx.need_destructive()
    first = ctx.head()
    first_height = (first["tip"] or {"height": 0})["height"]
    results: list[tuple[str, int, dict[str, str], str | None]] = []
    lock = threading.Lock()

    def writer(i: int) -> None:
        for j in range(3):
            body = {"content": ctx.content(f"h4-{i}-{j}"), "source_agent": "chaos100x", "session_id": f"chaos-{ctx.round:03d}", "type": "decision",
                    "evidence": [{"kind": "user-request", "ref": f"chaos100x:round-{ctx.round}"}]}
            r = ctx.client.post("/api/jarvis/memory", body)
            with lock:
                results.append(("write", r.status, r.headers, r.json["memory"]["id"] if r.status == 200 else None))

    def sealer(i: int) -> None:
        for _ in range(3):
            r = ctx.client.post("/api/jarvis/blocks/seal", {"force": True, "min_entries": 1, "max_entries": 4})
            with lock:
                results.append(("seal", r.status, r.headers, None))
            time.sleep(0.05)

    with concurrent.futures.ThreadPoolExecutor(max_workers=12) as pool:
        futures = [pool.submit(writer, i) for i in range(8)] + [pool.submit(sealer, i) for i in range(4)]
        for f in futures:
            f.result()
    odd = sorted({(kind, status) for kind, status, _, _ in results if status not in (200, 503)})
    check(not odd, f"concurrent callers got {odd}: only 200 and a clean 503 are acceptable")
    unlabelled = [kind for kind, status, headers, _ in results if status == 503 and not (headers.get("Retry-After") or headers.get("retry-after"))]
    check(not unlabelled, f"{len(unlabelled)} refusal(s) without Retry-After")
    shed = sum(1 for _, status, _, _ in results if status == 503)
    ctx.metrics.setdefault("h4_shed_503s", []).append(shed)
    check(shed <= len(results) // 4, f"{shed} of {len(results)} concurrent calls were shed: the pool is not coping")
    for kind, status, _, rid in results:
        if kind == "write" and status == 200:
            status_is(ctx.client.get(f"/api/jarvis/memory/{rid}"), 200, what="a write the API acknowledged")
    with ctx.stats.lock:
        ctx.state["writes"] = ctx.state.get("writes", 0) + sum(1 for k, st, _, _ in results if k == "write" and st == 200)
    ctx.seal(max_entries=4)
    rows = ctx.client.get(f"/api/jarvis/blocks?after_height={first_height}&limit=1000").json["blocks"]
    check(rows, "no block was sealed")
    for prev, cur in zip(rows, rows[1:]):
        check(cur["height"] == prev["height"] + 1 and cur["first_seq"] == prev["last_seq"] + 1 and cur["prev_block_hash"] == prev["block_hash"],
              f"block {cur['height']} is not contiguous with {prev['height']}")
    check(all(b["entry_count"] <= 4 for b in rows), "a block is larger than the force-seal limit")
    verify = ctx.client.get("/api/jarvis/blocks/verify")
    status_is(verify, 200, what="blocks verify")
    check(verify.json["ok"] is True, "blocks do not verify after concurrent sealing")
    check(ctx.client.get("/api/jarvis/memory/history/verify").json["ok"] is True, "history does not verify after concurrent writes")


# === Phases I, J, K: ugly conditions (the application killed, the database cut off, the volume full) ===================================================
# One probe per fault.  Every fault runs with concurrent writers, and afterwards the same gates:
#   1. no half-writes: every write the API acknowledged exists whole; every record that exists is whole (one `create` in its history, the content
#      it was sent); no memory row without history; the history counter equals the newest history seq
#   2. the history chain and the blocks verify (the API's and the database's own verifiers)
#   3. a replay receipt taken before the fault still re-derives (the service, and offline from the raw rows)
#   4. the API failed closed during the fault (nothing acknowledged that could not have been stored; no 500) and recovered after it
# and the recovery times are measured (fault to the first request that is answered, and to three consecutive good readiness answers).

@dataclasses.dataclass
class Attempt:
    token: str
    content: str
    status: int
    id: str | None
    t0: float
    t1: float


def chaos_response_failed() -> "Response":
    return Response(0, None, {}, 0.0)


class Writers:
    """Concurrent record writers that keep going through a fault and remember what each request was told."""

    def __init__(self, ctx: "Ctx", label: str, n: int = 4, timeout: float = 20, pause: float = 0.25):
        self.ctx, self.label, self.n, self.timeout, self.pause = ctx, label, n, timeout, pause
        self.attempts: list[Attempt] = []
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []

    def _run(self, i: int) -> None:
        seq = 0
        while not self._stop.is_set():
            seq += 1
            token = f"{self.label}w{i}n{seq}"
            content = f"chaos100x {token} {self.ctx.tag}"
            body = {"content": content, "source_agent": "chaos100x", "session_id": f"chaos-{self.ctx.round:03d}", "type": "decision",
                    "evidence": [{"kind": "user-request", "ref": f"chaos100x:{self.label}", "note": "written while a fault is injected"}]}
            t0 = time.time()
            try:
                r = self.ctx.client.post("/api/jarvis/memory", body, timeout=self.timeout)
            except Exception:  # a writer that dies unnoticed would make a fault look quieter than it was: it is counted as a failed request
                r = chaos_response_failed()
            t1 = time.time()
            rid = r.json["memory"]["id"] if r.status == 200 and isinstance(r.json, dict) and "memory" in r.json else None
            with self._lock:
                self.attempts.append(Attempt(token, content, r.status, rid, t0, t1))
            time.sleep(self.pause if r.status == 200 else max(self.pause, 0.05))   # paced: a fault is about the moment, not about the volume

    def start(self) -> "Writers":
        for i in range(self.n):
            t = threading.Thread(target=self._run, args=(i,), daemon=True)
            t.start()
            self._threads.append(t)
        return self

    def stop(self) -> list[Attempt]:
        self._stop.set()
        for t in self._threads:
            t.join(self.timeout + 10)
        with self.ctx.stats.lock:
            self.ctx.state["writes"] = self.ctx.state.get("writes", 0) + sum(1 for a in self.attempts if a.status == 200)
        return list(self.attempts)


def watch_recovery(ctx: "Ctx", t_fault: float, *, max_s: float, give_up: Callable[[], None] | None = None, give_up_after: float | None = None,
                   need_failure_by: float = 15.0) -> dict[str, Any]:
    """Poll /ready from the moment of the fault: when did it first stop being 200, and when had it been 200 three times in a row?
    ``give_up`` (called once) is the intervention to try when the outage has lasted ``give_up_after`` seconds (default: half of ``max_s``)."""
    statuses: Counter = Counter()
    first_fail = recovered = None
    streak = 0
    gave_up = False
    while time.time() - t_fault < max_s:
        r = ctx.client.get("/ready", key=False, timeout=3)
        now = time.time()
        statuses[r.status] += 1
        if r.status != 200:
            first_fail = first_fail or now
            streak = 0
        else:
            streak += 1
            if streak >= 3 and (first_fail or now - t_fault > need_failure_by):
                recovered = now
                break
        if give_up and not gave_up and first_fail and now - first_fail > (give_up_after if give_up_after is not None else max_s / 2):
            give_up()
            gave_up = True
        time.sleep(0.2)
    return {"detect_s": round(first_fail - t_fault, 2) if first_fail else None, "recover_s": round(recovered - t_fault, 2) if recovered else None,
            "ready_statuses": dict(statuses), "gave_up_and_intervened": gave_up, "t_up": recovered}


def _db_rows(ctx: "Ctx", sql: str) -> list[list[str]]:
    out = ctx.psql(sql)
    return [line.split("|") for line in out.splitlines() if line.strip()]


def orphaned_sessions(ctx: "Ctx") -> list[int]:
    """Client ports of the application's database sessions that have no matching established socket in the application container: sessions the
    server is holding for a client that is gone."""
    stack = ctx.need_destructive()
    table = ctx.docker("exec", stack["containers"]["app"], "cat", "/proc/net/tcp", timeout=30).stdout.splitlines()[1:]
    mine = set()
    for line in table:
        parts = line.split()
        if len(parts) > 3 and parts[3] == "01" and int(parts[2].split(":")[1], 16) == 5432:
            mine.add(int(parts[1].split(":")[1], 16))
    rows = ctx.psql("SELECT client_port FROM pg_stat_activity WHERE backend_type = 'client backend' AND usename = 'jarvis_app' "
                    "AND client_addr IS NOT NULL AND client_addr <> '127.0.0.1';")
    return [int(r) for r in rows.splitlines() if r.strip().isdigit() and int(r) not in mine]


def wait_no_orphans(ctx: "Ctx", seconds: float = 120) -> dict[str, Any]:
    """After a fault the server must give back the slots of clients that are gone, on its own, within ``seconds``."""
    t0 = time.time()
    seen = None
    while True:
        orphans = orphaned_sessions(ctx)
        seen = len(orphans) if seen is None else seen
        if not orphans:
            return {"orphaned_sessions_at_recovery": seen, "orphans_reaped_after_s": round(time.time() - t0, 1)}
        if time.time() - t0 > seconds:
            raise ProbeFail(f"{len(orphans)} database session(s) held for a client that is gone were still there {seconds:g} s after recovery "
                            "(max_connections is small: repeated incidents would lock the application out)")
        time.sleep(3)


def fault_gates(ctx: "Ctx", attempts: list[Attempt], label: str) -> dict[str, Any]:
    """The gates after a fault (the API must be answering again).  Raises ProbeFail naming the first gate that does not hold."""
    receipt = ctx.ensure_receipt()
    acked = [a for a in attempts if a.status == 200]
    rows = _db_rows(ctx, "SELECT m.id, m.content, (SELECT string_agg(h.op, ',' ORDER BY h.seq) FROM record_history h WHERE h.tenant_key = m.tenant_key AND h.memory_id = m.id) "
                         f"FROM memories m WHERE m.content LIKE 'chaos100x {label}w%' AND m.content LIKE '%{ctx.tag}%';")
    by_content: dict[str, list[list[str]]] = {}
    for r in rows:
        by_content.setdefault(r[1], []).append(r)
    sent = {a.content for a in attempts}
    check(all(c in sent for c in by_content), "a record exists that no request sent (a fabricated or corrupted record)")
    check(all(len(v) == 1 for v in by_content.values()), "a request produced two records (a duplicated write)")
    check(all(v[0][2] == "create" for v in by_content.values()), f"a record's history is not exactly one create: {sorted({v[0][2] for v in by_content.values()})}")
    lost = [a.token for a in acked if a.content not in by_content or by_content[a.content][0][0] != a.id]
    check(not lost, f"{len(lost)} write(s) the API acknowledged are not in the ledger (first: {lost[:3]})")
    landed_unacked = sum(1 for a in attempts if a.status != 200 and a.content in by_content)
    orphans = ctx.psql("SELECT count(*) FROM memories m WHERE NOT EXISTS (SELECT 1 FROM record_history h WHERE h.tenant_key = m.tenant_key AND h.memory_id = m.id);")
    check(orphans == "0", f"{orphans} memory row(s) have no history (a half-write)")
    gap = ctx.psql(f"SELECT ((SELECT last_seq FROM history_counters WHERE tenant_key = '{TENANT}') = (SELECT max(seq) FROM record_history WHERE tenant_key = '{TENANT}'))::text;")
    check(gap == "true", "the history counter and the newest history entry disagree (a half-written entry)")
    check(ctx.psql(f"SELECT count(*) FROM jarvis_verify_history('{TENANT}');") == "0", "the database's history verifier reports a problem")
    check(ctx.psql(f"SELECT count(*) FROM jarvis_verify_blocks('{TENANT}');") == "0", "the database's block verifier reports a problem")
    hv, bv = ctx.client.get("/api/jarvis/memory/history/verify", timeout=60), ctx.client.get("/api/jarvis/blocks/verify", timeout=60)
    status_is(hv, 200, what="history verify")
    status_is(bv, 200, what="blocks verify")
    check(hv.json["ok"] is True, f"history does not verify: {hv.json['problems'][:2]}")
    check(bv.json["ok"] is True, f"blocks do not verify: {bv.json['problems'][:2]}")
    rv = ctx.client.get(f"/api/jarvis/replay/receipts/{receipt['id']}/verify", timeout=60)
    status_is(rv, 200, what="receipt verify")
    check(rv.json["ok"] is True, f"the earlier receipt no longer re-derives: {rv.json['problems'][:2]}")
    off = ctx.mint_script("replay.sh", "verify", "--receipt", receipt["id"], "--signatures", "off")
    check(off.returncode == 0, f"the earlier receipt does not re-derive offline from the raw rows: {(off.stdout + off.stderr).strip()[-160:]}")
    after = ctx.create(content=ctx.content(f"{label} after the fault"))
    status_is(ctx.client.get(f"/api/jarvis/memory/{after['id']}"), 200, what="read back a write made after the fault")
    reaped = wait_no_orphans(ctx)
    return {"attempts": len(attempts), "acknowledged": len(acked), "failed": len(attempts) - len(acked), "landed_but_unacknowledged": landed_unacked, **reaped}


def fail_closed(attempts: list[Attempt], t_fault: float, t_up: float | None, *, allowed: tuple[int, ...], settle: float = 0.5,
                acks_allowed: bool = False) -> dict[str, Any]:
    """During the fault: no write started after it (plus a moment to settle) was acknowledged, and every refusal is one of the ``allowed`` statuses.
    ``acks_allowed`` is for a fault that is partial by nature (a nearly full volume: a write that fits in a page that already exists succeeds, the next that
    needs a new page is refused): acknowledgements are then permitted DURING it, and the gates afterwards must find each one whole and durable."""
    end = t_up if t_up else float("inf")
    window = [a for a in attempts if a.t0 > t_fault + settle and a.t1 < end - 0.5]
    wrong = [a for a in window if a.status == 200]
    odd = sorted({a.status for a in window if a.status != 200 and a.status not in allowed})
    hang = max((a.t1 - a.t0 for a in attempts if a.t0 >= t_fault - 0.1), default=0.0)
    if wrong and not acks_allowed:
        raise ProbeFail(f"{len(wrong)} write(s) begun after the fault were acknowledged with 200 while it was in force (first: {wrong[0].token})")
    check(not odd, f"during the fault the API answered {odd}, not only {list(allowed)}")
    return {"writes_during_fault": len(window), "statuses_during_fault": dict(Counter(a.status for a in window)), "longest_request_s": round(hang, 2)}


def _record_fault(ctx: "Ctx", probe_id: str, data: dict[str, Any]) -> None:
    ctx.metrics.setdefault("faults", {}).setdefault(probe_id, []).append(data)


HEAL_INTERVAL_S = 60       # jarvis-heal.timer runs every minute on the box
PARTITION_PROMPT_S = 15    # a request caught by a silent partition must be answered (503) within this


@probe("I1", "I", "kill -9 the application container mid-write: nothing is acknowledged while it is down; the self-heal restarts it; the gates hold", destructive=True, expect_5xx=(0, -1, 503))
def i1(ctx: Ctx) -> None:
    """`docker kill` is a stop through the API: Docker's restart policy deliberately ignores it (deploy/mint/bin/heal.sh says so), so the box's recovery
    is the self-heal timer.  The probe kills, gives Docker a few seconds to restart it on its own (it should not), then runs the throwaway copy of
    heal.sh exactly as the timer does and measures from the kill to a stable /ready.  The worst case on the box adds up to one timer interval."""
    stack = ctx.need_destructive()
    app = stack["containers"]["app"]
    ctx.ensure_receipt()
    writers = Writers(ctx, "i1").start()
    data: dict[str, Any] = {}
    heal_at: list[float] = []

    def heal() -> None:
        heal_at.append(time.time())
        done = ctx.mint_script("heal.sh", timeout=120)
        if done.returncode != 0:
            raise ProbeFail(f"the self-heal failed: {(done.stdout + done.stderr).strip()[-160:]}")

    try:
        time.sleep(1.0)
        killed = ctx.docker("kill", "-s", "KILL", app, timeout=30)
        t_fault = time.time()                                                # the signal has been delivered: from here nothing may be acknowledged
        check(killed.returncode == 0, f"could not kill the throwaway application container: {killed.stderr[:100]}")
        # Docker's own restart policy gets four seconds of outage to restart it (it should not: an API kill is a manual stop); then the self-heal runs
        rec = watch_recovery(ctx, t_fault, max_s=120, give_up=heal, give_up_after=4.0)
        time.sleep(1.0)
    finally:
        attempts = writers.stop()
    docker_restarted = not rec["gave_up_and_intervened"]
    t_heal = heal_at[0] if heal_at else None
    started = ctx.docker("inspect", "-f", "{{.State.StartedAt}}", app).stdout.strip()
    t_started = datetime.fromisoformat(started[:26] + "+00:00").timestamp() if started[:4].isdigit() else None
    data.update(detect_s=rec["detect_s"], recover_s=rec["recover_s"], docker_restarted_it_itself=docker_restarted,
                heal_run_after_s=round(t_heal - t_fault, 2) if t_heal else None, startup_after_heal_s=round(rec["t_up"] - t_heal, 2) if (t_heal and rec["t_up"]) else None,
                worst_case_recover_s=round(HEAL_INTERVAL_S + (rec["t_up"] - t_heal), 2) if (t_heal and rec["t_up"]) else None,
                restarted_after_s=round(t_started - t_fault, 2) if t_started else None, ready_statuses=rec["ready_statuses"])
    _record_fault(ctx, "I1", data)
    check(rec["recover_s"] is not None, f"the application did not come back within 120 s ({rec['ready_statuses']})")
    check(rec["detect_s"] is not None, "the outage was never observed: the kill did not take effect")
    check(t_started is not None and t_started > t_fault, "Docker does not report a restart of the application after the kill")
    data.update(fail_closed(attempts, t_fault, t_started, allowed=(0, -1, 503), settle=0.2))     # nothing acknowledged before the new process existed
    data.update(fault_gates(ctx, attempts, "i1"))


@probe("J1", "J", "partition the application from the database mid-transaction: fails closed, heals, and the gates hold", destructive=True, expect_5xx=(0, -1, 503))
def j1(ctx: Ctx) -> None:
    stack = ctx.need_destructive()
    db, net = stack["containers"]["db"], stack["network"]
    ctx.ensure_receipt()
    anchor = ctx.create(content=ctx.content("j1 read during the partition"))
    writers = Writers(ctx, "j1", timeout=25).start()
    healed = False
    try:
        time.sleep(1.0)
        cut = ctx.docker("network", "disconnect", net, db, timeout=30)
        t_fault = time.time()
        check(cut.returncode == 0, f"could not cut the throwaway database off the network: {cut.stderr[:100]}")
        reads, ready = [], []
        while time.time() - t_fault < 12:                                    # hold the partition
            reads.append(ctx.client.get(f"/api/jarvis/memory/{anchor['id']}", timeout=20))
            ready.append(ctx.client.get("/ready", key=False, timeout=20))
            health = ctx.client.get("/health", key=False, timeout=5)
            check(health.status == 200, f"liveness during the partition was {health.status}")
        t_heal = time.time()
        joined = ctx.docker("network", "connect", "--alias", "db", net, db, timeout=30)
        healed = joined.returncode == 0
        check(healed, f"could not reconnect the throwaway database: {joined.stderr[:100]}")
        rec = watch_recovery(ctx, t_heal, max_s=120, need_failure_by=0)
        check(rec["recover_s"] is not None, f"the application did not recover within 120 s of the partition healing ({rec['ready_statuses']})")
        time.sleep(1.0)
    finally:
        if not healed:
            ctx.docker("network", "connect", "--alias", "db", net, db, timeout=30)
        attempts = writers.stop()
    check(all(r.status != 200 for r in reads), "a record was read through a severed link")
    bad_reads = sorted({r.status for r in reads} - {0, -1, 503})
    check(not bad_reads, f"reads during the partition answered {bad_reads}, not only 503 or no answer")
    check(any(r.status in (0, -1, 503) for r in ready), "readiness never reported the partition")
    closed = fail_closed(attempts, t_fault, t_heal, allowed=(0, -1, 503), settle=1.0)
    slowest = max([r.ms for r in reads + ready] + [closed["longest_request_s"] * 1000]) / 1000
    data = {"recover_s": rec["recover_s"], "partition_s": round(t_heal - t_fault, 2), "slowest_answer_s": round(slowest, 2),
            "readiness_during": dict(Counter(r.status for r in ready)), "reads_during": dict(Counter(r.status for r in reads)), **closed}
    _record_fault(ctx, "J1", data)
    # a link that has gone silent must fail within seconds, not hang for as long as TCP keeps retrying (52 s measured before app/pg_store.py got keepalives)
    check(slowest <= PARTITION_PROMPT_S, f"a request, or /ready, hung for {slowest:.0f} s during the partition (more than {PARTITION_PROMPT_S} s): the dead connection was not noticed")
    data.update(fault_gates(ctx, attempts, "j1"))


@probe("K1", "K", "fill the database volume until writes fail, then free it: fails closed, recovers, and the gates hold", destructive=True, expect_5xx=(0, -1, 503))
def k1(ctx: Ctx) -> None:
    stack = ctx.need_destructive()
    if not stack.get("pgdata_mb") or "keeper" not in stack.get("containers", {}):
        raise Skip("needs the size-capped database volume: JARVIS_CHAOS_PGDATA_MB=256 scripts/chaos/throwaway_stack.sh up (a real disk is never filled)")
    keeper, db = stack["containers"]["keeper"], stack["containers"]["db"]
    ctx.ensure_receipt()
    restarts_before = ctx.docker("inspect", "-f", "{{.RestartCount}}", db).stdout.strip()
    since = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    writers = Writers(ctx, "k1", timeout=25).start()
    freed = False
    try:
        time.sleep(1.0)
        t_fault = time.time()
        fill = ctx.docker("exec", keeper, "sh", "-c", "dd if=/dev/zero of=/keep/chaos-filler bs=64k 2>/dev/null; df -B1 --output=avail /keep | tail -1", timeout=120)
        free_after_fill = int(fill.stdout.strip().splitlines()[-1] or -1) if fill.stdout.strip() else -1
        check(0 <= free_after_fill < 1024 * 1024, f"the volume was not filled (free: {free_after_fill} bytes)")
        first_failure = None
        while time.time() - t_fault < 25 and first_failure is None:        # until a write fails (or we give up)
            first_failure = next((a for a in list(writers.attempts) if a.status != 200 and a.t0 > t_fault), None)
            time.sleep(0.2)
        check(first_failure is not None, "no write failed within 25 s of filling the volume: the fault did not bite")
        time.sleep(3.0)                                                      # the fault in force
        t_free = time.time()
        ctx.docker("exec", keeper, "rm", "-f", "/keep/chaos-filler", timeout=60)
        freed = True
        rec = watch_recovery(ctx, t_free, max_s=180, need_failure_by=0)
        check(rec["recover_s"] is not None, f"the application did not recover within 180 s of freeing the space ({rec['ready_statuses']})")
        time.sleep(1.0)
    finally:
        if not freed:
            ctx.docker("exec", keeper, "rm", "-f", "/keep/chaos-filler", timeout=60)
        attempts = writers.stop()
    closed = fail_closed(attempts, first_failure.t0, t_free, allowed=(503,), settle=0.5, acks_allowed=True)
    closed["acknowledged_while_full"] = sum(1 for a in attempts if a.status == 200 and first_failure.t0 + 0.5 < a.t0 and a.t1 < t_free - 0.5)
    closed["refused_while_full"] = sum(1 for a in attempts if a.status == 503 and first_failure.t0 + 0.5 < a.t0 and a.t1 < t_free - 0.5)
    check(closed["refused_while_full"] > 0, "nothing was refused while the volume was full")
    gates = fault_gates(ctx, attempts, "k1")
    logs = ctx.docker("logs", "--since", since, db, timeout=60)
    text = logs.stdout + logs.stderr
    restarts_after = ctx.docker("inspect", "-f", "{{.RestartCount}}", db).stdout.strip()
    _record_fault(ctx, "K1", {"recover_s": rec["recover_s"], "first_failure_s": round(first_failure.t0 - t_fault, 2), "volume_full_s": round(t_free - t_fault, 2),
                              "no_space_errors_in_db_log": text.count("No space left"), "panics": text.count("PANIC"),
                              "db_restarts": int(restarts_after or 0) - int(restarts_before or 0), **closed, **gates})


# --- the runner -----------------------------------------------------------------------------------------------------------------------------

PROBES_PER_ROUND = len(PROBES)


def run_round(rnd: int, ctx_args: dict[str, Any], only: set[str] | None, log: Callable[[str], None], stats: Stats) -> list[dict[str, Any]]:
    rng = random.Random(f"{ctx_args['seed']}-{rnd}")
    ctx = Ctx(rnd=rnd, rng=rng, **{k: v for k, v in ctx_args.items() if k != "seed"})
    out = []
    for p in PROBES:
        if only and p.id not in only:
            continue
        stats.current.update(round=rnd, probe=p.id, expect=p.expect_5xx)
        t0 = time.perf_counter()
        status, detail = "PASS", ""
        try:
            p.fn(ctx)
        except Skip as exc:
            status, detail = "SKIP", str(exc)
        except ProbeFail as exc:
            status, detail = "FAIL", str(exc)
        except Exception as exc:  # a probe that crashes is a finding too
            status, detail = "ERROR", f"{type(exc).__name__}: {exc}"
        ms = (time.perf_counter() - t0) * 1000
        out.append({"round": rnd, "probe": p.id, "phase": p.phase, "status": status, "ms": round(ms, 1), "detail": detail, "destructive": p.destructive})
        log(f"r{rnd:03d} {p.id:<3} {status:<5} {ms:8.0f}ms  {p.title}" + (f"  -- {detail}" if detail else ""))
    return out


def final_checks(client: Client, stack: dict[str, Any] | None, destructive_blocked: str | None) -> dict[str, Any]:
    """After the last round: everything the ledger can say about itself, plus the offline verifiers on the throwaway copy."""
    res: dict[str, Any] = {}
    res["history_verify"] = client.get("/api/jarvis/memory/history/verify", timeout=120).json
    res["blocks_verify"] = client.get("/api/jarvis/blocks/verify", timeout=120).json
    res["attestations_verify"] = client.get("/api/jarvis/attestations/verify", timeout=120).json
    head = client.get("/api/jarvis/blocks/head").json
    res["blocks_head"] = {"history_seq": head["history_seq"], "sealed_seq": head["sealed_seq"], "tip_height": (head["tip"] or {}).get("height"),
                          "unsealed_entries": head["unsealed_entries"]}
    receipts = client.get("/api/jarvis/replay/receipts?limit=1000").json["receipts"]
    bad = [r["id"] for r in receipts if not client.get(f"/api/jarvis/replay/receipts/{r['id']}/verify", timeout=120).json.get("ok")]
    res["receipts"] = {"count": len(receipts), "failing_rederivation": bad}
    if stack and not destructive_blocked:
        env = dict(os.environ, JARVIS_HOME=f"{stack['dir']}/home")
        v = run_cmd([f"{stack['dir']}/mint/bin/jarvisctl", "verify"], timeout=900, env=env)
        res["jarvisctl_verify"] = {"rc": v.returncode, "tail": (v.stdout + v.stderr).strip().splitlines()[-4:]}
        s = run_cmd(["docker", "stats", "--no-stream", "--format", "{{.Name}} cpu={{.CPUPerc}} mem={{.MemUsage}}", *stack["containers"].values()], timeout=60)
        res["container_stats"] = s.stdout.strip().splitlines()
        db = stack["containers"]["db"]

        def q(database: str, sql: str) -> str:
            return run_cmd(["docker", "exec", "-u", "postgres", db, "psql", "-X", "-q", "-t", "-A", "-d", database, "-c", sql], timeout=60).stdout.strip()

        res["database_size"] = q("jarvis", "SELECT pg_size_pretty(pg_database_size('jarvis'));")
        res["scratch_databases_left"] = q("postgres", "SELECT count(*) FROM pg_database WHERE datname = 'chaos_scratch';")
        res["scratch_hba_rules_left"] = run_cmd(["docker", "exec", db, "grep", "-c", "chaos_scratch", HBA], timeout=30).stdout.strip()
        res["schema_version_after"] = q("jarvis", "SELECT max(version) FROM jarvis.schema_version;")
        res["counts"] = {t: q("jarvis", f"SELECT count(*) FROM jarvis.{t};") for t in ("memories", "record_history", "blocks", "evidence_objects", "attestations", "trust_statements")}
    return res


def fault_summary(faults: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    """Per fault: how many times, and the min / median / max of each timing, plus what the writers saw."""
    out: dict[str, Any] = {}
    for probe_id, runs in sorted(faults.items()):
        entry: dict[str, Any] = {"runs": len(runs)}
        for key in ("detect_s", "recover_s", "first_failure_s", "volume_full_s", "partition_s", "longest_request_s", "orphans_reaped_after_s"):
            values = sorted(r[key] for r in runs if isinstance(r.get(key), (int, float)))
            if values:
                entry[key] = {"min": values[0], "median": values[len(values) // 2], "max": values[-1]}
        for key in ("attempts", "acknowledged", "failed", "landed_but_unacknowledged", "writes_during_fault", "no_space_errors_in_db_log", "panics", "db_restarts", "orphaned_sessions_at_recovery"):
            entry[key] = sum(int(r.get(key) or 0) for r in runs)
        statuses: Counter = Counter()
        for r in runs:
            statuses.update({str(k): v for k, v in r.get("statuses_during_fault", {}).items()})
        entry["statuses_during_fault"] = dict(statuses)
        out[probe_id] = entry
    return out


def summarize(results: list[dict[str, Any]], stats: Stats, rounds_done: int, started: float, final: dict[str, Any], state: dict[str, Any], args: argparse.Namespace,
              capped: str | None) -> dict[str, Any]:
    by_probe: dict[str, Counter] = {}
    for r in results:
        by_probe.setdefault(r["probe"], Counter())[r["status"]] += 1
    lat = {p: {"n": len(v), "p50": round(pct(v, 0.5), 1), "p95": round(pct(v, 0.95), 1), "p99": round(pct(v, 0.99), 1), "max": round(max(v), 1)} for p, v in sorted(stats.latencies.items())}
    allv = [x for v in stats.latencies.values() for x in v]
    unexpected = [e for e in stats.five_xx if not e["expected"]]
    return {
        "name": "CL_CHAOS_100x", "probes_per_round": PROBES_PER_ROUND, "rounds_requested": args.rounds, "rounds_completed": rounds_done,
        "probe_runs": len(results), "probes_selected_per_round": len({r["probe"] for r in results if r["round"] == 1}) or PROBES_PER_ROUND,
        "expected_probe_runs": (len({r["probe"] for r in results if r["round"] == 1}) or PROBES_PER_ROUND) * rounds_done, "capped": capped,
        "seconds": round(time.time() - started, 1),
        "status_counts": dict(Counter(r["status"] for r in results)),
        "failures": [r for r in results if r["status"] in ("FAIL", "ERROR")],
        "skips": [r for r in results if r["status"] == "SKIP"],
        "per_probe": {p: dict(c) for p, c in sorted(by_probe.items())},
        "requests": stats.requests, "http_statuses": {str(k): v for k, v in sorted(stats.statuses.items())},
        "five_xx_total": len(stats.five_xx), "five_xx_expected": len(stats.five_xx) - len(unexpected), "five_xx_unexpected": unexpected,
        "five_xx_by_probe": {f"{p}:{c}": n for (p, c), n in sorted(Counter((e["probe"], e["status"]) for e in stats.five_xx).items())},
        "latency_ms": {"all": {"p50": round(pct(allv, 0.5), 1), "p95": round(pct(allv, 0.95), 1), "p99": round(pct(allv, 0.99), 1), "max": round(max(allv or [0]), 1),
                               "mean": round(statistics.fmean(allv), 1) if allv else 0}, "per_probe": lat},
        "writes": state.get("writes", 0), "metrics": state.get("metrics", {}), "fault_summary": fault_summary(state.get("metrics", {}).get("faults", {})), "final": final,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="cl_chaos_100x.py", description="CL_CHAOS_100x: repeated probe rounds against a throwaway stack only")
    ap.add_argument("--rounds", type=int, default=100)
    ap.add_argument("--target", help="override the target URL (default: the throwaway stack in stack.json)")
    ap.add_argument("--stack-dir", default=os.environ.get("JARVIS_CHAOS_DIR") or str(Path(os.environ.get("TMPDIR", "/tmp")) / "jarvis-chaos100x"))
    ap.add_argument("--seed", default=None, help="seeds the per-round content; default: a fresh random seed, printed in the header so a run can be repeated")
    ap.add_argument("--max-history", type=int, default=6000, help="stop before a round once the history counter reaches this (the ledger is bounded)")
    ap.add_argument("--only", help="comma-separated probe ids")
    ap.add_argument("--phases", help="comma-separated phase letters (for example I,J,K for the ugly-conditions faults); combined with --only if both are given")
    ap.add_argument("--out", help="directory for results.json and rounds.log")
    ap.add_argument("--count", action="store_true", help="print the per-round probe count and exit")
    ap.add_argument("--list", action="store_true", help="print the probe list and exit")
    ap.add_argument("--i-know-this-is-live", action="store_true", dest="allow_live", help="lift the live-target refusals (never used by the chaos task)")
    args = ap.parse_args(argv)

    if args.count:
        print(PROBES_PER_ROUND)
        return EXIT_OK
    if args.list:
        for p in PROBES:
            print(f"{p.id:<3} {p.phase} {'destructive ' if p.destructive else '            '}{p.title}")
        print(f"{PROBES_PER_ROUND} probes per round")
        return EXIT_OK
    if args.rounds < 1:
        print("--rounds must be at least 1", file=sys.stderr)
        return EXIT_USAGE

    try:
        stack = None
        try:
            stack = load_stack(args.stack_dir)
        except Refusal:
            if not args.target:
                raise
        url = args.target or stack["url"]
        target = assess_target(url, fetch_ready, allow_live=args.allow_live)
    except Refusal as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return EXIT_REFUSED

    destructive_blocked = throwaway_proof(stack, target) if stack else "no stack.json"
    key_file = Path(stack["secrets_dir"], "api-key") if stack else None
    if key_file is None or not key_file.is_file():
        print("REFUSED: no throwaway API key file (secrets/api-key in the throwaway stack)", file=sys.stderr)
        return EXIT_REFUSED
    stats = Stats()
    client = Client(target["url"], key_file.read_text().strip(), stats)
    out_dir = Path(args.out) if args.out else None
    if out_dir:
        out_dir.mkdir(parents=True, exist_ok=True)
    log_file = open(out_dir / "rounds.log", "w") if out_dir else None

    def log(line: str) -> None:
        print(line, flush=True)
        if log_file:
            log_file.write(line + "\n")
            log_file.flush()

    only = set(args.only.split(",")) if args.only else None
    if args.phases:
        wanted = {p.id for p in PROBES if p.phase in set(args.phases.upper().split(","))}
        only = wanted if only is None else (only | wanted)
    args.seed = args.seed or os.urandom(4).hex()
    selected = [p for p in PROBES if not only or p.id in only]
    shown = f"{PROBES_PER_ROUND} probes per round" + (f" ({len(selected)} selected)" if len(selected) != PROBES_PER_ROUND else "")
    log(f"CL_CHAOS_100x: seed {args.seed}; {shown}, {args.rounds} round(s) = {len(selected) * args.rounds} probe runs; "
        f"target {target['url']} stack {target['stack']}; destructive probes {'ENABLED' if not destructive_blocked else 'SKIPPED (' + destructive_blocked + ')'}")
    state: dict[str, Any] = {}
    results: list[dict[str, Any]] = []
    started, rounds_done, capped = time.time(), 0, None
    ctx_args = dict(client=client, stats=stats, stack=stack, target=target, destructive_ok=destructive_blocked, state=state, seed=args.seed, max_history=args.max_history)
    start_head = client.get("/api/jarvis/blocks/head").json
    for rnd in range(1, args.rounds + 1):
        head = client.get("/api/jarvis/blocks/head").json
        if head and head.get("history_seq", 0) >= args.max_history:
            capped = f"history counter {head['history_seq']} reached --max-history {args.max_history} before round {rnd}"
            log(f"STOP: {capped}")
            break
        results.extend(run_round(rnd, ctx_args, only, log, stats))
        rounds_done = rnd
    final = final_checks(client, stack, destructive_blocked)
    final["start_head"] = {"history_seq": (start_head or {}).get("history_seq"), "tip_height": ((start_head or {}).get("tip") or {}).get("height")}
    summary = summarize(results, stats, rounds_done, started, final, state, args, capped)
    log(json.dumps({k: summary[k] for k in ("probes_per_round", "rounds_completed", "probe_runs", "status_counts", "requests", "five_xx_total", "five_xx_expected")}))
    if out_dir:
        (out_dir / "results.json").write_text(json.dumps(summary, indent=2))
    bad = summary["status_counts"].get("FAIL", 0) + summary["status_counts"].get("ERROR", 0) + len(summary["five_xx_unexpected"])
    verify_bad = (not final["history_verify"].get("ok") or not final["blocks_verify"].get("ok") or not final["attestations_verify"].get("ok")
                  or final["receipts"]["failing_rederivation"] or final.get("jarvisctl_verify", {}).get("rc", 0) != 0)
    return EXIT_PROBE_FAILURES if (bad or verify_bad) else EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
