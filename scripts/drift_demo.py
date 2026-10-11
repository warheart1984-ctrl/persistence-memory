#!/usr/bin/env python3
"""Demo: run the drift watcher on last night's real agent summary against the real live records.  Read-only.

It reads the two records over the API with GET only (through the same write-refusing client the relay report uses), builds a read-only
in-memory store from them, and runs ``app.drift.run_check`` on it.  It does not need the drift endpoint (which is not deployed), it
writes nothing to the ledger and nothing to any log, and it never prints the API key.

    python scripts/drift_demo.py --key-file ~/.jarvis-api-key [--base-url http://127.0.0.1:8011]

The summary below is Devin's, as pasted by the operator on 2026-10-10.  Two claims in it were wrong: it credited Codex with a deploy
that Claude did, and it said the signature machinery was probably live when signing is shelved.  It is run against Codex's record, against
Claude's superseding record, and against both: the verdicts differ, which is why the supersede link matters.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import drift  # noqa: E402

OLD_ID = "mem-1dc144c193a1"  # written by Codex
NEW_ID = "mem-c42330528ad5"  # written by Claude; supersedes the old one

DEVIN_SUMMARY = (
    "And the record's content is notable: the live ledger went to schema v9 today — Codex deployed main@792e99b, migrating 7→9, "
    "with a successful restore drill and both pre/post-deploy backups verified offsite. "
    "The README's \"schema v6, signatures shelved\" line is now stale — the signature machinery is likely live on the box. "
    "Status draft: emr_latest listed it as active, but the fetched record's real status is draft."
)


def _load_relay():
    spec = importlib.util.spec_from_file_location("relay_evidence", ROOT / "scripts" / "relay_evidence.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class ReadOnlyStore:
    """Just enough of the store interface for drift.load_records, built from records read over GET."""

    def __init__(self, records: dict[str, dict]):
        self._records = records

    def get_memory(self, memory_id: str):
        data = self._records.get(memory_id)
        return SimpleNamespace(model_dump=lambda d=data: dict(d)) if data else None

    def list_memories(self, limit: int = 0):
        return [SimpleNamespace(id=r["id"], supersedes=r.get("supersedes"), created_at=r["created_at"]) for r in self._records.values()]


def fetch_records(base_url: str, key: str) -> dict[str, dict]:
    rel = _load_relay()
    client = rel.ReadOnlyClient(base_url, key)
    out = {}
    for rid in (OLD_ID, NEW_ID):
        status, body = client.get(f"/api/jarvis/memory/{rid}")
        if status != 200 or not isinstance(body, dict) or "memory" not in body:
            raise SystemExit(f"could not read {rid}: HTTP {status}")
        out[rid] = body["memory"]
    return out


def show(title: str, result: dict) -> None:
    print(f"\n=== {title}")
    print(f"verdict: {result['verdict'].upper()}   counts: {result['counts']}   check_digest: {result['check_digest'][:16]}…")
    for c in result["claims"]:
        mark = {"supported": "ok  ", "contradicted": "DRIFT", "unsupported": "????"}[c["status"]]
        hedge = " (hedged)" if c["hedged"] else ""
        print(f"  [{mark}] {c['reason']}{hedge}: {c['text']}")
        for f in c["findings"][:2]:
            if f["status"] != "supported" and f.get("detail"):
                print(f"           -> {f['detail']}")
                for e in f.get("evidence", [])[:2]:
                    print(f"              evidence: {e['record']}.{e['field']} = {str(e['value'])[:90]!r}")
    for s in result["skipped"]:
        print(f"  [skip] {s['reason']}: {s['text']}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--base-url", default="http://127.0.0.1:8011")
    ap.add_argument("--key-file")
    args = ap.parse_args(argv)
    path = args.key_file or os.environ.get("JARVIS_API_KEY_FILE")
    if not path:
        raise SystemExit("no key file: pass --key-file or set JARVIS_API_KEY_FILE")
    key = Path(path).expanduser().read_text(encoding="utf-8").strip()
    store = ReadOnlyStore(fetch_records(args.base_url, key))
    print("Devin's summary, as pasted on 2026-10-10:\n")
    print("  " + DEVIN_SUMMARY)
    results = {}
    for title, ids in ((f"against Codex's record only ({OLD_ID})", [OLD_ID]), (f"against Claude's superseding record only ({NEW_ID})", [NEW_ID]),
                       ("against both records (Claude's supersedes Codex's)", [OLD_ID, NEW_ID])):
        req = drift.validate_request({"summary": DEVIN_SUMMARY, "records": ids})
        results[title] = drift.run_check(req, store)
        show(title, results[title])
    codex_only, claude_only, both = list(results.values())
    print("\nSummary of verdicts:")
    for title, r in results.items():
        print(f"  {r['verdict'].upper():<10} {title}")
    actor = lambda r: next((c["reason"] for c in r["claims"] if "Codex deployed" in c["text"]), None)  # noqa: E731
    print(f"\nThe miscredited-deploy claim: {actor(codex_only)} against Codex's record, {actor(claude_only)} against Claude's, {actor(both)} against both.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
