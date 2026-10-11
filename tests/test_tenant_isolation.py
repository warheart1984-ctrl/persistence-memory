"""Tenant isolation matrix (RFC: multi-tenant ledger, phase 0).

Two tenants, alice and bob, exist today through OAuth subjects.  This file is the safety net the multi-tenant work lands on:

1. **The route registry fails closed.**  Every method+path the app serves must be classified below (operator-only, public, tenant data, MCP, or a
   shared process-wide resource).  A route that is added, renamed or removed without touching the registry fails CI, so isolation coverage cannot rot.
2. **Operator-only routes deny tenants** (and unauthenticated callers are refused everywhere that is not public).
3. **Tenant-data routes show bob nothing of alice's**, and for every id-addressed route the response to alice's id is *indistinguishable* from the response to an id
   that never existed (no "exists but not yours" oracle), for reads, writes, supersedes and cursors.
4. **Known gaps are written down as strict xfails**, so the day one is fixed the test flips and demands its promotion to a plain test.

The tenants today come from OAuth; per-tenant API keys (RFC phase 1) will add a second fixture over the same cases.  The file runs on the JSON store by default
and on the Postgres row store (forced RLS) when JARVIS_TEST_BACKEND=postgres, which is how CI's test-postgres job runs the whole suite.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

import app.auth as auth
from app.identity import Principal
from app.main import app

MARK = "isolation-marker-bluebird-7c1e"      # appears only in alice's data, never in a query (a response echoes its own query, which is not a leak)
SEARCH = "zephyrquartz"                       # the word queries use: it matches alice's records but is not their marker
GHOST = "mem-000000000000"                    # a well-formed id that exists for nobody
GHOST_ARTIFACT = "art-000000000000-s1"

OPERATOR, PUBLIC, NEUTRAL, ISOLATED, MCP, SHARED = "operator-only", "public", "authenticated-neutral", "tenant-data", "mcp", "shared-resource"

# ------------------------------------------------------------------------------------------------------------- the registry
REGISTRY: dict[tuple[str, str], tuple[str, str]] = {}


def _classify(cls: str, why: str, rows: str) -> None:
    for line in rows.strip().splitlines():
        method, path = line.split()
        assert (method, path) not in REGISTRY, f"{method} {path} classified twice"
        REGISTRY[(method, path)] = (cls, why)


_classify(OPERATOR, "reads or writes the whole ledger's integrity machinery: only the operator key (never a tenant) may call it", """
GET /api/jarvis/attestations
POST /api/jarvis/attestations
GET /api/jarvis/attestations/head
GET /api/jarvis/attestations/pending
GET /api/jarvis/attestations/verify
GET /api/jarvis/blocks
GET /api/jarvis/blocks/head
POST /api/jarvis/blocks/seal
GET /api/jarvis/blocks/verify
GET /api/jarvis/blocks/{height}
POST /api/jarvis/evidence
GET /api/jarvis/replay/contracts
GET /api/jarvis/replay/events
GET /api/jarvis/replay/receipts
POST /api/jarvis/replay/receipts
GET /api/jarvis/replay/receipts/{receipt_id}
GET /api/jarvis/replay/receipts/{receipt_id}/verify
GET /api/jarvis/replay/state
GET /api/jarvis/trust
GET /api/jarvis/trust/statements
POST /api/jarvis/trust/statements
""")
_classify(OPERATOR, "the server-side logs describe every caller: operator only until they are per tenant", """
GET /api/jarvis/tools/calls
GET /api/jarvis/tools/calls/verify
POST /api/jarvis/drift/check
GET /api/jarvis/drift/log
GET /api/jarvis/drift/log/verify
POST /api/jarvis/tools/emr_drift_check
""")
_classify(OPERATOR, "nx-search reads the host filesystem: operator only", """
POST /api/jarvis/memory/external-search
POST /api/jarvis/memory/external/ask
POST /api/jarvis/memory/external/describe
POST /api/jarvis/memory/external/forget
POST /api/jarvis/memory/external/prune
POST /api/jarvis/memory/external/reindex
POST /api/jarvis/memory/external/remember
POST /api/jarvis/memory/external/scan
POST /api/jarvis/memory/external/serve
POST /api/jarvis/memory/external/serve/stop
POST /api/jarvis/memory/external/spatialize
GET /api/jarvis/memory/external/stats
POST /api/jarvis/memory/external/watch
POST /api/jarvis/memory/external/watch/stop
POST /api/jarvis/memory/promote
GET /api/jarvis/memory/unified
""")
_classify(PUBLIC, "carries no tenant data: service metadata, the OAuth discovery documents, health, the API schema and static policy pages", """
GET /
GET /.well-known/oauth-protected-resource
GET /.well-known/oauth-protected-resource/mcp
GET /docs
GET /docs/oauth2-redirect
GET /health
GET /openapi.json
GET /privacy
GET /ready
GET /redoc
GET /terms
""")
_classify(NEUTRAL, "needs a credential but returns the same thing to every tenant (the tool catalog)", """
GET /api/jarvis/tools
""")
_classify(ISOLATED, "a tenant's own records, evidence, board, short-term memory, graph or search results: scoped by the authenticated tenant", """
GET /api/jarvis/evidence/{evidence_id}
GET /api/jarvis/evidence/{evidence_id}/verify
GET /api/jarvis/memory
POST /api/jarvis/memory
GET /api/jarvis/memory/active
POST /api/jarvis/memory/amul/anchor
GET /api/jarvis/memory/amul/artifacts/{artifact_id}
GET /api/jarvis/memory/amul/field/status
POST /api/jarvis/memory/amul/field/verify
GET /api/jarvis/memory/amul/lineage/{memory_id}
GET /api/jarvis/memory/board
PATCH /api/jarvis/memory/board
POST /api/jarvis/memory/board
GET /api/jarvis/memory/conflicts
POST /api/jarvis/memory/emr/correct
POST /api/jarvis/memory/emr/excite
POST /api/jarvis/memory/emr/reinforce
GET /api/jarvis/memory/emr/status
POST /api/jarvis/memory/graph/bfs
POST /api/jarvis/memory/graph/components
POST /api/jarvis/memory/graph/related
POST /api/jarvis/memory/graph/shortest-path
GET /api/jarvis/memory/graph/stats
GET /api/jarvis/memory/history/verify
GET /api/jarvis/memory/latest
POST /api/jarvis/memory/pipeline
GET /api/jarvis/memory/retrieve
GET /api/jarvis/memory/search
DELETE /api/jarvis/memory/stm
GET /api/jarvis/memory/stm
GET /api/jarvis/memory/stm/context
POST /api/jarvis/memory/stm/expand
DELETE /api/jarvis/memory/{memory_id}
GET /api/jarvis/memory/{memory_id}
PATCH /api/jarvis/memory/{memory_id}
GET /api/jarvis/memory/{memory_id}/history
GET /api/jarvis/memory/{memory_id}/resolve
POST /api/jarvis/tools/emr_fetch
POST /api/jarvis/tools/emr_latest
POST /api/jarvis/tools/emr_recall
POST /api/jarvis/tools/emr_remember
POST /api/jarvis/tools/emr_search
POST /api/jarvis/tools/emr_search_ledger
POST /api/jarvis/tools/emr_upsert
POST /api/jarvis/tools/fetch
POST /api/jarvis/tools/search
""")
_classify(MCP, "the MCP transport: the same tools as above through tools/call, authenticated per request", """
DELETE /mcp
GET /mcp
POST /mcp
DELETE /mcp/
GET /mcp/
POST /mcp/
""")
_classify(SHARED, "a process-wide resource that is not keyed by tenant (the inference backend, the RAG index): audited separately, see the tests at the end", """
POST /api/jarvis/llm/generate
GET /api/jarvis/llm/status
POST /api/jarvis/rag/documents
GET /api/jarvis/rag/log
POST /api/jarvis/rag/maintenance
POST /api/jarvis/rag/query
GET /api/jarvis/rag/status
""")


def _served_routes() -> set[tuple[str, str]]:
    out = set()
    for route in app.routes:
        for method in (getattr(route, "methods", None) or set()) - {"HEAD", "OPTIONS"}:
            out.add((method, route.path))
    return out


def test_every_served_route_is_classified_and_every_classification_is_served():
    served, known = _served_routes(), set(REGISTRY)
    assert not served - known, f"unclassified routes (add them to REGISTRY with a class and, if tenant data, a cross-tenant case): {sorted(served - known)}"
    assert not known - served, f"classified routes that are no longer served (remove them): {sorted(known - served)}"


def route_of(method: str, concrete_path: str) -> tuple[str, str] | None:
    """The registry key (method, route template) that a concrete request path resolves to, using the app's own router."""
    from starlette.routing import Match

    scope = {"type": "http", "method": method, "path": concrete_path.split("?", 1)[0], "root_path": "", "query_string": b"", "headers": []}
    for route in app.routes:
        match, _ = route.matches(scope)
        if match == Match.FULL:
            return method, route.path
    return None


# ----------------------------------------------------------------------------------------------------------------- fixtures
@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("JARVIS_AUTH_MODE", "oauth")
    monkeypatch.setenv("JARVIS_PUBLIC_BASE_URL", "https://memory.example")
    monkeypatch.setenv("JARVIS_MEMORY_WRITE_ENABLED", "true")
    monkeypatch.setenv("JARVIS_MCP_WRITE_ENABLED", "true")        # the write tools must be past their gate, or their isolation is never exercised
    monkeypatch.setenv("JARVIS_CURSOR_HMAC_KEY", "isolation-matrix-cursor-key-0123456789")
    monkeypatch.delenv("JARVIS_API_KEY", raising=False)

    def fake_validate(token: str, *, required_scope: str = "memory.read") -> Principal:
        return Principal(subject=token, scopes=frozenset({"memory.read", "memory.write"}), issuer="https://issuer.example")

    monkeypatch.setattr(auth, "validate_access_token", fake_validate)
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


def h(who: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {who}"}


def mk(client, who, content, **extra):
    r = client.post("/api/jarvis/memory", headers=h(who), json={"content": content, "source_agent": "t", "session_id": "s", "type": "fact", **extra})
    assert r.status_code == 200, r.text
    return r.json()["memory"]


@pytest.fixture
def world(client):
    """Alice has records (one superseding another), an anchored AMUL artifact, short-term memory and a board; bob has nothing."""
    first = mk(client, "alice", f"alice private note {MARK} alpha {SEARCH}")
    second = mk(client, "alice", f"alice private note {MARK} beta {SEARCH}", supersedes=first["id"])
    third = mk(client, "alice", f"alice private note {MARK} gamma {SEARCH}")
    anchored = client.post("/api/jarvis/memory/amul/anchor", headers=h("alice"), json={"memory_id": first["id"]})
    assert anchored.status_code == 200, anchored.text
    created = anchored.json().get("created") or []                 # AnchorReport: the new artifact ids
    assert created, f"anchoring produced no artifact: {anchored.text}"
    artifact = created[0]
    client.post("/api/jarvis/memory/emr/excite", headers=h("alice"), json={"query": SEARCH, "session_key": "default", "theta_promote": 0.0})
    client.post("/api/jarvis/memory/board", headers=h("alice"), json={"summary": f"alice board {MARK}"})
    reinforced = client.post("/api/jarvis/memory/emr/reinforce", headers=h("alice"), json={
        "memory_ids": [first["id"]], "outcome": {"signal": "positive", "source": "user", "outcome_id": f"outcome-{MARK}"}})
    assert reinforced.status_code == 200, reinforced.text                          # alice now has reinforcement state, which the status view reports
    return {"first": first["id"], "second": second["id"], "third": third["id"], "artifact": artifact}


VOLATILE = {"tenant", "result_digest", "ledger_head", "head", "ts", "created_at", "updated_at", "generated_at", "checked_at", "duration_ms"}
# Fields dropped for ONE case only. `path` is no longer masked everywhere: a server path in a response is something to see (two of them are known gaps below).
CASE_VOLATILE = {"amul-status": {"path"}}                 # the AMUL field's own directory, which holds the tenant's hash and so differs between any two tenants
# Cases that currently DO leak across tenants. They are excluded from the generic comparison and pinned by a strict xfail below, so the day they are fixed
# the xfail flips and the case is promoted back into the generic test.
KNOWN_LEAKS = {"emr-status"}
GENERATED = {"id", "content_sha256", "version"}   # a write that succeeds returns an id, a hash and a clock of its own: expected to differ between two calls


def norm(resp, *ids: str) -> tuple[int, str]:
    """(status, body with the ids that were asked about masked and the fields a successful write generates dropped), so two responses can be compared for
    indistinguishability."""
    try:
        text = json.dumps(_drop(resp.json(), VOLATILE | GENERATED), sort_keys=True)
    except ValueError:
        text = resp.text
    for x in ids:
        text = text.replace(x, "<ID>")
    return resp.status_code, text


def _drop(obj, keys):
    if isinstance(obj, dict):
        return {k: _drop(v, keys) for k, v in obj.items() if k not in keys}
    if isinstance(obj, list):
        return [_drop(v, keys) for v in obj]
    return obj


def assert_nothing_of_alices(resp, world=None, asked: tuple[str, ...] = ()):
    """Bob's response must not carry alice's marker text nor any id of hers. An id bob himself sent (`asked`) may be echoed back: that is his own input."""
    text = resp.text
    assert MARK not in text, f"alice's text leaked: {text[:300]}"
    for v in (world or {}).values():
        if v and v not in asked:
            assert v not in text, f"{v} leaked: {text[:300]}"


# ---------------------------------------------------------------------------------------- operator-only routes deny tenants
def _concrete(path: str) -> str:
    return (path.replace("{height}", "1").replace("{receipt_id}", "eo:sha256:" + "0" * 64).replace("{evidence_id}", "eo:sha256:" + "0" * 64)
            .replace("{memory_id}", GHOST).replace("{artifact_id}", GHOST_ARTIFACT))


OPERATOR_ROWS = sorted(k for k, (cls, _) in REGISTRY.items() if cls == OPERATOR)
NON_PUBLIC_ROWS = sorted(k for k, (cls, _) in REGISTRY.items() if cls != PUBLIC)
PUBLIC_ROWS = sorted(k for k, (cls, _) in REGISTRY.items() if cls == PUBLIC)


def _body_for(method: str):
    return {} if method in ("POST", "PATCH", "PUT") else None


# These three loop over the whole registry inside one test and report every failure together: each pytest case pays for a fresh store (on Postgres, a fresh
# schema and migration), so one test per route would add minutes to CI for no extra coverage.
def test_operator_only_routes_deny_every_tenant(client):
    failures = []
    for who in ("alice", "bob"):
        for method, path in OPERATOR_ROWS:
            r = client.request(method, _concrete(path), headers=h(who), json=_body_for(method))
            if r.status_code != 403 or MARK in r.text:
                failures.append(f"{method} {path} as {who}: {r.status_code} {r.text[:120]}")
    assert not failures, "operator-only routes answered a tenant:\n" + "\n".join(failures)


def test_a_request_with_no_credential_is_refused_everywhere_that_is_not_public(client):
    failures = []
    for method, path in NON_PUBLIC_ROWS:
        r = client.request(method, _concrete(path), json=_body_for(method))
        if r.status_code not in (401, 403) or MARK in r.text:
            failures.append(f"{method} {path}: {r.status_code} {r.text[:120]}")
    assert not failures, "answered with no credential:\n" + "\n".join(failures)


def test_public_routes_carry_nothing_of_a_tenants(client, world):
    failures = []
    for method, path in PUBLIC_ROWS:
        for headers in ({}, h("bob"), h("alice")):
            r = client.request(method, path, headers=headers)
            if r.status_code != 200 or MARK in r.text or "mem-" in r.text:
                failures.append(f"{method} {path}: {r.status_code}")
    assert not failures, "\n".join(failures)


@pytest.mark.parametrize("who", ["alice", "bob"])
def test_the_neutral_catalog_is_identical_for_every_tenant(client, who):
    first = client.get("/api/jarvis/tools", headers=h("alice"))
    again = client.get("/api/jarvis/tools", headers=h(who))
    assert first.status_code == again.status_code == 200 and first.json() == again.json()


# ------------------------------------------------------------------------------ id-addressed routes: "not yours" is "not found"
BODY = {"content": "bob writes this", "source_agent": "t", "session_id": "s", "type": "fact"}
APPROVED = {"user_requested": True, "user_statement": "the operator asked for this write"}   # what the write tools require beyond the gate flag


def id_cases(i: str, art: str | None) -> dict[str, tuple[str, str, dict | None]]:
    return {
        "get": ("GET", f"/api/jarvis/memory/{i}", None),
        "patch": ("PATCH", f"/api/jarvis/memory/{i}", {"subject": "hijacked", "expected_version": 1}),
        "delete": ("DELETE", f"/api/jarvis/memory/{i}", None),
        "resolve": ("GET", f"/api/jarvis/memory/{i}/resolve", None),
        "history": ("GET", f"/api/jarvis/memory/{i}/history", None),
        "amul-lineage": ("GET", f"/api/jarvis/memory/amul/lineage/{i}", None),
        "amul-anchor": ("POST", "/api/jarvis/memory/amul/anchor", {"memory_id": i}),
        "graph-related": ("POST", "/api/jarvis/memory/graph/related", {"memory_id": i}),
        "graph-bfs": ("POST", "/api/jarvis/memory/graph/bfs", {"start_id": i}),
        "graph-path": ("POST", "/api/jarvis/memory/graph/shortest-path", {"source": i, "target": i}),
        "stm-expand": ("POST", "/api/jarvis/memory/stm/expand", {"memory_id": i}),
        "reinforce": ("POST", "/api/jarvis/memory/emr/reinforce", {"memory_ids": [i], "outcome": {"signal": "positive", "source": "user", "outcome_id": "outcome-1"}}),
        "correct": ("POST", "/api/jarvis/memory/emr/correct", {"memory_ids": [i], "correction": {"source": "operator", "correction_id": "correction-1", "reason": "bob corrects"}}),
        "supersede-api": ("POST", "/api/jarvis/memory", {**BODY, "supersedes": i}),
        "supersede-tool": ("POST", "/api/jarvis/tools/emr_remember", {**BODY, **APPROVED, "supersedes": i}),
        "tool-fetch": ("POST", "/api/jarvis/tools/fetch", {"id": i}),
        "tool-emr_fetch": ("POST", "/api/jarvis/tools/emr_fetch", {"id": i}),
        "amul-artifact": ("GET", f"/api/jarvis/memory/amul/artifacts/{art or GHOST_ARTIFACT}", None),
        # evidence objects can only be created with the operator key today (a tenant gets 403), so there is no tenant evidence to cross: both ids are well-formed
        # and unknown, and the case proves the read path answers a tenant the same way every time and leaks nothing. Cross-tenant evidence becomes testable with phase 1.
        "evidence-get": ("GET", f"/api/jarvis/evidence/eo:sha256:{'0' * 64}", None),
        "evidence-verify": ("GET", f"/api/jarvis/evidence/eo:sha256:{'0' * 64}/verify", None),
        "tool-upsert": ("POST", "/api/jarvis/tools/emr_upsert", {**BODY, **APPROVED, "id": i}),
    }


CASE_NAMES = sorted(id_cases(GHOST, None))
VALIDATES_FIRST: set[str] = set()   # a case allowed to answer 422 (none today): an id case that stops at validation has not reached the lookup it claims to test


def _alice_snapshot(client, world):
    return {k: client.get(f"/api/jarvis/memory/{v}", headers=h("alice")).text for k, v in world.items() if k != "artifact" and v}


def test_alices_id_is_indistinguishable_from_an_id_that_never_existed(client, world):
    before = _alice_snapshot(client, world)
    failures = []
    for case in CASE_NAMES:
        # two fresh tenants in identical (empty) state: one is shown alice's id, the other an id that exists for nobody
        method, path, body = id_cases(world["first"], world["artifact"])[case]
        theirs = client.request(method, path, headers=h(f"bob-one-{case}"), json=body)
        method, path, body = id_cases(GHOST, GHOST_ARTIFACT)[case]
        nobodys = client.request(method, path, headers=h(f"bob-two-{case}"), json=body)
        if 422 in (theirs.status_code, nobodys.status_code) and case not in VALIDATES_FIRST:
            failures.append(f"{case}: the request never got past validation ({theirs.status_code} {theirs.text[:140]}), so it proves nothing about isolation")
            continue
        if norm(theirs, world["first"]) != norm(nobodys, GHOST):
            failures.append(f"{case}: bob can tell alice's id from a nonexistent one: {theirs.status_code} {theirs.text[:140]} | {nobodys.status_code} {nobodys.text[:140]}")
        try:
            assert_nothing_of_alices(theirs, world, asked=(world["first"],))
        except AssertionError as exc:
            failures.append(f"{case}: {exc}")
        if _alice_snapshot(client, world) != before:
            failures.append(f"{case}: bob's call changed alice's records")
    assert not failures, "\n".join(failures)


def test_the_isolation_cases_are_not_vacuous(client, world):
    """Alice does see her own record (so 'indistinguishable from nonexistent' is a real statement) and the supersede chain exists."""
    own = client.get(f"/api/jarvis/memory/{world['first']}", headers=h("alice"))
    assert own.status_code == 200 and MARK in own.text
    assert client.get(f"/api/jarvis/memory/{GHOST}", headers=h("alice")).status_code == 404
    assert client.post("/api/jarvis/tools/fetch", headers=h("alice"), json={"id": world["second"]}).status_code == 200
    assert client.get(f"/api/jarvis/memory/amul/artifacts/{world['artifact']}", headers=h("alice")).status_code == 200   # the artifact case reads a real one
    assert world["artifact"] != GHOST_ARTIFACT


# ------------------------------------------------------------------------- list / search / read-model routes: bob sees nothing of alice's
LIST_CASES = {
    "list": ("GET", "/api/jarvis/memory", None),
    "list-by-query": ("GET", f"/api/jarvis/memory?query={SEARCH}", None),
    "retrieve": ("GET", f"/api/jarvis/memory/retrieve?query={SEARCH}", None),
    "search": ("GET", f"/api/jarvis/memory/search?query={SEARCH}", None),
    "active": ("GET", f"/api/jarvis/memory/active?query={SEARCH}", None),
    "latest": ("GET", "/api/jarvis/memory/latest?limit=10", None),
    "conflicts": ("GET", "/api/jarvis/memory/conflicts", None),
    "graph-stats": ("GET", "/api/jarvis/memory/graph/stats", None),
    "graph-components": ("POST", "/api/jarvis/memory/graph/components", {}),
    "stm": ("GET", "/api/jarvis/memory/stm", None),
    "stm-context": ("GET", "/api/jarvis/memory/stm/context", None),
    "emr-status": ("GET", "/api/jarvis/memory/emr/status", None),
    "board": ("GET", "/api/jarvis/memory/board", None),
    "amul-status": ("GET", "/api/jarvis/memory/amul/field/status", None),
    "history-verify": ("GET", "/api/jarvis/memory/history/verify", None),            # 501 on the JSON store, a per-tenant verification on Postgres
    "tool-search": ("POST", "/api/jarvis/tools/search", {"query": SEARCH}),
    "tool-emr_search": ("POST", "/api/jarvis/tools/emr_search", {"query": SEARCH}),
    "tool-emr_search_ledger": ("POST", "/api/jarvis/tools/emr_search_ledger", {"query": SEARCH}),
    "tool-emr_latest": ("POST", "/api/jarvis/tools/emr_latest", {"limit": 10}),
    "tool-emr_recall": ("POST", "/api/jarvis/tools/emr_recall", {"intent": "constitutional", "query": SEARCH}),
    "pipeline": ("POST", "/api/jarvis/memory/pipeline", {"query": SEARCH, "session_id": "s", "session_key": "default"}),
}


def strip(obj):
    """Drop the fields that legitimately differ between two tenants (their own hash, digests, clocks) so the rest can be compared."""
    if isinstance(obj, dict):
        return {k: strip(v) for k, v in obj.items() if k not in VOLATILE}
    if isinstance(obj, list):
        return [strip(v) for v in obj]
    return obj


def _body(resp, case: str = ""):
    try:
        return _drop(strip(resp.json()), CASE_VOLATILE.get(case, set()))
    except ValueError:
        return resp.text


def test_a_second_tenant_sees_nothing_of_the_first(client, world):
    """Bob has written nothing, so every read must look exactly like a brand-new tenant's: that also catches aggregate-only leaks (counts, sizes) that carry no id."""
    failures = []
    for case, (method, path, body) in sorted(LIST_CASES.items()):
        if case in KNOWN_LEAKS:
            continue
        r = client.request(method, path, headers=h("bob"), json=body)
        empty = client.request(method, path, headers=h("carol-who-never-wrote"), json=body)
        if r.status_code not in (200, 404, 501):
            failures.append(f"{case}: {r.status_code} {r.text[:160]}")
            continue
        try:
            assert_nothing_of_alices(r, world)
        except AssertionError as exc:
            failures.append(f"{case}: {exc}")
        if (r.status_code, _body(r, case)) != (empty.status_code, _body(empty, case)):
            failures.append(f"{case}: bob's response differs from a brand-new tenant's:\n   bob   {r.status_code} {str(_body(r, case))[:200]}\n   empty {empty.status_code} {str(_body(empty, case))[:200]}")
    assert not failures, "\n".join(failures)


def test_the_aggregate_reads_are_not_vacuous_alice_sees_her_own_counts(client, world):
    stats = client.get("/api/jarvis/memory/graph/stats", headers=h("alice")).json()
    assert stats["node_count"] >= 3, stats
    assert client.get("/api/jarvis/memory/graph/stats", headers=h("carol-who-never-wrote")).json()["node_count"] == 0


def test_the_same_reads_do_return_alices_data_to_alice(client, world):
    failures = []
    for case in ("list", "retrieve", "search", "latest", "tool-emr_search_ledger", "tool-emr_latest"):
        method, path, body = LIST_CASES[case]
        r = client.request(method, path, headers=h("alice"), json=body)
        if not (r.status_code == 200 and (MARK in r.text or world["first"] in r.text or world["third"] in r.text)):
            failures.append(f"{case}: control read found nothing: {r.text[:160]}")
    assert not failures, "\n".join(failures)


MUTATION_CASES = {
    "board-post": ("POST", "/api/jarvis/memory/board", {"summary": "bob overwrote it"}),
    "board-patch": ("PATCH", "/api/jarvis/memory/board", {"summary": "bob overwrote it"}),
    "stm-clear": ("DELETE", "/api/jarvis/memory/stm", None),
    "emr-excite": ("POST", "/api/jarvis/memory/emr/excite", {"query": SEARCH, "session_key": "default", "theta_promote": 0.0}),
    "amul-verify": ("POST", "/api/jarvis/memory/amul/field/verify", {}),
}


def _alice_state(client, world):
    return {"records": _alice_snapshot(client, world), "board": client.get("/api/jarvis/memory/board", headers=h("alice")).text,
            "stm": client.get("/api/jarvis/memory/stm/context", headers=h("alice")).text, "graph": client.get("/api/jarvis/memory/graph/stats", headers=h("alice")).text}


def test_bob_s_writes_that_are_not_addressed_by_id_change_nothing_of_alices(client, world):
    before = _alice_state(client, world)
    failures = []
    for case, (method, path, body) in sorted(MUTATION_CASES.items()):
        r = client.request(method, path, headers=h("bob"), json=body)
        if r.status_code >= 500 or r.status_code == 422:
            failures.append(f"{case}: {r.status_code} {r.text[:120]} (a mutation case must get past validation)")
        try:
            assert_nothing_of_alices(r, world)
        except AssertionError as exc:
            failures.append(f"{case}: {exc}")
        if _alice_state(client, world) != before:
            failures.append(f"{case}: changed alice's state")
    assert not failures, "\n".join(failures)


def test_the_write_tools_are_open_so_their_isolation_is_really_exercised(client, world):
    """The id cases above only mean something if the write tools get past their gate (flag on, user_requested): an ordinary write by bob must be accepted."""
    for tool_name, body in (("emr_remember", {**BODY, **APPROVED}), ("emr_upsert", {**BODY, **APPROVED, "id": GHOST})):
        r = client.post(f"/api/jarvis/tools/{tool_name}", headers=h("bob-control"), json=body)
        assert "disabled" not in r.text and "user_requested" not in r.text, f"{tool_name} is still behind its gate: {r.text[:200]}"
    ok = client.post("/api/jarvis/tools/emr_remember", headers=h("bob-control"), json={**BODY, **APPROVED})
    assert ok.status_code == 200 and ok.json().get("accepted") is True, ok.text[:300]


def test_bob_cannot_change_alices_short_term_memory_or_board(client, world):
    ctx = client.get("/api/jarvis/memory/stm/context", headers=h("alice")).text
    board = client.get("/api/jarvis/memory/board", headers=h("alice")).text
    assert MARK in board
    assert client.delete("/api/jarvis/memory/stm", headers=h("bob")).status_code == 200
    client.patch("/api/jarvis/memory/board", headers=h("bob"), json={"summary": "bob overwrote it"})
    client.post("/api/jarvis/memory/board", headers=h("bob"), json={"summary": "bob overwrote it"})
    assert client.get("/api/jarvis/memory/stm/context", headers=h("alice")).text == ctx
    assert client.get("/api/jarvis/memory/board", headers=h("alice")).text == board


def test_a_cursor_minted_for_alice_is_refused_for_bob_exactly_like_a_malformed_one(client, world):
    page = client.get("/api/jarvis/memory/latest?limit=1", headers=h("alice")).json()
    cursor = page.get("next_cursor")
    assert cursor, f"alice should have more than one page: {page}"
    stolen = client.get(f"/api/jarvis/memory/latest?limit=1&cursor={cursor}", headers=h("bob-one"))
    garbage = client.get("/api/jarvis/memory/latest?limit=1&cursor=garbage", headers=h("bob-two"))
    assert norm(stolen, cursor) == norm(garbage, "garbage"), f"{stolen.status_code} {stolen.text[:200]} vs {garbage.status_code} {garbage.text[:200]}"
    assert_nothing_of_alices(stolen, world, asked=())


# ------------------------------------------------------------------------------------------------------ the same through MCP
MCP_HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


def mcp_call(client, who, name, arguments):
    r = client.post("/mcp", headers={**MCP_HEADERS, **h(who)}, json={"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": {"name": name, "arguments": arguments}})
    return r


def test_mcp_tools_call_cannot_tell_alices_id_from_a_nonexistent_one(client, world):
    failures = []
    for tool in ("emr_fetch", "fetch"):
        theirs = mcp_call(client, f"bob-one-{tool}", tool, {"id": world["first"]})
        nobodys = mcp_call(client, f"bob-two-{tool}", tool, {"id": GHOST})
        if theirs.status_code != nobodys.status_code or norm(theirs, world["first"]) != norm(nobodys, GHOST):
            failures.append(f"{tool}: {theirs.text[:200]} | {nobodys.text[:200]}")
        try:
            assert_nothing_of_alices(theirs, world, asked=(world["first"],))
        except AssertionError as exc:
            failures.append(f"{tool}: {exc}")
    assert not failures, "\n".join(failures)


def test_mcp_search_shows_bob_nothing_of_alices(client, world):
    r = mcp_call(client, "bob", "emr_search_ledger", {"query": SEARCH})
    assert_nothing_of_alices(r, world)
    assert MARK in mcp_call(client, "alice", "emr_search_ledger", {"query": SEARCH}).text


# --------------------------------------------------------------------------------- shared process-wide resources: written-down gaps
def test_the_rag_index_refuses_every_tenant_until_its_access_control_is_configured(client, world):
    for method, path in (("GET", "/api/jarvis/rag/status"), ("GET", "/api/jarvis/rag/log"), ("POST", "/api/jarvis/rag/query"),
                         ("POST", "/api/jarvis/rag/documents"), ("POST", "/api/jarvis/rag/maintenance")):
        r = client.request(method, path, headers=h("bob"), json={} if method == "POST" else None)
        assert r.status_code == 503, f"{method} {path}: {r.status_code} {r.text[:200]}"
        assert_nothing_of_alices(r, world)


@pytest.mark.xfail(strict=True, reason="GAP: the inference backend's address is shown to any tenant (phase 0 should make llm/status operator-only or neutral)")
def test_a_tenant_does_not_see_the_inference_backend_address(client):
    r = client.get("/api/jarvis/llm/status", headers=h("bob"))
    assert r.status_code in (403, 404) or "url" not in json.dumps(r.json()).lower()


@pytest.mark.xfail(strict=True, reason="GAP: any tenant can spend the shared inference backend (no per-tenant quota or operator gate yet)")
def test_a_tenant_cannot_spend_the_shared_inference_backend(client):
    r = client.post("/api/jarvis/llm/generate", headers=h("bob"), json={"user": "hello"})
    assert r.status_code in (403, 429)


@pytest.mark.xfail(strict=True, reason="GAP: the AMUL field status shows the server's filesystem path (data/tenants/<hash>/...) to the tenant")
def test_a_tenant_is_not_told_the_servers_filesystem_layout(client):
    r = client.get("/api/jarvis/memory/amul/field/status", headers=h("bob"))
    assert "data/" not in r.text and "/tenants/" not in r.text


def test_every_tenant_data_route_is_exercised_by_a_cross_tenant_case():
    """The registry alone only says a route was classified. A route classified `tenant-data` must also be hit by at least one of the cases in this file, so it
    cannot enter the registry (or stay in it) without a test that crosses tenants."""
    exercised = set()
    for i, art in ((GHOST, GHOST_ARTIFACT), ("mem-111111111111", "art-111111111111-s1")):
        exercised |= {route_of(m, p) for m, p, _ in id_cases(i, art).values()}
    exercised |= {route_of(m, p) for m, p, _ in LIST_CASES.values()}
    exercised |= {route_of(m, p) for m, p, _ in MUTATION_CASES.values()}
    exercised |= {("POST", "/api/jarvis/tools/emr_remember"), ("POST", "/mcp")}
    exercised.discard(None)
    isolated = {k for k, (cls, _) in REGISTRY.items() if cls == ISOLATED}
    missing = sorted(isolated - exercised)
    assert not missing, f"tenant-data routes with no cross-tenant case (add one to id_cases, LIST_CASES or MUTATION_CASES): {missing}"
    stray = sorted(k for k in exercised if k not in REGISTRY)
    assert not stray, f"cases that hit routes the registry does not know: {stray}"


@pytest.mark.xfail(strict=True, reason="REAL LEAK: EMR reinforcement state is a process-global map keyed by memory id only (app/emr.py _REINFORCEMENT, one shared sidecar file), "
                                       "and GET /api/jarvis/memory/emr/status reports every tenant's entries, including memory ids and outcome ids")
def test_emr_status_does_not_show_another_tenants_reinforcement(client, world):
    r = client.get("/api/jarvis/memory/emr/status", headers=h("bob"))
    assert r.status_code == 200
    assert MARK not in r.text and world["first"] not in r.text, "bob can read alice's reinforcement state"
    empty = client.get("/api/jarvis/memory/emr/status", headers=h("carol-who-never-wrote"))
    assert _body(r) == _body(empty)


@pytest.mark.xfail(strict=True, reason="GAP: the EMR status shows the server's filesystem path of the dynamics sidecar (dynamics.sidecar.path = data/emr-dynamics.json) to any tenant")
def test_a_tenant_is_not_told_the_emr_sidecar_path(client):
    r = client.get("/api/jarvis/memory/emr/status", headers=h("bob"))
    assert "data/" not in r.text and "emr-dynamics" not in r.text
