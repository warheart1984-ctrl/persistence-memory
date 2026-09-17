"""Benchmark graph construction: python scripts/benchmark-graph.py 1000 10000 100000"""
from __future__ import annotations
import sys, time, tracemalloc
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.graph import _build_graph

class Memory:
    def __init__(self, i: int): self.id=f"mem-{i}"; self.confidence=.9; self.created_at="2026-01-01T00:00:00+00:00"; self.subject=f"subject-{i%100}"; self.tags=[f"tag-{i%50}"]; self.supersedes=f"mem-{i-1}" if i else None
    def model_dump(self): return {"id": self.id}

for size in [int(x) for x in sys.argv[1:] or (1000, 10000, 100000)]:
    data=[Memory(i) for i in range(size)]; tracemalloc.start(); started=time.perf_counter(); graph=_build_graph(data); elapsed=time.perf_counter()-started; _,peak=tracemalloc.get_traced_memory(); tracemalloc.stop()
    print(f"{size:>8} nodes: {elapsed:8.3f}s, {peak/1024/1024:8.1f} MiB peak, {graph.number_of_edges():>10} directed edges")
