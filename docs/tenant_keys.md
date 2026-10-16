# Per-tenant API keys (RFC phase 1, slice 1)

Status: **built, not in the request path, not deployed.** This slice adds the storage, the token format, the verification primitive and the migrator-role CLI. Nothing authenticates a request with a
tenant key yet (that is the next slice, behind a flag), so merging this changes no behaviour of a running deployment except that the schema version becomes 10.

## Schema v10: the control schema

A second schema beside the ledger schema, `<schema>_control` (`jarvis_control` on the box), created by migration v10. It holds:

* `tenants(tenant_key, label, status, created_at, deleted_at)`. `tenant_key` must be `t_` followed by 32 random hex characters (a CHECK), so it can never be `operator`, empty, or an OAuth
  hash. `status` is `active | suspended | quiesced | restoring | deleting | deleted`.
* `api_keys(key_id, tenant_key, key_hash, scopes, status, label, created_at, expires_at, last_used_at, revoked_at)`. `key_hash` is `HMAC-SHA256(pepper, secret)`.

It is a separate schema on purpose: these tables are where the server learns *which* tenant a credential belongs to, so they cannot be under tenant row-level security, and the guard that audits the
ledger schema ("every table with a `tenant_key` column has forced RLS", at readiness and at strict startup) rightly does not apply to them.

**The application role has no privilege on these tables.** It can only call two `SECURITY DEFINER` functions (owned by the migrator, with a pinned `search_path`; `PUBLIC` has no `EXECUTE`):
`resolve_key(key_id)` returns the stored hash, scopes, statuses and expiry for one id, and `touch_key_last_used(key_id)` notes a use (at most once a minute per key). Creating tenants and keys,
revoking, and changing a tenant's status are the migrator's, through `python -m app.tenant_keys` on the box. The public API cannot create, change or list a key or a tenant.

**Lifecycle is enforced in the database, not only in the CLI.** A trigger on `tenants` refuses any transition out of `deleted` (a deleted tenant id can never be resurrected, even by a hand-written UPDATE),
and entering `deleting` or `deleted` revokes every key of the tenant; a second trigger refuses to issue a key for a tenant that is being, or has been, deleted. Suspending, quiescing and restoring a tenant do not
revoke its keys. The ledger schema may be up to 63 characters: past 55 the control schema name is shortened deterministically (a prefix, a hash of the whole name, then `_control`) so it never exceeds PostgreSQL's limit
and two different long schemas never share one.

Rollback: `V10_ROLLBACK` (with `__CONTROL__` replaced by the control schema's name) drops the control schema and the version row. It holds no ledger data, so no ledger data changes either way. As with v8 and v9, a v9
application refuses a v10 database and the reverse; rehearse on a throwaway stack before the live migration.

## Token and verification

`jlk_<key_id>_<secret>`: `key_id` is 16 hex characters, **public** (it is the lookup handle, so verification is one indexed read and an unknown id reveals nothing); `secret` is 256 bits from the OS CSPRNG
(43 URL-safe characters). The database never holds the token or the secret, only `HMAC-SHA256(pepper, "jlk1" 0x00 key_id 0x00 secret)`: the hash covers the key id as well as the secret, so a stored hash cannot be moved to another key's row. The pepper is at least 32 bytes and comes from `JARVIS_KEY_PEPPER_FILE` (first line) or
`JARVIS_KEY_PEPPER`; there is no default (a pepper file that is readable by group or others is refused), and it is never in the database, so a stolen database alone cannot verify a guess.

`tenant_keys.resolve(conn, control, token, pepper)` returns a `ResolvedKey` (key id, tenant key, scopes, the tenant's status) or `None`. **Every failure is the same `None`**: malformed, unknown id, wrong
secret, wrong pepper, revoked, expired. The HMAC comparison is made even when the id is unknown or the token is malformed, so an unknown id costs the same as a wrong secret. A valid key of a tenant that is
not `active` resolves with that status, and the caller decides (the RFC says: `503 tenant_unavailable`, for that tenant only).

## Issuing (on the box, as the migrator role)

```
python -m app.tenant_keys create-tenant --label NAME
python -m app.tenant_keys create-key --tenant t_<hex32> [--label L] [--scopes memory.read,memory.write] [--expires-days N] [--token-file PATH]
python -m app.tenant_keys revoke-key KEY_ID
python -m app.tenant_keys set-status --tenant t_<hex32> --status suspended
python -m app.tenant_keys list-tenants | list-keys [--tenant t_<hex32>]
```

The token exists in exactly one place: the output of `create-key`. Prefer `--token-file` (mode 600, never overwrites an existing file) so it never appears on a terminal. Listing never shows a hash.

Pinned by `tests/test_tenant_keys.py` (privileges, the constraints, round trip, uniform failure, expiry, revocation, the comparison on unknown ids, rate-limited `last_used_at`, the CLI, rollback and forward again).
