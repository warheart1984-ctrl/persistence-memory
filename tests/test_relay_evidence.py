"""scripts/relay_evidence.py: read-only by construction, re-derives digests, never leaks the key."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "relay_evidence.py"
KEY = "sk-test-SECRET-0123456789abcdef"
OLD, NEW, RECEIPT = "mem-1dc144c193a1", "mem-c42330528ad5", "eo:sha256:" + "ab" * 32


@pytest.fixture(scope="module")
def rel():
    spec = importlib.util.spec_from_file_location("relay_evidence", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def digest_rows(rows):
    return hashlib.sha256(json.dumps(rows, separators=(",", ":"), ensure_ascii=False).encode("utf-8")).hexdigest()


def rec(i, created, status="active", **extra):
    return {"id": i, "created_at": created, "status": status, **extra}


# ----------------------------------------------------------------------------------------------- the write guard


@pytest.mark.parametrize(
    "method,path",
    [
        ("POST", "/api/jarvis/memory"),
        ("PATCH", f"/api/jarvis/memory/{OLD}"),
        ("DELETE", f"/api/jarvis/memory/{OLD}"),
        ("PUT", f"/api/jarvis/memory/{OLD}"),
        ("POST", "/api/jarvis/blocks/seal"),
        ("POST", "/api/jarvis/replay/receipts"),
        ("POST", "/api/jarvis/tools/emr_remember"),
        ("POST", "/api/jarvis/tools/emr_upsert"),
        ("POST", "/api/jarvis/memory/promote"),
        ("GET", "/api/jarvis/rag/maintenance"),
        ("GET", "/api/jarvis/anything-else"),
    ],
)
def test_anything_that_is_not_a_read_is_refused_before_any_network_call(rel, method, path):
    sent: list[Any] = []

    def transport(*a, **k):
        sent.append(a)
        return 200, b"{}"

    client = rel.ReadOnlyClient("http://127.0.0.1:1", KEY, transport)
    with pytest.raises(rel.WriteRefused):
        client.request(method, path, body={"x": 1})
    assert sent == [] and client.calls == []


def test_the_allowed_reads_go_through_and_the_key_stays_in_the_header_only(rel):
    seen = {}

    def transport(method, url, headers, body):
        seen.update(method=method, url=url, headers=headers)
        return 200, b'{"ok": true}'

    client = rel.ReadOnlyClient("http://127.0.0.1:1", KEY, transport)
    assert client.get("/api/jarvis/blocks/verify") == (200, {"ok": True})
    assert client.tool("emr_latest", {}) == (200, {"ok": True})
    assert seen["headers"]["X-API-Key"] == KEY
    assert KEY not in json.dumps(client.calls)  # the request log carries no headers


# ---------------------------------------------------------------------------------------------------- digests


def test_digest_rederives_in_both_response_shapes(rel):
    old_shape = [rec("a", "2026-10-10T20:53:01.808201Z", "active"), rec("b", "2026-10-09T03:41:01.000000Z", "active")]
    d, formula = rel.derive_digest(old_shape)
    assert formula == "[id, created_at, status]" and d == digest_rows([[r["id"], r["created_at"], r["status"]] for r in old_shape])
    new_shape = [rec("a", "2026-10-10T20:53:01.808201Z", "draft", lifecycle="active"), rec("b", "2026-10-09T03:41:01.000000Z", "verified", lifecycle="superseded")]
    d, formula = rel.derive_digest(new_shape)
    assert formula == "[id, created_at, status, lifecycle]" and d == digest_rows([[r["id"], r["created_at"], r["status"], r["lifecycle"]] for r in new_shape])


def test_the_prior_digest_reconstruction_drops_the_new_record_and_restores_the_old_one(rel):
    current = [rec(NEW, "2026-10-10T20:53:01.808201Z")] + [rec(f"mem-{n}", f"2026-10-0{n}T00:00:00.000000Z") for n in range(9, 0, -1)]
    old = rec(OLD, "2026-10-10T20:39:50.728361Z")
    expected = digest_rows([[OLD, old["created_at"], "active"]] + [[f"mem-{n}", f"2026-10-0{n}T00:00:00.000000Z", "active"] for n in range(9, 0, -1)])
    assert rel.reconstruct_prior_digest(current, old, NEW) == expected
    assert rel.reconstruct_prior_digest(current, old, NEW) != rel.derive_digest(current[:10])[0]


# -------------------------------------------------------------------------------------- the checks, on canned data


def canned(*, old_writer="codex", new_writer="claude", newest=NEW, hv_ok=True, receipt_ok=True, hidden=True):
    new_row = rec(NEW, "2026-10-10T20:53:01.808201Z", superseded_by=None)
    old_row = rec(OLD, "2026-10-10T20:39:50.728361Z", "superseded", superseded_by=NEW)
    others = [rec(f"mem-{n}", f"2026-10-0{n}T00:00:00.000000Z") for n in range(9, 1, -1)]
    default_records = ([new_row] if newest == NEW else [rec(newest, "2026-10-11T00:00:00.000000Z")]) + others
    if not hidden:
        default_records.append(old_row)
    d, _ = rel_mod().derive_digest(default_records)
    return {
        "record_old": {"http_status": 200, "body": {"memory": {"id": OLD, "source_agent": old_writer, "supersedes": None}}},
        "record_new": {"http_status": 200, "body": {"memory": {"id": NEW, "source_agent": new_writer, "supersedes": OLD}}},
        "latest_default": {"http_status": 200, "body": {"records": default_records, "result_digest": d, "ledger_head": "block:x"}},
        "latest_default_12": {"http_status": 200, "body": {"records": default_records}},
        "latest_with_superseded": {"http_status": 200, "body": {"records": [new_row, old_row] + others}},
        "history_verify": {"http_status": 200, "body": {"ok": hv_ok, "problems": [] if hv_ok else ["x"]}},
        "blocks_verify": {"http_status": 200, "body": {"ok": True, "problems": []}},
        "receipt_verify": {"http_status": 200, "body": {"ok": receipt_ok, "problems": []}},
        "events_135_136": {"http_status": 200, "body": {"events": [{"seq": 135, "memory_id": OLD, "op": "create"}, {"seq": 136, "memory_id": NEW, "op": "create"}]}},
        "state": {"http_status": 200, "body": {"history_seq": 136, "record_count": 61, "state_root": "78cb"}},
    }


_REL = None


def rel_mod():
    global _REL
    if _REL is None:
        spec = importlib.util.spec_from_file_location("relay_evidence_b", SCRIPT)
        _REL = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = _REL
        spec.loader.exec_module(_REL)
    return _REL


def results(rel, raw, reported=None):
    return {c["check"]: c["result"] for c in rel.run_checks(raw, OLD, NEW, NEW, reported)}


def test_every_check_passes_on_consistent_data(rel):
    got = results(rel, canned())
    assert set(got.values()) == {"PASS", "NOT RUN"}, got
    assert got["digest_re_derives"] == "PASS" and got["superseded_record_hidden_by_default"] == "PASS"


@pytest.mark.parametrize(
    "kwargs,check",
    [
        ({"old_writer": "claude"}, "writer_of_old_record_is_codex"),
        ({"new_writer": "codex"}, "writer_of_new_record_is_claude"),
        ({"newest": "mem-ffffffffffff"}, "newest_record_is_expected"),
        ({"hv_ok": False}, "chain_verifies"),
        ({"receipt_ok": False}, "old_receipt_re_derives"),
        ({"hidden": False}, "superseded_record_hidden_by_default"),
    ],
)
def test_each_check_fails_when_its_claim_is_false(rel, kwargs, check):
    assert results(rel, canned(**kwargs))[check] == "FAIL"


def test_a_wrong_server_digest_fails_the_rederivation(rel):
    raw = canned()
    raw["latest_default"]["body"]["result_digest"] = "0" * 64
    assert results(rel, raw)["digest_re_derives"] == "FAIL"


def test_the_agents_digest_check_runs_only_when_one_was_reported(rel):
    assert results(rel, canned())["agent_digest_reproduces_from_seq_135_state"] == "NOT RUN"
    assert results(rel, canned(), reported="0" * 64)["agent_digest_reproduces_from_seq_135_state"] == "FAIL"


# ------------------------------------------------------------------------------------- end to end, key never leaks


def test_a_full_run_writes_both_reports_and_never_contains_the_key(rel, tmp_path):
    raw = canned()
    state = {
        "/api/jarvis/replay/state": raw["state"]["body"],
        f"/api/jarvis/memory/{OLD}": raw["record_old"]["body"],
        f"/api/jarvis/memory/{NEW}": raw["record_new"]["body"],
        f"/api/jarvis/memory/{OLD}/history": {"history": []},
        f"/api/jarvis/memory/{NEW}/history": {"history": []},
        "/api/jarvis/replay/events": raw["events_135_136"]["body"],
        "/api/jarvis/memory/history/verify": raw["history_verify"]["body"],
        "/api/jarvis/blocks/verify": raw["blocks_verify"]["body"],
        "/api/jarvis/blocks/head": {"tip": {"height": 3}},
        f"/api/jarvis/replay/receipts/{RECEIPT}/verify": raw["receipt_verify"]["body"],
    }

    def transport(method, url, headers, body):
        path = url.split("?")[0].replace("http://127.0.0.1:1", "")
        assert method in ("GET", "POST") and headers["X-API-Key"] == KEY
        if path.startswith("/api/jarvis/tools/calls"):
            return 404, b'{"detail": "the call log is switched off"}'
        if path == "/api/jarvis/tools/emr_latest":
            args = json.loads(body or b"{}")
            key = "latest_with_superseded" if args.get("include_superseded") else ("latest_default_12" if args.get("limit") == 12 else "latest_default")
            return 200, json.dumps(raw[key]["body"]).encode()
        return 200, json.dumps(state[path]).encode()

    keyfile = tmp_path / "key"
    keyfile.write_text(KEY + "\n")
    inputs = tmp_path / "inputs.json"
    inputs.write_text(json.dumps({"title": "T", "summary": "S", "known_gaps": ["g"], "agents": [{"agent": "Cursor", "entry_status": "not provided"}]}))
    out_json, out_md = tmp_path / "r.json", tmp_path / "r.md"
    code = rel.main(["--base-url", "http://127.0.0.1:1", "--key-file", str(keyfile), "--old-id", OLD, "--new-id", NEW, "--receipt", RECEIPT,
                     "--inputs", str(inputs), "--json-out", str(out_json), "--md-out", str(out_md)], transport=transport)
    assert code == 0
    ev = json.loads(out_json.read_text())
    assert ev["all_checks_pass"] is True and ev["read_at"]["history_seq"] == 136
    assert "not provided" in out_md.read_text() and "NOT verified by the script" in out_md.read_text()
    assert ev["call_log"]["available"] is False and "Nothing is claimed about any agent" in out_md.read_text()
    assert {c["check"]: c["result"] for c in ev["checks"]}["call_log_chain_verifies"] == "NOT RUN"
    for path in (out_json, out_md):
        assert KEY not in path.read_text()


# --------------------------------------------------------------------------------------- the call-log section


def call_entry(seq, client, digest="d" * 64, transport="mcp-stdio", args_sha256=None):
    return {"args_sha256": args_sha256 or hashlib.sha256(b"{}").hexdigest(), "seq": seq, "ts": f"2026-10-10T22:0{seq}:00.000000Z", "transport": transport, "client_name": client, "client_version": "1.0", "outcome": "ok",
            "result_digest": digest, "tenant": "operator", "tool": "emr_latest"}


AGENTS = [{"agent": "Devin"}, {"agent": "OpenCode"}, {"agent": "Codex"}, {"agent": "Cursor"}]


def test_the_call_log_section_shows_only_what_the_server_witnessed_per_agent(rel):
    raw = {
        "calls_emr_latest": {"http_status": 200, "body": {"entries": [call_entry(3, "OpenCode"), call_entry(2, "devin-cli"), call_entry(1, "OPENCODE")]}},
        "calls_verify": {"http_status": 200, "body": {"ok": True, "head": {"seq": 3, "entry_hash": "h" * 64}, "files": ["calls-20261010.jsonl"], "entries": 3, "problems": []}},
    }
    section = rel.call_log_section(raw, AGENTS)
    by = {a["agent"]: a for a in section["per_agent"]}
    assert section["available"] and section["chain_ok"] is True and section["head"]["entry_hash"] == "h" * 64
    assert by["OpenCode"]["witnessed_emr_latest_calls"] == 2 and by["OpenCode"]["latest"]["seq"] == 3  # newest first, case-insensitive match
    assert by["Devin"]["witnessed_emr_latest_calls"] == 1 and by["Devin"]["latest"]["client_name"] == "devin-cli"
    for silent in ("Codex", "Cursor"):
        assert by[silent]["witnessed_emr_latest_calls"] == 0 and by[silent]["latest"] is None and "no emr_latest call" in by[silent]["note"]


def test_an_unavailable_call_log_claims_nothing(rel):
    for status in (404, 401, 403):
        section = rel.call_log_section({"calls_emr_latest": {"http_status": status, "body": {"detail": "x"}}}, AGENTS)
        assert section["available"] is False and section["per_agent"] == [] and str(status) in section["reason"]


def test_the_log_endpoints_are_allowed_reads_and_nothing_else_on_that_path_is(rel):
    assert rel.is_allowed("GET", "/api/jarvis/tools/calls") and rel.is_allowed("GET", "/api/jarvis/tools/calls/verify")
    assert not rel.is_allowed("POST", "/api/jarvis/tools/calls") and not rel.is_allowed("DELETE", "/api/jarvis/tools/calls")
    assert not rel.is_allowed("GET", "/api/jarvis/tools/calls/other")


def test_the_markdown_labels_witnessed_calls_as_self_reported_and_records_the_head(rel):
    raw = canned()
    raw["calls_emr_latest"] = {"http_status": 200, "body": {"entries": [call_entry(7, "cursor")]}}
    raw["calls_verify"] = {"http_status": 200, "body": {"ok": True, "head": {"seq": 7, "entry_hash": "c" * 64}, "files": ["calls-20261010.jsonl"], "entries": 7, "problems": []}}
    checks = rel.run_checks(raw, OLD, NEW, NEW, None)
    assert {c["check"]: c["result"] for c in checks}["call_log_chain_verifies"] == "PASS"
    ev = rel.build_evidence(raw, checks, {"agents": AGENTS, "summary": "s"}, "http://x", [], {"old": OLD, "new": NEW, "receipt": RECEIPT})
    md = rel.render_markdown(ev, rel.render_printout(checks))
    assert "client names are self-reported" in md and "`" + "c" * 64 + "`" in md and "Record this off the box" in md and "| Cursor | 1 |" in md


def test_every_client_name_in_the_log_is_listed_and_unmatched_ones_are_flagged(rel):
    entries = [call_entry(5, "cursor-vscode"), call_entry(4, "mystery-client"), call_entry(3, "OpenCode"), call_entry(2, "cursor-vscode"),
               {**call_entry(1, None), "tool": "POST /api/jarvis/blocks/seal", "transport": "http-api"}]
    raw = {
        "calls_emr_latest": {"http_status": 200, "body": {"entries": [e for e in entries if e["tool"] == "emr_latest"]}},
        "calls_all": {"http_status": 200, "body": {"entries": entries}},
        "calls_verify": {"http_status": 200, "body": {"ok": True, "head": {"seq": 5, "entry_hash": "h" * 64}, "files": ["calls-20261010.jsonl"], "entries": 5, "problems": []}},
    }
    agents = [{"agent": "OpenCode"}, {"agent": "Cursor", "aliases": ["cursor-vscode"]}, {"agent": "Kilo"}]
    section = rel.call_log_section(raw, agents)
    by = {a["agent"]: a for a in section["per_agent"]}
    assert by["Cursor"]["witnessed_emr_latest_calls"] == 2 and by["Cursor"]["latest"]["seq"] == 5  # matched through the alias
    assert by["OpenCode"]["witnessed_emr_latest_calls"] == 1 and by["Kilo"]["witnessed_emr_latest_calls"] == 0 and "see the list of every client name" in by["Kilo"]["note"]
    seen = {(r["client_name"], r["transport"]): r for r in section["clients_seen"]}
    assert seen[("mystery-client", "mcp-stdio")]["matched_an_agent"] is False  # a call is in the log, but under a name no agent was matched on
    assert seen[("cursor-vscode", "mcp-stdio")]["calls"] == 2 and seen[("cursor-vscode", "mcp-stdio")]["matched_an_agent"] is True
    assert seen[("(none)", "http-api")]["tools"] == ["POST /api/jarvis/blocks/seal"]
    ev = rel.build_evidence(raw, rel.run_checks(canned(), OLD, NEW, NEW, None), {"agents": agents, "summary": "s"}, "http://x", [], {"old": OLD, "new": NEW, "receipt": RECEIPT})
    ev["call_log"] = section
    md = rel.render_markdown(ev, "printout")
    assert "Every client name the log saw" in md and "| mystery-client/1.0 | mcp-stdio | 1 | emr_latest |" in md and "**no**" in md


# ------------------------------------------------------------------------------ pagination of the call-log reads


def paged_log(n, *, old_name="oldest-client"):
    """A fake call-log endpoint with n entries (seq n..1, newest first); only the very oldest entries carry old_name."""
    rows = [{**call_entry(1, old_name if seq <= 5 else "newer-client"), "seq": seq, "ts": f"2026-10-10T{(seq // 3600) % 24:02d}:{(seq // 60) % 60:02d}:{seq % 60:02d}.000000Z"} for seq in range(n, 0, -1)]

    def transport(method, url, headers, body):
        from urllib.parse import parse_qs, urlparse

        parsed = urlparse(url)
        q = {k: v[0] for k, v in parse_qs(parsed.query).items()}
        assert method == "GET" and parsed.path == "/api/jarvis/tools/calls" and "X-API-Key" in headers
        limit = int(q.get("limit", 50))
        before = int(q["cursor"]) if "cursor" in q else None
        pool = [r for r in rows if (before is None or r["seq"] < before) and (not q.get("tool") or r["tool"] == q["tool"])]
        page = pool[:limit]
        more = len(pool) > limit
        return 200, json.dumps({"entries": page, "next_cursor": str(page[-1]["seq"]) if more else None}).encode()

    return rows, transport


def test_every_page_of_the_log_is_read_so_an_old_client_name_is_not_hidden(rel):
    rows, transport = paged_log(450)
    client = rel.ReadOnlyClient("http://x", KEY, transport)
    status, body = rel.fetch_all_calls(client)
    assert status == 200 and len(body["entries"]) == 450 and body["pages"] == 3 and body["truncated"] is False
    assert [e["seq"] for e in body["entries"]] == list(range(450, 0, -1))  # no duplicates, no gaps, newest first
    assert [c["query"] for c in client.calls] == [{"limit": 200}, {"limit": 200, "cursor": "251"}, {"limit": 200, "cursor": "51"}]
    raw = {"calls_emr_latest": {"http_status": 200, "body": body}, "calls_all": {"http_status": 200, "body": body},
           "calls_verify": {"http_status": 200, "body": {"ok": True, "head": {"seq": 450, "entry_hash": "h" * 64}, "files": ["f"], "entries": 450, "problems": []}}}
    section = rel.call_log_section(raw, [{"agent": "Oldest"}, {"agent": "Nobody"}])
    names = {r["client_name"] for r in section["clients_seen"]}
    assert names == {"oldest-client", "newer-client"}  # the old name only appears on page 3
    assert {a["agent"]: a["witnessed_emr_latest_calls"] for a in section["per_agent"]} == {"Oldest": 5, "Nobody": 0}
    assert section["truncated"] is False and section["coverage"].startswith("complete: all 450 log entries (3 page(s))")


def test_hitting_the_page_cap_is_reported_as_a_partial_view(rel, monkeypatch):
    monkeypatch.setattr(rel, "MAX_CALL_PAGES", 2)
    rows, transport = paged_log(450)
    client = rel.ReadOnlyClient("http://x", KEY, transport)
    status, body = rel.fetch_all_calls(client)
    assert status == 200 and len(body["entries"]) == 400 and body["pages"] == 2 and body["truncated"] is True
    raw = {"calls_emr_latest": {"http_status": 200, "body": body}, "calls_all": {"http_status": 200, "body": body},
           "calls_verify": {"http_status": 200, "body": {"ok": True, "head": {"seq": 450, "entry_hash": "h" * 64}, "files": ["f"], "entries": 450, "problems": []}}}
    section = rel.call_log_section(raw, [{"agent": "Oldest"}])
    assert section["truncated"] is True and section["coverage"].startswith("PARTIAL: only the newest 400 log entries")
    assert "oldest-client" not in {r["client_name"] for r in section["clients_seen"]}  # honestly missing, and said to be
    assert "PARTIAL" in section["per_agent"][0]["note"]
    ev = rel.build_evidence(raw, rel.run_checks(canned(), OLD, NEW, NEW, None), {"agents": [{"agent": "Oldest"}], "summary": "s"}, "http://x", [], {"old": OLD, "new": NEW, "receipt": RECEIPT})
    assert "PARTIAL: only the newest 400" in rel.render_markdown(ev, "p")


def test_a_failure_on_the_first_page_is_reported_unchanged(rel):
    client = rel.ReadOnlyClient("http://x", KEY, lambda *a: (404, b'{"detail":"switched off"}'))
    assert rel.fetch_all_calls(client) == (404, {"detail": "switched off"})
    assert len(client.calls) == 1


def test_the_markdown_does_not_paste_the_whole_log_into_the_raw_section(rel):
    rows, transport = paged_log(450)
    client = rel.ReadOnlyClient("http://x", KEY, transport)
    status, body = rel.fetch_all_calls(client)
    raw = {**canned(), "calls_emr_latest": {"http_status": status, "body": body}, "calls_all": {"http_status": status, "body": body},
           "calls_verify": {"http_status": 200, "body": {"ok": True, "head": {"seq": 450, "entry_hash": "h" * 64}, "files": ["f"], "entries": 450, "problems": []}}}
    ev = rel.build_evidence(raw, rel.run_checks(raw, OLD, NEW, NEW, None), {"agents": [], "summary": "s"}, "http://x", [], {"old": OLD, "new": NEW, "receipt": RECEIPT})
    md = rel.render_markdown(ev, "p")
    assert md.count('"seq":') < 120 and "entries_omitted_from_this_listing" in md and len(md) < 120_000


def test_the_script_identifies_itself_to_the_servers_call_log(rel):
    seen = {}
    client = rel.ReadOnlyClient("http://x", KEY, lambda m, u, h, b: (seen.update(h) or (200, b"{}")))
    client.tool("emr_latest", {})
    assert seen["X-Jarvis-MCP-Client"] == "relay-evidence/1" and seen["X-API-Key"] == KEY


def test_the_report_says_whether_the_witnessed_call_used_no_arguments(rel):
    with_args = call_entry(2, "devin-x", args_sha256=hashlib.sha256(b'{"limit":3}').hexdigest())
    raw = {"calls_emr_latest": {"http_status": 200, "body": {"entries": [call_entry(3, "cursor-x"), with_args]}},
           "calls_verify": {"http_status": 200, "body": {"ok": True, "head": {"seq": 3, "entry_hash": "h" * 64}, "files": ["f"], "entries": 3, "problems": []}}}
    by = {a["agent"]: a for a in rel.call_log_section(raw, [{"agent": "Cursor"}, {"agent": "Devin"}])["per_agent"]}
    assert by["Cursor"]["latest"]["no_arguments"] is True and by["Devin"]["latest"]["no_arguments"] is False
