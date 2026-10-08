#!/usr/bin/env python3
"""Soak: is the application's memory growth a leak or a cache, and does retrieval slow down with the size of the ledger?

    scripts/chaos/throwaway_stack.sh up
    scripts/chaos/soak.py --out out/soak            # about 70 minutes with the defaults
    scripts/chaos/throwaway_stack.sh down

Four phases against a THROWAWAY stack (the same guard as CL_CHAOS_100x: it refuses 8011, any non-loopback host and any target that does not
report a chaos-throwaway: identity; it reads memory only from the throwaway's own application container):

  growth    records are written and sealed in small batches while the ledger grows; every sample measures the application's memory (anonymous RSS,
            from /proc/1/status), the latency of a typical and of a hostile retrieve, the time `blocks/verify` and `history/verify` take, and the database size
  reads     the ledger stops growing; constant read load for a while.  Memory against REQUESTS on a constant ledger: a slope is a leak
  idle      no traffic: does memory come back?
  control   the application container is restarted and given the same read load.  If memory returns to the same level on the same ledger, the memory is
            explained by the ledger (state the process holds because of its size), not by the process's age
  concurrency  (``--concurrency``, on its own or after the rest) a restarted process is given the same reads at 1, 4, 16 and 40 callers at once and its
            high-water mark is read after each: a retrieve that materialises the ledger makes the peak follow callers x ledger size, which is how a
            flood of readers can look like a leak

The verdict is computed from the samples and printed with the numbers; it claims no more than they show.  Standard library only.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import math
import os
import re
import statistics
import subprocess
import sys
import threading
import time
import urllib.parse
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("cl_chaos_100x", HERE / "cl_chaos_100x.py")
chaos = importlib.util.module_from_spec(_spec)
sys.modules["cl_chaos_100x"] = chaos
_spec.loader.exec_module(chaos)

TENANT = chaos.TENANT
HOSTILE = "' OR '1'='1"
TYPICAL = "chaos100x"


# --- measurements -----------------------------------------------------------------------------------------------------------------------------

class SoakError(Exception):
    """The run cannot produce a valid result (a measurement that cannot be made, a control that cannot be set up): it stops, it does not carry on."""


class Failures:
    """Every request the soak itself needed to succeed and did not (a growth write, a seal, a timed retrieve or verification, a read under load).  A soak
    with any of these is a partial or invalid experiment and says so in its result and its exit status; it never quietly records them and goes on."""

    def __init__(self) -> None:
        self.items: list[dict[str, Any]] = []

    def note(self, what: str, status: Any) -> None:
        self.items.append({"what": what, "status": status})

    def __bool__(self) -> bool:
        return bool(self.items)

    def summary(self) -> dict[str, Any]:
        counts: dict[str, int] = {}
        for i in self.items:
            key = f"{i['what']}:{i['status']}"
            counts[key] = counts.get(key, 0) + 1
        return {"count": len(self.items), "by_kind": counts}


def process_memory_checked(container: str, runner=chaos.run_cmd, attempts: int = 3, sleep=time.sleep) -> dict[str, int]:
    """process_memory, retried; if the numbers still cannot be read the run stops: a missing measurement must never be read as 0 MiB."""
    for i in range(attempts):
        mem = process_memory(container, runner)
        if "RssAnon" in mem and "VmRSS" in mem:
            return mem
        if i + 1 < attempts:
            sleep(1)
    raise SoakError(f"cannot read the application's memory from {container} (/proc/1/status) after {attempts} attempts")


def process_memory(container: str, runner=chaos.run_cmd) -> dict[str, int]:
    """Anonymous and total resident memory of the application's main process (kB), from inside its own container."""
    r = runner(["docker", "exec", container, "cat", "/proc/1/status"], timeout=30)
    if r.returncode != 0:
        return {}
    out = {}
    for key in ("VmRSS", "RssAnon", "RssFile", "VmHWM", "VmData", "Threads"):
        m = re.search(rf"^{key}:\s+(\d+)", r.stdout, re.M)
        if m:
            out[key] = int(m.group(1))
    return out


def restart_and_wait(stack: dict[str, Any], target: dict[str, Any], *, ready_timeout: float = 120, runner=None, ready=None, sleep=time.sleep) -> str | None:
    """Restart the application container and wait until a NEW process answers /ready.  Returns None on success, else why it did not work: a restart that
    failed, a container whose start time did not change (the old process is still the one answering), or a readiness that never came back.  A control
    taken from the same process, or from none, would make the verdict meaningless."""
    runner = runner or chaos.run_cmd
    ready = ready or chaos.fetch_ready
    name = stack["containers"]["app"]

    def started() -> str:
        return runner(["docker", "inspect", "-f", "{{.State.StartedAt}}", name], timeout=30).stdout.strip()

    before = started()
    done = runner(["docker", "restart", name], timeout=120)
    if done.returncode != 0:
        return f"docker restart {name} failed: {done.stderr.strip()[:100]}"
    for _ in range(int(ready_timeout)):
        if ready(target["url"])[0] == 200:
            break
        sleep(1)
    else:
        return f"the application did not answer /ready within {ready_timeout:g} s of the restart"
    after = started()
    if not after or after == before:
        return f"the container's start time did not change ({before!r}): the process that answered is the old one"
    return None


def timed(fn, failures: "Failures | None" = None, what: str = "") -> float:
    """Milliseconds for one call; if the call returns a response that is not a 200 it is noted as a failure (a timing of an error is not a timing)."""
    t0 = time.perf_counter()
    resp = fn()
    ms = (time.perf_counter() - t0) * 1000
    if failures is not None and getattr(resp, "status", 200) != 200:
        failures.note(what or "timed request", getattr(resp, "status", None))
    return ms


def median_ms(fn, n: int = 3, failures: "Failures | None" = None, what: str = "") -> float:
    return statistics.median(timed(fn, failures, what) for _ in range(n))


def database_mb(stack: dict[str, Any], runner=chaos.run_cmd) -> float | None:
    r = runner(["docker", "exec", "-u", "postgres", stack["containers"]["db"], "psql", "-X", "-q", "-t", "-A", "-d", "jarvis", "-c",
                "SELECT pg_database_size('jarvis')/1048576.0;"], timeout=30)
    try:
        return round(float(r.stdout.strip()), 1)
    except ValueError:
        return None


class Load:
    """Constant read load on a ledger that is not changing; counts what it asked."""

    def __init__(self, client, record_id: str, threads: int = 4):
        self.client, self.record_id, self.threads = client, record_id, threads
        self.count = 0
        self.errors = 0
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._ts: list[threading.Thread] = []

    def _run(self, i: int) -> None:
        n = 0
        while not self._stop.is_set():
            n += 1
            path = [f"/api/jarvis/memory/retrieve?query={TYPICAL}&limit=50", f"/api/jarvis/memory/{self.record_id}", "/api/jarvis/blocks/head",
                    f"/api/jarvis/memory/retrieve?query={urllib.parse.quote(HOSTILE)}&limit=5"][(n + i) % 4]
            r = self.client.get(path, timeout=60)
            with self._lock:
                self.count += 1
                self.errors += 0 if r.status == 200 else 1

    def start(self) -> "Load":
        for i in range(self.threads):
            t = threading.Thread(target=self._run, args=(i,), daemon=True)
            t.start()
            self._ts.append(t)
        return self

    def stop(self) -> None:
        self._stop.set()
        for t in self._ts:
            t.join(90)


# --- analysis (pure functions: tested on synthetic data) --------------------------------------------------------------------------------------

def linear_fit(xs: list[float], ys: list[float]) -> dict[str, float]:
    """Least squares y = a + b x; returns slope, intercept and r2.  Fewer than 3 points or no spread in x: slope 0, r2 0."""
    n = len(xs)
    if n < 3 or max(xs) == min(xs):
        return {"slope": 0.0, "intercept": ys[0] if ys else 0.0, "r2": 0.0, "n": n}
    mx, my = statistics.fmean(xs), statistics.fmean(ys)
    sxx = sum((x - mx) ** 2 for x in xs)
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    syy = sum((y - my) ** 2 for y in ys)
    slope = sxy / sxx
    r2 = (sxy * sxy) / (sxx * syy) if syy > 0 else 0.0
    return {"slope": slope, "intercept": my - slope * mx, "r2": r2, "n": n}


def power_law_exponent(xs: list[float], ys: list[float]) -> dict[str, float]:
    """Fit y = c x^k on log-log axes: k = 1 is linear in the ledger's size, k = 2 quadratic, k near 0 flat."""
    pts = [(math.log(x), math.log(y)) for x, y in zip(xs, ys) if x > 0 and y > 0]
    fit = linear_fit([p[0] for p in pts], [p[1] for p in pts])
    return {"exponent": fit["slope"], "r2": fit["r2"], "n": fit["n"]}


def concurrency_note(levels: list[dict]) -> dict[str, Any]:
    """levels: [{"callers", "hwm_mib", "anon_mib"}] in ascending order of callers, one restarted process.  Does the high-water mark follow the callers?"""
    if len(levels) < 3:
        return {"notes": []}
    f = linear_fit([l["callers"] for l in levels], [l["hwm_mib"] for l in levels])
    out = {"mib_per_caller": round(f["slope"], 2), "r2": round(f["r2"], 3), "levels": levels}
    first, last = levels[0], levels[-1]
    rise = last["hwm_mib"] - first["hwm_mib"]
    if f["r2"] >= 0.8 and f["slope"] > 0.5 and rise > 10:
        out["notes"] = [f"the high-water mark follows the number of simultaneous callers: about {f['slope']:.1f} MiB per extra caller on this ledger "
                        f"({first['hwm_mib']:.0f} MiB at {first['callers']} caller to {last['hwm_mib']:.0f} MiB at {last['callers']}): each retrieve holds a copy of the "
                        "ledger while it runs, so a burst of readers raises the peak without any request leaking"]
    else:
        out["notes"] = [f"the high-water mark does not follow the number of simultaneous callers ({first['hwm_mib']:.0f} -> {last['hwm_mib']:.0f} MiB)"]
    return out


def verdict(growth: list[dict], reads: list[dict], idle: list[dict], control: list[dict]) -> dict[str, Any]:
    """Memory: is it a leak (it grows with REQUESTS on a constant ledger, and a restart gets it back) or the ledger (it returns to the same level after a
    restart on the same ledger)?  Latency: how does retrieval follow the number of records?"""
    out: dict[str, Any] = {}
    anon = lambda s: s.get("RssAnon", s.get("VmRSS", 0)) / 1024.0          # noqa: E731  MiB
    if len(growth) >= 3:
        f = linear_fit([s["records"] for s in growth], [anon(s) for s in growth])
        out["memory_vs_records"] = {"mib_per_1000_records": round(f["slope"] * 1000, 3), "r2": round(f["r2"], 3), "start_mib": round(anon(growth[0]), 1), "end_mib": round(anon(growth[-1]), 1)}
    if len(reads) >= 3:
        f = linear_fit([s["requests"] for s in reads], [anon(s) for s in reads])
        out["memory_vs_requests_on_a_constant_ledger"] = {"mib_per_100k_requests": round(f["slope"] * 100_000, 3), "r2": round(f["r2"], 3),
                                                          "first_mib": round(anon(reads[0]), 1), "last_mib": round(anon(reads[-1]), 1), "requests": reads[-1]["requests"] - reads[0]["requests"]}
    end_of_reads = anon(reads[-1]) if reads else None
    after_idle = anon(idle[-1]) if idle else None
    after_restart = anon(control[-1]) if control else None
    out["levels_mib"] = {"end_of_reads": end_of_reads and round(end_of_reads, 1), "after_idle": after_idle and round(after_idle, 1),
                         "after_restart_and_the_same_load": after_restart and round(after_restart, 1)}
    notes = []
    leak_rate = out.get("memory_vs_requests_on_a_constant_ledger")
    grows_with_requests = bool(leak_rate and leak_rate["r2"] >= 0.6 and leak_rate["mib_per_100k_requests"] > 2.0 and leak_rate["last_mib"] - leak_rate["first_mib"] > 2.0)
    if end_of_reads and after_restart:
        gap = (end_of_reads - after_restart) / end_of_reads
        out["restart_gap_fraction"] = round(gap, 3)
        if grows_with_requests and gap > 0.15:
            notes.append("LEAK SUSPECTED: memory grows with requests on a constant ledger, and a restart gets back "
                         f"{gap:.0%} of it")
        elif grows_with_requests:
            notes.append("memory still grows with requests on a constant ledger, but the restarted process reaches the same level: it converges on the ledger's size "
                         "(a cache filling) rather than growing without bound; a longer run would tell")
        elif abs(gap) <= 0.15:
            notes.append(f"NOT A LEAK on this evidence: memory is flat against requests on a constant ledger, and a restarted process under the same load returns to the same level "
                         f"(within {abs(gap):.0%}): the level is explained by the ledger, not by the process's age")
        elif gap > 0.15:
            notes.append(f"the restarted process uses {gap:.0%} LESS than the old one on the same ledger and load, yet memory is not growing with requests: accumulation over the "
                         "process's life (allocator fragmentation, caches that were warmed by the growth phase) rather than per-request growth; not proven either way")
        else:
            notes.append(f"the restarted process uses {-gap:.0%} MORE than the old one on the same ledger and load")
    if after_idle and end_of_reads:
        out["idle_release_fraction"] = round((end_of_reads - after_idle) / end_of_reads, 3)
    if len(growth) >= 4:
        lat = {}
        for key in ("retrieve_typical_ms", "retrieve_hostile_ms", "blocks_verify_ms", "history_verify_ms"):
            fit = power_law_exponent([s["records"] for s in growth], [s[key] for s in growth if s.get(key)] if all(s.get(key) for s in growth) else [])
            if fit["n"] >= 4:
                lat[key] = {"exponent": round(fit["exponent"], 2), "r2": round(fit["r2"], 3), "first_ms": round(growth[0][key], 1), "last_ms": round(growth[-1][key], 1)}
        out["latency_vs_records"] = lat
        for key, v in lat.items():
            shape = "about linear in the number of records" if 0.8 <= v["exponent"] <= 1.3 else ("super-linear" if v["exponent"] > 1.3 else ("sub-linear" if v["exponent"] > 0.3 else "flat"))
            notes.append(f"{key}: {v['first_ms']:.0f} ms -> {v['last_ms']:.0f} ms, {shape} (exponent {v['exponent']}, r2 {v['r2']})")
    out["notes"] = notes
    return out


# --- the run ----------------------------------------------------------------------------------------------------------------------------------

def sample(client, stack, phase: str, t0: float, requests: int, records: int, failures: "Failures | None" = None) -> dict[str, Any]:
    mem = process_memory_checked(stack["containers"]["app"])
    s = {"t": round(time.time() - t0, 1), "phase": phase, "requests": requests, "records": records, **mem}
    if phase in ("growth", "reads", "control"):
        s["retrieve_typical_ms"] = round(median_ms(lambda: client.get(f"/api/jarvis/memory/retrieve?query={TYPICAL}&limit=50", timeout=120), failures=failures, what="retrieve"), 1)
    if phase == "growth":
        s["retrieve_hostile_ms"] = round(median_ms(lambda: client.get(f"/api/jarvis/memory/retrieve?query={urllib.parse.quote(HOSTILE)}&limit=5", timeout=120), failures=failures, what="hostile retrieve"), 1)
        s["blocks_verify_ms"] = round(timed(lambda: client.get("/api/jarvis/blocks/verify", timeout=300), failures, "blocks/verify"), 1)
        s["history_verify_ms"] = round(timed(lambda: client.get("/api/jarvis/memory/history/verify", timeout=300), failures, "history/verify"), 1)
        s["db_mb"] = database_mb(stack)
    return s


def run_concurrency(args, stack, target, client, stats, out_dir) -> int:
    """Restart the application, then read at 1, 4, 16 and 40 callers at once (ascending: the high-water mark only rises), and read the mark after each."""
    lines = []

    def log(line: str) -> None:
        print(line, flush=True)
        lines.append(line)

    head_resp = client.get("/api/jarvis/blocks/head")
    if head_resp.status != 200 or not isinstance(head_resp.json, dict) or "history_seq" not in head_resp.json:
        raise SoakError(f"the ledger's blocks/head answered {head_resp.status} before the concurrency phase: nothing to measure against")
    head = head_resp.json
    first = client.get("/api/jarvis/memory/retrieve?query=chaos100x&limit=1")
    memories = first.json.get("memories") if first.status == 200 and isinstance(first.json, dict) else None
    if not memories:
        raise SoakError(f"no record to read: retrieve answered {first.status} with {'no records' if first.status == 200 else 'an error'}; the load would be cheap 404s, not reads")
    record_id = memories[0]["id"]
    log(f"concurrency: ledger at {head['history_seq']} entries; restarting the application container and reading at 1, 4, 16, 40 callers for {args.concurrency_seconds:g} s each")
    why = restart_and_wait(stack, target)
    if why:
        log(f"ABORTED: the concurrency phase needs a freshly restarted process and did not get one: {why}")
        return chaos.EXIT_PROBE_FAILURES
    base = process_memory_checked(stack["containers"]["app"])
    levels = [{"callers": 0, "hwm_mib": base.get("VmHWM", 0) / 1024, "anon_mib": base.get("RssAnon", 0) / 1024, "requests": 0, "p50_ms": None, "p99_ms": None, "unanswered": 0}]
    log(f"  after restart: high-water {levels[0]['hwm_mib']:.1f} MiB, anonymous {levels[0]['anon_mib']:.1f} MiB")
    for callers in (1, 4, 16, 40):
        load = Load(client, record_id, threads=callers).start()
        time.sleep(args.concurrency_seconds)
        load.stop()
        mem = process_memory_checked(stack["containers"]["app"])
        if load.count - load.errors <= 0:
            raise SoakError(f"no request was answered with 200 at {callers} callers: this level measured nothing")
        lat = sorted(l for l in client.stats.latencies.get("-", [])[-max(load.count, 1):])
        levels.append({"callers": callers, "hwm_mib": round(mem.get("VmHWM", 0) / 1024, 1), "anon_mib": round(mem.get("RssAnon", 0) / 1024, 1), "requests": load.count,
                       "p50_ms": round(chaos.pct(lat, 0.5), 1) if lat else None, "p99_ms": round(chaos.pct(lat, 0.99), 1) if lat else None, "unanswered": load.errors})
        log(f"  {callers:>2} callers: {load.count:>5} requests ({load.errors} not 200), high-water {levels[-1]['hwm_mib']:.1f} MiB, anonymous {levels[-1]['anon_mib']:.1f} MiB, "
            f"p50 {levels[-1]['p50_ms']} ms p99 {levels[-1]['p99_ms']} ms")
    result = {"ledger_entries": head["history_seq"], "levels": levels, "analysis": concurrency_note([l for l in levels if l["callers"] > 0])}
    log(json.dumps(result["analysis"]["notes"], indent=1))
    if out_dir:
        (out_dir / "concurrency.json").write_text(json.dumps(result, indent=2))
    return chaos.EXIT_OK


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="soak.py", description="Soak a THROWAWAY stack: leak or cache, and retrieval against ledger size")
    ap.add_argument("--stack-dir", default=os.environ.get("JARVIS_CHAOS_DIR") or str(Path(os.environ.get("TMPDIR", "/tmp")) / "jarvis-chaos100x"))
    ap.add_argument("--target")
    ap.add_argument("--out")
    ap.add_argument("--growth-min", type=float, default=40)
    ap.add_argument("--reads-min", type=float, default=15)
    ap.add_argument("--idle-min", type=float, default=5)
    ap.add_argument("--control-min", type=float, default=8)
    ap.add_argument("--tick-s", type=float, default=15)
    ap.add_argument("--records-per-tick", type=int, default=40)
    ap.add_argument("--sample-s", type=float, default=90)
    ap.add_argument("--max-records", type=int, default=40000, help="the ledger is bounded: growth stops here")
    ap.add_argument("--concurrency", action="store_true", help="run only the concurrency phase on the existing ledger (restarts the application container)")
    ap.add_argument("--concurrency-seconds", type=float, default=45)
    ap.add_argument("--i-know-this-is-live", action="store_true", dest="allow_live", help="never used")
    args = ap.parse_args(argv)

    try:
        stack = chaos.load_stack(args.stack_dir)
        target = chaos.assess_target(args.target or stack["url"], chaos.fetch_ready, allow_live=args.allow_live)
        blocked = chaos.throwaway_proof(stack, target)
        if blocked:
            raise chaos.Refusal(f"not a proven throwaway: {blocked}")
    except chaos.Refusal as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return chaos.EXIT_REFUSED
    key_file = Path(stack["secrets_dir"], "api-key")
    if not key_file.is_file():
        print("REFUSED: no throwaway API key file", file=sys.stderr)
        return chaos.EXIT_REFUSED
    stats = chaos.Stats()
    client = chaos.Client(target["url"], key_file.read_text().strip(), stats)
    out_dir = Path(args.out) if args.out else None
    if out_dir:
        out_dir.mkdir(parents=True, exist_ok=True)
    log_file = open(out_dir / "soak.log", "w") if out_dir else None

    def log(line: str) -> None:
        print(line, flush=True)
        if log_file:
            log_file.write(line + "\n")
            log_file.flush()

    if args.concurrency:
        try:
            return run_concurrency(args, stack, target, client, stats, out_dir)
        except SoakError as exc:
            print(f"ABORTED: {exc}", file=sys.stderr)
            return chaos.EXIT_PROBE_FAILURES
    tag = os.urandom(3).hex()
    t0 = time.time()
    samples: dict[str, list[dict]] = {"growth": [], "reads": [], "idle": [], "control": []}
    failures = Failures()
    first = client.get("/api/jarvis/blocks/head")
    if first.status != 200:
        print(f"ABORTED: the ledger's blocks/head answered {first.status} before the soak began", file=sys.stderr)
        return chaos.EXIT_PROBE_FAILURES
    head = first.json
    base_records = head["history_seq"]
    try:
        records = 0
        log(f"soak: target {target['url']} stack {target['stack']}; growth {args.growth_min:g} min, reads {args.reads_min:g}, idle {args.idle_min:g}, control {args.control_min:g}; "
            f"{args.records_per_tick} records every {args.tick_s:g} s, a sample every {args.sample_s:g} s; ledger starts at {base_records} history entries")

        def take(phase: str, requests: int) -> None:
            s = sample(client, stack, phase, t0, requests, base_records + records, failures)
            samples[phase].append(s)
            log(f"[{s['t']:7.0f}s] {phase:<7} entries {s['records']:>6} reqs {s['requests']:>7} anon {s.get('RssAnon', 0) / 1024:7.1f} MiB rss {s.get('VmRSS', 0) / 1024:7.1f} MiB"
                + (f" retrieve {s['retrieve_typical_ms']:7.0f} ms" if "retrieve_typical_ms" in s else "")
                + (f" hostile {s['retrieve_hostile_ms']:7.0f} ms verify b/h {s['blocks_verify_ms']:6.0f}/{s['history_verify_ms']:6.0f} ms db {s['db_mb']} MB" if "retrieve_hostile_ms" in s else ""))

        # --- growth ---
        take("growth", 0)
        end = time.time() + args.growth_min * 60
        next_sample = time.time() + args.sample_s
        last_id = None
        while time.time() < end and base_records + records < args.max_records:
            tick = time.time()
            for i in range(args.records_per_tick):
                r = client.post("/api/jarvis/memory", {"content": f"chaos100x soak {tag} {records} {i}", "source_agent": "soak", "session_id": f"soak-{tag}", "type": "decision",
                                                       "evidence": [{"kind": "user-request", "ref": "chaos100x:soak"}]}, timeout=60)
                if r.status == 200:
                    records += 1
                    last_id = r.json["memory"]["id"]
                else:
                    failures.note("growth write", r.status)
            sealed = client.post("/api/jarvis/blocks/seal", {"force": True, "min_entries": 1, "max_entries": 200}, timeout=120)
            if sealed.status != 200:
                failures.note("seal", sealed.status)
            if time.time() >= next_sample:
                take("growth", stats.requests)
                next_sample = time.time() + args.sample_s
            time.sleep(max(0.0, args.tick_s - (time.time() - tick)))
        take("growth", stats.requests)
        log(f"growth done: {records} records written, {stats.requests} requests, ledger at {base_records + records} entries")

        # --- reads on a constant ledger ---
        def hold(phase: str, minutes: float, load: Load | None) -> None:
            end_ = time.time() + minutes * 60
            while time.time() < end_:
                time.sleep(min(30.0, max(0.0, end_ - time.time())))
                take(phase, load.count if load else 0)

        load = Load(client, last_id or "mem-none").start()
        take("reads", 0)
        hold("reads", args.reads_min, load)
        load.stop()
        log(f"reads done: {load.count} requests, {load.errors} not answered with 200")
        if load.errors:
            failures.note("read load", f"{load.errors} of {load.count} not 200")

        # --- idle ---
        hold("idle", args.idle_min, None)

        # --- control: restart the application, same ledger, same load ---
        log("control: restarting the application container")
        why = restart_and_wait(stack, target)
        if why:
            log(f"ABORTED: the control needs a restarted process and did not get one: {why}")
            if out_dir:
                (out_dir / "soak.json").write_text(json.dumps({"aborted": why, "seconds": round(time.time() - t0), "samples": samples}, indent=2))
            return chaos.EXIT_PROBE_FAILURES
        load2 = Load(client, last_id or "mem-none").start()
        take("control", 0)
        hold("control", args.control_min, load2)
        load2.stop()
        if load2.errors:
            failures.note("control read load", f"{load2.errors} of {load2.count} not 200")

        result = {"args": vars(args), "seconds": round(time.time() - t0), "records_written": records, "ledger_entries": base_records + records, "samples": samples,
                  "verdict": verdict(samples["growth"], samples["reads"], samples["idle"], samples["control"]), "http_statuses": {str(k): v for k, v in sorted(stats.statuses.items())},
                  "five_xx": len(stats.five_xx), "requests": stats.requests}
        if failures:
            result["invalid"] = failures.summary()
            log(f"INVALID RUN: {failures.summary()['count']} request(s) the soak needed to succeed did not: {failures.summary()['by_kind']}; the verdict below rests on a partial experiment")
        log(json.dumps(result["verdict"], indent=1))
        if out_dir:
            (out_dir / "soak.json").write_text(json.dumps(result, indent=2))
        return chaos.EXIT_PROBE_FAILURES if failures else chaos.EXIT_OK

    except SoakError as exc:
        log(f"ABORTED: {exc}")
        if out_dir:
            (out_dir / "soak.json").write_text(json.dumps({"aborted": str(exc), "seconds": round(time.time() - t0), "samples": samples, "invalid": failures.summary() if failures else None}, indent=2))
        return chaos.EXIT_PROBE_FAILURES

if __name__ == "__main__":
    raise SystemExit(main())
