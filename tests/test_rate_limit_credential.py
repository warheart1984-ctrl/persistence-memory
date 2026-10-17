"""Rate limiting is per credential, not per address (RFC appendix item 17; from the mcp-multitenant-gateway study).

Pins: tenants behind one client address do not share a bucket; a stream of distinct subjects cannot grow the limiter without bound; failed key attempts are throttled per (address, key id) BEFORE
any HMAC or database work, without letting a stranger lock a key's owner out; and more than one worker is a startup refusal unless a shared limiter is declared.
"""

from __future__ import annotations

import pytest

from app import key_auth, public_security
from app.public_security import McpRateLimiter, config_problems
from tests.test_key_auth import bearer, op, world  # noqa: F401  (the key-tenant world fixture)

pg_only = pytest.mark.postgres


def _mcp(client, headers):
    return client.post("/mcp", headers=headers, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})


@pg_only
def test_tenants_behind_one_address_do_not_share_a_rate_limit_bucket(world, monkeypatch):
    monkeypatch.setenv("JARVIS_PUBLIC_MODE", "1")
    monkeypatch.setenv("JARVIS_TRUSTED_HOSTS", "testserver")
    monkeypatch.setenv("JARVIS_MCP_RATE_LIMIT_PER_MINUTE", "3")
    monkeypatch.setattr(public_security, "mcp_rate_limiter", McpRateLimiter())
    _, a = world.issue("a")
    _, b = world.issue("b")
    assert [_mcp(world.client, bearer(a)).status_code == 429 for _ in range(3)] == [False] * 3
    assert _mcp(world.client, bearer(a)).status_code == 429            # a is spent...
    assert _mcp(world.client, bearer(b)).status_code != 429            # ...and b, from the very same address, is untouched
    assert _mcp(world.client, op()).status_code != 429                 # as is the operator


@pg_only
def test_failed_key_attempts_are_refused_before_any_key_check_and_do_not_lock_out_other_keys(world, monkeypatch):
    monkeypatch.setenv("JARVIS_KEY_FAIL_LIMIT_PER_MINUTE", "3")
    monkeypatch.setattr(public_security, "key_failure_limiter", McpRateLimiter())
    tenant, token = world.issue("owner")
    _, other = world.issue("other")
    key_id = token.split("_")[1]
    wrong = f"jlk_{key_id}_{'A' * 43}"
    assert [world.client.get("/api/jarvis/memory", headers=bearer(wrong)).status_code for _ in range(3)] == [401, 401, 401]
    calls = []
    real = key_auth.authenticate
    monkeypatch.setattr(key_auth, "authenticate", lambda c: calls.append(c) or real(c))
    r = world.client.get("/api/jarvis/memory", headers=bearer(wrong))
    assert r.status_code == 429 and r.headers.get("retry-after") == "60"
    assert calls == [], "the throttled attempt reached the key check (HMAC and database)"
    # the throttle is per key id: another key from the same address is not affected
    assert world.client.get("/api/jarvis/memory", headers=bearer(other)).status_code == 200
    # a different key id guessed from the same address has its own count
    assert world.client.get("/api/jarvis/memory", headers=bearer(f"jlk_{'0' * 16}_{'B' * 43}")).status_code == 401


def test_the_limiter_table_is_bounded_and_keeps_live_subjects():
    lim = McpRateLimiter()
    lim.MAX_BUCKETS = 50
    for i in range(500):
        assert lim.allow(f"ip:{i}", limit=5)
    assert len(lim._requests) <= 50
    assert lim.allow("ip:499", limit=5)                                   # the newest are still tracked


def test_idle_buckets_are_evicted_first():
    clock = [1000.0]
    lim = McpRateLimiter(window=60)
    lim.MAX_BUCKETS = 4
    import app.public_security as ps
    real = ps.time.monotonic
    ps.time.monotonic = lambda: clock[0]
    try:
        for s in "abc":
            lim.allow(s, limit=1)
        clock[0] += 120                                                    # a, b, c are now idle for a whole window
        lim.allow("d", limit=1)
        lim.allow("e", limit=1)                                            # table is at its cap: the idle ones go, d stays
        assert set(lim._requests) == {"d", "e"}
        assert not lim.allow("d", limit=1)                                 # d's count survived
    finally:
        ps.time.monotonic = real


@pytest.mark.parametrize("env", ["WEB_CONCURRENCY", "UVICORN_WORKERS", "GUNICORN_WORKERS"])
def test_more_than_one_worker_is_a_startup_refusal_unless_a_shared_limiter_is_declared(monkeypatch, env):
    for k in ("WEB_CONCURRENCY", "UVICORN_WORKERS", "GUNICORN_WORKERS", "JARVIS_RATE_LIMIT_SHARED_BACKEND"):
        monkeypatch.delenv(k, raising=False)
    assert config_problems() == []
    monkeypatch.setenv(env, "1")
    assert config_problems() == []
    monkeypatch.setenv(env, "4")
    assert "4 workers" in config_problems()[0]
    with pytest.raises(RuntimeError, match="rate limiting refuses to start"):
        public_security.assert_config()
    monkeypatch.setenv("JARVIS_RATE_LIMIT_SHARED_BACKEND", "edge-waf")
    assert config_problems() == []


def test_a_burst_of_concurrent_attempts_cannot_all_pass_a_check_made_against_an_empty_bucket():
    """Codex on #112: the attempt is counted BEFORE the expensive verification, atomically, so at most `limit` of a concurrent burst are admitted."""
    import threading

    lim = McpRateLimiter()
    admitted, gate = [], threading.Barrier(40)

    def attempt():
        gate.wait()
        admitted.append(lim.reserve("keyfail:1.2.3.4:abc", 5) is not None)

    threads = [threading.Thread(target=attempt) for _ in range(40)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert sum(admitted) == 5


def test_a_released_reservation_is_not_counted_as_a_failure():
    lim = McpRateLimiter()
    for _ in range(50):
        lim.release("s", lim.reserve("s", 3))                                                   # a successful credential, or an outage: never a wrong guess
    assert [lim.reserve("s", 3) is not None for _ in range(4)] == [True, True, True, False]


def test_a_throttled_bucket_survives_a_flood_of_other_subjects_and_new_ones_are_refused_when_full():
    """Codex on #112: 10,000 distinct key ids must not evict the throttled target and reset its count."""
    lim = McpRateLimiter()
    lim.MAX_BUCKETS = 100
    for _ in range(3):
        assert lim.reserve("target", 3) is not None
    assert lim.reserve("target", 3) is None                                # at its limit
    for i in range(1000):
        lim.reserve(f"junk-{i}", 3)                                        # the flood; each junk id has a single failed guess
    assert lim.reserve("target", 3) is None, "the flood evicted the throttled bucket"
    # and when every bucket is at its limit, a brand-new subject is refused rather than evicting one
    full = McpRateLimiter()
    full.MAX_BUCKETS = 5
    for i in range(5):
        for _ in range(3):
            full.reserve(f"b{i}", 3)
    assert full.reserve("new", 3) is None
    assert full.reserve("b0", 3) is None


def test_releasing_a_success_removes_its_own_reservation_not_a_newer_failures():
    """Codex on #112 (round 2): attempts overlap and finish in any order. An old valid request finishing after a newer invalid one must leave the invalid one counted, at ITS time."""
    import app.public_security as ps

    clock = [100.0]
    real = ps.time.monotonic
    ps.time.monotonic = lambda: clock[0]
    try:
        lim = McpRateLimiter(window=60)
        old_valid = lim.reserve("s", 2)
        clock[0] = 150.0
        newer_invalid = lim.reserve("s", 2)                                 # stays counted: it was a wrong guess
        lim.release("s", old_valid)                                         # the older, valid one finishes last
        assert list(lim._requests["s"]) == [newer_invalid]
        clock[0] = 170.0                                                    # 70s after the old one, 20s after the failure: the failure must still count
        assert lim.reserve("s", 2) is not None and lim.reserve("s", 2) is None
    finally:
        ps.time.monotonic = real
