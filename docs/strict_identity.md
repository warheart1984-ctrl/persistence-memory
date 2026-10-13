# Strict identity (`JARVIS_MULTI_TENANT`)

**"Operator" is something a request *is*, never what it falls back to.** (Phase 0 of the multi-tenant RFC.)

## What changed (in every mode)
* `Principal` has a `kind`: `tenant` (an OAuth subject) or `operator` (the configured operator credential).
* The identity middleware binds a principal for every protected request: an OAuth subject, or the **operator principal** when the operator credential was presented. `auth.credential_is_operator(headers, path)` decides that with the same rules the gates use: the tool, MCP and call-log routes take `EMR_RECALL_API_KEY` when it is set and the ledger key otherwise; the ledger routes take the ledger key; the RAG routes have a key of their own (not the operator's); with no key configured only the explicit `JARVIS_ALLOW_UNAUTHENTICATED` opt-out is the operator.
* The operator-only gates (`require_operator_read`, `require_nx_operator`, evidence create, the call log, the drift watcher and its MCP tool) ask one question, `auth.caller_is_operator()`, instead of "is OAuth switched on?".
* The tenant a request's data belongs to comes from `identity.request_tenant()`; the call log is told who the caller was.

## `JARVIS_MULTI_TENANT=1` (strict)
Off by default: the shared-key deployment behaves exactly as before (pinned by tests). On:
* **No identity is not the operator.** `request_tenant()` is `None`; `get_store()` raises (HTTP 401) instead of serving the operator's ledger; the operator-only gates require an authenticated operator principal; log entries with no identity are filed under `_unauth`, never `operator`.
* **The app refuses to start** with a configuration that could fail open: `JARVIS_ALLOW_UNAUTHENTICATED` set, no PostgreSQL store (the JSON file store has no row-level isolation), `JARVIS_PG_STORE` not `rows`, or no credential at all (neither `JARVIS_API_KEY` nor OAuth).
* **The app also refuses to start against a database that cannot be shown to isolate tenants** (`app.pg_store.assert_tenant_isolation_ready`, run from the lifespan): the schema is not the version the code expects, the role is a superuser or has `BYPASSRLS`, or any table with a `tenant_key` column (except the retained legacy table `jarvis_tenant_ledgers`) lacks forced row-level security with a policy. These are the same checks `/ready` makes, but fatal: nothing is served unverified. A wrong database fails at once; a database that cannot be reached or queried is retried for `JARVIS_STARTUP_DB_WAIT_SECONDS` (default 30, at most 300) and then refused too, so a database that is still starting does not crash-loop the app. With the flag off nothing changes: the app starts and `/ready` reports.

## Not in this change
Per-tenant API keys and the control schema (phase 1); the seal timer, the signer, `attest verify` and the CLIs (`pg_verify`, `replay`, `attest`, `witness`, `pg_import`) still default to or act on the `operator` tenant only (phase 0 follow-up); the RLS startup check; per-tenant call and drift logs (the call log's own system entries, such as gap and summary entries, are still filed under `operator`).
