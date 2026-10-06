# Continuity Blocks

**Status: partial (schema v6, database layer only).** Blocks can be sealed and verified inside the database. Not built
yet: the HTTP endpoints, `jarvisctl seal` and its timer, and the block head in the backup anchors (next PR). Nothing seals
by itself until that timer is switched on. Blocks are **unsigned** (signatures are deferred, together with Evidence Object
signing).

## What a block is

A block seals a **contiguous range of one tenant's history entries**: `record_history.seq` from `first_seq` to
`last_seq`. It stores no copy of the entries and changes nothing in the history tables. Membership is the seq range.

| Column | Meaning |
|---|---|
| `height` | 1, 2, 3, ... per tenant, no holes |
| `first_seq`, `last_seq`, `entry_count` | the sealed range; each block starts right after the previous one ends |
| `entries_root` | RFC 6962 Merkle root over the entries' `row_hash` values, in seq order |
| `prev_block_hash` | the previous block's `block_hash` (64 zeros for height 1) |
| `block_hash` | `sha256("jarvis-block|v1|<bytes>:<tenant>|height|first|last|count|prev|root")` |
| `sealed_at`, `sealed_by`, `format` | when, by whom, and the hash format (1) |

Merkle: leaf = `sha256(0x00 || row_hash bytes)`, node = `sha256(0x01 || left || right)`, split at the largest power of two
below n. The root allows an inclusion proof of any entry in about log2(n) hashes; nothing consumes one yet.

## Sealing

`jarvis_seal_block(tenant, min_entries = 500, max_age = '1 hour', force = false, max_entries = 10000)`.

* Seals the unsealed tail when it holds at least `min_entries` entries or its oldest entry is older than `max_age`
  (or when forced). Never seals an empty range. At most `max_entries` per block; call again for the rest.
* Runs as the table owner. The application role can only call it; it cannot write a block row, and the hashes are computed
  in the database, never supplied by the caller.
* Not part of the write path: ordinary writes never wait for a seal and a seal never waits for them. Every write holds the
  tenant's history counter row until it commits, so commit order is seq order and any counter value a seal reads is backed
  by committed entries. Sealers are serialised by an advisory lock and need `READ COMMITTED`.
* The session tenant (`jarvis.tenant_key`) must equal the tenant argument.

## Verification

`python -m app.pg_verify` now also checks blocks. The database function `jarvis_verify_blocks` checks: no missing height,
`prev_block_hash` chain, blocks tile the history without gap or overlap, no block past the history counter, entry counts,
the Merkle root recomputed from the entries, and the block hash. `pg_verify` then **recomputes all of it again in Python**
(`app/blocks.py`, the RFC's recursive definition) so a bug or a tampered function in one implementation is caught by the
other, and re-resolves every evidence object cited by a sealed entry (missing or altered is a block-level error).
It reports `N block(s) sealed through seq S, U entries unsealed`, and warns (exit code still 0) when the unsealed tail is
more than 24 hours old, which means the seal timer has probably stopped.

## What this does and does not defend against

| Tamper | Caught by |
|---|---|
| a sealed history entry rewritten *and* its record chain, head and live row made consistent | the block's Merkle root (nothing else) |
| a block field altered | block hash / root / link checks |
| an entry removed from a sealed range | entry count and root |
| a middle or first block removed | height gap, prev link, tiling |
| history counter lowered | the block that reaches past it |
| **the newest block removed** | **not by the database alone** |
| **an entry rewritten and every later block re-sealed consistently** | **not by the database alone** |

The last two need a hash kept **outside** the database: the block head (`height` and `block_hash`) will be written into the
backup anchors (`anchors.sh`) in the next PR, and `backup.sh` already refuses an anchor set that is not a successor of the
last one. Until then these are known limits; `tests/test_pg_blocks.py` pins both so they cannot be mistaken for guarded cases.

## Migration and rollback

Schema v6 is additive: the `blocks` table, its triggers (UPDATE, DELETE and TRUNCATE refused), row-level security per
tenant, four functions and grants. No existing table is altered; a test compares every existing table byte for byte before
and after the migration and after the first seal. The code checks the schema version exactly, so v5 code refuses a v6
database. **Rollback is restoring the backup taken before the migration** (writes made after the migration are lost).
Take and drill a backup first.

## Tests

`tests/test_blocks.py` (hashing, known answers, RFC equivalence for n = 1..64) and `tests/test_pg_blocks.py`
(sealing rules, immutability and privileges, history untouched, concurrency with three sealers racing four writers, tamper
and missing-block proofs, and mutation checks). The mutation checks build a copy of the verifier without one named check
(`pg_schema._BLOCK_CHECKS`) and show that a tamper consistent in every other respect is flagged by the real verifier and
missed by the copy; a second set shows the Python recomputation catches each tamper even when the SQL verifier checks nothing.
