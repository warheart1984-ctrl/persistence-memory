"""Replay receipts: CES.Local.ReplayReceipt.v1 evidence objects issued only at sealed points, and their re-derivation."""

from __future__ import annotations

import hashlib

import psycopg
import pytest

from app import evidence, pg_store, replay
from app.evidence import EvidenceError, EvidenceObjectCreate
from app.models import MemoryUpdate
from app.pg_schema import EXPECTED_SCHEMA_VERSION, migrate
from app.pg_store import PostgresRowStore
from tests.test_pg_blocks import BLOCKS_RW, HISTORY_RW, _rewrite_entry_consistently, block_rows, forge, mk, seal, seal_all, tampering

H = hashlib.sha256(b"x").hexdigest()
GOOD = {"contract": "RC.Ledger.v1", "contract_version": 1, "tenant": "alice", "at_seq": 6, "block_height": 2, "block_hash": H,
        "state_root": H, "record_count": 5, "deleted_count": 1}


# --- the schema (no database) -------------------------------------------------------------------------------------

def test_a_good_receipt_payload_is_valid_and_has_a_stable_id():
    assert evidence.validate_payload(evidence.CES_REPLAY_RECEIPT, GOOD) == []
    a = evidence.object_id(evidence.CES_REPLAY_RECEIPT, GOOD)
    assert a == evidence.object_id(evidence.CES_REPLAY_RECEIPT, dict(reversed(list(GOOD.items())))) and a.startswith("eo:sha256:")
    assert a != evidence.object_id(evidence.CES_REPLAY_RECEIPT, GOOD | {"at_seq": 7})


@pytest.mark.parametrize("change", [
    {"contract": ""}, {"tenant": 5}, {"contract_version": 0}, {"contract_version": True}, {"at_seq": -1}, {"at_seq": "6"}, {"block_height": 0},
    {"record_count": -1}, {"deleted_count": 1.5}, {"state_root": "abc"}, {"state_root": H.upper()}, {"block_hash": H[:-1]}, {"extra": "field"},
])
def test_a_malformed_receipt_payload_is_refused(change):
    assert evidence.validate_payload(evidence.CES_REPLAY_RECEIPT, GOOD | change)


@pytest.mark.parametrize("missing", sorted(GOOD))
def test_every_receipt_field_is_required(missing):
    assert evidence.validate_payload(evidence.CES_REPLAY_RECEIPT, {k: v for k, v in GOOD.items() if k != missing})


def test_receipts_refuse_floats_like_every_evidence_object():
    with pytest.raises(EvidenceError) as exc:
        evidence.build_object(EvidenceObjectCreate(schema_id=evidence.CES_REPLAY_RECEIPT, payload=GOOD | {"at_seq": 6.0}, source_agent="t"))
    assert exc.value.code in ("evidence_payload_invalid",)


def test_the_receipt_payload_has_no_timestamp_and_no_free_text():
    assert set(replay.ReplayReceiptPayload.model_fields) == set(GOOD)


def test_a_receipt_only_exists_for_a_sealed_state():
    unsealed = replay.ReplayState(tenant="alice", at_seq=3, history_seq=3, sealed_seq=0, sealed=False, at_block_boundary=False, block=None,
                                  record_count=3, deleted_count=0, state_root=H, records=[], next_after_id=None)
    with pytest.raises(replay.ReplayError) as exc:
        replay.receipt_payload(unsealed)
    assert exc.value.code == "replay_not_sealed"
    sealed = unsealed.model_copy(update={"sealed": True, "block": replay.BlockRef(height=1, first_seq=1, last_seq=3, block_hash=H)})
    assert replay.receipt_payload(sealed) == {"contract": "RC.Ledger.v1", "contract_version": 1, "tenant": "alice", "at_seq": 3, "block_height": 1,
                                              "block_hash": H, "state_root": H, "record_count": 3, "deleted_count": 0}


# --- against a database -------------------------------------------------------------------------------------------

@pytest.fixture
def pg(pg_schema):
    assert migrate(pg_schema.admin_dsn, schema=pg_schema.schema, app_role="jarvis_app_test") == EXPECTED_SCHEMA_VERSION
    yield pg_schema
    pg_store.close_pools()


@pytest.fixture
def store(pg):
    return PostgresRowStore(pg.app_dsn, "alice", schema=pg.schema)


@pytest.fixture
def verify_env(pg, monkeypatch):
    monkeypatch.setenv("JARVIS_DATABASE_MIGRATE_URL", pg.admin_dsn)
    monkeypatch.setenv("JARVIS_DATABASE_SCHEMA", pg.schema)


def offline(pg, receipt_id):
    with psycopg.connect(pg.admin_dsn, options=f"-c search_path={pg.schema}") as conn:
        return replay.verify_receipt(conn, "alice", receipt_id)


def seven_in_three_blocks(pg, store):
    mk(store, 7)
    seal_all(pg, size=3)  # blocks 1-3, 4-6, 7-7


@pytest.mark.postgres
def test_a_receipt_at_a_sealed_point_records_exactly_what_the_replay_produced(pg, store):
    seven_in_three_blocks(pg, store)
    obj, created, state = store.create_replay_receipt(at_block=2)
    blk = {r[0]: r for r in block_rows(pg)}[2]
    assert created is True and obj.schema_id == evidence.CES_REPLAY_RECEIPT and obj.created_by == "operator"
    assert obj.payload == {"contract": "RC.Ledger.v1", "contract_version": 1, "tenant": "alice", "at_seq": 6, "block_height": 2,
                           "block_hash": blk[6], "state_root": state.state_root, "record_count": 6, "deleted_count": 0}
    assert state.state_root == store.replay_state(at_seq=6).state_root
    assert evidence.verify_stored(obj) == [] and store.get_evidence_object(obj.id) == obj


@pytest.mark.postgres
def test_the_same_replay_gives_the_same_receipt_and_a_different_point_a_different_one(pg, store):
    seven_in_three_blocks(pg, store)
    a, created_a, _ = store.create_replay_receipt(at_block=2)
    again, created_again, _ = store.create_replay_receipt(at_block=2)
    by_seq, created_seq, _ = store.create_replay_receipt(at_seq=6)
    assert (created_a, created_again, created_seq) == (True, False, False) and a.id == again.id == by_seq.id
    other, created_other, _ = store.create_replay_receipt(at_block=1)
    assert created_other is True and other.id != a.id


@pytest.mark.postgres
def test_receipts_are_allowed_inside_a_sealed_block_and_default_to_the_newest_block(pg, store):
    seven_in_three_blocks(pg, store)
    mid, _, state = store.create_replay_receipt(at_seq=5)
    assert (mid.payload["at_seq"], mid.payload["block_height"]) == (5, 2) and state.at_block_boundary is False
    tip, _, _ = store.create_replay_receipt()
    assert (tip.payload["at_seq"], tip.payload["block_height"]) == (7, 3)


@pytest.mark.postgres
def test_a_receipt_is_refused_at_a_point_no_sealed_block_covers(pg, store):
    mk(store, 3)
    with pytest.raises(replay.ReplayError) as exc:
        store.create_replay_receipt()
    assert exc.value.code == "replay_nothing_sealed" and exc.value.status == 409
    with pytest.raises(replay.ReplayError) as exc:
        store.create_replay_receipt(at_seq=3)
    assert exc.value.code == "replay_not_sealed"
    seal(pg, force=True)
    mk(store, 2, "later")
    with pytest.raises(replay.ReplayError) as exc:
        store.create_replay_receipt(at_seq=4)  # exists in the history but is not sealed yet
    assert exc.value.code == "replay_not_sealed" and "sealed through seq 3" in exc.value.message
    with pytest.raises(replay.ReplayError) as exc:
        store.create_replay_receipt(at_seq=0)
    assert exc.value.code == "replay_not_sealed"
    with pytest.raises(replay.ReplayError) as exc:
        store.create_replay_receipt(at_seq=99)
    assert exc.value.code == "replay_seq_out_of_range"
    with pytest.raises(replay.ReplayError) as exc:
        store.create_replay_receipt(at_block=9)
    assert exc.value.code == "replay_block_not_found"
    assert store.list_replay_receipts() == []


@pytest.mark.postgres
def test_a_deleted_record_is_counted_in_the_receipt(pg, store):
    a, b, c = mk(store, 3)
    store.delete_memory(b.id)
    seal(pg, force=True)
    obj, _, _ = store.create_replay_receipt()
    assert (obj.payload["record_count"], obj.payload["deleted_count"]) == (2, 1)


@pytest.mark.postgres
def test_receipts_are_tenant_scoped_and_listed_newest_first(pg, store):
    seven_in_three_blocks(pg, store)
    first, _, _ = store.create_replay_receipt(at_block=1)
    second, _, _ = store.create_replay_receipt(at_block=3)
    assert [r.id for r in store.list_replay_receipts()] == [second.id, first.id]
    assert [r.id for r in store.list_replay_receipts(limit=1)] == [second.id]
    bob = PostgresRowStore(pg.app_dsn, "bob", schema=pg.schema)
    assert bob.list_replay_receipts() == []
    with pytest.raises(replay.ReplayError) as exc:
        bob.get_replay_receipt(first.id)
    assert exc.value.code == "receipt_not_found"
    assert store.get_replay_receipt(first.id) == first


@pytest.mark.postgres
def test_get_refuses_an_id_that_is_not_a_receipt(pg, store):
    fact, _ = store.put_evidence_object(EvidenceObjectCreate(schema_id=evidence.CES_FACT, payload={"observation": "o", "method": "command", "source": "s"}))
    with pytest.raises(replay.ReplayError) as exc:
        store.get_replay_receipt(fact.id)
    assert exc.value.code == "not_a_replay_receipt"
    with pytest.raises(replay.ReplayError) as exc:
        store.get_replay_receipt("nope")
    assert exc.value.code == "receipt_id_invalid"
    assert [r.id for r in store.list_replay_receipts()] == []  # the fact is not listed as a receipt


# --- re-deriving a receipt ----------------------------------------------------------------------------------------------

@pytest.mark.postgres
def test_a_receipt_verifies_and_keeps_verifying_after_the_ledger_moves_on(pg, store):
    seven_in_three_blocks(pg, store)
    obj, _, _ = store.create_replay_receipt(at_block=2)
    ok = store.verify_replay_receipt(obj.id)
    assert ok.ok is True and ok.problems == [] and ok.replayed["state_root"] == obj.payload["state_root"] and ok.receipt.block_height == 2
    assert offline(pg, obj.id)["ok"] is True
    mk(store, 4, "afterwards")
    store.update_memory(mk(store, 1, "edited")[0].id, MemoryUpdate(subject="x"))
    seal_all(pg, size=3)
    assert store.verify_replay_receipt(obj.id).ok is True and offline(pg, obj.id)["ok"] is True


def forge_receipt(pg, store, **change):
    """A receipt object inserted behind the API's back: a well-formed, correctly hashed object whose content is wrong."""
    obj, _, _ = store.create_replay_receipt(at_block=2)
    payload = obj.payload | change
    new_id = evidence.object_id(evidence.CES_REPLAY_RECEIPT, payload)
    with pg.admin_conn() as conn:
        conn.execute("INSERT INTO evidence_objects (tenant_key, id, schema_id, payload, size_bytes, created_by) VALUES ('alice', %s, %s, %s::jsonb, 100, 'forger')",
                     (new_id, evidence.CES_REPLAY_RECEIPT, __import__("json").dumps(payload)))
    return new_id


FORGERIES = [
    ({"state_root": "f" * 64}, "state root"),
    ({"record_count": 99}, "record_count"),
    ({"deleted_count": 3}, "deleted_count"),
    ({"block_hash": "e" * 64}, "block's hash"),
    ({"block_height": 3}, "covering block"),
    ({"at_seq": 5}, "state root"),  # a different point than the root was taken at
    ({"tenant": "bob"}, "tenant"),
]


@pytest.mark.postgres
@pytest.mark.parametrize("change,keyword", FORGERIES, ids=[next(iter(c)) for c, _ in FORGERIES])
def test_a_forged_receipt_is_exposed_by_re_deriving_it(pg, store, change, keyword):
    seven_in_three_blocks(pg, store)
    rid = forge_receipt(pg, store, **change)
    served = store.verify_replay_receipt(rid)
    assert served.ok is False and any(keyword in p["problem"] for p in served.problems), served.problems
    raw = offline(pg, rid)
    assert raw["ok"] is False and any(keyword in p["problem"] for p in raw["problems"]), raw["problems"]


@pytest.mark.postgres
def test_a_receipt_object_altered_in_place_is_reported_damaged(pg, store):
    seven_in_three_blocks(pg, store)
    obj, _, _ = store.create_replay_receipt(at_block=2)
    with tampering(pg, ("evidence_objects", "evidence_objects_no_update")) as conn:
        conn.execute("UPDATE evidence_objects SET payload = jsonb_set(payload, '{state_root}', %s::jsonb) WHERE id = %s", ('"' + "a" * 64 + '"', obj.id))
    served = store.verify_replay_receipt(obj.id)
    assert served.ok is False and "damaged" in served.problems[0]["problem"] and "hash mismatch" in served.problems[0]["problem"]
    assert offline(pg, obj.id)["ok"] is False


@pytest.mark.postgres
def test_a_receipt_notices_a_block_that_was_removed_or_replaced(pg, store):
    seven_in_three_blocks(pg, store)
    obj, _, _ = store.create_replay_receipt(at_block=3)  # the newest block
    with tampering(pg, *BLOCKS_RW) as conn:
        conn.execute("DELETE FROM blocks WHERE height = 3")
    served = store.verify_replay_receipt(obj.id)
    assert served.ok is False and any("no sealed block covers seq 7" in p["problem"] for p in served.problems)
    raw = offline(pg, obj.id)
    assert raw["ok"] is False and any(p["check"] == "expected_block" for p in raw["problems"])


@pytest.mark.postgres
def test_a_receipt_closes_the_gap_the_database_alone_cannot_see(pg, store):
    """A consistent rewrite of an entry plus a full re-seal passes every check inside the database.  A receipt taken earlier
    does not: the state root it holds no longer matches what the history now replays to."""
    recs = mk(store, 5)
    store.update_memory(recs[2].id, MemoryUpdate(subject="before the forgery"))
    seal_all(pg, size=4)
    receipt, _, _ = store.create_replay_receipt()
    assert receipt.payload["at_seq"] == 6
    _rewrite_entry_consistently(pg, recs[2].id, 6, "a forged version of this record")
    prev = replay.GENESIS
    for height, *_ in block_rows(pg):
        prev = forge(pg, height, prev_block_hash=prev)["block_hash"]
    with psycopg.connect(pg.admin_dsn, options=f"-c search_path={pg.schema}") as conn:
        assert replay.verify_replay(conn, "alice", at_seq=6)["ok"] is True  # the database alone is satisfied
    served = store.verify_replay_receipt(receipt.id)
    assert served.ok is False
    assert any("state root on replay" in p["problem"] for p in served.problems) and any("block's hash" in p["problem"] for p in served.problems)
    raw = offline(pg, receipt.id)
    assert raw["ok"] is False and {"state_root", "expected_block"} <= {p["check"] for p in raw["problems"]}


@pytest.mark.postgres
def test_a_receipt_whose_history_is_gone_cannot_be_replayed(pg, store):
    seven_in_three_blocks(pg, store)
    obj, _, _ = store.create_replay_receipt(at_block=3)
    with pg.admin_conn() as conn:
        conn.execute("UPDATE history_counters SET last_seq = 5 WHERE tenant_key = 'alice'")
    served = store.verify_replay_receipt(obj.id)
    assert served.ok is False and any("cannot be replayed" in p["problem"] for p in served.problems)
    assert offline(pg, obj.id)["ok"] is False


@pytest.mark.postgres
def test_the_cli_re_derives_a_receipt_from_the_raw_rows(pg, store, verify_env, capsys):
    seven_in_three_blocks(pg, store)
    obj, _, _ = store.create_replay_receipt(at_block=2)
    assert replay.main(["verify", "--tenant", "alice", "--receipt", obj.id]) == 0
    out = capsys.readouterr().out
    assert out.startswith(f"ok: receipt {obj.id} re-derived") and "block 2" in out and obj.payload["state_root"] in out
    assert replay.main(["verify", "--tenant", "alice", "--receipt", obj.id, "--at-seq", "3"]) == 2
    assert replay.main(["verify", "--tenant", "alice", "--receipt", "nope"]) == 2
    assert replay.main(["verify", "--tenant", "alice", "--receipt", "eo:sha256:" + "0" * 64]) == 2
    capsys.readouterr()
    bad = forge_receipt(pg, store, state_root="f" * 64)
    assert replay.main(["verify", "--tenant", "alice", "--receipt", bad]) == 1
    out = capsys.readouterr().out
    assert "PROBLEM" in out and "[state_root]" in out and "postgresql://" not in out


@pytest.mark.postgres
def test_the_cli_refuses_a_fact_object_as_a_receipt(pg, store, verify_env, capsys):
    fact, _ = store.put_evidence_object(EvidenceObjectCreate(schema_id=evidence.CES_FACT, payload={"observation": "o", "method": "command", "source": "s"}))
    assert replay.main(["verify", "--tenant", "alice", "--receipt", fact.id]) == 2
    assert "not_a_replay_receipt" in capsys.readouterr().err


# --- the expected block (what the drill checks against the anchors) --------------------------------------------------------

@pytest.mark.postgres
def test_the_expected_block_check_catches_a_block_that_is_not_the_anchored_one(pg, store, monkeypatch):
    seven_in_three_blocks(pg, store)
    tip = {r[0]: r for r in block_rows(pg)}[3]
    def run(**kw):
        with psycopg.connect(pg.admin_dsn, options=f"-c search_path={pg.schema}") as conn:
            return replay.verify_replay(conn, "alice", at_block=3, **kw)
    assert run(expect_block_hash=tip[6], expect_block_height=3)["ok"] is True
    wrong = run(expect_block_hash="c" * 64)
    assert wrong["ok"] is False and [p["check"] for p in wrong["problems"]] == ["expected_block"] and "not the expected" in wrong["problems"][0]["problem"]
    assert run(expect_block_height=2)["ok"] is False
    monkeypatch.setitem(replay.CHECKS, "expected_block", lambda ctx: [])
    assert run(expect_block_hash="c" * 64)["ok"] is True  # without that check the wrong anchor goes unnoticed


@pytest.mark.postgres
def test_a_missing_expected_block_is_a_problem(pg, store):
    mk(store, 3)  # nothing sealed
    with psycopg.connect(pg.admin_dsn, options=f"-c search_path={pg.schema}") as conn:
        result = replay.verify_replay(conn, "alice", at_seq=3, expect_block_hash="a" * 64)
    assert result["ok"] is False and "no sealed block covers seq 3" in result["problems"][0]["problem"]


@pytest.mark.postgres
def test_the_verifiers_work_as_an_ordinary_row_level_security_bound_role_not_only_as_a_superuser(pg, store):
    """The migrate role the real command line uses is not a superuser: without the tenant set the ledger would look empty."""
    seven_in_three_blocks(pg, store)
    obj, _, _ = store.create_replay_receipt(at_block=2)
    with psycopg.connect(pg.app_dsn, options=f"-c search_path={pg.schema}") as conn:
        result = replay.verify_replay(conn, "alice", at_block=3, expect_block_hash={r[0]: r[6] for r in block_rows(pg)}[3])
        assert result["ok"] is True and result["record_count"] == 7 and result["entry_count"] == 7 and result["block"]["height"] == 3
    with psycopg.connect(pg.app_dsn, options=f"-c search_path={pg.schema}") as conn:
        assert replay.verify_receipt(conn, "alice", obj.id)["ok"] is True
    with psycopg.connect(pg.app_dsn, options=f"-c search_path={pg.schema}") as conn:
        assert replay.verify_replay(conn, "bob")["record_count"] == 0  # another tenant sees none of alice's history
