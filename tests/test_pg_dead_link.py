"""A silently dead link to the database (a dropped network) must not hold a request for as long as TCP keeps retrying.  The chaos partition fault
measured requests, and /ready, hanging for the whole partition plus the TCP recovery (52 s) because nothing on the client side bounded them:
statement_timeout is enforced by the server, which is the thing that is unreachable.  Keepalives and tcp_user_timeout on every pooled connection bound it."""

from __future__ import annotations

import os
import socket

import pytest

from app import pg_store

pytestmark = pytest.mark.postgres


def test_the_defaults_bound_a_dead_link_to_seconds(monkeypatch):
    for name in ("JARVIS_DATABASE_KEEPALIVES_IDLE_S", "JARVIS_DATABASE_KEEPALIVES_INTERVAL_S", "JARVIS_DATABASE_KEEPALIVES_COUNT", "JARVIS_DATABASE_TCP_USER_TIMEOUT_MS"):
        monkeypatch.delenv(name, raising=False)
    k = pg_store._link_kwargs()
    assert k == {"keepalives": 1, "keepalives_idle": 3, "keepalives_interval": 2, "keepalives_count": 2, "tcp_user_timeout": 5000}
    assert k["keepalives_idle"] + k["keepalives_interval"] * k["keepalives_count"] <= 10     # a quiet peer is declared dead within ten seconds
    assert k["tcp_user_timeout"] <= 10_000


def test_the_settings_can_be_tuned_and_a_bad_value_falls_back(monkeypatch):
    monkeypatch.setenv("JARVIS_DATABASE_KEEPALIVES_IDLE_S", "10")
    monkeypatch.setenv("JARVIS_DATABASE_TCP_USER_TIMEOUT_MS", "1500")
    monkeypatch.setenv("JARVIS_DATABASE_KEEPALIVES_COUNT", "lots")
    k = pg_store._link_kwargs()
    assert k["keepalives_idle"] == 10 and k["tcp_user_timeout"] == 1500 and k["keepalives_count"] == 2


def test_the_pool_hands_them_to_every_connection(monkeypatch):
    seen = {}

    class Pool:
        check_connection = staticmethod(lambda conn: None)

        def __init__(self, dsn, **kw):
            seen.update(kw)

        def open(self, wait=False):
            pass

        def close(self, timeout=0):
            pass

    monkeypatch.setattr(pg_store, "ConnectionPool", Pool)
    monkeypatch.setattr(pg_store, "_pools", {})
    pg_store._pool_for("postgresql://x@127.0.0.1:1/y", "s")
    kw = seen["kwargs"]
    assert kw["keepalives"] == 1 and kw["tcp_user_timeout"] == 5000 and kw["keepalives_idle"] == 3 and kw["connect_timeout"] == 5
    assert kw["options"].startswith("-c timezone=UTC")           # the existing options are intact


def test_a_real_connection_carries_the_options_on_its_socket(pg_schema, monkeypatch):
    """libpq must actually apply them: read them back from the connection's socket."""
    import psycopg

    for name in ("JARVIS_DATABASE_KEEPALIVES_IDLE_S", "JARVIS_DATABASE_KEEPALIVES_INTERVAL_S", "JARVIS_DATABASE_KEEPALIVES_COUNT", "JARVIS_DATABASE_TCP_USER_TIMEOUT_MS"):
        monkeypatch.delenv(name, raising=False)
    with psycopg.connect(pg_schema.admin_dsn, **pg_store._link_kwargs()) as conn:
        fd = os.dup(conn.pgconn.socket)
        try:
            s = socket.socket(fileno=fd)
            assert s.getsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE) == 1
            assert s.getsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE) == 3
            assert s.getsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL) == 2
            assert s.getsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT) == 2
            assert s.getsockopt(socket.IPPROTO_TCP, getattr(socket, "TCP_USER_TIMEOUT", 18)) == 5000
        finally:
            s.close()
    with psycopg.connect(pg_schema.admin_dsn) as plain:           # control: without the options the socket has none of them
        fd = os.dup(plain.pgconn.socket)
        s = socket.socket(fileno=fd)
        try:
            assert s.getsockopt(socket.IPPROTO_TCP, getattr(socket, "TCP_USER_TIMEOUT", 18)) == 0
        finally:
            s.close()
