# PostgreSQL ledger store (row-level)

When `JARVIS_DATABASE_URL` is set the ledger lives in PostgreSQL, one row per record, instead of a JSON
file. This document covers what it guarantees, how to run it, how to cut over from the JSON file or the
older JSONB-blob store, and — just as important — what it does **not** make safe.

Requires **PostgreSQL 15+** (the `supersedes` foreign key uses `ON DELETE SET NULL (column)`).

## Selecting a store

| Setting | Meaning |
|---|---|
| `JARVIS_DATABASE_URL` | Connection string **for the application**. Must be an ordinary role (see *Roles*). Unset = JSON file store. |
| `JARVIS_PG_STORE` | `rows` (default) = row-level tables. `blob` = the legacy store (one JSONB document per tenant, last-writer-wins). Anything else fails closed. |
| `JARVIS_DATABASE_SCHEMA` | Optional schema (becomes the `search_path`). |
| `JARVIS_DATABASE_MIGRATE_URL` | Role allowed to run DDL, used by `python -m app.pg_migrate`, `app.pg_import`, `app.pg_verify`. Falls back to `JARVIS_DATABASE_URL`. |
| `JARVIS_DATABASE_APP_ROLE` | Role that `pg_migrate` grants DML to (optional; see *Roles*). |
| `JARVIS_DATABASE_POOL_MAX` / `_CONNECT_TIMEOUT` / `_STATEMENT_TIMEOUT_MS` / `_LOCK_TIMEOUT_MS` | Pool size (10), connect timeout (5 s), statement timeout (10 s), lock timeout (5 s). |
| `JARVIS_LEGACY_BLOB_SCHEMA` | Where to look for the legacy `jarvis_tenant_ledgers` table (default `public`). |
| `JARVIS_PG_IGNORE_LEGACY_BLOB` | `1` disables the legacy-data guard (see *Cutover*). |

Tenants: with OAuth, each subject is an opaque `tenant_key`; without OAuth there is one tenant, `operator`.

## What it guarantees

* **Fail closed.** Any database error — outage, timeout, missing or mismatched schema, unexpected
  constraint failure — is HTTP 503 with a generic body (`/health` reports `unavailable`; MCP clients get
  "Ledger store unavailable"). Detail goes to the `jarvis.store` log only. There is never a fallback to
  another store, and the schema version must match the code exactly.
* **Invalid data cannot exist.** CHECK constraints mirror the models: confidence 0–1, type and status
  enums, content 1–2000 chars, ≤ 32 tags, ≤ 32 evidence links, 64-hex `content_sha256`, and so on.
  `supersedes` is a per-tenant foreign key; deleting a target nulls the pointer (the history keeps it).
* **No lost updates.** Each record has a `version`, owned by the database. Updates are read-modify-write
  guarded by `WHERE version = :seen` and retried (5 attempts, jittered backoff). Under heavy contention on
  one record an update can still lose every retry and returns **409**; clients may pass
  `expected_version` on `PATCH` to be told about a conflict instead of having the update applied on top of
  newer state (409 on mismatch). Deletes are a single atomic statement.
* **Two tenant fences.** Every query filters on `tenant_key`, and row-level security (forced, so it also
  applies to the table owner) only exposes rows matching the per-transaction `jarvis.tenant_key`.
* **Append-only history.** Every create, update and delete is recorded by database triggers in the same
  transaction as the change (so any writer is covered), with before/after snapshots, actor, a gapless
  per-tenant sequence number and a per-record sha256 hash chain. Deleted records keep their history and a
  chain head. Rows that existed when history capture began, or that were imported, carry `op='backfill'`
  — a snapshot at that time, never a claimed past edit. `GET /api/jarvis/memory/{id}/history` reads it;
  `GET /api/jarvis/memory/history/verify` and `python -m app.pg_verify [--tenant T | --all]` recompute the
  chain and check sequence gaps, chain heads and live rows.

## Roles

Run the application as an **ordinary role**: not a superuser, no `BYPASSRLS`. The store refuses to serve
(503) otherwise, because those roles skip row-level security.

Recommended, two roles:

1. A *migrator/owner* role (`JARVIS_DATABASE_MIGRATE_URL`) that owns the tables and runs
   `python -m app.pg_migrate`.
2. An *application* role (`JARVIS_DATABASE_URL`) that `pg_migrate` grants only what it needs
   (`JARVIS_DATABASE_APP_ROLE=<name>`): DML on `memories` and `boards`, **read-only** on `record_history`,
   `chain_heads`, `history_counters`. It has no DDL, and the history tables can only be written by the
   `SECURITY DEFINER` capture trigger, so the application cannot forge or edit history.

**Single-role deployments** (for example Render's managed Postgres, where the one database user owns
everything) work, but the application is then the owner: it could disable RLS or alter the history tables.
The checks above still catch accidents and partial tampering, not a compromised application.

## Cutover from the JSON file (or the JSONB-blob store)

Do this with the service stopped or read-only; the importer re-checks the source hash and aborts on change.

1. `python -m app.pg_migrate` — creates/upgrades the schema (idempotent, atomic).
2. **Dry run** (the default; nothing is kept):
   `python -m app.pg_import --source data/jarvis-store.json`
   It imports inside a transaction, verifies counts, per-record hashes, board and history chain, then rolls
   back, and lists any problem (over-long content, dangling `supersedes`, cycles, bad timestamps). Resolve
   problems at the source, or pass `--null-dangling-supersedes` knowingly (dropped pointers are listed).
3. **Apply:** `python -m app.pg_import --source … --apply --manifest /somewhere/else/manifest.json`
   (manifest: counts and per-record hashes; refused next to the source file).
4. **Verify:** `python -m app.pg_import --source … --verify` and `python -m app.pg_verify`.
5. Set `JARVIS_DATABASE_URL`, make sure `JARVIS_PG_STORE` is `rows` (or unset), start the service.

The importer only reads the source: the JSON file is never written, renamed or deleted. Keep it as your
rollback. `--source-blob TENANT_KEY --tenant NAME` imports a legacy blob ledger the same way (the blob
table is only read).

**Legacy-data guard.** If a tenant still has records in the legacy `jarvis_tenant_ledgers` table while its
row store is empty, the store refuses to serve (503) instead of quietly starting a second, empty ledger.
Import the data, keep the old store with `JARVIS_PG_STORE=blob`, or override with
`JARVIS_PG_IGNORE_LEGACY_BLOB=1`.

**Render.** `render.yaml` pins `JARVIS_PG_STORE=blob` so that merging this code cannot switch a running
deployment's store by itself (`autoDeployTrigger: commit`). Migrate and import first, then change it to
`rows`.

**Rollback.** Point back at the JSON file (unset `JARVIS_DATABASE_URL`). Anything written to Postgres after
the cutover is not in the JSON file; export it first if you need it.

## What is still per-instance

The ledger and board are shared and safe across workers and instances. These are **not** — each process
or instance keeps its own copy, in files or memory:

* the AMUL field (`JARVIS_AMUL_PATH`, per tenant under `tenants/`) and its GC checkpoints,
* short-term memory (STM) sessions,
* the EMR reinforcement/dynamics overlay (`JARVIS_EMR_DYNAMICS_PATH`),
* RAG documents, query logs and replay data, and the LLM adapter logs.

Running several workers or instances therefore gives each its own STM, reinforcement state and AMUL/RAG
files while sharing one ledger. Keep those components to a single instance (or accept that divergence)
until they are moved into the database.

## Testing

* `scripts/test-postgres.sh` starts a throwaway container (its own port and password, removed afterwards)
  and runs the JSON backend plus the Postgres tests, then the whole suite on the row store.
* Postgres tests need `JARVIS_TEST_PG_DSN` (a disposable **superuser** connection); they skip without it.
* `JARVIS_TEST_BACKEND=postgres` runs every `get_store()`/HTTP test against a fresh migrated schema,
  including the red-team tests. Tests about the JSON file itself are marked `json_store_only`; their
  Postgres counterparts live in `tests/test_pg_*.py`. CI runs both (`test-postgres` job).

## Not addressed / follow-ups

* **Export chain-head hashes outside the database.** A database owner (or superuser) can rewrite the
  history, chain heads and counters together consistently; the verifier only catches partial tampering.
  Follow-up: periodically export each tenant's latest `(seq, chain-head hashes)` to somewhere the database
  owner cannot rewrite (object storage with object lock, a git repository, a transparency log), and have
  `pg_verify` compare against the last exported anchor. Not implemented.
* **Deletion does not erase content.** A hard delete removes the row, but its before-snapshot stays in
  `record_history` by design. Any erasure/retention workflow has to account for that and needs a
  deliberate, documented tombstoning path (it would break the hash chain by design). Not implemented.
* **`emr_upsert` is not atomic.** It creates the new draft and archives the target as two separate store
  calls; a failure between them leaves both visible. Not addressed here.
* **Self-minted `verified`** status, unbounded request sizes elsewhere, and `/health` information leakage
  are unchanged from the earlier red-team list.
* **Writes within one tenant serialize on that tenant's history counter** until commit (the price of a
  gapless sequence). Fine for a memory ledger; revisit if one tenant needs high write concurrency.
* Retry exhaustion under extreme contention returns 409 rather than waiting; there is no queue.
