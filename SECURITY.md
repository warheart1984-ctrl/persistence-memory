# Security Policy

## Supported versions

| Version | Supported |
|---------|-----------|
| 0.2.x   | Yes       |
| < 0.2   | Legacy skeleton — upgrade |

## Reporting a vulnerability

Open a private security advisory on GitHub or contact the repository owner.
Do not commit secrets, API keys, or production store dumps.

## Operator hardening (baseline)

1. Set `JARVIS_API_KEY` when binding beyond loopback.
2. Set `JARVIS_CORS_ORIGINS` to explicit origins (never `*` in shared networks).
3. Set `JARVIS_ENV=production` (disables uvicorn reload).
4. Persist `/data` (or `JARVIS_STORE_PATH`) on durable volume; never commit store files.
5. Prefer TLS termination at a reverse proxy; this service speaks plain HTTP.
6. The ledger does **not** enforce multi-tenant isolation — one store per deployment.
