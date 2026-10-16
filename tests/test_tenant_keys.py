"""Per-tenant API keys, slice 1 (RFC phase 1): the v10 control schema, the token format, the HMAC verification, and the migrator-role issuing.

Nothing here is in the request path yet. What these tests pin: the application role can only call the two lookup functions (no table privilege), the database stores only an HMAC of the
secret (never the secret), every failure to authenticate looks the same, and the control schema does not trip the "every tenant_key table has forced RLS" guard that audits the ledger schema.
"""

from __future__ import annotations

import hmac
import re
from datetime import datetime, timedelta, timezone

import psycopg
import pytest
from psycopg.rows import dict_row

from app import pg_store, tenant_keys
from app.pg_schema import control_schema_name, migrate, v10_rollback_sql

pg_only = pytest.mark.postgres
PEPPER = b"unit-test-pepper-0123456789-0123456789"


@pytest.fixture
def pg(pg_schema):
    migrate(pg_schema.admin_dsn, schema=pg_schema.schema, app_role="jarvis_app_test")
    pg_schema.control = control_schema_name(pg_schema.schema)
    yield pg_schema
    pg_store.close_pools()


def _admin(pg):
    return psycopg.connect(pg.admin_dsn, autocommit=True, row_factory=dict_row)


def _app(pg):
    return psycopg.connect(pg.app_dsn, autocommit=True, row_factory=dict_row)


def _issue(pg, **kw):
    with _admin(pg) as c:
        tenant = tenant_keys.create_tenant(c, pg.control, "alice")
        token = tenant_keys.create_key(c, pg.control, tenant, PEPPER, **kw)
    return tenant, token


# ---------------------------------------------------------------------------------------------------------------- tokens (no database)
def test_a_token_has_one_exact_shape_and_nothing_else_parses():
    key_id, secret = tenant_keys.new_key()
    token = tenant_keys.make_token(key_id, secret)
    assert re.fullmatch(r"jlk_[0-9a-f]{16}_[A-Za-z0-9_-]{43}", token)
    assert tenant_keys.parse_token(token) == (key_id, secret)
    for bad in (None, "", token + " ", " " + token, token + "\n", token + "\r\n", "\n" + token, token.upper(), token[:-1], token + "x", "jlk_" + "g" * 16 + "_" + secret, f"jlk_{key_id}", secret, f"Bearer {token}", token.replace("jlk_", "jlx_")):
        assert tenant_keys.parse_token(bad) is None, bad
    assert len({tenant_keys.new_key()[1] for _ in range(50)}) == 50


def test_the_hash_depends_on_the_pepper_and_the_secret_and_is_not_the_secret():
    h = tenant_keys.key_hash("0123456789abcdef", "s", PEPPER)
    assert re.fullmatch(r"[0-9a-f]{64}", h) and h != "s"
    assert h != tenant_keys.key_hash("0123456789abcdef", "s", PEPPER + b"x") and h != tenant_keys.key_hash("0123456789abcdef", "t", PEPPER)
    assert h != tenant_keys.key_hash("fedcba9876543210", "s", PEPPER)                    # bound to the key id as well


def test_the_pepper_is_required_and_strong_and_may_come_from_a_file(tmp_path, monkeypatch):
    monkeypatch.delenv("JARVIS_KEY_PEPPER", raising=False)
    monkeypatch.delenv("JARVIS_KEY_PEPPER_FILE", raising=False)
    with pytest.raises(tenant_keys.KeyConfigError):
        tenant_keys.pepper()                                                            # no weak default
    monkeypatch.setenv("JARVIS_KEY_PEPPER", "short")
    with pytest.raises(tenant_keys.KeyConfigError):
        tenant_keys.pepper()
    f = tmp_path / "pepper"
    f.write_text("p" * 40 + "\nsecond line ignored\n")
    f.chmod(0o644)
    monkeypatch.setenv("JARVIS_KEY_PEPPER_FILE", str(f))
    with pytest.raises(tenant_keys.KeyConfigError, match="must not be readable"):
        tenant_keys.pepper()                                                            # a world-readable pepper file is refused
    f.chmod(0o600)
    assert tenant_keys.pepper() == b"p" * 40
    monkeypatch.setenv("JARVIS_KEY_PEPPER_FILE", str(tmp_path / "missing"))
    with pytest.raises(tenant_keys.KeyConfigError) as exc:
        tenant_keys.pepper()
    assert str(tmp_path) not in str(exc.value)                                          # no path in the message


# --------------------------------------------------------------------------------------------------------------------------- the schema
@pg_only
def test_v10_creates_the_control_schema_with_the_constraints_the_design_needs(pg):
    with _admin(pg) as c:
        assert c.execute("SELECT to_regnamespace(%s) IS NOT NULL AS x", (pg.control,)).fetchone()["x"]
        t = tenant_keys.create_tenant(c, pg.control, "ok")
        assert re.fullmatch(r"t_[0-9a-f]{32}", t)
        for bad in ("operator", "", "t_short", "t_" + "G" * 32, "a" * 64):                # operator, empty, malformed, an OAuth-style hash: all refused by the table itself
            with pytest.raises(psycopg.errors.CheckViolation):
                c.execute(psycopg.sql.SQL("INSERT INTO {}.tenants (tenant_key) VALUES (%s)").format(psycopg.sql.Identifier(pg.control)), (bad,))
        with pytest.raises(psycopg.errors.CheckViolation):
            c.execute(psycopg.sql.SQL("UPDATE {}.tenants SET status = 'weird' WHERE tenant_key = %s").format(psycopg.sql.Identifier(pg.control)), (t,))
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            tenant_keys.create_key(c, pg.control, "t_" + "0" * 32, PEPPER)                # a key needs an existing tenant


@pg_only
def test_the_application_role_has_no_table_privilege_in_the_control_schema_only_the_two_functions(pg):
    with _admin(pg) as c:
        for table in ("tenants", "api_keys"):
            for priv in ("SELECT", "INSERT", "UPDATE", "DELETE"):
                assert not c.execute("SELECT has_table_privilege('jarvis_app_test', %s, %s) AS x", (f'"{pg.control}"."{table}"', priv)).fetchone()["x"], (table, priv)
        for fn in ("resolve_key(text)", "touch_key_last_used(text)"):
            assert c.execute("SELECT has_function_privilege('jarvis_app_test', %s, 'EXECUTE') AS x", (f'"{pg.control}".{fn}',)).fetchone()["x"]
            assert not c.execute("SELECT has_function_privilege('public', %s, 'EXECUTE') AS x", (f'"{pg.control}".{fn}',)).fetchone()["x"]
    tenant, token = _issue(pg)
    with _app(pg) as c:
        for stmt in (f'SELECT * FROM "{pg.control}".api_keys', f'SELECT * FROM "{pg.control}".tenants', f'UPDATE "{pg.control}".api_keys SET status = \'active\'',
                     f'INSERT INTO "{pg.control}".tenants (tenant_key) VALUES (\'t_{"1" * 32}\')', f'DELETE FROM "{pg.control}".api_keys'):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                c.execute(stmt)
        assert tenant_keys.resolve(c, pg.control, token, PEPPER).tenant_key == tenant      # ... and it still authenticates through the functions


@pg_only
def test_the_ledger_schemas_rls_guard_does_not_trip_on_the_control_schema(pg, monkeypatch):
    """api_keys and tenants carry a tenant_key column and no RLS on purpose; they live in another schema, which the guard (readiness and strict startup) does not audit."""
    monkeypatch.setenv("JARVIS_MULTI_TENANT", "1")
    monkeypatch.setenv("JARVIS_API_KEY", "control-schema-guard-key-0123456789")
    monkeypatch.setenv("JARVIS_DATABASE_URL", pg.app_dsn)
    monkeypatch.setenv("JARVIS_DATABASE_SCHEMA", pg.schema)
    monkeypatch.setenv("JARVIS_PG_STORE", "rows")
    _issue(pg)
    assert pg_store.assert_tenant_isolation_ready() is None
    with _admin(pg) as c:
        assert pg_store.tenant_tables_without_forced_rls(c, pg.schema) == []


# ----------------------------------------------------------------------------------------------------------- issuing and verifying
@pg_only
def test_a_key_round_trips_and_the_database_never_holds_the_secret(pg):
    tenant, token = _issue(pg, label="agent one", scopes=("memory.read",))
    key_id, secret = tenant_keys.parse_token(token)
    with _admin(pg) as c:
        rows = c.execute(psycopg.sql.SQL("SELECT * FROM {}.api_keys").format(psycopg.sql.Identifier(pg.control))).fetchall()
        assert len(rows) == 1 and rows[0]["key_id"] == key_id and rows[0]["tenant_key"] == tenant and rows[0]["scopes"] == ["memory.read"]
        assert rows[0]["key_hash"] == tenant_keys.key_hash(key_id, secret, PEPPER)
        assert secret not in repr(rows) and token not in repr(rows)
    with _app(pg) as c:
        got = tenant_keys.resolve(c, pg.control, token, PEPPER)
    assert got == tenant_keys.ResolvedKey(key_id=key_id, tenant_key=tenant, scopes=frozenset({"memory.read"}), tenant_status="active")


@pg_only
def test_every_way_of_failing_to_authenticate_is_the_same_answer(pg):
    tenant, token = _issue(pg)
    key_id, secret = tenant_keys.parse_token(token)
    other_secret = tenant_keys.new_key()[1]
    with _admin(pg) as a, _app(pg) as c:
        assert tenant_keys.resolve(c, pg.control, token, PEPPER) is not None                              # positive control
        failures = [
            None, "", "garbage",                                                                         # malformed
            tenant_keys.make_token(key_id, other_secret),                                                # right id, wrong secret
            tenant_keys.make_token(tenant_keys.new_key()[0], secret),                                    # unknown id, a real secret
            token,                                                                                       # (checked below under a wrong pepper)
        ]
        results = [tenant_keys.resolve(c, pg.control, t, PEPPER) for t in failures[:-1]]
        assert results == [None] * 5
        assert tenant_keys.resolve(c, pg.control, token, PEPPER + b"x") is None                        # right token, wrong pepper
        assert tenant_keys.revoke_key(a, pg.control, key_id) is True
        assert tenant_keys.resolve(c, pg.control, token, PEPPER) is None                                 # revoked
        assert tenant_keys.revoke_key(a, pg.control, key_id) is False                                    # already revoked


@pg_only
def test_an_expired_key_does_not_authenticate_and_a_future_one_does(pg):
    _, expired = _issue(pg, expires_at=datetime.now(timezone.utc) - timedelta(seconds=1))
    _, future = _issue(pg, expires_at=datetime.now(timezone.utc) + timedelta(days=1))
    with _app(pg) as c:
        assert tenant_keys.resolve(c, pg.control, expired, PEPPER) is None
        assert tenant_keys.resolve(c, pg.control, future, PEPPER) is not None


@pg_only
def test_an_unknown_key_id_still_costs_a_comparison_so_it_is_not_a_cheap_oracle(pg, monkeypatch):
    calls = []
    real = hmac.compare_digest
    monkeypatch.setattr(tenant_keys.hmac, "compare_digest", lambda a, b: calls.append(1) or real(a, b))
    _, token = _issue(pg)
    key_id, secret = tenant_keys.parse_token(token)
    with _app(pg) as c:
        tenant_keys.resolve(c, pg.control, tenant_keys.make_token(tenant_keys.new_key()[0], secret), PEPPER)
        n_unknown = len(calls)
        tenant_keys.resolve(c, pg.control, tenant_keys.make_token(key_id, tenant_keys.new_key()[1]), PEPPER)
    assert n_unknown == 1 and len(calls) == 2
    calls.clear()
    with _app(pg) as c:
        tenant_keys.resolve(c, pg.control, "garbage", PEPPER)
    assert len(calls) == 1                                                                              # even a malformed token compares once


@pg_only
def test_a_valid_key_of_a_non_active_tenant_resolves_with_its_status_for_the_caller_to_decide(pg):
    tenant, token = _issue(pg)
    with _admin(pg) as a, _app(pg) as c:
        for status in ("suspended", "restoring", "quiesced"):
            assert tenant_keys.set_tenant_status(a, pg.control, tenant, status) is True
            assert tenant_keys.resolve(c, pg.control, token, PEPPER).tenant_status == status
        assert tenant_keys.set_tenant_status(a, pg.control, tenant, "deleting") is True
        assert tenant_keys.resolve(c, pg.control, token, PEPPER) is None                      # deleting revokes the tenant's keys: the caller sees the uniform failure
        assert tenant_keys.set_tenant_status(a, pg.control, tenant, "deleted") is True
        assert a.execute(psycopg.sql.SQL("SELECT deleted_at IS NOT NULL AS d FROM {}.tenants").format(psycopg.sql.Identifier(pg.control))).fetchone()["d"] is True
        assert tenant_keys.set_tenant_status(a, pg.control, "t_" + "9" * 32, "active") is False
        with pytest.raises(ValueError):
            tenant_keys.set_tenant_status(a, pg.control, tenant, "weird")


@pg_only
def test_last_used_is_noted_but_at_most_once_a_minute(pg):
    tenant, token = _issue(pg)
    key_id = tenant_keys.parse_token(token)[0]
    with _app(pg) as c, _admin(pg) as a:
        read = lambda: a.execute(psycopg.sql.SQL("SELECT last_used_at FROM {}.api_keys WHERE key_id = %s").format(psycopg.sql.Identifier(pg.control)), (key_id,)).fetchone()["last_used_at"]  # noqa: E731
        assert read() is None
        tenant_keys.resolve(c, pg.control, token, PEPPER)
        first = read()
        assert first is not None
        tenant_keys.resolve(c, pg.control, token, PEPPER)
        assert read() == first                                                                          # not rewritten on every request


@pg_only
def test_issuing_validates_scopes_and_the_tenant_id_and_listing_never_shows_a_hash(pg):
    with _admin(pg) as a:
        t = tenant_keys.create_tenant(a, pg.control, "x")
        for bad in ((), ("memory.admin",), ("memory.read", "root")):
            with pytest.raises(ValueError):
                tenant_keys.create_key(a, pg.control, t, PEPPER, scopes=bad)
        for bad in ("operator", "t_zz", "x" * 40):
            with pytest.raises(ValueError):
                tenant_keys.create_key(a, pg.control, bad, PEPPER)
        tenant_keys.create_key(a, pg.control, t, PEPPER, label="one")
        listed = tenant_keys.list_keys(a, pg.control, t)
        assert len(listed) == 1 and "key_hash" not in listed[0] and listed[0]["label"] == "one"
        assert [x["tenant_key"] for x in tenant_keys.list_tenants(a, pg.control)] == [t]


@pg_only
def test_the_cli_issues_a_key_that_authenticates_and_writes_a_token_file_with_mode_600(pg, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("JARVIS_DATABASE_MIGRATE_URL", pg.admin_dsn)
    monkeypatch.setenv("JARVIS_DATABASE_SCHEMA", pg.schema)
    monkeypatch.setenv("JARVIS_KEY_PEPPER", PEPPER.decode())
    assert tenant_keys.main(["create-tenant", "--label", "cli tenant"]) == 0
    tenant = capsys.readouterr().out.strip()
    assert tenant_keys.TENANT_KEY_RE.match(tenant)
    token_file = tmp_path / "token"
    assert tenant_keys.main(["create-key", "--tenant", tenant, "--label", "cli key", "--token-file", str(token_file)]) == 0
    out = capsys.readouterr().out
    token = token_file.read_text().strip()
    assert oct(token_file.stat().st_mode & 0o777) == "0o600" and token not in out                      # the token went to the file, not the terminal
    with _app(pg) as c:
        assert tenant_keys.resolve(c, pg.control, token, PEPPER).tenant_key == tenant
    assert tenant_keys.main(["create-key", "--tenant", tenant, "--token-file", str(token_file)]) == 1   # never overwrites an existing file
    assert tenant_keys.main(["create-key", "--tenant", "operator"]) == 1
    assert tenant_keys.main(["list-keys", "--tenant", tenant]) == 0 and "cli key" in capsys.readouterr().out
    key_id = tenant_keys.parse_token(token)[0]
    assert tenant_keys.main(["revoke-key", key_id]) == 0 and "revoked" in capsys.readouterr().out
    with _app(pg) as c:
        assert tenant_keys.resolve(c, pg.control, token, PEPPER) is None


# ------------------------------------------------------------------------------------------------------------------------- rollback
@pytest.mark.postgres
def test_v10_rolls_back_cleanly_and_forward_again_and_changes_no_ledger_data(pg_schema):
    assert migrate(pg_schema.admin_dsn, schema=pg_schema.schema, app_role="jarvis_app_test", up_to=9) == 9
    control = control_schema_name(pg_schema.schema)
    assert migrate(pg_schema.admin_dsn, schema=pg_schema.schema, app_role="jarvis_app_test") == 10
    with psycopg.connect(pg_schema.admin_dsn, options=f"-c search_path={pg_schema.schema}") as conn:
        conn.execute(v10_rollback_sql(pg_schema.schema))
    with pg_schema.admin_conn() as conn:
        assert conn.execute("SELECT max(version) FROM schema_version").fetchone()[0] == 9
        assert conn.execute("SELECT to_regnamespace(%s) IS NULL", (control,)).fetchone()[0]
    assert migrate(pg_schema.admin_dsn, schema=pg_schema.schema, app_role="jarvis_app_test") == 10      # and forward again
    with pg_schema.admin_conn() as conn:
        assert conn.execute("SELECT to_regnamespace(%s) IS NOT NULL", (control,)).fetchone()[0]


# ----------------------------------------------------------------------------------------------------- review findings (verification of #107)
@pg_only
def test_a_deleted_tenant_can_never_be_reactivated_by_the_cli_or_by_hand_and_its_keys_stay_dead(pg):
    tenant, token = _issue(pg)
    second = None
    with _admin(pg) as a:
        second = tenant_keys.create_key(a, pg.control, tenant, PEPPER)
        assert tenant_keys.set_tenant_status(a, pg.control, tenant, "deleting") is True
        keys = tenant_keys.list_keys(a, pg.control, tenant)
        assert keys and all(k["status"] == "revoked" and k["revoked_at"] is not None for k in keys)       # entering deleting revokes every key
        assert tenant_keys.set_tenant_status(a, pg.control, tenant, "deleted") is True
        for status in ("active", "suspended", "restoring", "quiesced", "deleting"):
            with pytest.raises(ValueError, match="cannot be reactivated"):
                tenant_keys.set_tenant_status(a, pg.control, tenant, status)
            with pytest.raises(psycopg.errors.CheckViolation):                                          # and the database refuses a hand-written UPDATE too
                a.execute(psycopg.sql.SQL("UPDATE {}.tenants SET status = %s WHERE tenant_key = %s").format(psycopg.sql.Identifier(pg.control)), (status, tenant))
        assert tenant_keys.set_tenant_status(a, pg.control, tenant, "deleted") is True                   # idempotent
        with pytest.raises(psycopg.errors.CheckViolation):
            tenant_keys.create_key(a, pg.control, tenant, PEPPER)                                         # no new key for a deleted tenant
    with _app(pg) as c:
        assert tenant_keys.resolve(c, pg.control, token, PEPPER) is None and tenant_keys.resolve(c, pg.control, second, PEPPER) is None


@pg_only
def test_suspending_and_restoring_a_tenant_does_not_revoke_its_keys(pg):
    tenant, token = _issue(pg)
    with _admin(pg) as a, _app(pg) as c:
        for status in ("suspended", "restoring", "quiesced", "active"):
            tenant_keys.set_tenant_status(a, pg.control, tenant, status)
        assert tenant_keys.resolve(c, pg.control, token, PEPPER).tenant_status == "active"              # the key lives on through every non-deleting state
        assert tenant_keys.set_tenant_status(a, pg.control, tenant, "deleting") is True
        assert tenant_keys.resolve(c, pg.control, token, PEPPER) is None


@pg_only
def test_a_stored_hash_is_bound_to_its_key_id_so_it_cannot_be_moved_to_another_key(pg):
    t1, a_token = _issue(pg)
    with _admin(pg) as a:
        t2 = tenant_keys.create_tenant(a, pg.control, "other")
        b_token = tenant_keys.create_key(a, pg.control, t2, PEPPER)
    a_id, a_secret = tenant_keys.parse_token(a_token)
    b_id = tenant_keys.parse_token(b_token)[0]
    with _admin(pg) as a:
        a.execute(psycopg.sql.SQL("UPDATE {}.api_keys SET key_hash = (SELECT key_hash FROM {}.api_keys WHERE key_id = %s) WHERE key_id = %s").format(
            psycopg.sql.Identifier(pg.control), psycopg.sql.Identifier(pg.control)), (a_id, b_id))
    with _app(pg) as c:
        assert tenant_keys.resolve(c, pg.control, tenant_keys.make_token(b_id, a_secret), PEPPER) is None   # A's secret under B's id does not authenticate
        assert tenant_keys.resolve(c, pg.control, a_token, PEPPER).tenant_key == t1


@pg_only
def test_expiry_is_judged_by_the_databases_clock(pg):
    """The function returns whether the key has expired by now(); the application's clock is not consulted."""
    _, token = _issue(pg, expires_at=datetime.now(timezone.utc) + timedelta(days=1))
    with _app(pg) as c:
        assert tenant_keys.resolve(c, pg.control, token, PEPPER) is not None
        row = c.execute(psycopg.sql.SQL("SELECT expired FROM {}.resolve_key(%s)").format(psycopg.sql.Identifier(pg.control)), (tenant_keys.parse_token(token)[0],)).fetchone()
        assert row["expired"] is False
    with _admin(pg) as a:
        a.execute(psycopg.sql.SQL("UPDATE {}.api_keys SET expires_at = now() - interval '1 second'").format(psycopg.sql.Identifier(pg.control)))
    with _app(pg) as c:
        assert tenant_keys.resolve(c, pg.control, token, PEPPER) is None


def test_the_control_schema_name_fits_postgres_for_every_ledger_schema_name_and_never_collides():
    from app.pg_schema import control_schema_name as name

    assert name("jarvis") == "jarvis_control"                                           # unchanged for ordinary names
    seen = set()
    for n in range(1, 64):
        base = "a" * n
        c = name(base)
        assert len(c) <= 63 and c.endswith("_control"), n
        seen.add(c)
    assert len(seen) == 63
    assert name("x" * 60 + "aaa") != name("x" * 60 + "bbb") and name("x" * 63) == name("x" * 63)   # long names are shortened deterministically, with a hash of the whole name


@pytest.mark.postgres
def test_v10_works_on_a_ledger_schema_with_a_63_character_name(pg_server):
    admin_dsn, _ = pg_server
    schema = ("L" + "x" * 62)
    assert len(schema) == 63
    with psycopg.connect(admin_dsn, autocommit=True) as conn:
        conn.execute(psycopg.sql.SQL("CREATE SCHEMA {}").format(psycopg.sql.Identifier(schema)))
    try:
        assert migrate(admin_dsn, schema=schema, app_role="jarvis_app_test") == 10
        with psycopg.connect(admin_dsn, autocommit=True) as conn:
            assert conn.execute("SELECT to_regnamespace(%s) IS NOT NULL", (psycopg.sql.Identifier(control_schema_name(schema)).as_string(conn),)).fetchone()[0]
    finally:
        with psycopg.connect(admin_dsn, autocommit=True) as conn:
            conn.execute(psycopg.sql.SQL("DROP SCHEMA {} CASCADE").format(psycopg.sql.Identifier(schema)))
            conn.execute(psycopg.sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(psycopg.sql.Identifier(control_schema_name(schema))))


_LEDGER_TABLES = ("memories", "boards", "record_history", "history_counters", "chain_heads", "evidence_objects", "blocks", "attestations", "trust_statements")


@pytest.mark.postgres
def test_v10_rollback_and_forward_again_with_real_ledger_data_changes_no_ledger_row(pg_schema):
    """The earlier rollback test used an empty database. Here the operator and a key tenant have written; every ledger table must be byte-identical after rollback + forward."""
    from app.models import EvidenceLink, MemoryCreate
    from app.pg_store import PostgresRowStore

    assert migrate(pg_schema.admin_dsn, schema=pg_schema.schema, app_role="jarvis_app_test") == 10
    control = control_schema_name(pg_schema.schema)
    with psycopg.connect(pg_schema.admin_dsn, autocommit=True, row_factory=dict_row) as a:
        tenant = tenant_keys.create_tenant(a, control, "writer")
        tenant_keys.create_key(a, control, tenant, PEPPER)
    for who in ("operator", tenant):
        store = PostgresRowStore(pg_schema.app_dsn, who, schema=pg_schema.schema)
        for i in range(3):
            store.create_memory(MemoryCreate(content=f"{who} record {i}", source_agent="t", session_id="s", type="fact", evidence=[EvidenceLink(kind="doc", ref="x", note="y")]))
    pg_store.close_pools()

    def checksums():
        with pg_schema.admin_conn() as conn:
            return {t: conn.execute(psycopg.sql.SQL("SELECT md5(coalesce(string_agg(x::text, ',' ORDER BY x::text), '')) FROM {} x").format(psycopg.sql.Identifier(t))).fetchone()[0] for t in _LEDGER_TABLES}

    before = checksums()
    assert all(before.values())
    with psycopg.connect(pg_schema.admin_dsn, options=f"-c search_path={pg_schema.schema}") as conn:
        conn.execute(v10_rollback_sql(pg_schema.schema))
    assert checksums() == before
    assert migrate(pg_schema.admin_dsn, schema=pg_schema.schema, app_role="jarvis_app_test") == 10
    assert checksums() == before
    with pg_schema.admin_conn() as conn:
        assert conn.execute("SELECT count(*) FROM memories WHERE tenant_key = %s", (tenant,)).fetchone()[0] == 3     # the key tenant's rows survived (unreachable without v10, intact)


# ----------------------------------------------------------------------------------------------------- review findings (Codex, round 2 on #107)
@pg_only
def test_issuing_a_key_and_deleting_the_tenant_cannot_race_either_way_round(pg):
    """The trigger reads the tenant's status FOR SHARE: a concurrent transition to deleting waits for an uncommitted insert (and then revokes that key), and an insert waits for an uncommitted
    transition (and then sees it). Neither order can leave an active key on a deleting tenant."""
    import threading

    ident = psycopg.sql.Identifier(pg.control)
    tenant, _ = _issue(pg)
    open_conns = []

    def conn(autocommit=False):
        c = psycopg.connect(pg.admin_dsn, autocommit=autocommit, row_factory=dict_row, options="-c lock_timeout=20000")
        open_conns.append(c)
        return c

    try:
        # order 1: the transition holds the row; the insert must wait, then be refused
        deleting = conn()
        deleting.execute(psycopg.sql.SQL("UPDATE {}.tenants SET status = 'deleting' WHERE tenant_key = %s").format(ident), (tenant,))      # uncommitted
        outcome = {}

        def insert_while_deleting():
            c = conn(autocommit=True)
            try:
                tenant_keys.create_key(c, pg.control, tenant, PEPPER)
                outcome["insert"] = "issued"
            except psycopg.errors.CheckViolation:
                outcome["insert"] = "refused"

        t = threading.Thread(target=insert_while_deleting)
        t.start()
        t.join(timeout=1.0)
        still_waiting = t.is_alive()
        deleting.commit()
        t.join(timeout=25)
        assert still_waiting, "the insert should be waiting for the uncommitted transition"
        assert outcome["insert"] == "refused"
        # order 2: the insert holds the share lock; the transition must wait, then revoke the new key
        tenant2, _ = _issue(pg)
        inserting = conn()
        new_token = tenant_keys.create_key(inserting, pg.control, tenant2, PEPPER)                                                           # uncommitted

        def delete_while_inserting():
            tenant_keys.set_tenant_status(conn(autocommit=True), pg.control, tenant2, "deleting")

        t2 = threading.Thread(target=delete_while_inserting)
        t2.start()
        t2.join(timeout=1.0)
        still_waiting = t2.is_alive()
        inserting.commit()
        t2.join(timeout=25)
        assert still_waiting, "the transition should be waiting for the uncommitted insert"
        with _app(pg) as c:
            assert tenant_keys.resolve(c, pg.control, new_token, PEPPER) is None                                                         # the key that raced in was revoked
    finally:
        for c in open_conns:                                                                                                              # never leave a transaction open: it would block the fixture's teardown
            try:
                c.rollback()
            except Exception:  # noqa: BLE001
                pass
            c.close()


@pg_only
def test_an_explicit_zero_day_expiry_expires_now_and_a_negative_one_is_refused(pg, monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("JARVIS_DATABASE_MIGRATE_URL", pg.admin_dsn)
    monkeypatch.setenv("JARVIS_DATABASE_SCHEMA", pg.schema)
    monkeypatch.setenv("JARVIS_KEY_PEPPER", PEPPER.decode())
    assert tenant_keys.main(["create-tenant"]) == 0
    tenant = capsys.readouterr().out.strip()
    f = tmp_path / "t0"
    assert tenant_keys.main(["create-key", "--tenant", tenant, "--expires-days", "0", "--token-file", str(f)]) == 0
    with _app(pg) as c, _admin(pg) as a:
        assert tenant_keys.list_keys(a, pg.control, tenant)[0]["expires_at"] is not None          # 0 is an expiry (now), not "never"
        assert tenant_keys.resolve(c, pg.control, f.read_text().strip(), PEPPER) is None
    assert tenant_keys.main(["create-key", "--tenant", tenant, "--expires-days", "-1", "--token-file", str(tmp_path / "t1")]) == 1
    assert tenant_keys.main(["create-key", "--tenant", tenant, "--token-file", str(tmp_path / "t2")]) == 0
    with _admin(pg) as a:
        assert any(k["expires_at"] is None for k in tenant_keys.list_keys(a, pg.control, tenant))   # omitting it still means no expiry


@pg_only
def test_setting_deleted_again_keeps_the_first_deletion_timestamp(pg):
    tenant, _ = _issue(pg)
    with _admin(pg) as a:
        tenant_keys.set_tenant_status(a, pg.control, tenant, "deleted")
        first = tenant_keys.list_tenants(a, pg.control)[0]["deleted_at"]
        assert first is not None
        tenant_keys.set_tenant_status(a, pg.control, tenant, "deleted")
        assert tenant_keys.list_tenants(a, pg.control)[0]["deleted_at"] == first


@pytest.mark.postgres
def test_v10_and_its_rollback_work_for_a_mixed_case_ledger_schema(pg_server):
    """validate_schema_name allows uppercase; v10 creates the control schema as a quoted identifier, so the rollback must quote it too."""
    admin_dsn, _ = pg_server
    schema = "Ledger_MixedCase"
    with psycopg.connect(admin_dsn, autocommit=True) as conn:
        conn.execute(psycopg.sql.SQL("CREATE SCHEMA {}").format(psycopg.sql.Identifier(schema)))
    try:
        assert migrate(admin_dsn, schema=schema, app_role="jarvis_app_test") == 10
        with psycopg.connect(admin_dsn, autocommit=True) as conn:
            assert conn.execute("SELECT count(*) FROM pg_namespace WHERE nspname = 'Ledger_MixedCase_control'").fetchone()[0] == 1
        with psycopg.connect(admin_dsn) as conn:                                                      # (one transaction; the mixed-case schema is a quoted identifier)
            conn.execute(psycopg.sql.SQL("SET LOCAL search_path TO {}").format(psycopg.sql.Identifier(schema)))
            conn.execute(v10_rollback_sql(schema))
        with psycopg.connect(admin_dsn, autocommit=True) as conn:
            assert conn.execute("SELECT count(*) FROM pg_namespace WHERE nspname = 'Ledger_MixedCase_control'").fetchone()[0] == 0
            assert conn.execute(psycopg.sql.SQL("SELECT max(version) FROM {}.schema_version").format(psycopg.sql.Identifier(schema))).fetchone()[0] == 9
    finally:
        with psycopg.connect(admin_dsn, autocommit=True) as conn:
            conn.execute(psycopg.sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(psycopg.sql.Identifier(schema)))
            conn.execute(psycopg.sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(psycopg.sql.Identifier(control_schema_name(schema))))
