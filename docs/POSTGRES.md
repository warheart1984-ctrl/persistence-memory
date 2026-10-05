# PostgreSQL ledger store (row-level)

When `JARVIS_DATABASE_URL` is set the ledger lives in PostgreSQL, one row per record, instead of a JSON
file. This document covers what it guarantees, how to run it, how to cut over from the JSON file or the
older JSONB-blob store, and — just as important — what it does **not** make safe.

Requires **PostgreSQL 15+** (the `supersedes` foreign key uses `ON DELETE SET NULL (column)`).

## Selecting a store

| Setting | Meaning |
|---|---|
| `JARVIS_DATABASE_URL` | Connection string **for the application**. Must be an ordinary role (see *Roles*). Unset = **no ledger** (503) unless `JARVIS_STORE_BOOTSTRAP` opts in to the JSON file store. |
| `JARVIS_PG_STORE` | `rows` (default) = row-level tables. `blob` = the legacy store (one JSONB document per tenant, last-writer-wins). Anything else fails closed. |
| `JARVIS_DATABASE_SCHEMA` | Optional schema (becomes the `search_path`). |
| `JARVIS_STORE_BOOTSTRAP` | `1`/`true`/`yes`/`on` allows the local JSON file store when no database URL is set (first run, local development, tests). Off by default: with no database and no opt-in every ledger route and `/ready` answer 503 `ledger_unavailable`, and no `data/` folder or JSON file is ever created. |
| `JARVIS_DATABASE_MIGRATE_URL` | Role allowed to run DDL, used by `python -m app.pg_migrate`, `app.pg_import`, `app.pg_verify`. Falls back to `JARVIS_DATABASE_URL`. |
| `JARVIS_DATABASE_APP_ROLE` | Role that `pg_migrate` grants DML to (optional; see *Roles*). |
| `JARVIS_DATABASE_POOL_MAX` | Fixed pool size (10). The pool never grows. |
| `JARVIS_DATABASE_POOL_TIMEOUT_MS` | How long a request waits for a free connection (1000). Then 503. |
| `JARVIS_DATABASE_POOL_MAX_WAITING` | How many requests may wait at all (= pool size). Beyond that: instant 503. |
| `JARVIS_DATABASE_CONNECT_TIMEOUT` / `_STATEMENT_TIMEOUT_MS` / `_LOCK_TIMEOUT_MS` | Connect (5 s), statement (10 s) and lock (5 s) timeouts. |
| `JARVIS_RETRY_AFTER_SECONDS` | Value of `Retry-After` on every 503 (default 5; 1-3600). |
| `JARVIS_LEGACY_BLOB_SCHEMA` | Where to look for the legacy `jarvis_tenant_ledgers` table (default `public`). |
| `JARVIS_PG_IGNORE_LEGACY_BLOB` | `1` disables the legacy-data guard (see *Cutover*). |

Tenants: with OAuth, each subject is an opaque `tenant_key`; without OAuth there is one tenant, `operator`.

## What it guarantees

* **Fail closed.** Any database error — outage, timeout, missing or mismatched schema, unexpected
  constraint failure — is HTTP 503 with a generic body (`/ready` reports `unavailable`; MCP clients get
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

## Error contract

Three refusals look different on purpose; clients should react to the **code**, not the status text.

| Situation | HTTP | `code` | `Retry-After` | What a client should do |
|---|---|---|---|---|
| The ledger cannot be served now (database down, timeout, pool saturated, schema/role problem, legacy data not imported) | 503 | `ledger_unavailable` | yes | Back off. Retry **at most once**, after `Retry-After` plus jitter. **Do not** treat it like a version conflict or retry in a loop. |
| The record changed under you (`expected_version` stale, or retries exhausted under contention) | 409 | `version_conflict` | no | Re-read the record, decide, resend with the new version. Retrying the same request unchanged cannot help. |
| Not allowed (bad or missing key/token, missing scope, writes disabled) | 401 / 403 | `denied` | no | Do not retry; fix the credential or the request. |

Any other 503 (for example a deployment missing a required key) carries `Retry-After` and the generic
code `unavailable`. Everything else (400, 404, 422, ...) keeps its previous shape. The same codes appear
on MCP tool errors as `structuredContent.error.code`. `detail` is human-readable and never contains
hosts, credentials or record ids; those go to the `jarvis.store` log.

**Timeouts.** Statement and lock timeouts cancel the statement, which aborts and rolls back the *whole*
transaction (row, history entry, sequence number, chain head): there is no partial write, and the caller
gets `503 ledger_unavailable`, not a conflict. This is tested with a deliberately slow trigger and a held
row lock.

**Pool.** The pool has a fixed size, a short checkout timeout and a bounded waiting list. A saturated
pool is shed with an immediate 503 rather than queued behind a slow database.

## Liveness and readiness

* `GET /health` - **liveness**: the process is up. Never touches the ledger, reports no counts or paths.
* `GET /ready` - **readiness**: 200 only if all of these hold, checked live on every call (never cached);
  otherwise 503 with `Retry-After` and the failing check names (no details):
  `database` (`SELECT 1` as the application role), `schema_version` (matches the code exactly), `role`
  (not superuser, no `BYPASSRLS`), `history_write_denied` (the role is *proven* unable to write
  `record_history`, `chain_heads` or `history_counters`: the catalog shows no write privilege and attempted
  INSERT/UPDATE/DELETE are refused, inside rolled-back savepoints; `TRUNCATE` is checked in the catalog
  only because attempting it would take an exclusive lock) and `legacy_data` (no un-imported legacy
  blob ledger). The JSON store reports a single `store` check.

Point a platform's *readiness* probe (or deploy gate) at `/ready` and its *liveness* probe at `/health`.
Using `/ready` as a liveness probe restarts the service during every database outage, which does not help.
`render.yaml` keeps `healthCheckPath: /health` for that reason; Render has a single probe.

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

**Two roles are required in practice: a single-role deployment does not work as shipped.** If the
application connects as the role that owns the tables, that role holds write privileges on the history
tables, so the `/ready` check `history_write_denied` fails permanently. Reads and writes themselves still
succeed, but readiness never goes green, and any platform that gates traffic or deploys on `/ready` will
treat the service as down. There is no waiver switch. (An earlier version of this document said
single-role deployments "work"; that was wrong.)

Before planning around a second role, check that you can create one:
`select rolcreaterole from pg_roles where rolname = current_user;`. A managed provider's default user may
not be allowed to (Render's documentation does not say what its default user can do). If you cannot
create roles, the only route would be a deliberate, explicit opt-in that waives the
`history_write_denied` check. **That does not exist yet**; it would have to be built, tested and accepted
knowing what it gives up:

* **RLS stops being a barrier against a compromised application.** The owner can run
  `ALTER TABLE ... DISABLE ROW LEVEL SECURITY` or drop the policies. It would still protect against
  accidental cross-tenant queries.
* **History tamper-evidence only covers accidents.** Whoever holds the application credentials can disable
  the append-only triggers and rewrite history, chain heads and counters consistently; the verifier would
  then report clean.
* **The application could run DDL**, including altering or dropping the ledger tables.

What would still hold: the CHECK constraints, optimistic locking, fail-closed 503s and the per-tenant
query filters. Exporting chain-head hashes outside the database (see *Not addressed*) would matter more in
that mode.

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

**Render.** While `JARVIS_DATABASE_URL` is unset the service answers 503 unless `JARVIS_STORE_BOOTSTRAP=1` is set (then it uses the JSON file store on its disk), and
`JARVIS_PG_STORE` has no effect (setting `rows` without a URL silently keeps the JSON store). With a URL,
`render.yaml` pins `JARVIS_PG_STORE=blob` so that merging this code cannot switch a running deployment's
store by itself (`autoDeployTrigger: commit`). Migrate and import first, then change it to `rows`.

**Hosting the database.** The row store needs PostgreSQL 15 or newer (tested on 16). Render's *Free*
Postgres is not suitable: it expires 30 days after creation, is deleted after a further 14-day grace
period, holds 1 GB, has no backups of any kind, and only one is allowed per workspace. Render's paid
databases include point-in-time recovery (past 3 days on a Hobby workspace, 7 days on Pro or higher).
Whatever the host, plan backups before putting a ledger on it. Back up with `pg_dump` as a superuser or a
`BYPASSRLS` role, never as the application role: with forced row-level security an ordinary role either
fails (`query would be affected by row-level security policy`) or, if you add `--enable-row-security`,
**exits successfully with zero rows** - a silently empty backup. After every dump, check that it contains
rows.

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

* **Export chain-head hashes outside the database, and verify them after a restore before serving.**
  A database owner (or superuser) can rewrite the history, chain heads and counters together
  consistently; the verifier only catches partial tampering, and a restore from backup can silently roll
  the ledger back. Follow-up: periodically export each tenant's latest `(seq, chain-head hashes)` to
  somewhere the database owner cannot rewrite (object storage with object lock, a git repository, a
  transparency log); after any restore, run `pg_verify` against the last exported anchor and keep
  `/ready` failing (a new readiness check, `anchor`) until it passes. Not implemented.
* **Deletion does not erase content.** A hard delete removes the row, but its before-snapshot stays in
  `record_history` by design. Any erasure/retention workflow has to account for that and needs a
  deliberate, documented tombstoning path (it would break the hash chain by design). Not implemented.
* **`emr_upsert` is not atomic.** It creates the new draft and archives the target as two separate store
  calls; a failure between them leaves both visible. Not addressed here.
* **Self-minted `verified`** status and unbounded request sizes elsewhere are unchanged from the earlier red-team list. (`/health` no longer reports the store path, record count or board id; the auth/flag summary it still shows is static configuration.)
* **Writes within one tenant serialize on that tenant's history counter** until commit (the price of a
  gapless sequence). Fine for a memory ledger; revisit if one tenant needs high write concurrency.
* Retry exhaustion under extreme contention returns 409 rather than waiting; there is no queue.
