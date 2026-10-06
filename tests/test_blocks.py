"""Continuity Blocks hashing: Merkle root (RFC 6962) and block hash, with no database."""

from __future__ import annotations

import hashlib

import pytest

from app import blocks

H = [hashlib.sha256(f"entry-{i}".encode()).hexdigest() for i in range(64)]


def sha(*parts: bytes) -> bytes:
    return hashlib.sha256(b"".join(parts)).digest()


def leaf(h: str) -> bytes:
    return sha(b"\x00", bytes.fromhex(h))


def node(a: bytes, b: bytes) -> bytes:
    return sha(b"\x01", a, b)


def bottom_up(hashes: list[str]) -> str:
    """A different construction from blocks._mth (level by level, an unpaired node is carried up unchanged)."""
    level = [leaf(h) for h in hashes]
    while len(level) > 1:
        nxt = [node(level[i], level[i + 1]) for i in range(0, len(level) - 1, 2)]
        if len(level) % 2:
            nxt.append(level[-1])
        level = nxt
    return level[0].hex()


def test_a_single_entry_is_just_its_leaf_hash():
    # the RFC 6962 leaf prefix is the byte 0x00; the empty leaf has the well known hash 6e340b9c...
    assert hashlib.sha256(b"\x00").hexdigest() == "6e340b9cffb37a989ca544e6bb780a2c78901d3fb33738768511a30617afa01d"
    assert blocks.merkle_root([H[0]]) == leaf(H[0]).hex()


def test_two_three_and_four_entries_by_hand():
    a, b, c, d = (leaf(h) for h in H[:4])
    assert blocks.merkle_root(H[:2]) == node(a, b).hex()
    assert blocks.merkle_root(H[:3]) == node(node(a, b), c).hex()  # n=3: split 2 | 1
    assert blocks.merkle_root(H[:4]) == node(node(a, b), node(c, d)).hex()


def test_five_and_six_entries_split_at_the_largest_power_of_two_below_n():
    L = [leaf(h) for h in H[:6]]
    four = node(node(L[0], L[1]), node(L[2], L[3]))
    assert blocks.merkle_root(H[:5]) == node(four, L[4]).hex()
    assert blocks.merkle_root(H[:6]) == node(four, node(L[4], L[5])).hex()


@pytest.mark.parametrize("n", range(1, 65))
def test_the_recursive_rfc_definition_equals_the_level_by_level_construction_the_database_uses(n):
    assert blocks.merkle_root(H[:n]) == bottom_up(H[:n])


def test_order_and_content_matter():
    assert blocks.merkle_root([H[0], H[1]]) != blocks.merkle_root([H[1], H[0]])
    assert blocks.merkle_root([H[0], H[1], H[2]]) != blocks.merkle_root([H[0], H[1]])
    assert blocks.merkle_root([H[0], H[1]]) != blocks.merkle_root([H[0], H[2]])


def test_a_leaf_cannot_pose_as_a_node():
    # second-preimage guard: the two-entry root differs from a "leaf" built from the two child hashes
    a, b = leaf(H[0]), leaf(H[1])
    assert blocks.merkle_root(H[:2]) != sha(b"\x00", a, b).hex()


def test_an_empty_block_and_bad_hashes_are_refused():
    with pytest.raises(ValueError):
        blocks.merkle_root([])
    for bad in ("abc", "G" * 64, H[0].upper(), H[0] + "00", ""):
        with pytest.raises(ValueError):
            blocks.merkle_root([bad])


def _fields(**over):
    base = dict(tenant="alice", height=1, first_seq=1, last_seq=10, entry_count=10,
                prev_block_hash=blocks.GENESIS_HASH, entries_root=H[0])
    base.update(over)
    return base


def test_block_hash_known_answer():
    expected = hashlib.sha256(f"jarvis-block|v1|5:alice|1|1|10|10|{'0' * 64}|{H[0]}".encode()).hexdigest()
    assert blocks.block_hash(**_fields()) == expected


def test_block_hash_length_prefixes_the_tenant_in_bytes():
    e = hashlib.sha256(f"jarvis-block|v1|6:héllo|1|1|10|10|{'0' * 64}|{H[0]}".encode()).hexdigest()
    assert blocks.block_hash(**_fields(tenant="héllo")) == e  # 5 characters, 6 bytes
    # a tenant that tries to smuggle in the next field cannot collide with a different layout
    a = blocks.block_hash(**_fields(tenant="a|1", height=2))
    b = blocks.block_hash(**_fields(tenant="a", height=1))
    assert a != b


@pytest.mark.parametrize("field,value", [
    ("tenant", "bob"), ("height", 2), ("first_seq", 2), ("last_seq", 11), ("entry_count", 9),
    ("prev_block_hash", H[5]), ("entries_root", H[6]), ("fmt", 2),
])
def test_every_field_is_covered_by_the_block_hash(field, value):
    assert blocks.block_hash(**_fields(**{field: value})) != blocks.block_hash(**_fields())


def test_block_hash_refuses_malformed_hashes():
    with pytest.raises(ValueError):
        blocks.block_hash(**_fields(prev_block_hash="zz"))
    with pytest.raises(ValueError):
        blocks.block_hash(**_fields(entries_root="0" * 63))
