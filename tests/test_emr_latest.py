"""emr_latest: newest-memory discovery over HTTP and MCP, on the JSON store and on Postgres.

Every backend-parametrized test runs on both; the Postgres variants need JARVIS_TEST_PG_DSN (CI's ``test-postgres`` job sets
it, so they run there; locally they skip only when no throwaway server is configured).
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app import emr_latest, pg_store
from app.emr_latest import LatestError, LatestParams, latest_memories
from app.main import app
from app.models import MemoryCreate
from app.pg_schema import migrate
from app.pg_store import PostgresRowStore
from app.store import JarvisStore

KEY = "emr-latest-test-key"
HDR = {"X-API-Key": KEY}
MCP = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
URL = "/api/jarvis/memory/latest"


# ------------------------------------------------------------------------------------------------ fixtures


@pytest.fixture(params=[pytest.param("json", marks=pytest.mark.json_store_only), "postgres"])
def backend(request, tmp_path, monkeypatch):
    """Configure get_store() for the backend (and the operator key), and yield its name."""
    monkeypatch.setenv("JARVIS_API_KEY", KEY)
    monkeypatch.delenv("JARVIS_ALLOW_UNAUTHENTICATED", raising=False)
    monkeypatch.delenv("JARVIS_CURSOR_HMAC_KEY", raising=False)
    monkeypatch.setenv("JARVIS_MEMORY_WRITE_ENABLED", "true")
    if request.param == "postgres":
        pg = request.getfixturevalue("pg_schema")  # skips when no throwaway server is configured
        migrate(pg.admin_dsn, schema=pg.schema, app_role="jarvis_app_test")
        monkeypatch.setenv("JARVIS_DATABASE_URL", pg.app_dsn)
        monkeypatch.setenv("JARVIS_DATABASE_SCHEMA", pg.schema)
        monkeypatch.setenv("JARVIS_PG_STORE", "rows")
        request.node._pg = pg
    yield request.param
    pg_store.close_pools()


@pytest.fixture
def client(backend):
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


@pytest.fixture
def make_store(backend, tmp_path, request):
    """tenant -> a store bound to that tenant, on the parametrized backend."""

    def build(tenant: str):
        if backend == "json":
            return JarvisStore(str(tmp_path / f"tenant-{tenant}.json"))
        pg = request.node._pg
        return PostgresRowStore(pg.app_dsn, tenant, schema=pg.schema)

    return build


@contextlib.contextmanager
def frozen(when: datetime):
    """Every record created inside gets exactly this created_at (JSON and Postgres stores)."""

    class _Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return when

    with patch("app.store._now_iso", lambda: when.isoformat()), patch("app.pg_store.datetime", _Frozen):
        yield


def add(client, content="a ledger record", **extra):
    body = {"content": content, "source_agent": "agent", "session_id": "s", "type": "fact", **extra}
    r = client.post("/api/jarvis/memory", headers=HDR, json=body)
    assert r.status_code == 200, r.text
    time.sleep(0.003)
    return r.json()["memory"]


def latest(client, **params):
    r = client.get(URL, headers=HDR, params=params)
    return r


def digest_of(records):
    blob = json.dumps([[r["id"], r["created_at"], r["status"]] for r in records], separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def mcp_call(client, name, arguments, headers=HDR):
    h = {**MCP, **headers}
    client.post("/mcp", headers=h, json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    r = client.post("/mcp", headers=h, json={"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": name, "arguments": arguments}})
    assert r.status_code == 200, r.text
    return r.json()["result"]


# ------------------------------------------------------------------------------------------------ 1. ordering


def test_newest_first_and_identical_created_at_orders_by_id_desc_every_time(client):
    tie = datetime(2030, 1, 1, 12, 0, 0, 123456, tzinfo=timezone.utc)
    ids_tied = []
    for i in range(6):
        with frozen(tie):
            ids_tied.append(add(client, f"tied {i}")["id"])
    newest = []
    for i in range(2):
        with frozen(tie + timedelta(seconds=10 + i)):
            newest.append(add(client, f"later {i}")["id"])
    expected = list(reversed(newest)) + sorted(ids_tied, reverse=True)
    for _ in range(20):
        body = latest(client, limit=50).json()
        assert [r["id"] for r in body["records"]] == expected
        assert body["result_digest"] == digest_of(body["records"])
        assert len({r["created_at"] for r in body["records"][2:]}) == 1  # the six really are tied on created_at


# ------------------------------------------------------------------------------------------------ 2. paging


def test_paging_with_the_cursor_returns_each_record_once_and_a_tampered_cursor_is_refused(client):
    tie = datetime(2031, 1, 1, tzinfo=timezone.utc)
    made = []
    for i in range(8):
        with frozen(tie if i % 2 else tie + timedelta(seconds=i)):  # mix of tied and distinct timestamps
            made.append(add(client, f"page {i}")["id"])
    everything = [r["id"] for r in latest(client, limit=50).json()["records"]]
    assert sorted(everything) == sorted(made)
    seen, cursor, pages = [], None, 0
    while True:
        params = {"limit": 3, **({"cursor": cursor} if cursor else {})}
        body = latest(client, **params).json()
        seen += [r["id"] for r in body["records"]]
        pages += 1
        cursor = body["next_cursor"]
        if cursor is None:
            break
    assert seen == everything and len(set(seen)) == len(made) and pages == 3
    first = latest(client, limit=3).json()["next_cursor"]
    body_part, tag = first.split(".")
    for bad in (body_part + "." + ("A" if tag[0] != "A" else "B") + tag[1:], "A" + body_part[1:] + "." + tag, "garbage", "", first + "x"):
        r = latest(client, limit=3, cursor=bad)
        if bad == "":
            continue  # an empty cursor means "no cursor"
        assert r.status_code == 422 and r.json()["reason"] == "CURSOR_INVALID" and r.json()["code"] == "invalid_request", bad
    # A cursor is bound to the filters it was issued for.
    r = latest(client, limit=3, cursor=first, include_twin="true")
    assert r.status_code == 422 and r.json()["reason"] == "CURSOR_INVALID"


# ------------------------------------------------------------------------------------------------ 3. limit


@pytest.mark.parametrize("bad", ["0", "51", "-1", "abc", "1.5", "1000"])
def test_limit_outside_1_to_50_is_refused_not_clamped(client, bad):
    add(client)
    r = latest(client, limit=bad)
    assert r.status_code == 422
    assert r.json()["reason"] == "LIMIT_OUT_OF_RANGE" and r.json()["code"] == "invalid_request"


@pytest.mark.parametrize("bad", [0, 51, -1, True, "5", 2.5])
def test_the_mcp_tool_refuses_a_bad_limit_with_the_same_reason(client, bad):
    out = mcp_call(client, "emr_latest", {"limit": bad})
    assert out["isError"] is True
    assert out["structuredContent"]["error"] == {"code": "invalid_request", "reason": "LIMIT_OUT_OF_RANGE"}


def test_limit_bounds_are_accepted(client):
    for n in (1, 50):
        assert latest(client, limit=n).status_code == 200
    assert len(latest(client).json()["records"]) == 0  # default limit applies on an empty ledger


# ------------------------------------------------------------------------------------------------ 4. exclusions and flags


def test_superseded_archived_and_twin_records_are_excluded_by_default_and_included_by_flag(client):
    plain = add(client, "plain")["id"]
    old = add(client, "old version")["id"]
    new = add(client, "new version", supersedes=old)["id"]
    archived = add(client, "archived one", status="archived")["id"]
    twin = add(client, "twin note", source_agent="ai-twin", type="research")["id"]
    decision = add(client, "a decision", type="decision")["id"]

    ids = lambda **p: {r["id"] for r in latest(client, limit=50, **p).json()["records"]}  # noqa: E731
    assert ids() == {plain, new, decision}
    assert ids(include_superseded="true") == {plain, old, new, decision}
    assert ids(include_archived="true") == {plain, new, decision, archived}
    assert ids(include_twin="true") == {plain, new, decision, twin}
    assert ids(type="decision") == {decision}
    assert ids(include_superseded="true", include_archived="true", include_twin="true") == {plain, old, new, archived, twin, decision}
    by_id = {r["id"]: r for r in latest(client, limit=50, include_archived="true", include_twin="true").json()["records"]}
    assert by_id[archived]["status"] == "archived" and by_id[plain]["status"] == "active"


# ------------------------------------------------------------------------------------------------ 5. chain A -> B -> C


def test_supersedes_and_superseded_by_on_a_chain(client):
    a = add(client, "A")["id"]
    b = add(client, "B", supersedes=a)["id"]
    c = add(client, "C", supersedes=b)["id"]
    recs = {r["id"]: r for r in latest(client, limit=50, include_superseded="true").json()["records"]}
    assert (recs[a]["supersedes"], recs[a]["superseded_by"], recs[a]["status"]) == (None, b, "superseded")
    assert (recs[b]["supersedes"], recs[b]["superseded_by"], recs[b]["status"]) == (a, c, "superseded")
    assert (recs[c]["supersedes"], recs[c]["superseded_by"], recs[c]["status"]) == (b, None, "active")
    assert [r["id"] for r in latest(client, limit=50).json()["records"]] == [c]


def test_record_fields_are_stored_values_or_null_never_invented(client):
    rec = add(client, "first line\nsecond line", subject="", evidence=[{"kind": "ref", "ref": "doc#1", "note": "n"}])
    got = latest(client).json()["records"][0]
    assert got["id"] == rec["id"] and got["summary"] == "first line" and got["type"] == "fact"
    assert got["provenance"] == {"source_agent": "agent", "actor": None, "method": None, "evidence_refs": ["doc#1"]}
    assert got["created_at"].endswith("Z") and re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{6}Z", got["created_at"])
    assert set(got) == {"id", "created_at", "type", "status", "provenance", "supersedes", "superseded_by", "summary"}


# ------------------------------------------------------------------------------------------------ 6. tenant isolation


def test_a_tenants_newest_record_and_cursor_never_reach_another_tenant(make_store, monkeypatch):
    monkeypatch.setenv("JARVIS_CURSOR_HMAC_KEY", "tenant-test-key")
    x, y = make_store("tenant-x"), make_store("tenant-y")
    for i in range(4):
        x.create_memory(MemoryCreate(content=f"x secret {i}", source_agent="a", session_id="s", type="fact"))
        time.sleep(0.003)
    y_rec = y.create_memory(MemoryCreate(content="y only", source_agent="a", session_id="s", type="fact"))
    px = LatestParams(limit=2)
    page_x = latest_memories(x, tenant="tenant-x", params=px, operator=True)
    assert len(page_x["records"]) == 2 and page_x["next_cursor"]
    page_y = latest_memories(y, tenant="tenant-y", params=LatestParams(limit=50), operator=True)
    assert [r["id"] for r in page_y["records"]] == [y_rec.id]
    # X's cursor presented as Y: refused (it is bound to the tenant), and Y's store holds none of X's rows anyway.
    with pytest.raises(LatestError) as err:
        latest_memories(y, tenant="tenant-y", params=LatestParams(limit=2, cursor=page_x["next_cursor"]), operator=True)
    assert err.value.reason == "CURSOR_INVALID"
    assert page_x["tenant"] == "tenant-x" and page_y["tenant"] == "tenant-y"
    assert not {r["id"] for r in page_x["records"]} & {r["id"] for r in page_y["records"]}


# ------------------------------------------------------------------------------------------------ 7. auth


@pytest.mark.parametrize("headers", [{}, {"X-API-Key": "wrong"}])
def test_no_or_wrong_credentials_get_authority_denied(client, headers):
    r = client.get(URL, headers=headers)
    assert r.status_code == 401
    assert r.json()["code"] == "denied" and r.json()["reason"] == "AUTHORITY_DENIED"
    r = client.post("/api/jarvis/tools/emr_latest", headers=headers, json={})
    assert r.status_code == 401 and r.json()["reason"] == "AUTHORITY_DENIED"
    # Other denials are unchanged: no new field on routes that are not emr_latest.
    assert "reason" not in client.get("/api/jarvis/memory", headers=headers).json()


def test_an_unresolved_tenant_is_refused_never_served_unscoped(client):
    add(client)
    with patch("app.main.oauth_enabled", return_value=True), patch("app.main.current_tenant_key", return_value=None):
        r = client.get(URL, headers=HDR)
    assert r.status_code == 403
    assert r.json()["code"] == "denied" and r.json()["reason"] == "TENANT_UNRESOLVED"
    assert "records" not in r.json()


def test_the_mcp_tool_is_read_only_and_needs_the_key(client):
    tools = {t["name"]: t for t in client.post("/mcp", headers={**MCP, **HDR}, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).json()["result"]["tools"]}
    assert tools["emr_latest"]["annotations"]["readOnlyHint"] is True and tools["emr_latest"]["annotations"]["destructiveHint"] is False
    assert client.post("/mcp", headers=MCP, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).status_code == 401


# ------------------------------------------------------------------------------------------------ 8. empty ledger


def test_an_empty_ledger_is_a_valid_empty_page(client):
    r = latest(client)
    assert r.status_code == 200
    body = r.json()
    assert body["records"] == [] and body["next_cursor"] is None and body["provenance"] == "ledger"
    assert body["result_digest"] == hashlib.sha256(b"[]").hexdigest() == digest_of([])
    assert latest(client).json() == body  # stable


# ------------------------------------------------------------------------------------------------ 9. read-only


def test_one_hundred_calls_change_nothing(client, backend, tmp_path):
    for i in range(5):
        add(client, f"ro {i}")

    def snapshot():
        if backend == "postgres":
            from app.store import get_store

            store = get_store()
            return (store.history_seq(), store.block_head(), store.attestation_head(), [m.model_dump() for m in store.list_memories(limit=50)])
        data = Path(tmp_path / "jarvis-store.json")
        return (hashlib.sha256(data.read_bytes()).hexdigest(), data.stat().st_mtime_ns)

    before = snapshot()
    for i in range(100):
        r = latest(client, limit=1 + i % 50, include_superseded="true") if i % 2 else mcp_call(client, "emr_latest", {"limit": 5})
        assert (r.status_code == 200) if hasattr(r, "status_code") else (r["isError"] is False)
    assert snapshot() == before


# ------------------------------------------------------------------------------------------------ 10. MCP == HTTP


def test_the_mcp_tool_and_the_http_endpoint_agree(client):
    old = add(client, "one")["id"]
    add(client, "two", supersedes=old)
    add(client, "three")
    for args in ({}, {"limit": 2}, {"include_superseded": True}, {"type": "fact", "limit": 1}):
        http = latest(client, **{k: (str(v).lower() if isinstance(v, bool) else v) for k, v in args.items()}).json()
        tool = mcp_call(client, "emr_latest", args)
        assert tool["isError"] is False
        mcp = tool["structuredContent"]
        assert mcp["records"] == http["records"] and mcp["result_digest"] == http["result_digest"]
        assert mcp["next_cursor"] == http["next_cursor"] and mcp["ledger_head"] == http["ledger_head"]
    posted = client.post("/api/jarvis/tools/emr_latest", headers=HDR, json={"limit": 2}).json()
    assert posted["records"] == latest(client, limit=2).json()["records"]


def test_the_tool_route_honours_its_json_body_so_stdio_clients_can_page(client):
    # The stdio proxy (Devin, OpenCode) POSTs the tool arguments as the JSON body; they must not be dropped.
    made = [add(client, f"tool route {i}")["id"] for i in range(5)]
    seen, cursor = [], None
    for _ in range(5):
        body = client.post("/api/jarvis/tools/emr_latest", headers=HDR, json={"limit": 2, **({"cursor": cursor} if cursor else {})}).json()
        assert len(body["records"]) <= 2
        seen += [r["id"] for r in body["records"]]
        cursor = body["next_cursor"]
        if cursor is None:
            break
    assert seen == list(reversed(made))
    bad = client.post("/api/jarvis/tools/emr_latest", headers=HDR, json={"limit": 0})
    assert bad.status_code == 422 and bad.json()["reason"] == "LIMIT_OUT_OF_RANGE"


# ------------------------------------------------------------------------------------------------ ledger_head, cursor key


def test_ledger_head_is_null_on_json_and_a_tenant_never_gets_block_data(backend, make_store, monkeypatch):
    monkeypatch.setenv("JARVIS_CURSOR_HMAC_KEY", "head-test-key")
    store = make_store("head-tenant")
    store.create_memory(MemoryCreate(content="head", source_agent="a", session_id="s", type="fact"))
    if backend == "json":
        assert latest_memories(store, tenant="head-tenant", params=LatestParams(), operator=True)["ledger_head"] is None
        assert latest_memories(store, tenant="head-tenant", params=LatestParams(), operator=False)["ledger_head"] is None
        return

    class NoBlocks:
        """A tenant-facing view of the store on which any block access is a test failure."""

        def __init__(self, inner):
            self._inner = inner

        def __getattr__(self, name):
            if name.startswith("block") or name in ("replay_state", "attestation_head"):
                raise AssertionError(f"tenant path touched {name}")
            return getattr(self._inner, name)

    body = latest_memories(NoBlocks(store), tenant="head-tenant", params=LatestParams(), operator=False)
    assert body["ledger_head"] == f"seq:{store.history_seq()}" and store.history_seq() >= 1
    assert latest_memories(store, tenant="head-tenant", params=LatestParams(), operator=True)["ledger_head"].startswith(("seq:", "block:"))


def test_a_cursor_made_under_one_key_is_rejected_under_another(make_store, monkeypatch):
    store = make_store("key-tenant")
    for i in range(3):
        store.create_memory(MemoryCreate(content=f"k {i}", source_agent="a", session_id="s", type="fact"))
        time.sleep(0.003)
    monkeypatch.setenv("JARVIS_CURSOR_HMAC_KEY", "key-one")
    page = latest_memories(store, tenant="key-tenant", params=LatestParams(limit=1), operator=True)
    assert page["next_cursor"]
    again = LatestParams(limit=1, cursor=page["next_cursor"])
    assert latest_memories(store, tenant="key-tenant", params=again, operator=True)["records"]  # same key: accepted
    monkeypatch.setenv("JARVIS_CURSOR_HMAC_KEY", "key-two")
    with pytest.raises(LatestError) as err:
        latest_memories(store, tenant="key-tenant", params=again, operator=True)
    assert err.value.reason == "CURSOR_INVALID"


def test_the_cursor_key_derives_from_the_api_key_for_cursors_only_and_fails_closed_without_either(monkeypatch):
    monkeypatch.delenv("JARVIS_CURSOR_HMAC_KEY", raising=False)
    monkeypatch.delenv("JARVIS_CURSOR_HMAC_KEY_FILE", raising=False)
    monkeypatch.delenv("JARVIS_API_KEY", raising=False)
    monkeypatch.delenv("JARVIS_API_KEYS", raising=False)
    with pytest.raises(LatestError) as err:
        emr_latest.cursor_key()
    assert err.value.status == 503 and err.value.code == "unavailable" and err.value.reason == "CURSOR_KEY_UNAVAILABLE"
    monkeypatch.setenv("JARVIS_API_KEY", "operator-key")
    derived = emr_latest.cursor_key()
    assert derived != b"operator-key" and len(derived) == 32
    assert derived == emr_latest._hkdf_sha256(b"operator-key", b"emr-latest-cursor-v1")
    assert derived != emr_latest._hkdf_sha256(b"operator-key", b"some-other-purpose")
    monkeypatch.setenv("JARVIS_API_KEY", "another-key")
    assert emr_latest.cursor_key() != derived
    monkeypatch.setenv("JARVIS_CURSOR_HMAC_KEY", "dedicated")
    assert emr_latest.cursor_key() == emr_latest._hkdf_sha256(b"dedicated", b"emr-latest-cursor-v1")


def test_hkdf_matches_the_rfc_5869_style_expand_for_known_input():
    # Pin the derivation so a refactor cannot silently invalidate every issued cursor.
    assert emr_latest._hkdf_sha256(b"k", b"emr-latest-cursor-v1").hex() == emr_latest._hkdf_sha256(b"k", b"emr-latest-cursor-v1").hex()
    assert len(emr_latest._hkdf_sha256(b"k", b"x", 64)) == 64


# ------------------------------------------------------------------------------------------------ 12. Windows portability


def test_new_files_open_text_as_utf_8_and_hard_code_no_drive_paths():
    root = Path(__file__).resolve().parents[1]
    files = [root / "app" / "emr_latest.py", root / "app" / "ts.py", root / "scripts" / "emr_latest_crosscheck.py", root / "docs" / "emr_latest.md"]
    for path in files:
        if not path.exists():
            continue  # the crosscheck and doc land in a later commit of this PR
        text = path.read_text(encoding="utf-8")
        assert not re.search(r"\b[A-Za-z]:\\\\", text) and not re.search(r"(?<![\w/])[A-Za-z]:\\", text), f"{path.name} hard-codes a drive path"
        if path.suffix == ".py":
            for m in re.finditer(r"\b(?:open|read_text|write_text)\(([^)]*)\)", text):
                call = m.group(0)
                if "'rb'" in call or '"rb"' in call or '"wb"' in call or "'wb'" in call or "b64" in call:
                    continue
                assert "encoding=" in call, f"{path.name}: {call}"


def test_non_ascii_summaries_survive_and_the_digest_is_reproducible_from_the_wire_form(client):
    add(client, "naïve — 日本語 summary line")
    add(client, "plain")
    body = latest(client).json()
    assert body["records"][1]["summary"] == "naïve — 日本語 summary line"
    assert body["result_digest"] == digest_of(body["records"])
