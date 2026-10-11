# Tenant isolation matrix

`tests/test_tenant_isolation.py` is the safety net for the multi-tenant work (RFC, phase 0). Two tenants, `alice` and `bob`, exist today through OAuth subjects; per-tenant API keys will add a second fixture over the same cases.

## The rule: a route must be classified

Every method+path the app serves is listed in `REGISTRY` with one class:

| class | meaning | what the matrix proves |
|---|---|---|
| `operator-only` | the integrity machinery, the call and drift logs, nx-search | a tenant gets **403**, never data |
| `public` | service metadata, OAuth discovery, health, docs | the same for everyone, carries no tenant data |
| `authenticated-neutral` | needs a credential, identical for every tenant (the tool catalog) | identical bodies for alice and bob |
| `tenant-data` | records, evidence, board, short-term memory, graph, search | see below |
| `mcp` | the MCP transport (same tools through `tools/call`) | same cases through `/mcp` |
| `shared-resource` | process-wide, not keyed by tenant (inference backend, RAG index) | fail-closed where configured, **strict xfail** where not |

`test_every_served_route_is_classified_and_every_classification_is_served` fails if the app serves a route that is not in the registry, or the registry names one that is gone. **Adding a route means classifying it here, and, if it is `tenant-data`, adding it to `id_cases` or `LIST_CASES`.**

## Tenant-data cases

* **id-addressed routes** (get, patch, delete, resolve, history, lineage, anchor, graph, stm-expand, reinforce, supersede through the API and the tool, fetch, upsert): bob's response to *alice's* id must be byte-for-byte the response to an id that never existed (compared against a second fresh tenant, ids masked), must not contain anything of alice's, and must not change alice's records. No "exists but not yours" oracle.
* **list / search / read-model routes**: bob sees none of alice's text or ids; the same read does return alice's data to alice (so the check is not vacuous).
* **a cursor** minted for alice is refused for bob exactly like a malformed one.
* **short-term memory and the board**: bob cannot clear or overwrite alice's.
* The same id and search cases through `/mcp`.

## What keeps the matrix honest

* `test_every_tenant_data_route_is_exercised_by_a_cross_tenant_case` resolves every case's request through the app's own router and requires every `tenant-data` route to be hit by at least one case (`id_cases`, `LIST_CASES` or `MUTATION_CASES`), so a route cannot sit in the registry with no test that crosses tenants.
* The read-model cases compare bob's response with a **brand-new tenant's**, not only "alice's text is absent": a count or a size that reveals alice's records fails too.
* The write tools (`emr_remember`, `emr_upsert`) run with `JARVIS_MCP_WRITE_ENABLED` on and approved payloads, and a control test proves they are past their gate; otherwise both sides would return the same generic refusal whatever the isolation.
* Fields a successful write generates (id, hash, version, clocks) are masked when two responses are compared.
* Evidence objects can only be created with the operator key today, so there is no tenant evidence to cross: the two evidence reads are exercised with unknown ids until per-tenant keys land (phase 1).

## Known gaps

Written as `xfail(strict=True)`, so the day one is fixed the test flips and has to be promoted to a plain test:

* the inference backend's address is shown to any tenant (`GET /api/jarvis/llm/status`);
* any tenant can spend the shared inference backend (`POST /api/jarvis/llm/generate`);
* the AMUL field status shows the server's filesystem path.


The RAG routes are not exercised for isolation (they refuse every tenant with 503 until their access control is configured); that is asserted, and the RAG index needs its own audit before it is enabled.

Runs on the JSON store by default and, with `JARVIS_TEST_BACKEND=postgres`, on the Postgres row store with forced row-level security (CI's `test-postgres` job).

Fixed (and promoted from strict xfails to plain tests): EMR reinforcement state was one process-global map keyed by memory id only, and `GET /api/jarvis/memory/emr/status` reported every tenant's entries; it is now keyed by tenant (docs/emr_dynamics_per_tenant.md), and the sidecar path is shown to the operator only.
