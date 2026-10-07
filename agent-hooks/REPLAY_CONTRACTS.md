# Replay Contracts

**Status: partial.** `RC.Ledger.v1`, the ledger's own contract, is implemented on the PostgreSQL row store: state and events as of
a point in the history, receipts at sealed points (stored as Evidence Objects), an offline verifier, `jarvisctl replay`, and a
restore-drill step. No schema change (still v6). The five domain contracts (`RC.AIKI.v1`, `RC.ARIS.v1`, `RC.SX.v1`, `RC.Lineage.v1`,
`RC.Mandala.v1`) are **declared only**: they have no schema files, no owner and no algorithm here. Receipts and the blocks they sit in can
be signed (the signer, signature levels L0/L1/L2 in `replay verify`, the `JARVIS_SIGNATURES` switch: `SIGNATURES.md`), but that is built, not
deployed: **nothing on the live ledger is signed**.

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
  and `block` names the covering block and its hash, plus `signed` and `signature_level` (additive fields; the contract version is still 1):
  `signed` is true only when a valid attestation of that block was verified against the pinned roots (L1 or better), `signature_level` is
  0 unsigned / 1 Mint-signed / 2 root-cosigned, or null when `JARVIS_SIGNATURES=off`. They never enter a receipt, which stays deterministic.
* **events** (`GET /api/jarvis/replay/events`): the ordered history entries, each with op, version, the **recorded actor**
  (whoever the ledger recorded as making the change; history entries carry no signatures), the entry's hashes, the `before` / `after` images
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

`python -m app.replay verify [--tenant T] [--at-seq N | --at-block H] [--expect-root HEX] [--expect-block-hash HEX]` or
`... verify --receipt EO_ID` (exit 0 = verified, 1 = problems, 2 = bad request or no database URL; same connection variables as
`pg_verify`, and it sets the tenant itself so row-level security cannot hide the ledger from a non-superuser role) replays the
entries **from the raw rows**, not from the SQL that serves the endpoints, and checks:

| check | finds |
|---|---|
| `seq` | an entry removed from the sequence (or `at_seq` beyond the counter) |
| `entry_hash` | an entry whose content no longer matches its `row_hash` (recomputed in Python from the text Postgres renders) |
| `chain_link` | an entry whose `prev_hash` is not the previous entry's hash |
| `state_root` | the replayed root differing from `--expect-root` (a receipt, an anchor, an earlier run) |
| `live_match` | at the current end: a live record that differs from its latest history entry, or is missing or extra |
| `blocks` | any sealed block up to the covering one failing its checks (the same ones `pg_verify` runs) |
| `expected_block` | the sealed block covering the point not being the one expected: `--expect-block-hash` (an anchor's hash, as the drill passes it) or a receipt's block |

`python -m app.replay schemas --write | --check` regenerates / checks the files under `schemas/rc/` (a test fails if they drift
from the models).

## Receipts

A receipt records what the replay at a **sealed** point produced: contract and version, tenant, `at_seq`, the covering block's height
and hash, the state root, and the record and deleted counts. It is stored as an Evidence Object, `CES.Local.ReplayReceipt.v1`
(`EVIDENCE_OBJECTS.md`), so it is content-addressed and immutable. There is no timestamp in it, so the same replay at the same point
always gives the same receipt, and asking twice returns the existing one (`created: false`).

* `POST /api/jarvis/replay/receipts` (`{"at_seq": N}` or `{"at_block": H}` or nothing = the end of the newest sealed block; operator
  key; writes must be enabled). A point no sealed block covers is refused (`replay_not_sealed`, 422; `replay_nothing_sealed`, 409).
* `GET /api/jarvis/replay/receipts` (newest first), `GET .../receipts/{id}`, `GET .../receipts/{id}/verify`.
* **Typed-in receipts are impossible through the API**: the generic `POST /api/jarvis/evidence` refuses this schema
  (`evidence_schema_reserved`). A receipt written straight into the database is still just a claim, which is why `verify` exists.
* **`verify` re-derives it**: the stored object must still hash to its id, and replaying at its point must give the same state root, the
  same counts, and the same covering block (height and hash). The service answers from its own SQL; `python -m app.replay verify
  --receipt` does it from the raw rows.
* **A receipt taken now is what exposes a later rewrite.** An entry rewritten consistently with every later block re-sealed passes the
  database's own checks, but no longer replays to the root and block hash a receipt recorded earlier (tested).
* Receipts are Evidence Objects but do **not** count as Clause V fact evidence.

## Operating it

`jarvisctl replay state [--at-seq N | --at-block H]` (the root, the counts, whether the point is sealed) · `receipt [...]` (issue) ·
`receipts` (list) · `check ID` (the service re-derives it) · `verify [--tenant T] [--receipt ID | --at-seq N | --at-block H]
[--expect-root R] [--expect-block-hash H]` (the offline verifier in a one-off container, from the raw rows; exit code passed through).

**The restore drill replays too.** After restoring the newest backup into a scratch database, `jarvisctl drill` replays it at every
tenant's last anchored block (`app.replay verify --at-block H --expect-block-hash <hash from the set's own anchors>`): the state is
rebuilt from the raw entries and the sealed block covering it must be exactly the block the anchors name. With `--prove-detection` it
also alters the newest entry of that block, requires the replay to fail, puts it back exactly, and requires it to pass again. The step
is skipped, with a warning, for a set with no sealed blocks or when the app image predates this module.

## What this does and does not defend against

* An entry altered, removed or reordered, a live row changed behind the history's back, a block altered: found (tests for each,
  and for each check a verifier with that check removed misses what it exists for).
* An entry rewritten **consistently** (its hash, its record's chain, the live row): the record checks pass; the block root of a
  sealed point catches it.
* An entry rewritten consistently **and every later block re-sealed**: the database alone passes. The block anchors in the backups
  catch it, and so does any receipt (or state root) recorded earlier. Only history that was never anchored or receipted is exposed.
* **A receipt is a claim until it is verified**: anyone with the database owner's power can add a receipt object that is well-formed and
  correctly hashed. `verify` exposes one whose content does not replay. A receipt can also carry a Mint-key attestation (made by the host
  signer only after it re-derived the receipt from the raw rows), and `verify` reports it, together with its block's, as **L0 unsigned, L1
  Mint-signed or L2 root-cosigned**; the receipt is only as signed as its block. That is the signer's word about a digest, not proof that
  the replay is right: re-deriving is what shows that. `JARVIS_SIGNATURES=require` makes unsigned or invalid a failure and never reports
  success without a trust root; the default is `warn`. None of this runs on the live ledger yet.
* **Rolling back to a build older than this one while receipts exist** makes that build's `pg_verify` report them as an unknown
  schema. Forward upgrades are unaffected.
* **Authority is the recorded actor, not a proof.** Today that is the tenant key (`operator`) plus the record's own `source_agent`;
  records are not signed, and a signature on a block or receipt does not say who wrote what is in it.
* A point after the last sealed block is replayable (`sealed: false`) but only chain-verified, not block-anchored.
