# RFC: Multi-tenant Continuity Ledger

| | |
|---|---|
| Status | Draft, for decision |
| Date | 2026-10-11 |
| Applies to | persistence-memory (Jarvis Continuity Ledger), schema v9 |
| Gate | Nothing here ships to anyone but the operator until the Red Team exit criteria (section 9) pass |

> **Implementation status (2026-10-12):** see [Implementation status](#implementation-status-2026-10-12) at the end. The body below is the RFC as written on 2026-10-11 and is
> unchanged (tables re-flowed to Markdown from a pasted copy).

## 0. How this was written, and what I did not verify

I read the checkout at `C:\Users\randj\persistence-memory-main` (read-only): `app/auth.py`, `app/identity.py`, `app/store.py`,
`app/pg_store.py`, `app/pg_schema.py`, `app/blocks.py`, `app/emr_latest.py`, `app/call_log.py`, `app/call_witness.py`,
`docs/POSTGRES.md`, `deploy/mint/bin/{backup,restore,drill,anchors}.sh`. I did not run anything against the live ledger for this RFC.

Gaps you should know about:

- The drift watcher (PRs #82/#83, main 5a8d756) is not in the checkout I read. Section 6 treats the drift log by analogy with the call log. Confirm against 5a8d756 before implementation.
- I did not read `amul*.py`, `graph.py`, `emr_embed.py`, `nx_search_client.py` or `evidence.py` in detail. They are listed in section 8 as isolation audit targets, not as verified findings.
- I did not read the `jarvisctl` seal/offsite/attest wrappers, so "does every timer iterate all tenants" is an open question (section 10).

## 1. Summary

The storage layer is already multi-tenant. The authentication and operational layers are not. This RFC proposes:

1. Per-tenant API keys resolved server-side to a tenant, replacing the single shared key. No request parameter can ever select a tenant.
2. Keep one hash chain per tenant (the existing design) and add an operator-only epoch anchor that commits to every tenant's chain head, so a tenant is provable separately and the operator can still prove "nothing was dropped".
3. Per-tenant call log and drift log, with the tenant recorded on every entry and tenant-scoped read views, separate from the operator views.
4. Per-tenant backup, restore and deletion, built from tenant-scoped exports, with an explicit, recorded rollback/erasure event so anchors do not quarantine a legitimate restore.
5. Fail closed. Remove every `or "operator"` default that turns "no identity" into "operator".

## 2. Where we are today (verified in code)

### Already tenant-aware

- Every table has `tenant_key`, `PRIMARY KEY (tenant_key, id)`, and `FORCE ROW LEVEL SECURITY` with policy
  `tenant_key = current_setting('jarvis.tenant_key', true)` (`pg_schema.py` ~L78-87, 148-152, 253-262, 448-452).
- `tenant_key` is immutable (trigger `jarvis_bump_version`). The supersede foreign key is `(tenant_key, supersedes)`, so a record cannot supersede another tenant's record at the DB level.
- The history chain is per record, with a gapless per-tenant `seq` (`history_counters`, `chain_heads`, `jarvis_verify_history(p_tenant, ...)`).
- Continuity Blocks are per tenant: height, `prev_block_hash`, `entries_root`, and the tenant is length-prefixed in the block hash preimage (`blocks.py`).
- `PostgresRowStore` is constructed per tenant, sets `jarvis.tenant_key` with transaction-local `set_config(..., true)`, and also filters every query with `tenant_key = %s` (belt and braces).
- `emr_latest` cursors are bound to the tenant and filters (`_scope`); `latest_memories` refuses with `TENANT_UNRESOLVED` when no tenant resolves.
- The anchors file, `restore.sh` and `drill.sh` already iterate tenants (`head|tenant|...`, `counter|tenant|...`, `block|tenant|...`, `pg_verify --tenant`, `replay verify --tenant`).
- The app role is required to be non-superuser, no BYPASSRLS; the store refuses to serve otherwise.

### Not tenant-aware (the work)

| # | Gap | Evidence |
|---|---|---|
| G1 | One shared key. `ApiKeyMiddleware` compares to a single `configured_api_key()`; nothing maps a key to a tenant. | `auth.py` ~L380-395 |
| G2 | Tenant only comes from OAuth (`identity_middleware` -> `set_principal` -> `current_tenant_key()` = sha256(issuer, subject)). Key auth never sets a principal. | `auth.py` ~L428, `identity.py` |
| G3 | Fail-open default. `get_store()` uses `tenant or "operator"`; `emr_latest`/`ledger_search` use `current_tenant_key() or (None if oauth_enabled() else "operator")`; `operator = not oauth_enabled()` grants block/replay data by deployment mode, not by identity. | `store.py` ~L665, `main.py` L487, 517, 512 |
| G4 | `require_operator_read`, `require_nx_operator` and the call-log gate decide "operator" from the OAuth/non-OAuth mode, not from a key class. | `auth.py` L243-256, `main.py` L943 |
| G5 | The call log is one global hash chain, one file set, with `tenant` as a field that defaults to `"operator"`. Only OAuth requests set `scope["jarvis_tenant"]`. | `call_log.py` L7, 346, 565; `call_witness.py` L190 |
| G6 | Backups are whole-database `pg_dump -Fc`, run as the postgres superuser. No per-tenant dump, restore or delete. Restore replaces the whole DB. | `backup.sh` L34, `restore.sh` L74 |
| G7 | History is append-only by design ("the app cannot change history"); there is no sanctioned tenant-erasure path. | `jarvis_history_immutable`, README |
| G8 | JSON/blob store fallbacks and file-based subsystems (AMUL field, caches) are not obviously tenant-keyed. | `store.py` L691, `amul*.py` |

## 3. Decision 1: tenant identity and keys

### Requirements

- The tenant is derived only from the authenticated credential, server-side. No tool, route, header or body field named `tenant`, `board`, `tenant_key`, `user` etc. may be accepted. MCP tool input schemas must not declare one; unknown properties are rejected, not ignored.
- One tenant may hold several keys (rotation, per-agent keys). A key belongs to exactly one tenant.
- Keys are revocable and individually auditable without revealing the secret.

### Design

New control-plane tables in a separate schema (`jarvis_control`), not subject to tenant RLS and not readable by the app role except through two `SECURITY DEFINER` functions (`resolve_key`, and `touch_key_last_used`):

- `tenants(tenant_key PK, label, status active|suspended|deleting|deleted, created_at, deleted_at)`
- `api_keys(key_id PK, tenant_key FK, key_hash, scopes, status active|revoked, created_at, expires_at, last_used_at, label)`

Token format: `jlk_<key_id>_<secret>`. `key_id` (public, non-secret) does the lookup, so verification is O(1) and not an enumeration oracle. `secret` is 256 bits of CSPRNG output.

- Store `key_hash = HMAC-SHA256(pepper, secret)`. The secret is high entropy, so a slow KDF is unnecessary; the pepper (a file, never in the DB) means a stolen DB alone cannot be used to verify guesses. Compare with `secrets.compare_digest`. Uniform 401 for unknown `key_id`, bad secret, revoked, expired (no oracle).
- New `tenant_key` values for key-based tenants are `t_` + 128 random bits (hex). They are not derived from a name. `operator` and the existing OAuth hash namespace stay valid and reserved; `t_` is a distinct prefix so the namespaces cannot collide.
- Key classes: `tenant` (reads/writes own data) and `operator` (the current shared key, becomes the explicit operator class). There is no key class that can name another tenant over HTTP. Cross-tenant administration happens only through `jarvisctl tenant ...` on the box, using the migrator role, never through the public API (this removes "operator key stolen => read everyone" from the HTTP blast radius).
- Extend `Principal` with `kind: tenant|operator` and `tenant_key`, and set it for key auth exactly as OAuth does today (`set_principal`). Everything downstream already reads `current_tenant_key()`; make that return `principal.tenant_key` for both OAuth and key auth.

### Fail-closed changes (must-do, small, high value)

- Delete every `tenant or "operator"` fallback (G3). No principal => 401 before a store is ever constructed. `get_store()` raises if no principal and multi-tenant mode is on.
- Replace `operator = not oauth_enabled()` with `operator = principal.kind == "operator"`.
- `require_operator_read`, `require_nx_operator`, call-log, blocks and replay gates check `principal.kind`, not deployment mode (G4).
- A deployment flag `JARVIS_MULTI_TENANT=1` makes the app refuse to start if the legacy single-key path, JSON store, or `JARVIS_ALLOW_UNAUTHENTICATED` is configured.

### Not chosen

- Passing tenant in a header (`X-Tenant`): rejected, violates the requirement and recreates the confused-deputy problem.
- One Postgres role per tenant: rejected for now. RLS keyed on a per-transaction setting already gives isolation, and per-tenant roles do not scale with pooling. Revisit only if tenants need DB-level credentials.

## 4. Decision 2: isolation on every read and write

Isolation is layered; all layers must hold independently.

1. **Auth layer:** tenant from key only (section 3).
2. **Application layer:** every query filters `tenant_key = %s` (already true in `pg_store.py`). Rule: no SQL string in `app/` may reference a tenant-scoped table without a `tenant_key` predicate, enforced by a test that parses the SQL the store emits (see Red Team).
3. **Database layer:** `FORCE ROW LEVEL SECURITY` with `jarvis.tenant_key` set per transaction. Add a startup check that every table in the `jarvis` schema with a `tenant_key` column has RLS enabled and forced, and fail startup otherwise (schema drift guard).
4. **Role layer:** app role stays non-superuser, no BYPASSRLS (already enforced by readiness).

Operations in scope (each needs a cross-tenant negative test): recall/list, retrieve, search (`ledger_search`), fetch/get, `emr_latest`, conflicts, history, history/verify, supersede (create with `supersedes`), PATCH/DELETE, evidence create/get/verify, blocks, replay, drift check, pipeline, board get/set.

Specific hazards to close:

- Supersede/evidence references to a foreign id must return exactly the same response as a nonexistent id (no "exists but not yours" oracle). Return 404, never 403, for out-of-tenant ids.
- Content-addressed evidence ids (`eo:sha256:...`) are the same across tenants for identical bytes. Keep them tenant-keyed (already `(tenant_key, id)`); GET/verify for another tenant's hash must be indistinguishable from "not found". Creation must not reveal that another tenant already holds the same hash.
- Cursors are already tenant-bound; add a test that a cursor minted for tenant A is rejected for B with the same error as a malformed cursor.
- Counters leak volume. `ledger_head` returns the tenant's own `last_seq` only; keep blocks/replay operator-only until per-tenant block routes (section 5) exist.
- Shared caches (`_stores` is keyed by tenant: fine). Audit and key by tenant: EMR embeddings/graph caches, AMUL field files, ledger-search ranking stats, nx-search (currently correctly operator-only; keep it that way).
- Error bodies must never echo SQL, other-tenant ids, or counts.
- Rate limits and quotas per tenant (a noisy tenant must not starve others via the shared pool): per-key rate limit, per-tenant row/byte quota, per-tenant statement timeout.

## 5. Decision 3: hash chain design (the big one)

### Options

- **A. Per-tenant chains (extend what exists).** Each tenant has its own gapless `seq`, its own record chains, its own block chain. This is the current schema.
- **B. One shared chain, per-tenant provability.** A single global sequence over all tenants; a tenant proves its records via Merkle inclusion/consistency proofs against a global root.
- **C. Hybrid (recommended): per-tenant chains + an operator epoch anchor.** Keep A. Add a periodic epoch: a hash over the sorted list of `(tenant_key, tenant_block_height, tenant_block_hash, tenant_last_seq)`, chained to the previous epoch. Epochs are operator-only, stored in the control schema, and exported in the off-database anchors file with a new line type `epoch|<n>|<epoch_hash>`.

### Comparison

| Criterion | A per-tenant | B shared | C hybrid |
|---|---|---|---|
| Tenant verifies own data without seeing others | yes | needs Merkle proofs; proof sizes and global ordering metadata leak other tenants' write rates | yes |
| Tenant gap/deletion/reorder detection | yes (gapless seq, chain heads) | yes, but a global gap is not attributable to a tenant | yes |
| Write contention | one counter row per tenant; tenants never contend | single hot counter row serialises every write of every tenant | per-tenant (epochs are batched, off the write path) |
| Restore/delete one tenant | clean: its chain is self-contained | breaks the global chain for everyone (holes) | clean; epoch records the event (section 7) |
| Operator can prove "no tenant's chain was dropped or rolled back" | no (each chain is independent; dropping a tenant is invisible) | yes | yes (epoch commits to every tenant head) |
| Existing code, anchors, replay, attest, drill | already per tenant | large rewrite of all five | no rewrite; additive |
| Information a tenant learns about others | none | write-rate and ordering side channel | none |

### Recommendation: C

- It is the only option that keeps tenant isolation and removes the one thing pure A lacks: a tenant-erasure or silent rollback is not detectable globally. The epoch gives the operator that.
- It is additive: blocks, replay, attest, `anchors.sh`, `restore.sh`, `drill.sh` already key on `tenant_key`. Only the epoch table, its sealing job, one anchors line type and one verifier are new.
- A tenant can verify itself fully (`jarvis_verify_history(tenant)`, its blocks, a replay at its block) and can be given its own tenant-scoped, redacted epoch inclusion proof: a Merkle proof that its `(height, block_hash)` is in epoch N, with sibling hashes only, no other tenant's id or data. Tenant ids are opaque random values (section 3), so even the epoch preimage is not meaningful to a tenant.
- Epoch sealing is a batched, operator-side job (like `jarvis-seal.timer`); it takes no lock on tenant writes.

### Needed to make C real

- Per-tenant blocks routes (`/api/jarvis/blocks`, `/head`, `/verify`) enabled for the `tenant` key class on the tenant's own chain. Today they are operator-only (`require_operator_read`). Operator keeps cross-tenant views only via `jarvisctl`.
- `epochs` table + `jarvisctl epoch seal|verify`, and the verifier must fail if any tenant present in the previous epoch is absent now unless a recorded `tenant_deleted` event explains it.
- Signing (currently shelved) can later cover epochs and per-tenant blocks identically. Out of scope here, but the design must not preclude it.

## 6. Decision 4: call log and drift log per tenant

Today (verified): one chain across all daily files in `JARVIS_CALL_LOG_DIR`, `tenant` is a field, OAuth sets it, key auth defaults to `"operator"`, and operator-only access is by deployment mode.

### Design

- One call-log chain per tenant, plus one operator chain for operator-class calls and for calls that fail authentication (tenant unknown, logged against `_unauth` with no key material). Layout: `JARVIS_CALL_LOG_DIR/<tenant_key>/calls-YYYYMMDD.jsonl`, mode 600, the existing locking, seq/prev_hash and retention-anchor logic per directory.
- The `tenant` field is set from `principal.tenant_key` at the outermost witness layer for every auth path (fix G5), never defaulted. A call with no resolvable principal is a 401 and is recorded in `_unauth`, not under `operator`.
- Reads: a tenant key may read and verify its own call log (`GET /api/jarvis/calls`) and nothing else. Operator views (all tenants, `_unauth`, aggregate health) stay separate and operator-class only, and are never reachable by a tenant key or an OAuth token.
- Raw arguments, keys, headers and tokens remain unrecorded (existing rule). Add: the tenant log never contains another tenant's `result_digest` inputs or any cross-tenant count.
- The call-log head per tenant goes into the anchors file (`calllog|<tenant>|<seq>|<entry_hash>`) and into the epoch preimage, closing the existing known limits ("anyone who can write the directory can rebuild a chain; trailing entries can be truncated") the same way blocks do for history.
- Drift log: same shape, `drift/<tenant_key>/`. The drift watcher must run per tenant (iterate active tenants, scoped store per tenant) and write to that tenant's drift log with tenant recorded; operator-only views of fleet drift remain separate. To be confirmed against 5a8d756 (section 0).

## 7. Decision 5: backup, restore and deletion per tenant

Today: whole-DB `pg_dump -Fc` as the superuser, whole-DB restore, anchors compared as "legitimate successor".

### Design

- Keep the whole-DB backup as the operator's disaster-recovery copy (unchanged, still verified by `drill`).
- Add tenant-scoped export: `jarvisctl tenant export <t>` writes a self-contained, age-encrypted bundle for one tenant using `COPY (SELECT ... WHERE tenant_key = :t)` over every tenant-keyed table (memories, boards, record_history, history_counters, chain_heads, evidence_objects, blocks, attestations, trust_statements, call/drift logs), plus a manifest: row counts per table, the tenant's chain head, last block, call-log head, schema version, and the epoch it belongs to. Per-tenant encryption keys make "delete the key" a valid crypto-shred of that tenant's offsite copies.
- Tenant restore (`jarvisctl tenant restore <t> --yes`): set tenant `status=restoring` (the app returns 503 `tenant_unavailable` for that tenant only, others unaffected), restore into a scratch DB, run the existing gates (counts, anchors, `pg_verify --tenant`, `replay verify --at-block`), then in one transaction replace that tenant's rows via the migrator role, then re-enable. Other tenants are never touched.
- **Rollback is an event, not a silent regression.** Restoring a tenant to an earlier state moves its seq backwards, which today's anchor check treats as tampering ("counter went BACKWARDS"). Add a recorded `tenant_restored` event (operator control log, in the next epoch) carrying old head, new head and reason; the anchor checker accepts a backwards move for that tenant only if a matching event exists. Without it, restores are quarantined as designed.
- Tenant deletion (`jarvisctl tenant delete <t> --yes-destroy-tenant-data`): `status=deleting`, revoke all keys, export a final encrypted bundle (retention optional), then in a single transaction delete the tenant's rows using the migrator role (the append-only protection is on the app role; the migrator already owns the tables). Record a `tenant_deleted` event with the final chain head, last block hash and row counts. The epoch verifier requires that event for a vanished tenant, and offsite bundles are crypto-shredded by destroying the tenant key. The deletion event retains no content, only hashes and counts.
- Anchors: add `tenantevent|<tenant>|<kind>|<hash>` lines. `anchors_check` treats a missing tenant as valid only with a matching `tenant_deleted` line.

## 8. Threat model and Red Team plan

Assumptions: a tenant holds a valid key and is hostile; the network is untrusted; the operator and migrator are trusted; the database is not directly reachable by tenants.

### Attack classes the Red Team must try (each must fail, with an identical error to the "not found" case)

1. Read A's data with B's key: every read route and MCP tool in section 4, by id, by subject, by query, by cursor, by `supersedes`, by history.
2. Write across tenants: create with `supersedes` = A's id, PATCH/DELETE A's id, evidence reference to A's `eo:` id, evidence create of A's hash.
3. Tenant selection smuggling: `tenant`, `tenant_key`, `board`, `user`, `X-Tenant`, `X-Forwarded-*`, JSON extra properties, query-string and path variants, MCP tool-argument extras.
4. Identity confusion: unauthenticated, revoked, expired, wrong-`key_id`/right-secret, key with `jlk_` prefix tricks, OAuth token on a key-only route and vice versa, HTTP method override, path normalisation (`//`, `%2e`, trailing slash) against the exempt-path list.
5. Oracles: timing and error-text differences between "not yours" and "not found", cursor-reuse errors, evidence existence by hash, counters and block heads leaking volume.
6. Resource abuse: one tenant exhausting the pool, long statements, huge payloads, many history reads (cross-tenant DoS).
7. Operator-surface reachability: tenant key against call log, drift log, blocks (until section 5 routes), replay, nx, admin and `/mcp` tools marked operator.
8. Cache and subsystem bleed: AMUL field/files, EMR embeddings/graph caches, ledger-search ranking, process-wide singletons keyed without the tenant.
9. DB layer: with a `jarvis.tenant_key` unset, empty, or set to another tenant, with a SQL-injection attempt, under connection reuse; confirm RLS returns zero rows and write attempts fail.
10. Lifecycle: use of a key mid-restore or after delete; resurrected tenant id; reuse of a deleted tenant's id by a new tenant (must be impossible: ids are random and a tombstone row is kept).

### Test architecture

- A cross-tenant matrix test (`tests/test_tenant_isolation.py`): create tenants A and B with data, then for every route in the OpenAPI document and every MCP tool, execute it as B against A's identifiers and assert the response is byte-for-byte equivalent to the "nonexistent id" response. The test fails if a new route is added without being registered in the matrix, so isolation coverage cannot rot.
- A SQL guard test that captures the statements `PostgresRowStore` emits and rejects any tenant-table statement without a `tenant_key` predicate.
- A startup check that RLS is enabled and forced on every `tenant_key` table (G8 schema-drift guard).
- A fuzz pass over the tenant-selection smuggling inputs (attack class 3).

## 9. Exit criteria (the gate before "handing the server to anyone")

1. All Red Team classes in section 8 attempted and recorded; zero cross-tenant read, write, or distinguishable-oracle result.
2. No `or "operator"` default reachable; the app refuses to start in multi-tenant mode with the shared key, JSON store or unauthenticated mode.
3. Per-tenant call log, with operator views separate and tenant keys denied on them.
4. A tenant restore and a tenant delete performed in a drill with other tenants live, passing the gates, and the anchors check accepting them only with their recorded events.
5. Epoch seal and verify working, including detection of a silently dropped tenant in a negative drill (extend `drill --prove-detection`).
6. Isolation matrix test in CI, failing closed on unregistered routes.

## 10. Rollout

| Phase | Content | Exposure |
|---|---|---|
| 0 | Fail-closed fixes (G3, G4), `JARVIS_MULTI_TENANT` flag, RLS startup guard, isolation matrix test (operator-only tenant today) | none |
| 1 | Control schema, key issuance (`jarvisctl tenant create` / `key`), key -> Principal, per-tenant call log | one test tenant, internal |
| 2 | Per-tenant blocks routes, epochs + verifier, anchors line types | internal |
| 3 | Tenant export/restore/delete + drills, deletion and rollback events | internal |
| 4 | Red Team, remediation, exit criteria | gate |
| 5 | Invite external tenants | after gate |

Schema changes are a new migration (v10), applied by `jarvisctl up` per the existing rule (v9 code refuses v10 and the reverse; rehearse on the throwaway stack first, as with v8/v9).

## 11. Open questions (for you)

1. Is the operator allowed to read tenant content at all? This RFC assumes the operator can export/restore (migrator role) but the public API never exposes cross-tenant reads. If tenants must be protected from the operator too, we need per-tenant envelope encryption of content, which changes search and is a separate RFC.
2. Retention on deletion: hard delete with hashes-only tombstone (proposed), or retain an encrypted final bundle for N days?
3. Tenant self-service verification: are tenants given block/replay verification of their own chain (proposed, needs the section 5 routes), or is verification operator-run only?
4. Quotas: initial per-tenant limits (rows, bytes, requests/min)?
5. Do the seal, offsite, attest and watchdog timers already iterate all tenants? I did not verify `jarvisctl`; `anchors.sh`, `restore.sh` and `drill.sh` do.
6. Drift watcher details from 5a8d756 to finalise section 6.
7. Existing OAuth tenants: keep as-is (sha256 of issuer+subject), mapped as `kind=tenant` principals alongside key tenants? Proposed yes.

## 12. Non-goals

Signing and key ceremonies (still shelved); per-tenant encryption of content at rest; cross-tenant sharing or federation; billing; changing Clause V or record semantics; the nx-search tools (remain operator-only).

---

## Implementation status (2026-10-12)

Added after the RFC was written; the RFC body above is unchanged. Phase 0 is built and merged to main; the live box runs it with strict mode **off**
(`JARVIS_MULTI_TENANT` and OAuth unset). Evidence: the throwaway combined drill of 2026-10-12 (key mode, strict mode, OAuth plus strict; 57 checks, 0 failed) and the
live before/after capture of the deploy.

### Gaps (section 2)

| # | Status |
|---|---|
| G1, G2 | **Not done** (phase 1). Only OAuth yields a tenant. |
| G3 | **Fixed** (#91): strict mode has no identity fallback and no store without a principal; the app refuses to start with the shared key's opt-out, the JSON store or a non-rows store. Off by default (flag unset). |
| G4 | **Fixed** (#91): `Principal.kind` decides "operator"; gates check it. |
| G5 | **Partly fixed:** every path labels the tenant from the principal and an unidentified call is filed as `_unauth`; still one global chain, not one per tenant. |
| G6, G7 | **Not done** (phase 3). |
| G8 | **Partly fixed:** EMR reinforcement per tenant (#92); the inference backend is operator-only (#99); server paths hidden from tenants; the AMUL field already lives in per-tenant directories; the RAG index is still unaudited. |

### Built beyond the RFC's phase 0

- Isolation matrix with a route registry that fails closed (#90), no known gaps left as strict xfails except the RAG audit.
- RLS readiness check `rls_tables` (#94): a table with a `tenant_key` column and no forced RLS makes `/ready` return 503 (readiness, not startup abort).
- Per-tenant seal, sign and verify from the box, `jarvisctl tenants` (#95, #97): cross-tenant administration only through the migrator role on the box, never the public API. Built, **not scheduled**.
- Drift call-log match filtered by tenant (#93); `pg_verify` fails for an unknown or empty tenant name (#98).
- A **recall credential** (#101, `docs/recall_credential.md`): a read-only key (`EMR_RECALL_API_KEY`) limited to seven read tools. This is a third credential class beside `tenant` and `operator` in section 3.

### Not built

Per-tenant keys and the control schema, per-tenant call-log chains and routes, per-tenant blocks routes, epochs and their verifier, the SQL-predicate guard test, the smuggling fuzz pass, per-tenant quotas and rate limits, the v10 migration, and tenant export/restore/delete with their events and drills.

### Exit criteria (section 9)

1 (Red Team), 3, 4 and 5 are open. 2 is met in code behind the flag (not enabled). 6 is met (matrix in CI, fail-closed on unregistered routes).

### Open questions answered by what was built or checked

- **Q5:** no. The seal, attest and sign scripts act on the operator tenant only and backups are whole-database. `jarvisctl tenants` is the per-tenant version, unscheduled.
- **Q6:** the drift watcher is operator-only; its entries carry a tenant label but the log is one chain. Per-tenant drift logs are not built.
