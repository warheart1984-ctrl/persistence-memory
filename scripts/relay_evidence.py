#!/usr/bin/env python3
"""Read-only evidence report for the four-agent relay test.

Re-runs a fixed set of checks against a live ledger and prints PASS or FAIL for each, then writes a JSON and a Markdown
report.  It never writes to the ledger: every request goes through ``ReadOnlyClient``, which refuses (before any network
call) anything that is not on a short allow-list of GET reads and the three read-only tool POSTs.  The API key is read
from a file, sent only in a request header, and never printed, logged or put in a report.

    python scripts/relay_evidence.py --key-file ~/.jarvis-api-key \
        --inputs docs/evidence/relay-test-2026-10-10.inputs.json \
        --json-out docs/evidence/RELAY-TEST-2026-10-10.json --md-out docs/evidence/RELAY-TEST-2026-10-10.md

The numbers it checks are point-in-time: "the newest record is X" is true only until the next write, so every report
records the history seq it was read at.  Anything the script did not itself read (what agents say they saw) goes in the
"reported by agent" section and is never counted as verified.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

CLIENT_NAME = "relay-evidence/1"
EMPTY_ARGS_SHA256 = hashlib.sha256(b"{}").hexdigest()  # the call log's args_sha256 for a call made with no arguments
MAX_CALL_PAGES = 50  # 200 entries per page: up to 10,000 log entries are read; past that the report says it is a partial view
DEFAULT_BASE = "http://127.0.0.1:8011"
DEFAULT_OLD_ID = "mem-1dc144c193a1"  # written by Codex
DEFAULT_NEW_ID = "mem-c42330528ad5"  # written by Claude; supersedes the old one
DEFAULT_RECEIPT = "eo:sha256:3f0d9fb25b8e82eaa4cfd6ee7e185cdf97c0a68dc3c8cef602041996ebf04a66"

# ---------------------------------------------------------------------------------------------------- the write guard

_ID = r"[A-Za-z0-9:_\-]+"
ALLOWED: list[tuple[str, re.Pattern[str]]] = [
    ("GET", re.compile(rf"^/api/jarvis/memory/mem-[0-9a-f]+$")),
    ("GET", re.compile(rf"^/api/jarvis/memory/mem-[0-9a-f]+/history$")),
    ("GET", re.compile(r"^/api/jarvis/memory/history/verify$")),
    ("GET", re.compile(r"^/api/jarvis/memory/latest$")),
    ("GET", re.compile(r"^/api/jarvis/blocks/(verify|head)$")),
    ("GET", re.compile(r"^/api/jarvis/replay/(state|events)$")),
    ("GET", re.compile(rf"^/api/jarvis/replay/receipts/{_ID}/verify$")),
    ("POST", re.compile(r"^/api/jarvis/tools/(emr_latest|emr_fetch|emr_search_ledger)$")),
    ("GET", re.compile(r"^/api/jarvis/tools/calls(/verify)?$")),  # the server-side call log: operator-only reads
]


class WriteRefused(Exception):
    """Raised before any network call when a request is not on the read-only allow-list."""


def is_allowed(method: str, path: str) -> bool:
    return any(method.upper() == m and rx.match(path) for m, rx in ALLOWED)


Transport = Callable[[str, str, dict[str, str], bytes | None], tuple[int, bytes]]


def _urllib_transport(method: str, url: str, headers: dict[str, str], body: bytes | None) -> tuple[int, bytes]:
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:  # noqa: S310 - the base URL is operator-supplied
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


class ReadOnlyClient:
    def __init__(self, base_url: str, key: str, transport: Transport | None = None):
        self._base = base_url.rstrip("/")
        self._key = key
        self._transport = transport or _urllib_transport
        self.calls: list[dict[str, Any]] = []  # method, path, status: never headers, never the key

    def request(self, method: str, path: str, *, query: dict[str, Any] | None = None, body: dict[str, Any] | None = None) -> tuple[int, Any]:
        method = method.upper()
        if not is_allowed(method, path):
            raise WriteRefused(f"refused: {method} {path} is not a read-only call")
        url = self._base + path + (("?" + urllib.parse.urlencode(query)) if query else "")
        # Self-reported, so the server's call log shows these reads as this report's own and not as an anonymous Python client.
        headers = {"X-API-Key": self._key, "Accept": "application/json", "X-Jarvis-MCP-Client": CLIENT_NAME}
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        status, raw = self._transport(method, url, headers, data)
        self.calls.append({"method": method, "path": path, "query": query or None, "status": status})
        try:
            return status, json.loads(raw.decode("utf-8")) if raw else None
        except ValueError:
            return status, {"_non_json_body_bytes": len(raw)}

    def get(self, path: str, **query: Any) -> tuple[int, Any]:
        return self.request("GET", path, query=query or None)

    def tool(self, name: str, arguments: dict[str, Any] | None = None) -> tuple[int, Any]:
        return self.request("POST", f"/api/jarvis/tools/{name}", body=arguments or {})


# --------------------------------------------------------------------------------------------------------- digests


def derive_digest(records: list[dict[str, Any]]) -> tuple[str, str]:
    """Re-derive a result_digest from the returned records.  Returns (digest, formula).

    The server hashes the compact JSON (UTF-8, no ASCII escaping) of ``[[id, created_at, status], ...]`` in returned order;
    after the stored-status change it hashes ``[[id, created_at, status, lifecycle], ...]``.  The shape is chosen by whether
    the records carry ``lifecycle``.
    """
    with_lifecycle = bool(records) and all("lifecycle" in r for r in records)
    rows = [[r["id"], r["created_at"], r["status"], r["lifecycle"]] if with_lifecycle else [r["id"], r["created_at"], r["status"]] for r in records]
    blob = json.dumps(rows, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest(), ("[id, created_at, status, lifecycle]" if with_lifecycle else "[id, created_at, status]")


def reconstruct_prior_digest(default_list: list[dict[str, Any]], old: dict[str, Any], new_id: str, limit: int = 10) -> str:
    """The digest ``emr_latest`` (old response shape) returned at the seq before the superseding record was written.

    At that seq the newest record was ``old`` (still active), ``new_id`` did not exist, and every listed record was active.
    So: the current default list, without ``new_id``, plus ``old``, newest first, first ``limit``, all status "active".
    This is a reconstruction from the current ledger, not a historical read, and is reported as such.
    """
    rows = {r["id"]: r for r in default_list if r["id"] != new_id}
    rows[old["id"]] = old
    ordered = sorted(rows.values(), key=lambda r: (r["created_at"], r["id"]), reverse=True)[:limit]
    blob = json.dumps([[r["id"], r["created_at"], "active"] for r in ordered], separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------------------------------------- the checks


def _result(name: str, ok: bool | None, detail: str) -> dict[str, Any]:
    return {"check": name, "result": "PASS" if ok else ("FAIL" if ok is False else "NOT RUN"), "detail": detail}


def fetch_all_calls(client: ReadOnlyClient, **filters: Any) -> tuple[int, Any]:
    """GET /api/jarvis/tools/calls, following next_cursor page by page.

    Returns (status, body).  A failure on the first page is returned as it came (the call log may simply not be there).  Otherwise body is
    ``{"entries": [...all pages, newest first...], "pages": n, "truncated": bool}``; ``truncated`` is True if MAX_CALL_PAGES was reached with
    more pages still to read, or if a later page failed (then ``error`` says which), so the caller must call the result a partial view.
    """
    entries: list[dict[str, Any]] = []
    cursor: str | None = None
    pages = 0
    attempts = 0
    head_seq: int | None = None  # the log head the first page reported
    head_mismatch = False
    while pages < MAX_CALL_PAGES:
        query = {**filters, "limit": 200, **({"cursor": cursor} if cursor else {})}
        status, body = client.get("/api/jarvis/tools/calls", **query)
        if status != 200 or not isinstance(body, dict):
            if pages == 0:
                return status, body  # the very first page failed: report it as it came
            # a LATER page failed: keep what was read, but this is not the whole log, and the caller must say so
            return 200, {"entries": entries, "pages": pages, "truncated": True, "head_seq": head_seq, "head_mismatch": head_mismatch, "error": f"page {pages + 1} failed with HTTP {status}"}
        if pages == 0:
            head = body.get("head")
            head_seq = int(head["seq"]) if isinstance(head, dict) and isinstance(head.get("seq"), int) else None
            page = body.get("entries") or []
            newest = page[0].get("seq") if page and isinstance(page[0], dict) else None
            # The server builds the page and the head in two steps, so an entry written between them can leave the head ahead of the page.
            # For an unfiltered read the newest entry on the first page IS the head, so re-read until they agree (a few tries), then say so.
            if not filters and page and head_seq is not None and newest != head_seq:
                if attempts < 3:
                    attempts += 1
                    time.sleep(0.05)
                    continue
                head_mismatch = True
        entries += list(body.get("entries") or [])
        pages += 1
        cursor = body.get("next_cursor")
        if not cursor:
            break
    return 200, {"entries": entries, "pages": pages, "truncated": bool(cursor), "head_seq": head_seq, "head_mismatch": head_mismatch}


def gather(client: ReadOnlyClient, old_id: str, new_id: str, receipt_id: str) -> dict[str, Any]:
    """Every read the checks need.  Raw bodies are kept for the report (they never contain the key)."""
    raw: dict[str, Any] = {}

    def keep(label: str, status_body: tuple[int, Any]) -> Any:
        raw[label] = {"http_status": status_body[0], "body": status_body[1]}
        return status_body[1]

    keep("state", client.get("/api/jarvis/replay/state", limit=1))
    keep("record_old", client.get(f"/api/jarvis/memory/{old_id}"))
    keep("record_new", client.get(f"/api/jarvis/memory/{new_id}"))
    keep("history_old", client.get(f"/api/jarvis/memory/{old_id}/history"))
    keep("history_new", client.get(f"/api/jarvis/memory/{new_id}/history"))
    keep("events_135_136", client.get("/api/jarvis/replay/events", from_seq=135, limit=2))
    keep("history_verify", client.get("/api/jarvis/memory/history/verify"))
    keep("blocks_verify", client.get("/api/jarvis/blocks/verify"))
    keep("blocks_head", client.get("/api/jarvis/blocks/head"))
    keep("receipt_verify", client.get(f"/api/jarvis/replay/receipts/{receipt_id}/verify"))
    keep("latest_default", client.tool("emr_latest", {}))
    keep("latest_default_12", client.tool("emr_latest", {"limit": 12}))
    keep("latest_with_superseded", client.tool("emr_latest", {"include_superseded": True, "limit": 12}))
    all_status_body = fetch_all_calls(client)
    keep("calls_all", all_status_body)
    if all_status_body[0] != 200:  # only then is the narrower, filtered read worth making (it is the fallback, and is labelled as such)
        keep("calls_emr_latest", fetch_all_calls(client, tool="emr_latest"))
    keep("calls_verify", client.get("/api/jarvis/tools/calls/verify"))
    return raw


def call_log_section(raw: dict[str, Any], agents: list[dict[str, Any]]) -> dict[str, Any]:
    """What the SERVER says about each agent's emr_latest calls (client names are self-reported by the clients).

    ``available`` is False when the server has no call log (not deployed, switched off, or the key is not the operator's); nothing is
    then claimed about any agent.
    """
    all_raw = raw.get("calls_all") or {}
    all_ok = all_raw.get("http_status") == 200 and isinstance(all_raw.get("body"), dict)
    filt = raw.get("calls_emr_latest") or {}
    filt_ok = filt.get("http_status") == 200 and isinstance(filt.get("body"), dict)
    verify = (raw.get("calls_verify") or {}).get("body") or {}
    if not all_ok and not filt_ok:
        status = all_raw.get("http_status") if all_raw else filt.get("http_status")
        return {"available": False, "reason": f"GET /api/jarvis/tools/calls returned HTTP {status} (the call log is not deployed, is switched off, or this key may not read it)",
                "head": None, "chain_ok": None, "per_agent": [], "clients_seen": [], "coverage": None, "truncated": None, "pages_read": 0, "snapshot_head_seq": None, "newer_entries_not_included": 0}
    # ONE snapshot feeds both the coverage statement and the per-agent table: the unfiltered read, or (only if that failed) the filtered fallback.
    primary = all_raw["body"] if all_ok else filt["body"]
    entries = primary.get("entries") or []
    errors = [e for e in (primary.get("error"),) if e]
    truncated = bool(primary.get("truncated") or not all_ok)
    pages = int(primary.get("pages") or 1)
    n_all = len(entries)
    if not all_ok:
        coverage = (f"PARTIAL: the unfiltered read of the log failed (HTTP {all_raw.get('http_status')}), so the client-name list below is built only from the "
                    f"{len(entries)} emr_latest entries that were read; other clients and older names may be missing")
    elif truncated:
        why = ("; " + "; ".join(errors)) if errors else ""
        coverage = f"PARTIAL: only the newest {n_all} log entries ({pages} pages) were read; older entries, and so older client names, are not in this report{why}"
    else:
        coverage = f"complete: all {n_all} log entries ({pages} page(s)) were read"
    # the snapshot ends at the newest entry actually READ (the head the endpoint reported can run ahead of its own page)
    newest_read = entries[0].get("seq") if all_ok and entries and isinstance(entries[0], dict) and isinstance(entries[0].get("seq"), int) else None
    snapshot = newest_read if newest_read is not None else primary.get("head_seq")
    verify_head = ((verify.get("head") or {}).get("seq")) if isinstance(verify.get("head"), dict) else None
    newer = (verify_head - snapshot) if isinstance(verify_head, int) and isinstance(snapshot, int) and verify_head > snapshot else 0
    if newer:
        coverage += (f"; complete only THROUGH seq {snapshot} (the newest entry read): {newer} newer entr{'y was' if newer == 1 else 'ies were'} "
                     f"written while this report was being read, up to head seq {verify_head}, and are not included")
        coverage = coverage.replace("complete: all", "through seq %d: all" % snapshot, 1) if coverage.startswith("complete: all") else coverage
    per_agent = []
    matched_names: set[str] = set()
    for a in agents:
        name = str(a.get("agent") or "")
        needles = [n.lower() for n in [name, *[str(x) for x in (a.get("aliases") or [])]] if n]
        hits = [e for e in entries if e.get("tool") == "emr_latest" and any(n in str(e.get("client_name") or "").lower() for n in needles)]
        matched_names |= {str(e.get("client_name")) for e in hits}
        per_agent.append({
            "agent": name,
            "matched_on": needles,
            "witnessed_emr_latest_calls": len(hits),
            "latest": ({**{k: hits[0].get(k) for k in ("seq", "ts", "transport", "client_name", "client_version", "outcome", "result_digest", "tenant")},
                        "no_arguments": hits[0].get("args_sha256") == EMPTY_ARGS_SHA256} if hits else None),
            "note": "server-witnessed; the client name is self-reported" if hits else ("no emr_latest call from a client whose name contains any of " + ", ".join(repr(n) for n in needles) + " is in the part of the server's log that was read (it may have called under another name, before the log existed, or not at all: see the list of every client name the log saw)" + (" - and that view is PARTIAL" if truncated else "")),
        })
    seen: dict[tuple[str, str, str], dict[str, Any]] = {}
    for e in entries:
        if e.get("outcome") in ("gap", "denied_suppressed"):
            continue
        key = (str(e.get("client_name") or "(none)"), str(e.get("client_version") or ""), str(e.get("transport")))
        row = seen.setdefault(key, {"client_name": key[0], "client_version": key[1], "transport": key[2], "calls": 0, "tools": set(), "last_ts": e.get("ts")})
        row["calls"] += 1
        row["tools"].add(str(e.get("tool")))
        row["last_ts"] = max(str(row["last_ts"]), str(e.get("ts")))
    clients_seen = sorted(({**r, "tools": sorted(r["tools"]), "matched_an_agent": r["client_name"] in matched_names} for r in seen.values()), key=lambda r: (-r["calls"], r["client_name"]))
    return {"available": True, "reason": None, "head": verify.get("head"), "chain_ok": verify.get("ok"), "problems": verify.get("problems"), "per_agent": per_agent, "clients_seen": clients_seen, "coverage": coverage, "truncated": truncated, "pages_read": pages, "snapshot_head_seq": snapshot, "newer_entries_not_included": newer,
            "log_files": verify.get("files"), "entries_total": verify.get("entries")}


def run_checks(raw: dict[str, Any], old_id: str, new_id: str, expect_newest: str, reported_digest: str | None) -> list[dict[str, Any]]:
    def body(label: str) -> Any:
        return (raw.get(label) or {}).get("body")

    out: list[dict[str, Any]] = []
    rec_old = (body("record_old") or {}).get("memory") or {}
    rec_new = (body("record_new") or {}).get("memory") or {}
    out.append(_result("writer_of_old_record_is_codex", rec_old.get("source_agent") == "codex", f"{old_id} source_agent={rec_old.get('source_agent')!r}"))
    out.append(_result("writer_of_new_record_is_claude", rec_new.get("source_agent") == "claude", f"{new_id} source_agent={rec_new.get('source_agent')!r}"))

    latest = body("latest_default") or {}
    with_sup = body("latest_with_superseded") or {}
    sup_rows = {r["id"]: r for r in (with_sup.get("records") or [])}
    old_row = sup_rows.get(old_id) or {}
    superseded_flag = old_row.get("lifecycle") == "superseded" or (old_row.get("lifecycle") is None and old_row.get("status") == "superseded")
    link = rec_new.get("supersedes") == old_id and old_row.get("superseded_by") == new_id and superseded_flag
    out.append(_result("supersede_link_is_correct", link,
                       f"{new_id}.supersedes={rec_new.get('supersedes')!r}; {old_id}.superseded_by={old_row.get('superseded_by')!r}; old is superseded={superseded_flag}"))

    records = latest.get("records") or []
    newest = records[0]["id"] if records else None
    out.append(_result("newest_record_is_expected", newest == expect_newest, f"emr_latest newest={newest!r}, expected {expect_newest!r} (point in time)"))

    digest, formula = derive_digest(records) if records else ("", "")
    out.append(_result("digest_re_derives", bool(records) and digest == latest.get("result_digest"),
                       f"sha256 over {formula} of the {len(records)} returned records = {digest[:16]}…; server said {str(latest.get('result_digest'))[:16]}…"))

    hidden = old_id not in {r["id"] for r in records} and old_id in sup_rows
    out.append(_result("superseded_record_hidden_by_default", hidden, f"{old_id} in default list: {old_id in {r['id'] for r in records}}; in include_superseded list: {old_id in sup_rows}"))

    hv, bv = body("history_verify") or {}, body("blocks_verify") or {}
    out.append(_result("chain_verifies", bool(hv.get("ok")) and bool(bv.get("ok")), f"history ok={hv.get('ok')} problems={hv.get('problems')}; blocks ok={bv.get('ok')} problems={bv.get('problems')}"))

    rv = body("receipt_verify") or {}
    out.append(_result("old_receipt_re_derives", bool(rv.get("ok")), f"receipt verify ok={rv.get('ok')} problems={rv.get('problems')}"))

    events = (body("events_135_136") or {}).get("events") or []
    by_seq = {e.get("seq"): e for e in events}
    hist_ok = by_seq.get(135, {}).get("memory_id") == old_id and by_seq.get(136, {}).get("memory_id") == new_id
    out.append(_result("history_seq_135_136_are_the_two_records", hist_ok,
                       f"seq 135 -> {by_seq.get(135, {}).get('memory_id')!r} ({by_seq.get(135, {}).get('op')!r}); seq 136 -> {by_seq.get(136, {}).get('memory_id')!r} ({by_seq.get(136, {}).get('op')!r})"))

    if reported_digest:
        default_12 = (body("latest_default_12") or {}).get("records") or []
        old_for_recon = {"id": old_id, "created_at": old_row.get("created_at") or "", "status": "active"}
        recon = reconstruct_prior_digest(default_12, old_for_recon, new_id) if old_row.get("created_at") else ""
        out.append(_result("agent_digest_reproduces_from_seq_135_state", recon == reported_digest,
                           f"reconstructed {recon[:16]}… vs agents' reported {reported_digest[:16]}… (reconstruction from the current ledger, not a historical read)"))
    else:
        out.append(_result("agent_digest_reproduces_from_seq_135_state", None, "no agent-reported digest in the inputs file"))
    cv = body("calls_verify") or {}
    status = (raw.get("calls_verify") or {}).get("http_status")
    if status == 200:
        out.append(_result("call_log_chain_verifies", bool(cv.get("ok")), f"call log entries={cv.get('entries')} head seq={((cv.get('head') or {}).get('seq'))} problems={cv.get('problems')}"))
    else:
        out.append(_result("call_log_chain_verifies", None, f"the server has no readable call log (HTTP {status}): not deployed, switched off, or not the operator key"))
    return out


# ----------------------------------------------------------------------------------------------------------- report


def _esc(text: Any) -> str:
    return str(text).replace("|", "\\|")


def render_printout(checks: list[dict[str, Any]]) -> str:
    return "\n".join(f"{c['result']:<8} {c['check']}: {c['detail']}" for c in checks)


def build_evidence(raw: dict[str, Any], checks: list[dict[str, Any]], inputs: dict[str, Any], base_url: str, calls: list[dict[str, Any]], ids: dict[str, str]) -> dict[str, Any]:
    state = (raw.get("state") or {}).get("body") or {}
    latest = (raw.get("latest_default") or {}).get("body") or {}
    return {
        "report": inputs.get("title", "Relay test evidence"),
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": {"base_url": base_url, "script": "scripts/relay_evidence.py", "mode": "read-only (GET reads and read-only tool POSTs only)"},
        "ids": ids,
        "read_at": {
            "history_seq": state.get("history_seq"),
            "record_count": state.get("record_count"),
            "state_root": state.get("state_root"),
            "ledger_head": latest.get("ledger_head"),
            "result_digest_emr_latest_default": latest.get("result_digest"),
        },
        "summary": inputs.get("summary", ""),
        "known_gaps": inputs.get("known_gaps", []),
        "checks": checks,
        "all_checks_pass": all(c["result"] == "PASS" for c in checks if c["result"] != "NOT RUN") and any(c["result"] == "PASS" for c in checks),
        "agents_reported_not_verified": inputs.get("agents", []),
        "call_log": call_log_section(raw, inputs.get("agents", [])),
        "requests_made": calls,
        "raw": raw,
    }


def render_markdown(ev: dict[str, Any], printout: str) -> str:
    L: list[str] = [f"# {ev['report']}", "", f"Generated {ev['generated_at']} by `scripts/relay_evidence.py` against `{ev['source']['base_url']}` ({ev['source']['mode']}).", ""]
    L += ["## Summary", "", ev["summary"].strip(), ""]
    ra = ev["read_at"]
    L += ["## What the ledger looked like when read", "", f"- History seq: **{ra['history_seq']}**, records: **{ra['record_count']}**",
          f"- State root: `{ra['state_root']}`", f"- Ledger head: `{ra['ledger_head']}`", f"- `emr_latest` (no arguments) `result_digest`: `{ra['result_digest_emr_latest_default']}`", ""]
    L += ["## Results (read and re-run by the script)", "", "| Result | Check | Detail |", "|---|---|---|"]
    for c in ev["checks"]:
        L.append(f"| **{c['result']}** | `{c['check']}` | {_esc(c['detail'])} |")
    L += ["", f"All checks passed: **{ev['all_checks_pass']}**", ""]
    if ev["known_gaps"]:
        L += ["## Known gaps and deviations", ""] + [f"- {g}" for g in ev["known_gaps"]] + [""]
    L += ["## Agent results: reported by agent, NOT verified by the script", ""]
    L += ["These are what each agent said it saw, as pasted by the operator. The script did not run, observe or confirm any of them.", ""]
    L += ["| Agent | Status of this entry | Newest id reported | Digest reported | Notes |", "|---|---|---|---|---|"]
    for a in ev["agents_reported_not_verified"]:
        L.append(f"| {a.get('agent')} | {a.get('entry_status', 'reported by agent')} | `{a.get('newest_id') or '-'}` | `{a.get('result_digest') or '-'}` | {_esc(a.get('notes', ''))} |")
    cl = ev["call_log"]
    L += ["", "## Server-witnessed calls to emr_latest (client names are self-reported)", ""]
    if not cl["available"]:
        L += [f"Not available: {cl['reason']}. Nothing is claimed about any agent here.", ""]
    else:
        head = cl.get("head") or {}
        L += [f"Call log chain verifies: **{cl['chain_ok']}** ({cl.get('entries_total')} entries in {len(cl.get('log_files') or [])} file(s)). "
              f"**Head: seq {head.get('seq')}, hash `{head.get('entry_hash')}`**. Record this off the box, and in the next ledger record that is written.", "",
              "| Agent | Witnessed emr_latest calls | Latest: seq, time, transport | Client as it reported itself | result_digest | Called with no arguments | Outcome |", "|---|---|---|---|---|---|---|"]
        for a in cl["per_agent"]:
            e = a["latest"]
            if e:
                L.append(f"| {a['agent']} | {a['witnessed_emr_latest_calls']} | {e['seq']}, {e['ts']}, {e['transport']} | {_esc(e['client_name'])}/{_esc(e['client_version'])} | `{e['result_digest']}` | {'yes' if e.get('no_arguments') else 'no'} | {e['outcome']} |")
            else:
                L.append(f"| {a['agent']} | 0 | - | - | - | - | {_esc(a['note'])} |")
        L.append("")
        if cl.get("clients_seen"):
            L += [f"Every client name the log saw (coverage: {cl.get('coverage')}; self-reported, so a name is a claim and not an identity). The last column says whether it was counted for one of the agents above. Rows named `relay-evidence` are this report's own read-only calls:", "",
                  "| Client name / version | Transport | Calls | Tools | Last seen | Matched an agent above |", "|---|---|---|---|---|---|"]
            for r in cl["clients_seen"]:
                L.append(f"| {_esc(r['client_name'])}/{_esc(r['client_version'])} | {r['transport']} | {r['calls']} | {_esc(', '.join(r['tools']))} | {r['last_ts']} | {'yes' if r['matched_an_agent'] else '**no**'} |")
            L.append("")
    L += ["## Script printout", "", "```", printout, "```", "", "## Raw command outputs", "",
          "Every request the script made (method, path, query, HTTP status; no headers, no key):", "", "```json", json.dumps(ev["requests_made"], indent=2), "```", ""]
    for label, item in ev["raw"].items():
        body = item["body"]
        if isinstance(body, dict) and isinstance(body.get("entries"), list) and len(body["entries"]) > 20:  # the full log is not pasted into the report
            body = {**body, "entries": body["entries"][:20], "entries_omitted_from_this_listing": len(body["entries"]) - 20}
        L += [f"### `{label}` (HTTP {item['http_status']})", "", "```json", json.dumps(body, indent=2, ensure_ascii=False), "```", ""]
    return "\n".join(L).rstrip() + "\n"


# --------------------------------------------------------------------------------------------------------- command


def read_key(path: str | None) -> str:
    path = path or os.environ.get("JARVIS_API_KEY_FILE")
    if not path:
        raise SystemExit("no key file: pass --key-file or set JARVIS_API_KEY_FILE")
    key = Path(path).expanduser().read_text(encoding="utf-8").strip()
    if not key:
        raise SystemExit("the key file is empty")
    return key


def main(argv: list[str] | None = None, transport: Transport | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--base-url", default=DEFAULT_BASE)
    ap.add_argument("--key-file")
    ap.add_argument("--old-id", default=DEFAULT_OLD_ID)
    ap.add_argument("--new-id", default=DEFAULT_NEW_ID)
    ap.add_argument("--receipt", default=DEFAULT_RECEIPT)
    ap.add_argument("--expect-newest", default=None, help="default: --new-id")
    ap.add_argument("--inputs", help="JSON: title, summary, known_gaps[], agents[] (reported by agents)")
    ap.add_argument("--json-out")
    ap.add_argument("--md-out")
    args = ap.parse_args(argv)

    inputs = json.loads(Path(args.inputs).read_text(encoding="utf-8")) if args.inputs else {}
    client = ReadOnlyClient(args.base_url, read_key(args.key_file), transport)
    raw = gather(client, args.old_id, args.new_id, args.receipt)
    reported = next((a.get("result_digest") for a in inputs.get("agents", []) if a.get("agent") in ("Devin", "OpenCode") and a.get("result_digest")), None)
    checks = run_checks(raw, args.old_id, args.new_id, args.expect_newest or args.new_id, reported)
    printout = render_printout(checks)
    print(printout)
    ev = build_evidence(raw, checks, inputs, args.base_url, client.calls, {"old": args.old_id, "new": args.new_id, "receipt": args.receipt})
    if args.json_out:
        Path(args.json_out).write_text(json.dumps(ev, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    if args.md_out:
        Path(args.md_out).write_text(render_markdown(ev, printout), encoding="utf-8")
    return 0 if ev["all_checks_pass"] else 1


if __name__ == "__main__":
    sys.exit(main())
