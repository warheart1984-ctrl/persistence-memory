# Evidence Objects

**Status: partial.** Content-addressed, immutable evidence objects exist, with hashes only and a minimal local schema.
There are **no signatures** (that is a root-authority question, deliberately not answered here), no Replay Contracts yet (Continuity Blocks exist, partial and unsigned: `CONTINUITY_BLOCKS.md`). An Evidence Object proves *what was recorded*, not who vouches for it.

## What it is

An evidence *link* (`evidence: [{kind, ref, note}]`) is only a pointer: nothing proves the thing it points at existed or
stayed unchanged. An Evidence Object is the thing itself, small enough to store.

```
id = "eo:sha256:" + sha256( canonical JSON of {"schema_id", "payload", "pointer"?} )
```

Canonical JSON: keys sorted, no whitespace, UTF-8 (not `\u` escapes), no NaN. The id is the hash, so the same content
always has the same id and one changed byte gives a different one. **Floating-point numbers are refused** (use a string or
an integer): a float can come back from storage in a different textual form and break the hash.

Limits: the canonical envelope is at most **64 KB**; nesting at most 8 deep. Larger evidence is recorded as a
`pointer` `{uri, sha256, size_bytes, media_type?}` to the external content. The service does **not** fetch a pointer: it
records the hash and says "not checked" when verifying.

## The minimal local CES

These schemas are local to this ledger. They are not the CCS charter's registered `CES.*` schemas (those stubs are
not in this repository). Extra payload fields are allowed and are part of the hash.

| Schema | Required | Optional |
|---|---|---|
| `CES.Local.DecisionEvidence.v1` | `statement`, `authority`, `source` (non-empty strings) | `decided_at` (ISO-8601) |
| `CES.Local.FactEvidence.v1` | `observation`, `source`, `method` (one of `file`, `url`, `commit`, `test`, `receipt`, `command`, `document`, `doc`, `issue`, `pr`, `log`) | `excerpt`, `observed_at` (ISO-8601) |
| `CES.Local.ReplayReceipt.v1` | `contract`, `contract_version`, `tenant`, `at_seq`, `block_height`, `block_hash`, `state_root`, `record_count`, `deleted_count` (no other fields) | none. **Reserved**: only `POST /api/jarvis/replay/receipts` creates one, from a replay at a sealed point (`REPLAY_CONTRACTS.md`); the generic create route refuses it, and it does not count as Clause V fact evidence |

## API (operator key only)

| Call | What |
|---|---|
| `POST /api/jarvis/evidence` | `{schema_id, payload, pointer?, source_agent?, id?}`. Idempotent: the same content returns the existing object with `created: false`. A supplied `id` that is not the hash is refused. Never available through an OAuth user token |
| `GET /api/jarvis/evidence/{id}` | the object |
| `GET /api/jarvis/evidence/{id}/verify` | recomputes the hash and re-checks the schema; reports a pointer's content as not checked |

There is no update or delete route. Refusals are HTTP 422 (413 when too large) with a stable `code`:
`evidence_schema_unknown`, `evidence_payload_invalid`, `evidence_too_large`, `evidence_hash_mismatch`,
`evidence_id_invalid`, and (for links) `evidence_object_invalid` with per-reference reasons
`evidence_object_unresolved` or `evidence_object_hash_mismatch`.

## Linking from a record

`{"kind": "evidence-object", "ref": "eo:sha256:<hash>"}`. The ledger refuses a link that does not resolve to an intact
object in the same tenant (whatever the Clause V mode). For the Clause V gate, an evidence-object link counts as
checkable evidence for `fact`, `architecture` and `research` only when it resolves to a **FactEvidence** object; a
`decision` may cite either kind (and still accepts a `user-request` link).

## Integrity

* PostgreSQL row store (`evidence_objects`, schema v5): the application role can only `SELECT` and `INSERT`; triggers
  refuse `UPDATE`, `DELETE` and `TRUNCATE` for everyone; row-level security isolates tenants.
* `python -m app.pg_verify` (and `jarvisctl verify`) re-hash every object of the tenant and report any mismatch.
* Backups include the table; the restore and the drill compare its row count when the set has one. Older sets and
  older databases (schema v4) keep working unchanged.
* The JSON file store keeps objects in an append-only `<ledger>.evidence.jsonl` next to the ledger file (local use and
  tests). The legacy JSONB-blob store does not support evidence objects (HTTP 501).

## Not claimed

Signatures, authorship proof, that a pointer's content still matches, that the evidence is true, or any CCS root
authority. Continuity Blocks are in (`CONTINUITY_BLOCKS.md`); next in the roadmap: Replay Contracts.
