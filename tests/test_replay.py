"""RC.Ledger.v1 without a database: the state root, the entry hash, the fold, the registry and the published schemas."""

from __future__ import annotations

import hashlib
import json

import pytest

from app import blocks, replay

H = [hashlib.sha256(f"row-{i}".encode()).hexdigest() for i in range(40)]


def sha(*parts: bytes) -> bytes:
    return hashlib.sha256(b"".join(parts)).digest()


def leaf(mid: str, row_hash: str) -> bytes:
    ident = mid.encode()
    return sha(b"\x00", f"jarvis-state|v1|{len(ident)}:".encode(), ident, b"|", row_hash.encode())


def node(a: bytes, b: bytes) -> bytes:
    return sha(b"\x01", a, b)


# --- the state root ----------------------------------------------------------------------------------------------

def test_the_empty_state_has_the_empty_tree_hash():
    assert replay.EMPTY_ROOT == hashlib.sha256(b"").hexdigest() == "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    assert replay.state_root([]) == replay.EMPTY_ROOT


def test_one_record_is_just_its_leaf():
    assert replay.state_root([("mem-a", H[0])]) == leaf("mem-a", H[0]).hex()


def test_two_and_three_records_by_hand():
    a, b, c = leaf("mem-a", H[0]), leaf("mem-b", H[1]), leaf("mem-c", H[2])
    assert replay.state_root([("mem-a", H[0]), ("mem-b", H[1])]) == node(a, b).hex()
    assert replay.state_root([("mem-a", H[0]), ("mem-b", H[1]), ("mem-c", H[2])]) == node(node(a, b), c).hex()


def test_the_input_order_does_not_matter_the_ids_are_sorted_bytewise():
    pairs = [("mem-b", H[1]), ("mem-a", H[0]), ("mem-c", H[2])]
    assert replay.state_root(pairs) == replay.state_root(sorted(pairs)) == replay.state_root(list(reversed(pairs)))
    # bytewise, not locale: uppercase sorts before lowercase, and a multibyte id sorts by its UTF-8 bytes
    ordered = [("Z", H[0]), ("a", H[1]), ("é", H[2])]
    assert replay.state_root(ordered) == blocks.merkle_tree_hash([leaf(i, h) for i, h in ordered]).hex()
    assert replay.state_root(list(reversed(ordered))) == replay.state_root(ordered)


def test_an_id_with_a_different_content_or_a_moved_hash_changes_the_root():
    base = replay.state_root([("mem-a", H[0]), ("mem-b", H[1])])
    assert replay.state_root([("mem-a", H[2]), ("mem-b", H[1])]) != base
    assert replay.state_root([("mem-a", H[1]), ("mem-b", H[0])]) != base  # the same hashes under swapped ids
    assert replay.state_root([("mem-a", H[0])]) != base


def test_the_id_is_length_prefixed_so_no_id_can_pose_as_an_id_and_a_hash():
    assert leaf("a|" + H[0][:10], H[1]) != leaf("a", H[0][:10] + H[1])
    assert replay.state_leaf("a", H[0]) != replay.state_leaf("a" + "\x00", H[0])


def test_a_state_leaf_can_never_equal_a_block_entry_leaf():
    assert replay.state_leaf("mem-a", H[0]) != blocks.leaf_hash(H[0])
    assert replay.state_root([("mem-a", H[0])]) != blocks.merkle_root([H[0]])


@pytest.mark.parametrize("n", range(1, 41))
def test_the_root_matches_a_level_by_level_construction(n):
    pairs = [(f"mem-{i:03d}", H[i]) for i in range(n)]
    level = [leaf(i, h) for i, h in pairs]
    while len(level) > 1:
        nxt = [node(level[i], level[i + 1]) for i in range(0, len(level) - 1, 2)]
        if len(level) % 2:
            nxt.append(level[-1])
        level = nxt
    assert replay.state_root(pairs) == level[0].hex()


def test_bad_input_is_refused():
    with pytest.raises(ValueError):
        replay.state_root([("a", H[0]), ("a", H[1])])
    for bad in ("abc", H[0].upper(), H[0] + "0", ""):
        with pytest.raises(ValueError):
            replay.state_leaf("a", bad)


# --- the entry hash --------------------------------------------------------------------------------------------------

def test_the_entry_hash_is_the_documented_concatenation():
    expect = hashlib.sha256(f'{"0" * 64}|create|1||{{"id": "x"}}'.encode()).hexdigest()
    assert replay.entry_row_hash("0" * 64, "create", 1, None, '{"id": "x"}') == expect
    expect = hashlib.sha256(f"{H[0]}|delete|3|{{\"id\": \"x\"}}|".encode()).hexdigest()
    assert replay.entry_row_hash(H[0], "delete", 3, '{"id": "x"}', None) == expect


def test_every_field_of_an_entry_is_covered_by_its_hash():
    base = replay.entry_row_hash(H[0], "update", 2, '{"a": 1}', '{"a": 2}')
    for args in ((H[1], "update", 2, '{"a": 1}', '{"a": 2}'), (H[0], "create", 2, '{"a": 1}', '{"a": 2}'), (H[0], "update", 3, '{"a": 1}', '{"a": 2}'),
                 (H[0], "update", 2, '{"a": 9}', '{"a": 2}'), (H[0], "update", 2, '{"a": 1}', '{"a": 9}')):
        assert replay.entry_row_hash(*args) != base


# --- the fold ---------------------------------------------------------------------------------------------------------

def entry(seq, mid, op, row_hash=None, after="{}"):
    return replay.Entry(seq=seq, memory_id=mid, op=op, version=1, prev_hash="0" * 64, row_hash=row_hash or H[seq], before_text=None, after_text=after)


def test_the_fold_keeps_the_newest_entry_up_to_the_seq_and_drops_deleted_records():
    es = [entry(1, "a", "create"), entry(2, "b", "create"), entry(3, "a", "update"), entry(4, "b", "delete"), entry(5, "c", "backfill")]
    at5 = replay.fold_state(es, 5)
    assert sorted(at5.live) == ["a", "c"] and at5.live["a"].seq == 3 and at5.deleted == 1
    at3 = replay.fold_state(es, 3)
    assert sorted(at3.live) == ["a", "b"] and at3.live["a"].seq == 3 and at3.deleted == 0
    at2 = replay.fold_state(es, 2)
    assert at2.live["a"].seq == 1 and sorted(at2.live) == ["a", "b"]
    assert replay.fold_state(es, 0).live == {} and replay.fold_state([], 7).live == {}


def test_the_fold_does_not_depend_on_the_order_entries_arrive_in():
    es = [entry(1, "a", "create"), entry(2, "a", "update"), entry(3, "a", "delete"), entry(4, "a", "create")]
    assert replay.fold_state(es, 4).live["a"].seq == 4 == replay.fold_state(list(reversed(es)), 4).live["a"].seq
    assert replay.fold_state(es, 3).deleted == 1 and replay.fold_state(es, 3).live == {}


# --- the registry and the published schemas ----------------------------------------------------------------------------

def test_only_rc_ledger_v1_is_implemented_and_the_five_domain_contracts_are_declared():
    assert replay.REGISTRY["RC.Ledger.v1"].status == "implemented" and replay.REGISTRY["RC.Ledger.v1"].algorithm
    domain = {k: v for k, v in replay.REGISTRY.items() if k != "RC.Ledger.v1"}
    assert sorted(domain) == ["RC.AIKI.v1", "RC.ARIS.v1", "RC.Lineage.v1", "RC.Mandala.v1", "RC.SX.v1"]
    for spec in domain.values():
        assert spec.status == "declared" and spec.algorithm is None and spec.owner is None and spec.schemas == []


def test_the_published_schemas_are_exactly_what_the_models_produce():
    for name, text in replay.expected_schema_files().items():
        assert (replay.SCHEMA_DIR / name).read_text("utf-8") == text, f"schemas/rc/{name} is stale: python -m app.replay schemas --write"
    on_disk = {p.name for p in replay.SCHEMA_DIR.iterdir()}
    assert on_disk == set(replay.expected_schema_files())  # nothing stray, in particular no stub for a contract that does not exist


def test_the_schema_check_command_reports_stale_files(tmp_path, monkeypatch):
    monkeypatch.setattr(replay, "SCHEMA_DIR", tmp_path)
    assert replay.main(["schemas", "--check"]) == 1  # nothing there yet
    assert replay.main(["schemas", "--write"]) == 0 and replay.main(["schemas", "--check"]) == 0
    (tmp_path / "registry.json").write_text("{}\n")
    assert replay.main(["schemas", "--check"]) == 1


def test_the_schemas_describe_the_inputs_and_outputs():
    state = json.loads((replay.SCHEMA_DIR / "RC.Ledger.v1.state.schema.json").read_text())
    assert {"state_root", "at_seq", "sealed", "block", "records", "record_count"} <= set(state["properties"])
    inp = json.loads((replay.SCHEMA_DIR / "RC.Ledger.v1.input.schema.json").read_text())
    assert {"at_seq", "at_block", "after_id", "limit"} == set(inp["properties"])
    events = json.loads((replay.SCHEMA_DIR / "RC.Ledger.v1.events.schema.json").read_text())
    assert "events" in events["properties"] and "next_from_seq" in events["properties"]
