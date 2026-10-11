"""The drift watcher: deterministic claim checks of an agent's summary against the ledger records it cites.

Backend-parametrized tests run on the JSON store and on Postgres (CI's test-postgres job sets JARVIS_TEST_PG_DSN).  See docs/drift_watcher.md.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import call_log, drift, pg_store
from app.main import app
from app.pg_schema import migrate
from app.store import JarvisStore

KEY = "drift-test-KEY-0123456789"
HDR = {"X-API-Key": KEY}
MCP = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
ROOT = Path(__file__).resolve().parents[1]

OLD_CONTENT = "Schema v9 deployed to live on 2026-10-10 (main 792e99b)"
NEW_CONTENT = ("Live ledger deployed from schema 7 to schema 9 on 2026-10-10 (main 792e99b, ~19:16-19:20Z), by Claude from a Jon-approved plan after a passing "
               "throwaway drill. Signing stayed shelved (JARVIS_SIGNATURES=warn, no trust roots, nothing signed); twin routes return 404. Corrects the earlier "
               "record, whose deploy-report evidence points at a path on the Windows PC that the ledger cannot read.")
EVIDENCE = [{"kind": "deploy-report", "ref": "/drills/DEPLOY-REPORT.md", "note": "Plain-text deploy report on the box. sha256 ca74e35ae05ac7da2a1801ec61c0258f5f7bfc8f22ddbb38b774b765c40e6872"}]


# ----------------------------------------------------------------------------------------------------- fixtures


@pytest.fixture(params=[pytest.param("json", marks=pytest.mark.json_store_only), "postgres"])
def backend(request, monkeypatch):
    monkeypatch.setenv("JARVIS_API_KEY", KEY)
    monkeypatch.delenv("JARVIS_ALLOW_UNAUTHENTICATED", raising=False)
    monkeypatch.delenv("JARVIS_CURSOR_HMAC_KEY", raising=False)
    monkeypatch.setenv("JARVIS_MEMORY_WRITE_ENABLED", "true")
    if request.param == "postgres":
        pg = request.getfixturevalue("pg_schema")
        migrate(pg.admin_dsn, schema=pg.schema, app_role="jarvis_app_test")
        monkeypatch.setenv("JARVIS_DATABASE_URL", pg.app_dsn)
        monkeypatch.setenv("JARVIS_DATABASE_SCHEMA", pg.schema)
        monkeypatch.setenv("JARVIS_PG_STORE", "rows")
    yield request.param
    pg_store.close_pools()


@pytest.fixture
def client(backend):
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


@pytest.fixture
def jclient(monkeypatch):
    monkeypatch.setenv("JARVIS_API_KEY", KEY)
    monkeypatch.delenv("JARVIS_ALLOW_UNAUTHENTICATED", raising=False)
    monkeypatch.setenv("JARVIS_MEMORY_WRITE_ENABLED", "true")
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


def add(client, content, agent, *, supersedes=None, subject=None, type_="decision"):
    body = {"content": content, "source_agent": agent, "session_id": "s", "type": type_, "subject": subject or f"{agent}-{abs(hash(content)) % 10**6}",
            "evidence": EVIDENCE, **({"supersedes": supersedes} if supersedes else {})}
    r = client.post("/api/jarvis/memory", headers=HDR, json=body)
    assert r.status_code == 200, r.text
    return r.json()["memory"]


@pytest.fixture
def pair(client):
    """The real case: Codex wrote the first record; Claude later wrote the record that supersedes it (and says Claude did the deploy)."""
    old = add(client, OLD_CONTENT, "codex", subject="Schema v9 deployed to live on 2026-10-10 (main 792e99b)")
    new = add(client, NEW_CONTENT, "claude", supersedes=old["id"], subject="Schema v9 deployed to live on 2026-10-10 (main 792e99b)")
    return old, new


def check(http, summary, ids, **extra):
    r = http.post("/api/jarvis/drift/check", headers=HDR, json={"summary": summary, "records": ids, **extra})
    assert r.status_code == 200, r.text
    return r.json()


def only(result):
    assert len(result["claims"]) == 1, result["claims"]
    return result["claims"][0]


# ------------------------------------------------------------------------------------------------ the real case


def test_codex_deployed_main_792e99b_is_contradicted_by_claudes_record(client, pair):
    old, new = pair
    result = check(client, "Codex deployed main 792e99b.", [new["id"]])
    claim = only(result)
    assert result["verdict"] == "drift" and claim["status"] == "contradicted" and claim["reason"] == "ACTOR_CONTRADICTED"
    fields = {(e["record"], e["field"]): e["value"] for f in claim["findings"] for e in f["evidence"]}
    assert fields[(new["id"], "source_agent")] == "claude"
    assert "Claude" in fields[(new["id"], "content")]  # the content text that names who did the deploy
    assert "codex" in claim["findings"][0]["detail"].lower() and "claude" in claim["findings"][0]["detail"].lower()


def test_the_same_claim_against_codexs_own_record_is_unsupported_not_contradicted(client, pair):
    """Codex's record says who WROTE it, not who deployed. Authorship is not the action: the claim cannot be confirmed, but it is not refuted either."""
    old, new = pair
    alone = only(check(client, "Codex deployed main 792e99b.", [old["id"]]))
    assert alone["status"] == "unsupported" and alone["reason"] == "ACTOR_AUTHORSHIP_ONLY"
    assert "WROTE" in alone["findings"][0]["detail"] and "source_agent codex" in alone["findings"][0]["detail"]
    both = only(check(client, "Codex deployed main 792e99b.", [old["id"], new["id"]]))
    assert both["status"] == "contradicted"  # the superseding record settles it


def test_authorship_claims_are_checked_against_source_agent(client, pair):
    old, new = pair
    assert only(check(client, "Codex wrote the first record.", [old["id"]]))["status"] == "supported"
    wrong = only(check(client, "Claude wrote the first record.", [old["id"]]))
    assert wrong["status"] == "contradicted" and wrong["findings"][0]["field"] == "source_agent" and wrong["findings"][0]["found"] == "codex"
    assert only(check(client, "The record was written by Claude.", [new["id"]]))["status"] == "supported"


def test_signatures_are_live_is_contradicted_by_a_warn_mode_record(client, pair):
    old, new = pair
    claim = only(check(client, "The signature machinery is likely live on the box.", [new["id"]]))
    assert claim["status"] == "contradicted" and claim["reason"] == "STATE_CLAIM_CONTRADICTED" and claim["hedged"] is True
    assert "shelved" in claim["findings"][0]["evidence"][0]["value"].lower()
    assert only(check(client, "Signing is enabled.", [new["id"]]))["status"] == "contradicted"
    assert only(check(client, "Signatures are not live.", [new["id"]]))["status"] != "contradicted"  # a correct negative is not flagged


def test_a_correct_summary_passes_with_zero_findings(client, pair):
    old, new = pair
    summary = ("Claude deployed the live ledger from schema 7 to schema 9 on 2026-10-10 using main 792e99b after a passing throwaway drill. "
               "Signing stayed shelved, and the twin routes return 404.")
    result = check(client, summary, [new["id"]])
    assert result["verdict"] == "pass", result["claims"]
    assert result["counts"]["contradicted"] == 0 and result["counts"]["unsupported"] == 0 and result["counts"]["supported"] == result["counts"]["claims"] >= 2
    assert all(not c["findings"] or all(f["status"] == "supported" for f in c["findings"]) for c in result["claims"])


# --------------------------------------------------------------------------------------- other claim families


def test_supersede_direction_is_checked_in_every_form(client, pair):
    old, new = pair
    ids = [old["id"], new["id"]]
    assert only(check(client, f"{new['id']} supersedes {old['id']}.", ids))["status"] == "supported"
    wrong = only(check(client, f"{old['id']} supersedes {new['id']}.", ids))
    assert wrong["status"] == "contradicted" and wrong["reason"] == "SUPERSEDE_DIRECTION" and new["id"] in wrong["findings"][0]["found"]
    assert only(check(client, f"{old['id']} was superseded by {new['id']}.", ids))["status"] == "supported"
    assert only(check(client, f"{new['id']} was superseded by {old['id']}.", ids))["status"] == "contradicted"
    assert only(check(client, "The old record supersedes the new one.", ids))["status"] == "contradicted"
    assert only(check(client, "The new record corrects the old record.", ids))["status"] == "supported"
    assert only(check(client, "Claude's record supersedes Codex's record.", ids))["status"] == "supported"
    assert only(check(client, "Codex's record supersedes Claude's record.", ids))["status"] == "contradicted"


def test_wrong_status_and_lifecycle_are_caught(client, pair):
    old, new = pair
    verified = only(check(client, "The decision is verified.", [new["id"]]))
    assert verified["status"] == "contradicted" and verified["reason"] == "STATUS_CONTRADICTED" and verified["findings"][0]["found"] == "draft"
    keyed = only(check(client, "The record status is active.", [new["id"]]))  # "status ... active": the stored status is draft
    assert keyed["status"] == "contradicted" and keyed["findings"][0]["field"] == "status"
    assert only(check(client, "The record status is draft.", [new["id"]]))["status"] == "supported"
    assert only(check(client, "The new record is active.", [new["id"]]))["status"] == "supported"  # lifecycle active, a different field
    assert only(check(client, "The first record is superseded.", [old["id"]]))["status"] == "supported"
    gone = only(check(client, "The first record is active.", [old["id"]]))
    assert gone["status"] == "contradicted" and gone["reason"] == "LIFECYCLE_CONTRADICTED"


def test_commits_dates_hashes_and_numbers(client, pair):
    old, new = pair
    ok = only(check(client, "It was deployed on 2026-10-10 from main 792e99b.", [new["id"]]))
    assert ok["status"] == "supported"
    commit = only(check(client, "Claude deployed main 5cb7fee.", [new["id"]]))
    assert commit["status"] == "contradicted" and commit["reason"] == "TOKEN_MISMATCH" and commit["findings"][0]["found"] == "792e99b"
    date = only(check(client, "Claude deployed it on 2026-10-09.", [new["id"]]))
    assert date["status"] == "contradicted" and date["reason"] == "TOKEN_MISMATCH"
    h = only(check(client, "The report hash is ca74e35ae05ac7da2a1801ec61c0258f5f7bfc8f22ddbb38b774b765c40e6872.", [new["id"]]))
    assert h["status"] == "supported"
    bad_hash = only(check(client, "The report sha256 is " + "ab" * 32 + ".", [new["id"]]))
    assert bad_hash["status"] == "contradicted"
    number = only(check(client, "Claude migrated it from schema 7 to schema 8.", [new["id"]]))
    assert number["status"] == "contradicted" and number["reason"] == "NUMBER_MISMATCH"
    unknown = only(check(client, "It was built from commit 1234abc.", [new["id"]]))
    assert unknown["status"] == "unsupported" and unknown["reason"] == "TOKEN_UNSUPPORTED"


def test_an_unsupported_claim_is_marked_unsupported_never_passed(client, pair):
    old, new = pair
    result = check(client, "The deploy used a blue-green rollout with canary traffic.", [new["id"]])
    claim = only(result)
    assert claim["status"] == "unsupported" and claim["reason"] == "UNSUPPORTED_TEXT" and result["verdict"] == "unverified"
    assert "blue" in claim["findings"][0]["detail"]
    strong = only(check(client, "The drill proves the deploy is completely safe.", [new["id"]]))
    assert strong["status"] == "unsupported"
    assert only(check(client, "Cursor reviewed the deploy.", [new["id"]]))["status"] == "unsupported"
    assert only(check(client, "Claude did not deploy it.", [new["id"]]))["status"] != "supported"  # a negation the record does not make


def test_ids_that_were_not_submitted_or_do_not_exist_are_reported(client, pair):
    old, new = pair
    missing = only(check(client, f"{old['id']} is a draft.", [new["id"]]))
    assert missing["status"] == "unsupported" and missing["reason"] == "CITE_MISSING"
    ghost = check(client, "mem-ffffffffffff is a draft.", ["mem-ffffffffffff"])
    assert only(ghost)["reason"] == "RECORD_NOT_FOUND" and any("RECORD_NOT_FOUND" in w for w in ghost["warnings"])


def test_questions_and_opinions_are_skipped_and_counted_not_silently_dropped(client, pair):
    old, new = pair
    result = check(client, "Did Codex deploy it? I think we should rotate the key. Claude deployed main 792e99b.", [new["id"]])
    assert result["counts"]["skipped"] == 2 and {s["reason"] for s in result["skipped"]} == {"QUESTION", "OPINION_OR_INSTRUCTION"}
    assert result["counts"]["claims"] == 1 and only(result)["status"] == "supported"


def test_claims_can_be_supplied_pre_extracted_and_get_the_same_checks(client, pair):
    old, new = pair
    r = client.post("/api/jarvis/drift/check", headers=HDR, json={"records": [new["id"]], "claims": [{"text": "Codex deployed main 792e99b", "cites": [new["id"]]}, {"text": "Signing is enabled"}]})
    assert r.status_code == 200 and [c["status"] for c in r.json()["claims"]] == ["contradicted", "contradicted"]


# ------------------------------------------------------------------------------------------ determinism, limits


def test_the_check_is_deterministic_and_the_digest_moves_with_the_claims(client, pair):
    old, new = pair
    a = check(client, "Codex deployed main 792e99b.", [new["id"]])
    b = check(client, "Codex deployed main 792e99b.", [new["id"]])
    c = check(client, "Claude deployed main 792e99b.", [new["id"]])
    assert a["check_digest"] == b["check_digest"] and a["claims"] == b["claims"] and a["check_digest"] != c["check_digest"] and len(a["check_digest"]) == 64
    assert a["method"] == "deterministic rules; no model"


def test_the_limits_reject_with_the_numbers_and_never_truncate(client, pair):
    old, new = pair
    ids = [new["id"]]

    def refused(body, reason):
        r = client.post("/api/jarvis/drift/check", headers=HDR, json=body)
        assert r.status_code == 422 and r.json()["reason"] == reason, r.text
        return r.json()["detail"]

    assert "20001" in refused({"summary": "x " * 10000 + "y", "records": ids}, "SUMMARY_TOO_LONG") or True
    d = refused({"summary": "a" * 20001, "records": ids}, "SUMMARY_TOO_LONG")
    assert "20001" in d and "20000" in d and "Nothing was checked" in d
    d = refused({"summary": "Claude deployed it.", "records": [f"mem-{i:012x}" for i in range(21)]}, "TOO_MANY_RECORDS")
    assert "21" in d and "20" in d
    d = refused({"records": ids, "claims": [{"text": f"Claude deployed main 792e99b number {i}"} for i in range(201)]}, "TOO_MANY_CLAIMS")
    assert "201" in d and "200" in d
    refused({"summary": "Claude deployed it."}, "RECORDS_REQUIRED")
    refused({"records": ids}, "SUMMARY_REQUIRED")
    # exactly at the limits is accepted, and every claim is reported (none quietly cut)
    ok = client.post("/api/jarvis/drift/check", headers=HDR, json={"records": ids, "claims": [{"text": f"Claude deployed main 792e99b batch {i}"} for i in range(200)]})
    assert ok.status_code == 200 and ok.json()["counts"]["claims"] == 200
    assert client.post("/api/jarvis/drift/check", headers=HDR, json={"summary": "Claude deployed main 792e99b. " + "z" * 19900, "records": ids}).status_code == 200


# ------------------------------------------------------------------------ read-only, isolation, who may use it


def _ledger_fingerprint(client, backend):
    if backend == "postgres":
        s = client.get("/api/jarvis/replay/state?limit=1", headers=HDR).json()
        return {"state_root": s["state_root"], "history_seq": s["history_seq"], "records": s["record_count"], "receipts": client.get("/api/jarvis/replay/receipts", headers=HDR).json()["receipts"]}
    recs = client.get("/api/jarvis/memory?limit=200", headers=HDR).json()["memories"]
    hist = client.get("/api/jarvis/memory/history/verify", headers=HDR)
    return {"records": json.dumps(recs, sort_keys=True), "history": (hist.status_code, json.dumps(hist.json(), sort_keys=True))}


def test_a_hundred_checks_leave_the_ledger_exactly_as_it_was(client, backend, pair):
    old, new = pair
    before = _ledger_fingerprint(client, backend)
    for i in range(100):
        check(client, ("Codex deployed main 792e99b.", "The decision is verified.", "Signing is enabled.")[i % 3], [old["id"], new["id"]])
    assert _ledger_fingerprint(client, backend) == before


def test_the_module_never_writes_the_ledger():
    tree = (ROOT / "app" / "drift.py").read_text(encoding="utf-8")
    for forbidden in ("create_memory", "update_memory", "delete_memory", "upsert", "append_history"):
        assert forbidden not in tree, forbidden


def test_another_tenants_record_is_not_found_for_the_operator(jclient, tmp_path, monkeypatch):
    other = JarvisStore(str(tmp_path / "tenants" / "someone-else.json"))
    from app.models import MemoryCreate

    rec = other.create_memory(MemoryCreate(content="private tenant record by Codex", source_agent="codex", session_id="s", type="decision", subject="private", evidence=EVIDENCE))
    result = check(jclient, "Codex wrote it.", [rec.id])
    assert only(result)["reason"] == "RECORD_NOT_FOUND" and result["records"][0]["found"] is False
    assert "private tenant record" not in json.dumps(result)


def test_a_tenant_is_refused_everywhere_and_the_operator_key_is_required(monkeypatch, tmp_path):
    import app.auth as auth
    from app.identity import Principal

    monkeypatch.setenv("JARVIS_AUTH_MODE", "oauth")
    monkeypatch.setenv("JARVIS_CURSOR_HMAC_KEY", "k" * 32)
    monkeypatch.setenv("JARVIS_STORE_PATH", str(tmp_path / "operator.json"))
    monkeypatch.setenv("JARVIS_TENANT_STORE_DIR", str(tmp_path / "tenants"))
    user = Principal(subject="alice", scopes=frozenset({"memory.read", "memory.write"}), issuer="https://issuer.example")
    monkeypatch.setattr(auth, "validate_access_token", lambda token, *, required_scope="memory.read": user)
    bearer = {"Authorization": "Bearer alice"}
    body = {"summary": "Codex deployed main 792e99b.", "records": ["mem-000000000000"]}
    with TestClient(app, raise_server_exceptions=False) as c:
        for method, path in (("POST", "/api/jarvis/drift/check"), ("POST", "/api/jarvis/tools/emr_drift_check"), ("GET", "/api/jarvis/drift/log"), ("GET", "/api/jarvis/drift/log/verify")):
            r = c.request(method, path, headers=bearer, json=body if method == "POST" else None)
            assert r.status_code == 403 and r.json()["reason"] == "AUTHORITY_DENIED", (path, r.status_code, r.text)
        h = {**MCP, **bearer}
        c.post("/mcp", headers=h, json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
        out = c.post("/mcp", headers=h, json={"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "emr_drift_check", "arguments": body}}).json()["result"]
        assert out["isError"] is True and out["structuredContent"]["error"]["reason"] == "AUTHORITY_DENIED"


def test_without_the_operator_key_it_is_refused(jclient):
    for path in ("/api/jarvis/drift/check", "/api/jarvis/tools/emr_drift_check"):
        assert jclient.post(path, json={"summary": "x y z", "records": ["mem-000000000000"]}).status_code == 401
        assert jclient.post(path, headers={"X-API-Key": "wrong"}, json={"summary": "x y z", "records": ["mem-000000000000"]}).status_code == 401


# --------------------------------------------------------------------------------------- the surfaces and the log


def test_the_http_route_the_tool_route_and_the_mcp_tool_agree(client, pair):
    old, new = pair
    body = {"summary": "Codex deployed main 792e99b.", "records": [new["id"]]}
    a = client.post("/api/jarvis/drift/check", headers=HDR, json=body).json()
    b = client.post("/api/jarvis/tools/emr_drift_check", headers=HDR, json=body).json()
    h = {**MCP, **HDR}
    client.post("/mcp", headers=h, json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    tools = client.post("/mcp", headers=h, json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"}).json()["result"]["tools"]
    assert "emr_drift_check" in {t["name"] for t in tools}
    c = client.post("/mcp", headers=h, json={"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "emr_drift_check", "arguments": body}}).json()["result"]
    assert c["isError"] is False and a["check_digest"] == b["check_digest"] == c["structuredContent"]["check_digest"] and c["structuredContent"]["verdict"] == "drift"
    names = {t["function"]["name"] for t in client.get("/api/jarvis/tools", headers=HDR).json()["tools"]}
    assert "emr_drift_check" in names


def test_every_check_is_appended_to_the_drift_log_without_the_summary_text(jclient, pair):
    """The log keeps the verdict, counts, digest and the non-supported clauses (truncated); it never keeps the whole summary or what was supported/skipped."""
    old, new = pair
    secret = "UNIQUE-SUMMARY-MARKER-91c4"
    summary = f"Claude deployed main 792e99b on 2026-10-10. Is {secret} relevant? Codex deployed main 792e99b."
    r1 = check(jclient, summary, [new["id"]], client="devin")
    log = jclient.get("/api/jarvis/drift/log", headers=HDR).json()
    entry = log["entries"][0]
    assert log["provenance"] == "drift log (not ledger records)" and entry["tool"] == "emr_drift_check" and entry["verdict"] == "drift"
    assert entry["result_digest"] == r1["check_digest"] and entry["records"] == [new["id"]] and entry["args_sha256"] == hashlib.sha256(summary.encode()).hexdigest()
    assert entry["summary_chars"] == len(summary) and r1["drift_log_seq"] == entry["seq"]
    assert [f["claim"] for f in entry["findings"]] == ["Codex deployed main 792e99b"] and entry["findings"][0]["status"] == "contradicted"  # only the clause that drifted
    blob = b"".join(p.read_bytes() for p in drift.drift_log_dir().rglob("*") if p.is_file())
    assert secret.encode() not in blob and b"Claude deployed main" not in blob  # skipped and supported text is not stored
    assert any(p.name.startswith("drift-") for p in drift.drift_log_dir().iterdir())
    v = jclient.get("/api/jarvis/drift/log/verify", headers=HDR).json()
    assert v["ok"] is True and v["entries"] == 1


def test_the_drift_log_chain_detects_tampering(jclient, pair):
    old, new = pair
    for i in range(4):
        check(jclient, "Codex deployed main 792e99b.", [new["id"]])
    path = next(drift.drift_log_dir().glob("drift-*.jsonl"))
    lines = path.read_text(encoding="utf-8").splitlines()
    doctored = json.loads(lines[1])
    doctored["verdict"] = "pass"
    lines[1] = json.dumps(doctored, sort_keys=True, separators=(",", ":"))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    assert jclient.get("/api/jarvis/drift/log/verify", headers=HDR).json()["ok"] is False


def test_a_drift_log_that_cannot_be_written_never_blocks_the_verdict(jclient, pair, monkeypatch):
    old, new = pair
    monkeypatch.setattr(call_log.CallLog, "_append_locked", lambda self, *a, **k: (_ for _ in ()).throw(OSError("disk full")))
    r = jclient.post("/api/jarvis/drift/check", headers=HDR, json={"summary": "Codex deployed main 792e99b.", "records": [new["id"]]})
    body = r.json()
    assert r.status_code == 200 and body["verdict"] == "drift" and body["drift_log_seq"] is None
    assert body["drift_log"] == "degraded" and any("drift log unavailable" in w for w in body["warnings"])
    t = jclient.post("/api/jarvis/tools/emr_drift_check", headers=HDR, json={"summary": "Codex deployed main 792e99b.", "records": [new["id"]]})
    assert t.status_code == 200 and "degraded" in t.text


def test_a_logged_check_says_the_drift_log_is_ok(jclient, pair):
    old, new = pair
    body = check(jclient, "Codex deployed main 792e99b.", [new["id"]])
    assert body["drift_log"] == "ok" and body["drift_log_seq"] is not None
    assert not any("drift log unavailable" in w for w in body.get("warnings", []))


def test_the_flag_off_means_a_404_and_no_files(jclient, pair, monkeypatch):
    old, new = pair
    monkeypatch.setenv("JARVIS_DRIFT_CHECK_ENABLED", "0")
    r = jclient.post("/api/jarvis/drift/check", headers=HDR, json={"summary": "Codex deployed it.", "records": [new["id"]]})
    assert r.status_code == 404 and r.json()["reason"] == "DRIFT_CHECK_DISABLED"
    assert jclient.get("/api/jarvis/drift/log", headers=HDR).status_code == 404
    assert not drift.drift_log_dir().exists() or not list(drift.drift_log_dir().glob("drift-*.jsonl"))
    monkeypatch.delenv("JARVIS_DRIFT_CHECK_ENABLED")
    monkeypatch.delenv("JARVIS_ENV", raising=False)
    assert drift.enabled() is True
    monkeypatch.setenv("JARVIS_ENV", "production")
    assert drift.enabled() is False  # off in production until a deploy turns it on
    monkeypatch.setenv("JARVIS_DRIFT_CHECK_ENABLED", "1")
    assert drift.enabled() is True


def test_the_call_log_witnesses_the_checks_themselves(jclient, pair):
    old, new = pair
    jclient.post("/api/jarvis/drift/check", headers={**HDR, "X-Jarvis-MCP-Client": "caza-tool/1.0"}, json={"summary": "Codex deployed main 792e99b.", "records": [new["id"]]})
    jclient.post("/api/jarvis/tools/emr_drift_check", headers=HDR, json={"summary": "Codex deployed main 792e99b.", "records": [new["id"]]})
    tools = [(e["transport"], e["tool"], e["client_name"]) for e in call_log.get_call_log().entries_desc()]
    assert ("http-api", "POST /api/jarvis/drift/check", "caza-tool") in tools and any(t[1] == "emr_drift_check" and t[0] == "http-tool" for t in tools)


# ---------------------------------------------------------------- what was served: the call log link (and its limits)


def test_the_call_log_shows_which_logged_calls_could_have_served_the_records(jclient, pair):
    old, new = pair
    jclient.post("/api/jarvis/tools/emr_fetch", headers={**HDR, "X-Jarvis-MCP-Client": "devin-agent/1.0"}, json={"id": new["id"]})
    latest = jclient.post("/api/jarvis/tools/emr_latest", headers={**HDR, "X-Jarvis-MCP-Client": "devin-agent/1.0"}, json={}).json()
    r = check(jclient, "Codex deployed main 792e99b.", [new["id"]], client="devin", served_digest=latest["result_digest"])
    whys = {m["why"] for m in r["call_log"]["matches"]}
    assert any("arguments hash equals" in w and new["id"] in w for w in whys) and any("result_digest equals" in w for w in whys)
    assert "cannot show what text the agent was shown" in r["call_log"]["note"]
    none = check(jclient, "Codex deployed main 792e99b.", [new["id"]], client="someone-else")
    assert none["call_log"]["matches"] == [] or all("devin" not in str(m["client_name"]) for m in none["call_log"]["matches"])


def test_a_record_that_changed_since_it_was_served_is_flagged(jclient, pair):
    old, new = pair
    r = check(jclient, "Claude deployed main 792e99b.", [new["id"]], served_content_sha256={new["id"]: "0" * 64})
    assert any("RECORD_CHANGED_SINCE_SERVED" in w for w in r["warnings"])


# ------------------------------------------------------------- the call log's prefix change leaves its behavior alone


def test_the_default_call_log_is_unchanged_by_the_prefix_setting(tmp_path):
    from datetime import datetime, timezone

    now = datetime(2026, 10, 11, 12, 0, 0, tzinfo=timezone.utc)
    default = call_log.CallLog(tmp_path / "default", clock=lambda: now)
    explicit = call_log.CallLog(tmp_path / "explicit", clock=lambda: now, file_prefix="calls")
    fields = {"transport": "http-tool", "tool": "emr_latest", "outcome": "ok", "args_sha256": "a" * 64}
    e1, e2 = default.append(dict(fields)), explicit.append(dict(fields))
    assert e1 == e2 and sorted(p.name for p in (tmp_path / "default").glob("*.jsonl")) == ["calls-20261011.jsonl"]
    expected = hashlib.sha256(json.dumps({k: v for k, v in e1.items() if k != "entry_hash"}, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
    assert e1["entry_hash"] == expected and e1["prev_hash"] == call_log.GENESIS and default.verify()["ok"] is True
    other = call_log.CallLog(tmp_path / "other", clock=lambda: now, file_prefix="drift")
    other.append(dict(fields))
    assert [p.name for p in (tmp_path / "other").glob("*.jsonl")] == ["drift-20261011.jsonl"]
    assert other.verify()["ok"] is True and call_log.CallLog(tmp_path / "other", clock=lambda: now).verify()["entries"] == 0  # the two prefixes never read each other's files


def test_a_drift_log_and_a_call_log_in_one_directory_do_not_mix(tmp_path):
    shared = tmp_path / "shared"
    calls, drifts = call_log.CallLog(shared), call_log.CallLog(shared, file_prefix="drift")
    calls.append({"transport": "http-tool", "tool": "emr_latest", "outcome": "ok", "args_sha256": "a" * 64})
    drifts.append({"tool": "emr_drift_check", "outcome": "ok", "args_sha256": "b" * 64})
    assert calls.verify()["entries"] == 1 and drifts.verify()["entries"] == 1 and calls.verify()["head"]["seq"] == 1 and drifts.verify()["head"]["seq"] == 1


def test_quoted_text_is_not_treated_as_something_the_author_asserts(client, pair):
    """Devin quoted the README's old line to call it stale. 'schema v6' there is a quotation, not a claim about the record."""
    old, new = pair
    claim = only(check(client, 'The README\'s "schema v6, signatures shelved" line is now stale.', [new["id"]]))
    assert claim["quoted_text_not_checked"] == ["\"schema v6, signatures shelved\""]
    assert claim["status"] != "contradicted" and all(f["reason"] != "NUMBER_MISMATCH" for f in claim["findings"])
    outside = only(check(client, "The ledger is at schema v6.", [new["id"]]))  # the same number asserted outside quotes is still checked
    assert outside["status"] == "contradicted" and outside["reason"] == "NUMBER_MISMATCH"


def test_identifiers_with_underscores_stay_whole(client, pair):
    old, new = pair
    result = check(client, "JARVIS_SIGNATURES=warn and the twin routes return 404.", [new["id"]])
    assert result["verdict"] != "drift" and "jarvissignatures" not in json.dumps(result).lower()
    assert drift._clean("emr_latest and _emphasis_ and JARVIS_SIGNATURES") == "emr_latest and emphasis and JARVIS_SIGNATURES"
