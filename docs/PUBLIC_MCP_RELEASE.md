# Public Jarvis Memoryboard release

## What is implemented

The service is a **submission-ready, tool-only** MCP application. `search`,
`fetch`, `emr_search`, `emr_fetch`, and `emr_recall` are read tools. Memory
writes remain disabled by default and must not be enabled until the write review
below is complete.

When `JARVIS_AUTH_MODE=oauth`, every `/mcp` and `/api/jarvis/*` request needs a
Bearer access token. The authenticated OIDC `iss` + `sub` is hashed into the
ledger namespace; request payloads cannot choose a tenant or board belonging to
another user. The service publishes RFC 9728 protected-resource metadata at:

`/.well-known/oauth-protected-resource/mcp`

## Render configuration

Keep the current `operator` mode until the identity provider is ready. Then add
these secrets/environment values in Render and switch the mode to `oauth`:

| Variable | Required value |
| --- | --- |
| `JARVIS_AUTH_MODE` | `oauth` |
| `JARVIS_OIDC_ISSUER` | Exact HTTPS issuer URL from the OIDC provider |
| `JARVIS_OIDC_AUDIENCE` | API audience configured at that provider |
| `JARVIS_OIDC_JWKS_URL` | Provider JWKS endpoint used to validate signatures |
| `JARVIS_PUBLIC_BASE_URL` | `https://jarvis-memoryboard-teu9.onrender.com` |
| `JARVIS_SUPPORT_EMAIL` | Monitored public support address |

The authorization server must implement OAuth 2.1 authorization-code + PKCE,
publish OIDC/OAuth discovery metadata, advertise `S256`, preserve the `resource`
parameter, and support the `memory.read` scope. For a public ChatGPT release,
support client metadata documents or dynamic client registration.

## Writes and retention

Set `JARVIS_DATABASE_URL` to the private Render PostgreSQL connection string
before enabling OAuth. The application stores one JSONB Continuity Ledger
document per OAuth tenant key; this preserves the ledger validation model while
moving durability off the Render disk.

Do not set `JARVIS_MEMORY_WRITE_ENABLED=true` or
`JARVIS_MCP_WRITE_ENABLED=true` for the first public release. Before enabling
writes, add an immutable audit table and deletion/export workflow, establish
backup/restore tests, and conduct a write-action/prompt-injection review.

The `/privacy` and `/terms` endpoints are technical transparency endpoints;
replace them with reviewed, jurisdiction-appropriate policy text and a real
support address before directory submission.

## Verification gate

1. Configure a non-production OIDC tenant first.
2. Verify no token yields `401` plus `WWW-Authenticate` resource metadata.
3. Verify a `memory.read` token can use `tools/list`, `search`, and `fetch`.
4. Verify two subjects cannot retrieve each other's records.
5. Verify a read-only token cannot write, even if a client requests a write tool.
6. Run the OAuth login flow in ChatGPT Developer Mode, then submit only after
   organization verification, public policy/support artifacts, and review-safe
   test accounts are available.
