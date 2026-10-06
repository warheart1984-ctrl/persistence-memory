# Replay Contracts

**Status: partial.** `RC.Ledger.v1`, the ledger's own contract, is implemented on the PostgreSQL row store (read-only, no schema
change). The five domain contracts (`RC.AIKI.v1`, `RC.ARIS.v1`, `RC.SX.v1`, `RC.Lineage.v1`, `RC.Mandala.v1`) are **declared only**:
they have no schema files, no owner and no algorithm here. Not built yet: receipts (a stored, content-addressed record of a replay
at a sealed point), `jarvisctl replay`, and the restore-drill step that replays a restored copy.

## What a Replay Contract is

A registered, versioned, deterministic reconstruction rule: the same input always gives the same output and the same hash. The
registry is `GET /api/jarvis/replay/contracts` (also `schemas/rc/registry.json`). Constitutional Boundary Clause III: replay
reconstructs **what happened, in what order, under what recorded authority, with what evidence**; it does **not** reconstruct any
consumer's domain logic. That is why only the ledger's own contract is implemented here: AIKI, ARIS, SX, Lineage and Mandala keep
their semantics, and their replay logic, to themselves.

## RC.Ledger.v1

Input: the tenant's history up to a point: `at_seq` (a `record_history.seq`; 0 is the empty ledger), or `at_block` (the last entry
of that sealed block), or neither (the current end). Output:

* **state** (`GET /api/jarvis/replay/state`): every record as it was at that seq, exactly as its history entry stored it, with
  the count of records deleted by then and the **state root**. Paged by `after_id` and `limit`; the root always covers the whole
  state. `sealed` says whether the point lies inside a sealed block, `at_block_boundary` whether it is that block's last entry,
  and `block` names the covering block and its hash.
* **events** (`GET /api/jarvis/replay/events`): the ordered history entries, each with op, version, the **recorded actor**
  (whoever the ledger recorded as making the change; there are no signatures), the entry's hashes, the `before` / `after` images
  and its evidence links. An evidence-object link is resolved and re-hashed (`intact`, `missing` or `tampered`); every other link
  kind is a pointer and is reported `not-checked`.

All three endpoints are operator-key only (OAuth user tokens get 403) and answer 501 on the JSON store (it has no history).
Errors carry a code: `replay_seq_out_of_range` (422), `replay_block_not_found` (404), `replay_bound_ambiguous` (422).

### The state root

Nothing is re-serialized, so floats, key order and JSON formatting cannot change it. A record's state at seq S is the `row_hash` of
its newest history entry with `seq <= S`; that hash already commits, through the per-record chain, to the full content. The leaves
are the (record id, row_hash) pairs of the records that exist at S, sorted by id **bytewise**, and the root is the RFC 6962 Merkle
tree hash over them:

```
leaf = sha256( 0x00 || "jarvis-state|v1|" || <id length in bytes>:<id> || "|" || row_hash )
root = MTH(leaves)          empty state: sha256("")
```

The tag keeps a state leaf from ever equalling a block-entry leaf; the id is length-prefixed so no id can pose as an id and a hash.

### Determinism

The output depends only on the tenant's history entries up to `at_seq`: no clock, no randomness, no network. The state at S does not
change when the ledger changes after S, when blocks are sealed, or after a backup and restore (the tests replay the ledger as it was
after every single write of a random create / update / delete / supersede workload, and again on a copy of the history rows in a fresh
schema).

## Verifying a replay

`python -m app.replay verify [--tenant T] [--at-seq N | --at-block H] [--expect-root HEX]` (exit 0 = verified, 1 = problems,
2 = bad request or no database URL; same connection variables as `pg_verify`) replays the entries **from the raw rows**, not
from the SQL that serves the endpoints, and checks:

| check | finds |
|---|---|
| `seq` | an entry removed from the sequence (or `at_seq` beyond the counter) |
| `entry_hash` | an entry whose content no longer matches its `row_hash` (recomputed in Python from the text Postgres renders) |
| `chain_link` | an entry whose `prev_hash` is not the previous entry's hash |
| `state_root` | the replayed root differing from `--expect-root` (a receipt, an anchor, an earlier run) |
| `live_match` | at the current end: a live record that differs from its latest history entry, or is missing or extra |
| `blocks` | any sealed block up to the covering one failing its checks (the same ones `pg_verify` runs) |

`python -m app.replay schemas --write | --check` regenerates / checks the files under `schemas/rc/` (a test fails if they drift
from the models).

## What this does and does not defend against

* An entry altered, removed or reordered, a live row changed behind the history's back, a block altered: found (tests for each,
  and for each check a verifier with that check removed misses what it exists for).
* An entry rewritten **consistently** (its hash, its record's chain, the live row): the record checks pass; the block root of a
  sealed point catches it.
* An entry rewritten consistently **and every later block re-sealed**: the database alone passes. The block anchors in the backups
  catch it, and so will any state root recorded earlier: `--expect-root`, and receipts once they exist.
* **Authority is the recorded actor, not a proof.** Today that is the tenant key (`operator`) plus the record's own `source_agent`;
  nothing is signed.
* A point after the last sealed block is replayable (`sealed: false`) but only chain-verified, not block-anchored.
