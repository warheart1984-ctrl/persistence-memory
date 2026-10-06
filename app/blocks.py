"""Continuity Blocks: pure hashing helpers (no database access).

A block seals a contiguous range of one tenant's history entries (``record_history.seq``).  It commits to
those entries through an RFC 6962 Merkle root over their ``row_hash`` values, and to the previous block
through ``prev_block_hash``.  The database computes the same values in SQL (``jarvis_merkle_root`` and
``jarvis_block_hash``); this module is the independent second implementation that ``pg_verify`` uses to
cross-check what the database stored.  See docs/CONTINUITY_BLOCKS.md.
"""

from __future__ import annotations

import hashlib
import re

FORMAT = 1
GENESIS_HASH = "0" * 64

_HEX64 = re.compile(r"^[0-9a-f]{64}$")


def _check_hex64(value: str, what: str) -> bytes:
    if not isinstance(value, str) or not _HEX64.match(value):
        raise ValueError(f"{what} must be 64 lowercase hex characters")
    return bytes.fromhex(value)


def leaf_hash(row_hash: str) -> bytes:
    """RFC 6962 leaf hash: sha256(0x00 || the 32 raw bytes of the entry's row_hash)."""
    return hashlib.sha256(b"\x00" + _check_hex64(row_hash, "row_hash")).digest()


def node_hash(left: bytes, right: bytes) -> bytes:
    """RFC 6962 interior node: sha256(0x01 || left || right)."""
    return hashlib.sha256(b"\x01" + left + right).digest()


def _mth(hashes: list[bytes]) -> bytes:
    # RFC 6962 section 2.1: split at the largest power of two strictly smaller than n.
    n = len(hashes)
    if n == 1:
        return hashes[0]
    k = 1
    while k * 2 < n:
        k *= 2
    return node_hash(_mth(hashes[:k]), _mth(hashes[k:]))


def merkle_tree_hash(leaf_hashes: list[bytes]) -> bytes:
    """The RFC 6962 Merkle tree hash over already-hashed leaves (used by Replay Contracts for the state root)."""
    if not leaf_hashes:
        raise ValueError("a tree needs at least one leaf")
    return _mth(leaf_hashes)


def merkle_root(row_hashes: list[str]) -> str:
    """The Merkle tree hash (hex) of the entries' row_hash values, in seq order.  Refuses an empty list."""
    if not row_hashes:
        raise ValueError("a block must contain at least one entry")
    return _mth([leaf_hash(h) for h in row_hashes]).hex()


def block_hash(
    *, tenant: str, height: int, first_seq: int, last_seq: int, entry_count: int,
    prev_block_hash: str, entries_root: str, fmt: int = FORMAT,
) -> str:
    """sha256 over ``jarvis-block|v<fmt>|<len>:<tenant>|height|first|last|count|prev|root``.

    The tenant is length-prefixed (in bytes) so no tenant name can be confused with the fields after it."""
    _check_hex64(prev_block_hash, "prev_block_hash")
    _check_hex64(entries_root, "entries_root")
    t = tenant.encode("utf-8")
    text = f"jarvis-block|v{fmt}|{len(t)}:{tenant}|{height}|{first_seq}|{last_seq}|{entry_count}|{prev_block_hash}|{entries_root}"
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
