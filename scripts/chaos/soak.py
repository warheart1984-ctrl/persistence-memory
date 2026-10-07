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


def timed(fn) -> float:
    t0 = time.perf_counter()
    fn()
    return (time.perf_counter() - t0) * 1000


def median_ms(fn, n: int = 3) -> float:
    return statistics.median(timed(fn) for _ in range(n))


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

def sample(client, stack, phase: str, t0: float, requests: int, records: int) -> dict[str, Any]:
    mem = process_memory(stack["containers"]["app"])
    s = {"t": round(time.time() - t0, 1), "phase": phase, "requests": requests, "records": records, **mem}
    if phase in ("growth", "reads", "control"):
        s["retrieve_typical_ms"] = round(median_ms(lambda: client.get(f"/api/jarvis/memory/retrieve?query={TYPICAL}&limit=50", timeout=120)), 1)
    if phase == "growth":
        s["retrieve_hostile_ms"] = round(median_ms(lambda: client.get(f"/api/jarvis/memory/retrieve?query={urllib.parse.quote(HOSTILE)}&limit=5", timeout=120)), 1)
        s["blocks_verify_ms"] = round(timed(lambda: client.get("/api/jarvis/blocks/verify", timeout=300)), 1)
        s["history_verify_ms"] = round(timed(lambda: client.get("/api/jarvis/memory/history/verify", timeout=300)), 1)
        s["db_mb"] = database_mb(stack)
    return s


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

    tag = os.urandom(3).hex()
    t0 = time.time()
    samples: dict[str, list[dict]] = {"growth": [], "reads": [], "idle": [], "control": []}
    head = client.get("/api/jarvis/blocks/head").json
    base_records = head["history_seq"]
    records = 0
    log(f"soak: target {target['url']} stack {target['stack']}; growth {args.growth_min:g} min, reads {args.reads_min:g}, idle {args.idle_min:g}, control {args.control_min:g}; "
        f"{args.records_per_tick} records every {args.tick_s:g} s, a sample every {args.sample_s:g} s; ledger starts at {base_records} history entries")

    def take(phase: str, requests: int) -> None:
        s = sample(client, stack, phase, t0, requests, base_records + records)
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
        client.post("/api/jarvis/blocks/seal", {"force": True, "min_entries": 1, "max_entries": 200}, timeout=120)
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

    # --- idle ---
    hold("idle", args.idle_min, None)

    # --- control: restart the application, same ledger, same load ---
    log("control: restarting the application container")
    run = chaos.run_cmd(["docker", "restart", stack["containers"]["app"]], timeout=120)
    if run.returncode != 0:
        log(f"could not restart the application: {run.stderr[:100]}")
    for _ in range(120):
        if chaos.fetch_ready(target["url"])[0] == 200:
            break
        time.sleep(1)
    load2 = Load(client, last_id or "mem-none").start()
    take("control", 0)
    hold("control", args.control_min, load2)
    load2.stop()

    result = {"args": vars(args), "seconds": round(time.time() - t0), "records_written": records, "ledger_entries": base_records + records, "samples": samples,
              "verdict": verdict(samples["growth"], samples["reads"], samples["idle"], samples["control"]), "http_statuses": {str(k): v for k, v in sorted(stats.statuses.items())},
              "five_xx": len(stats.five_xx), "requests": stats.requests}
    log(json.dumps(result["verdict"], indent=1))
    if out_dir:
        (out_dir / "soak.json").write_text(json.dumps(result, indent=2))
    return chaos.EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
