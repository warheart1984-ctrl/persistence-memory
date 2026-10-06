"""Evidence Objects on the PostgreSQL row store: append-only, tenant-isolated, verifiable, linkable."""

from __future__ import annotations

import psycopg
import pytest

from app import evidence as ev
from app import pg_store, pg_verify
from app.clause_v import ClauseVViolation
from app.evidence import CES_DECISION, CES_FACT, EvidenceError, EvidenceObjectCreate
from app.models import EvidenceLink, MemoryCreate
from app.pg_schema import EXPECTED_SCHEMA_VERSION, migrate
from app.pg_store import PostgresRowStore

pytestmark = pytest.mark.postgres

FACT = {"observation": "The ledger listens on 127.0.0.1:8011.", "method": "command", "source": "ss -ltn"}
DECISION = {"statement": "Use PostgreSQL row-level security.", "authority": "operator", "source": "chat:triage"}


@pytest.fixture
def pg(pg_schema):
    assert migrate(pg_schema.admin_dsn, schema=pg_schema.schema, app_role="jarvis_app_test") == EXPECTED_SCHEMA_VERSION
    yield pg_schema
    pg_store.close_pools()


@pytest.fixture
def store(pg, monkeypatch):
    monkeypatch.setenv("JARVIS_CLAUSE_V", "enforce")
    monkeypatch.setenv("JARVIS_CLAUSE_V_SOFT", "warn")
    return PostgresRowStore(pg.app_dsn, "alice", schema=pg.schema)


def put(store, schema=CES_FACT, payload=None, **kw):
    return store.put_evidence_object(EvidenceObjectCreate(schema_id=schema, payload=payload or (FACT if schema == CES_FACT else DECISION), **kw))


def test_the_schema_is_at_least_version_5(pg):
    assert EXPECTED_SCHEMA_VERSION >= 5
    with pg.admin_conn() as conn:
        assert conn.execute("SELECT to_regclass('evidence_objects') IS NOT NULL").fetchone()[0]


def test_put_get_and_idempotence(store):
    obj, created = put(store, source_agent="first")
    assert created is True and obj.id == ev.object_id(CES_FACT, FACT) and obj.created_by == "first" and obj.size_bytes > 0
    again, created2 = put(store, source_agent="second")
    assert created2 is False and again == obj  # the first record stands
    assert store.get_evidence_object(obj.id) == obj
    assert store.get_evidence_object("eo:sha256:" + "0" * 64) is None
    assert [o.id for o in store.all_evidence_objects()] == [obj.id]


def test_a_pointer_and_unicode_round_trip_exactly(store):
    pointer = {"uri": "file:///big.log", "sha256": "ab" * 32, "size_bytes": 7_000_000}
    obj, _ = put(store, schema=CES_DECISION, payload=DECISION | {"note": "héllo ✓", "n": 12345678901234}, pointer=pointer)
    back = store.get_evidence_object(obj.id)
    assert back.payload["note"] == "héllo ✓" and back.payload["n"] == 12345678901234 and back.pointer == pointer
    assert ev.verify_stored(back) == []  # what comes out of jsonb still hashes to the id


def test_the_application_role_can_read_and_insert_but_never_update_or_delete(pg, store):
    obj, _ = put(store)
    with pg.app_conn("alice") as conn:
        assert conn.execute("SELECT count(*) FROM evidence_objects").fetchone()[0] == 1
    for statement in ("UPDATE evidence_objects SET created_by = 'x'", "DELETE FROM evidence_objects", "TRUNCATE evidence_objects"):
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            with pg.app_conn("alice") as conn:
                conn.execute(statement)
    assert store.get_evidence_object(obj.id) == obj


def test_even_the_owner_is_stopped_by_the_triggers(pg, store):
    put(store)
    for statement in ("UPDATE evidence_objects SET created_by = 'x'", "DELETE FROM evidence_objects", "TRUNCATE evidence_objects"):
        with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
            with pg.admin_conn() as conn:
                conn.execute(statement)


def test_tenants_cannot_see_each_others_objects(pg, store):
    mine, _ = put(store)
    bob = PostgresRowStore(pg.app_dsn, "bob", schema=pg.schema)
    assert bob.get_evidence_object(mine.id) is None and bob.all_evidence_objects() == []
    theirs, created = put(bob)
    assert created is True and theirs.id == mine.id  # the same content has the same id in every tenant
    with pg.app_conn("alice") as conn:
        assert conn.execute("SELECT count(*) FROM evidence_objects").fetchone()[0] == 1


def test_the_table_refuses_a_malformed_row_whatever_the_application_says(pg):
    with pg.admin_conn() as conn:
        for bad_id in ("eo:sha256:abc", "sha256:" + "a" * 64):
            with pytest.raises(psycopg.errors.CheckViolation):
                conn.execute(
                    "INSERT INTO evidence_objects (tenant_key, id, schema_id, payload, size_bytes, created_by) "
                    "VALUES ('t', %s, 's', '{}', 1, 'x')", (bad_id,))
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute(
                "INSERT INTO evidence_objects (tenant_key, id, schema_id, payload, size_bytes, created_by) "
                "VALUES ('t', %s, 's', '{}', 70000, 'x')", ("eo:sha256:" + "a" * 64,))


def _fact(store, links, **over):
    base = dict(content="The ledger listens on loopback.", source_agent="t", session_id="s", type="fact", evidence=links)
    base.update(over)
    return store.create_memory(MemoryCreate(**base))


def test_a_record_can_link_an_object_and_the_gate_counts_a_fact_evidence(store):
    fact, _ = put(store)
    rec = _fact(store, [EvidenceLink(kind="evidence-object", ref=fact.id)])
    assert rec.evidence[0].ref == fact.id


def test_the_gate_refuses_a_decision_evidence_object_for_a_fact(store):
    decision, _ = put(store, schema=CES_DECISION)
    with pytest.raises(ClauseVViolation) as exc:
        _fact(store, [EvidenceLink(kind="evidence-object", ref=decision.id)])
    assert [r.code for r in exc.value.reasons] == ["clause_v_evidence_required"]
    ok = store.create_memory(MemoryCreate(content="A decision.", source_agent="t", session_id="s", type="decision", evidence=[EvidenceLink(kind="evidence-object", ref=decision.id)]))
    assert ok.type == "decision"


def test_a_dangling_or_foreign_link_is_refused_and_nothing_is_written(pg, store):
    ghost = "eo:sha256:" + "9" * 64
    with pytest.raises(EvidenceError) as exc:
        _fact(store, [EvidenceLink(kind="evidence-object", ref=ghost)])
    assert exc.value.code == "evidence_object_invalid" and exc.value.reasons[0]["code"] == "evidence_object_unresolved"
    bob = PostgresRowStore(pg.app_dsn, "bob", schema=pg.schema)
    bobs, _ = put(bob)
    with pytest.raises(EvidenceError):  # alice cannot cite bob's object
        _fact(store, [EvidenceLink(kind="evidence-object", ref=bobs.id)])
    assert store.list_memories(limit=10) == []


def test_update_checks_links_inside_the_same_transaction(store):
    from app.models import MemoryUpdate

    fact, _ = put(store)
    rec = _fact(store, [EvidenceLink(kind="file", ref="docs/POSTGRES.md")])
    with pytest.raises(EvidenceError):
        store.update_memory(rec.id, MemoryUpdate(evidence=[EvidenceLink(kind="evidence-object", ref="eo:sha256:" + "8" * 64)]))
    upd = store.update_memory(rec.id, MemoryUpdate(status="verified", evidence=[EvidenceLink(kind="evidence-object", ref=fact.id)]))
    assert upd.status == "verified" and upd.evidence[0].ref == fact.id


def test_pg_verify_rehashes_every_object_and_reports_a_forged_one(pg, store, monkeypatch, capsys):
    obj, _ = put(store)
    monkeypatch.setenv("JARVIS_DATABASE_MIGRATE_URL", pg.admin_dsn)
    monkeypatch.setenv("JARVIS_DATABASE_SCHEMA", pg.schema)
    assert pg_verify.main(["--tenant", "alice"]) == 0
    assert "1 evidence object(s) re-hashed" in capsys.readouterr().out
    with pg.admin_conn() as conn:  # a superuser who disables the guard and forges the content, keeping the id
        conn.execute("ALTER TABLE evidence_objects DISABLE TRIGGER evidence_objects_no_update")
        conn.execute("UPDATE evidence_objects SET payload = jsonb_set(payload, '{source}', '\"forged\"')")
        conn.execute("ALTER TABLE evidence_objects ENABLE TRIGGER evidence_objects_no_update")
    assert pg_verify.main(["--tenant", "alice"]) == 1
    out = capsys.readouterr()
    assert obj.id in out.out and "hash mismatch" in out.out and "postgresql://" not in out.out + out.err


def test_a_forged_object_cannot_be_linked(pg, store):
    obj, _ = put(store)
    with pg.admin_conn() as conn:
        conn.execute("ALTER TABLE evidence_objects DISABLE TRIGGER evidence_objects_no_update")
        conn.execute("UPDATE evidence_objects SET payload = jsonb_set(payload, '{source}', '\"forged\"')")
        conn.execute("ALTER TABLE evidence_objects ENABLE TRIGGER evidence_objects_no_update")
    with pytest.raises(EvidenceError) as exc:
        _fact(store, [EvidenceLink(kind="evidence-object", ref=obj.id)])
    assert exc.value.reasons[0]["code"] == "evidence_object_hash_mismatch"
