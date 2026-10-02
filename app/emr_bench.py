"""Labelled EMR retrieval benchmark, scored by the emr_eval harness.

The corpus (tests/fixtures/emr_bench/memories.jsonl) gives each memory an
`age_hours` instead of a timestamp; the runner stamps it relative to now so
decay sees the same ages on every run. Retrieval is scored through
`emr_recall`, the same governed path the LLM generate route and the MCP
tools use. The cases (cases.jsonl) are human labels in emr_eval's format,
and the case_id prefix names the category:

    kw     keyword query                  nq     natural-language question
    morph  different word forms           para   paraphrase, no shared key terms
    neg    unrelated (must abstain)       near   adjacent but unstored (must abstain)

Run:
    python -m app.emr_bench [--json-out bench.json] [--show-failures] [--embeddings]

--embeddings scores with emr_embed switched on (needs the `embed` extra). The
model is cached in JARVIS_EMR_EMBED_MODEL_DIR (default ~/.cache/emr-embed-model)
and memory vectors in a scratch file per run.
"""

from __future__ import annotations

import argparse
import json
import statistics
import tempfile
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import os

import app.emr as emr
import app.emr_embed as emr_embed
from app.emr_eval import run_evaluation
from app.emr_tool import EmrRecallRequest, emr_recall
from app.store import JarvisStore

BENCH_DIR = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "emr_bench"
CATEGORIES = ("kw", "nq", "morph", "para", "neg", "near")
AGE_BUCKETS = ((24, "<1d"), (24 * 7, "1-7d"), (24 * 30, "1-4w"), (float("inf"), ">1mo"))


def _age_bucket(hours: float) -> str:
    return next(label for limit, label in AGE_BUCKETS if hours < limit)


def materialize_ledger(memories_path: Path, out_path: Path, now: datetime) -> dict[str, float]:
    """Write a ledger file with timestamps `age_hours` before `now`; return ages by id."""
    ages: dict[str, float] = {}
    memories = []
    for line in memories_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        age = float(row.pop("age_hours"))
        ages[row["id"]] = age
        stamp = (now - timedelta(hours=age)).isoformat()
        memories.append({
            **row,
            "created_at": stamp,
            "updated_at": stamp,
            "source_agent": "emr-bench",
            "session_id": f"bench-{row['id']}",
            "evidence": [],
        })
    out_path.write_text(json.dumps({"memories": memories}), encoding="utf-8")
    return ages


def _rate(values: list[float]) -> float | None:
    return round(statistics.fmean(values), 4) if values else None


def summarize(per_case: list[dict[str, Any]], labels: dict[str, dict], ages: dict[str, float]) -> dict:
    """Per-category and per-age-bucket scores from emr_eval's per_case rows."""
    by_cat: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    by_age: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    for row in per_case:
        cat = row["case_id"].split("-")[0]
        bucket = by_cat[cat]
        if row.get("expected_empty"):
            bucket["false_positive"].append(float(row["false_positive"]))
            bucket["abstained"].append(float(row["abstained"]))
            continue
        hit = float(row["recall_at_k"] > 0)
        bucket["hit_at_k"].append(hit)
        bucket["top1"].append(float(row["top1_hit"]))
        bucket["rr"].append(row["reciprocal_rank"])
        bucket["abstained"].append(float(row["abstained"]))
        age = min(ages[mid] for mid in labels[row["case_id"]]["relevant_ids"])
        by_age[_age_bucket(age)]["hit_at_k"].append(hit)
        by_age[_age_bucket(age)]["abstained"].append(float(row["abstained"]))

    def fold(groups: dict[str, dict[str, list[float]]], order) -> dict:
        return {
            name: {"cases": max(len(v) for v in groups[name].values()),
                   **{metric: _rate(v) for metric, v in groups[name].items()}}
            for name in order if name in groups
        }

    positives = [r for r in per_case if not r.get("expected_empty")]
    negatives = [r for r in per_case if r.get("expected_empty")]
    return {
        "overall": {
            "positive_cases": len(positives),
            "hit_at_k": _rate([float(r["recall_at_k"] > 0) for r in positives]),
            "top1": _rate([float(r["top1_hit"]) for r in positives]),
            "mrr": _rate([r["reciprocal_rank"] for r in positives]),
            "wrongly_abstained": _rate([float(r["abstained"]) for r in positives]),
            "negative_cases": len(negatives),
            "negative_false_positive": _rate([float(r["false_positive"]) for r in negatives]),
        },
        "by_category": fold(by_cat, CATEGORIES),
        "by_age": fold(by_age, [label for _, label in AGE_BUCKETS]),
    }


def _recall_rows(store: JarvisStore, labels: dict[str, dict], k: int) -> list[dict[str, Any]]:
    """Score every case through emr_recall; rows mirror emr_eval's per_case."""
    by_id = {rec.id: rec for rec in store.list_memories(limit=9999)}
    rows = []
    for case_id, label in labels.items():
        result = emr_recall(store, EmrRecallRequest(
            intent="chat", query=label["query"], max_memories=k,
            session_key=f"emr-bench-{case_id}", include_provenance=False,
        ))
        emr.clear_stm(f"emr-bench-{case_id}")
        returned = [item.memory_id for item in result.bundle][:k]
        row = {"case_id": case_id, "query": label["query"], "returned": returned,
               "abstained": result.abstained, "abstention_reason": result.abstention_reason}
        if label.get("expected_empty"):
            row.update(expected_empty=True, false_positive=bool(returned))
        else:
            # Same content hash counts as the same memory, as in emr_eval.
            want = {by_id[mid].content_sha256 for mid in label["relevant_ids"]}
            got = [by_id[mid].content_sha256 for mid in returned]
            first = next((i for i, h in enumerate(got, start=1) if h in want), 0)
            row.update(recall_at_k=len(want & set(got)) / len(want), top1_hit=first == 1,
                       reciprocal_rank=1.0 / first if first else 0.0)
        rows.append(row)
    return rows


def run_bench(
    bench_dir: Path = BENCH_DIR, k: int = 5, safety: bool = True, embeddings: bool = False
) -> dict[str, Any]:
    cases_path = bench_dir / "cases.jsonl"
    labels = {
        row["case_id"]: row
        for row in (json.loads(ln) for ln in cases_path.read_text(encoding="utf-8").splitlines() if ln.strip())
    }
    with tempfile.TemporaryDirectory(prefix="emr-bench-") as tmp:
        ledger = Path(tmp) / "ledger.json"
        dynamics = Path(tmp) / "dynamics.json"
        # Point EMR at scratch files so a benchmark run can never touch a
        # real dynamics sidecar.
        original = emr.DYNAMICS_PATH
        emr.DYNAMICS_PATH, emr._dynamics_loaded = str(dynamics), False
        saved_env = {k: os.environ.get(k) for k in (
            "JARVIS_EMR_EMBEDDINGS", "JARVIS_EMR_EMBED_CACHE", "JARVIS_EMR_EMBED_MODEL_DIR")}
        os.environ["JARVIS_EMR_EMBEDDINGS"] = "1" if embeddings else "0"
        os.environ["JARVIS_EMR_EMBED_CACHE"] = str(Path(tmp) / "vectors.json")
        os.environ.setdefault(
            "JARVIS_EMR_EMBED_MODEL_DIR", str(Path.home() / ".cache" / "emr-embed-model"))
        try:
            ages = materialize_ledger(bench_dir / "memories.jsonl", ledger, datetime.now(timezone.utc))
            per_case = _recall_rows(JarvisStore(str(ledger)), labels, k)
            report = run_evaluation(
                ledger_path=ledger, rag_log_path=None, dynamics_path=dynamics,
                human_labels_path=cases_path, k=k, max_cases=len(labels),
            ) if safety else None
        finally:
            emr.DYNAMICS_PATH, emr._dynamics_loaded = original, False
            for key, value in saved_env.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
    if embeddings and emr_embed._model is None:
        raise RuntimeError("--embeddings: the embedding model did not load (pip install '.[embed]')")
    return {
        "k": k,
        "embeddings": embeddings,
        "summary": summarize(per_case, labels, ages),
        # emr_eval's contradiction, reinforcement and graph safety gates.
        "safety_status": report["safety_status"] if report else "skipped",
        "safety_gates": report["safety_gates"] if report else {},
        "per_case": per_case,
    }


def _print_table(result: dict[str, Any]) -> None:
    o = result["summary"]["overall"]
    mode = "lexical + embeddings" if result["embeddings"] else "lexical"
    print(f"EMR benchmark ({mode}, k={result['k']}, safety={result['safety_status']})")
    print(f"  answerable: {o['positive_cases']} cases  hit@k {o['hit_at_k']}  top1 {o['top1']}  "
          f"mrr {o['mrr']}  wrongly abstained {o['wrongly_abstained']}")
    print(f"  must abstain: {o['negative_cases']} cases  false positives {o['negative_false_positive']}")
    print("  by category:")
    for name, m in result["summary"]["by_category"].items():
        if "hit_at_k" in m:
            print(f"    {name:6} n={m['cases']:3}  hit@k {m['hit_at_k']}  top1 {m['top1']}  abstained {m['abstained']}")
        else:
            print(f"    {name:6} n={m['cases']:3}  false positives {m['false_positive']}  abstained {m['abstained']}")
    print("  answerable, by age of the right memory:")
    for name, m in result["summary"]["by_age"].items():
        print(f"    {name:6} n={m['cases']:3}  hit@k {m['hit_at_k']}  abstained {m['abstained']}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Labelled EMR retrieval benchmark")
    parser.add_argument("--k", type=int, default=5)
    parser.add_argument("--json-out")
    parser.add_argument("--show-failures", action="store_true")
    parser.add_argument("--embeddings", action="store_true")
    args = parser.parse_args(argv)
    result = run_bench(k=args.k, embeddings=args.embeddings)
    _print_table(result)
    if args.show_failures:
        print("  failures:")
        for row in result["per_case"]:
            failed = row["false_positive"] if row.get("expected_empty") else row["recall_at_k"] == 0
            if failed:
                why = row.get("abstention_reason") or ("returned " + ",".join(row["returned"][:3]))
                print(f"    {row['case_id']:9} {why:28} {row['query']}")
    if args.json_out:
        Path(args.json_out).write_text(json.dumps(result, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
