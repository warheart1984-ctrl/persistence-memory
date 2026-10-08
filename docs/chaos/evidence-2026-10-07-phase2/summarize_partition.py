#!/usr/bin/env python3
"""Summarize the two raw outputs of partition_probe.py (before / after the connection bounds) into the comparison the report quotes.

    python3 summarize_partition.py partition-before-old-pg_store.txt partition-after-this-branch.txt
"""
import ast
import sys
from collections import Counter


def summarize(path):
    cut = healed = None
    rows = []
    for line in open(path):
        if line.startswith("cut at"):
            parts = line.split()
            cut, healed = float(parts[2]), float(parts[5])
        elif line.startswith("("):
            rows.append(ast.literal_eval(line))
    during = [r for r in rows if cut is not None and cut <= r[1] <= healed]            # requests begun while the database was cut off
    caught = [r for r in during if r[1] <= cut + 0.5]                                  # begun in the first half second: they held a pooled connection
    longest = max((r[2] - r[1] for r in during), default=0.0)
    return {"file": path.split("/")[-1], "cut_at": cut, "healed_at": healed, "requests_begun_during_the_partition": len(during),
            "statuses": dict(Counter(r[3] for r in during)), "longest_request_s": round(longest, 1),
            "requests_caught_in_flight": [(r[0], r[1], r[2], r[3], round(r[2] - r[1], 1)) for r in caught],
            "requests_answered_before_the_link_healed": sum(1 for r in during if r[2] < healed)}


for p in sys.argv[1:]:
    s = summarize(p)
    print(f"== {s['file']}\n  database cut off at {s['cut_at']} s, reconnected at {s['healed_at']} s\n  {s['requests_begun_during_the_partition']} requests begun during the partition: {s['statuses']}"
          f"\n  answered before the link healed: {s['requests_answered_before_the_link_healed']}\n  longest request: {s['longest_request_s']} s")
    for r in s["requests_caught_in_flight"]:
        print(f"  caught in flight: {r[0]:<5} began {r[1]} s, answered {r[2]} s with {r[3]}  ({r[4]} s)")
