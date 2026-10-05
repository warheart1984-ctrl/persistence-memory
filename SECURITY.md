# Security Policy

## Supported versions

| Version | Supported |
|---------|-----------|
| 0.2.x   | Yes       |
| < 0.2   | Legacy skeleton — upgrade |

## Reporting a vulnerability

Open a private security advisory on GitHub or contact the repository owner.
Do not commit secrets, API keys, or production store dumps.

## Authentication (required by default)

| Mode | Env | Behavior |
|------|-----|----------|
| **Default (secure)** | `JARVIS_API_KEY=<secret>` | Protected routes require `Authorization: Bearer <key>` or `X-API-Key` |
| **Local-dev opt-out** | `JARVIS_ALLOW_UNAUTHENTICATED=1` | Open routes when no key is set — **loopback / trusted host only** |
| **Misconfigured** | neither set | Protected routes return **401** (not open) |

Public paths (no key): `/`, `/health`, `/docs`, `/openapi.json`, `/redoc`.

If both are set, **`JARVIS_API_KEY` wins** — requests must present the key.

Do **not** use the opt-out when the port is forwarded, bound on a shared network, or exposed via Docker/publish without another auth layer.

Generate a key:

```powershell
python -c "import secrets; print(secrets.token_hex(32))"
```

## Operator hardening (baseline)

1. Set `JARVIS_API_KEY` for any non-throwaway deployment (required by default).
2. Set `JARVIS_CORS_ORIGINS` to explicit origins (never `*` in shared networks).
3. Set `JARVIS_ENV=production` (disables uvicorn reload).
4. Persist `/data` (or `JARVIS_STORE_PATH`) on durable volume; never commit store files.
5. Prefer TLS termination at a reverse proxy; this service speaks plain HTTP.
6. The JSON file store does **not** enforce multi-tenant isolation — one store per deployment. The PostgreSQL row store isolates OAuth tenants with `tenant_key` filters plus forced row-level security (`docs/POSTGRES.md`).
7. Prefer `type=decision` (+ evidence) over chat dumps — Clause V hygiene is **partial** / not API-enforced (`docs/CLAUSE_V_HYGIENE.md`).
8. Follow `docs/OPERATOR_DEPLOY_CHECKLIST.md` before shared-network exposure.
9. The JSON store is atomic (temp file, fsync, replace) and locked **within one process**. Multi-worker and multi-instance deployments are **last-writer-wins**: run a single worker and a single instance per store file (`docs/PLATFORM_LIMITS.md`). The PostgreSQL **row** store (`JARVIS_PG_STORE=rows`, the default with `JARVIS_DATABASE_URL`) is safe for multiple workers and instances: row-level writes with optimistic locking, 409 on conflict. The legacy JSONB-blob store (`JARVIS_PG_STORE=blob`) replaces a whole document per write and is last-writer-wins.
10. A ledger file that cannot be parsed, or that contains an invalid record, makes the service fail closed (HTTP 503, `/health` reports `unavailable`) instead of starting empty; repair or restore the file rather than deleting it.
11. PostgreSQL deployments: run the application as an **ordinary role** (not a superuser, no `BYPASSRLS`); the store refuses to serve otherwise. Prefer separate migrator and application roles. History is append-only and hash-chained, but a database owner can rewrite it; export chain-head hashes outside the database if you need tamper-evidence against the owner (follow-up, `docs/POSTGRES.md`).
12. Even with the row store, AMUL, STM, the EMR reinforcement overlay, RAG files and the LLM logs are **per-instance**. Run those on a single instance (`docs/POSTGRES.md`, *What is still per-instance*).
13. Deleting a record does not erase its content from `record_history`; plan retention/erasure accordingly.
