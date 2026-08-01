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
6. The ledger does **not** enforce multi-tenant isolation — one store per deployment.
7. Prefer `type=decision` (+ evidence) over chat dumps — Clause V hygiene is **partial** / not API-enforced (`docs/CLAUSE_V_HYGIENE.md`).
8. Follow `docs/OPERATOR_DEPLOY_CHECKLIST.md` before shared-network exposure.
9. Treat the JSON store as **single-writer** — atomic writes ≠ multi-writer safety (`docs/PLATFORM_LIMITS.md`).
