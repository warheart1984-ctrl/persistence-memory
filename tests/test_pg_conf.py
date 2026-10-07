"""The database configuration baked into the image: sessions of vanished clients must be reaped in about a minute, not held for the operating system's
two hours (the chaos partition fault stranded 6-7 sessions per run against max_connections = 30)."""

from __future__ import annotations

import re
from pathlib import Path

CONF = Path(__file__).resolve().parents[1] / "deploy" / "mint" / "db" / "postgresql.conf"


def settings() -> dict[str, str]:
    out = {}
    for line in CONF.read_text().splitlines():
        m = re.match(r"^\s*([a-z_\.]+)\s*=\s*'?([^#']*?)'?\s*(#.*)?$", line)
        if m:
            out[m.group(1)] = m.group(2).strip()
    return out


def seconds(value: str) -> float:
    m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*(ms|s|min)?", value)
    assert m, value
    n = float(m.group(1))
    return n / 1000 if m.group(2) == "ms" else n * 60 if m.group(2) == "min" else n


def test_a_vanished_clients_session_is_reaped_within_two_minutes():
    s = settings()
    idle, interval, count = (seconds(s[k]) for k in ("tcp_keepalives_idle", "tcp_keepalives_interval", "tcp_keepalives_count"))
    assert 0 < idle <= 60 and 0 < interval <= 30 and 0 < count <= 5, (idle, interval, count)       # 0 would mean "the operating system's default": two hours
    assert idle + interval * count <= 120
    assert 0 < seconds(s["tcp_user_timeout"]) <= 60


def test_the_connection_limit_is_still_the_documented_small_one_and_the_pool_fits_under_it():
    s = settings()
    assert int(s["max_connections"]) == 30
    pool_max = 10                                                    # app/pg_store.py: JARVIS_DATABASE_POOL_MAX default
    assert int(s["max_connections"]) - 3 - pool_max >= 10            # superuser reserve, the pool, and room for the migrator, backups and admin


def test_the_settings_have_a_comment_that_says_why():
    text = CONF.read_text()
    assert "Reap the sessions of clients that have vanished" in text and "with max_connections this small" in text
