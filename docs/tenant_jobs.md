# Per-tenant jobs: seal, sign and verify each tenant's own chain

`seal.sh`, `attest.sh sign` and `attest.sh verify` act on the **operator tenant only**: they talk to the API with the operator key, and the API never selects a tenant. With a second tenant its history would never be sealed or signed. This adds the per-tenant versions as a **new command**; the timers, `seal.sh` and `attest.sh` are unchanged and keep working on the operator tenant exactly as before. Nothing here is switched on by this change.

```
jarvisctl tenants list                                 the tenants that have history
jarvisctl tenants seal   [--force] [--tenants a,b]     seal each tenant's unsealed history
jarvisctl tenants sign   [--dry-run] [--tenants a,b]   sign each tenant's pending blocks
jarvisctl tenants status [--tenants a,b]               each tenant's signing state
jarvisctl tenants verify [--tenants a,b]               pg_verify each tenant: history chain, blocks, signatures
```

## How a tenant is chosen (and never by the API)
* **The list** comes from the superuser path the backup scripts use (`docker exec -u postgres psql`), because the migrator role cannot bypass row-level security and so cannot see more than one tenant.
* **The work** runs in a one-off `migrate` container (`python -m app.tenant_jobs`) as the migrator role, one tenant at a time, with the tenant bound by `identity.acting_for`, which nothing in the HTTP stack sets and no request parameter, header or token can set. `seal` calls the same `store.seal_blocks` the API route calls; for signing, the host signer reads and posts through a per-tenant API (`tenant-api.sh`, JSON lines to `app.tenant_jobs serve`) that runs the application's own routes and dependencies in process.
* **The private key** never leaves the host: signing is still `app.signer`, with its custody checks, its refusal to sign what it did not build itself, and its offline replay check (now told the tenant: `replay.sh verify --tenant T`).

## Rules
1. **One tenant cannot break the others.** Each runs on its own; an exception is logged and recorded and the loop goes on. Every failure is listed at the end (`FAILED tenants: ...`), the job exits non-zero, and a critical notification names them. A tenant that has not had its key ceremony fails with `key_not_authorized` while the others are signed.
2. **The operator alone gets the output it gets today.** The lines `seal.sh` logs, and the text `attest.sh sign` prints, are produced by the same wording (guarded by a test); another tenant's lines carry `tenant <key>: `, and a multi-tenant run ends with `all N tenants ok`.
3. **The watchdog is untouched.** The commands write their own state files (`tenants-seal.last_ok`, `tenants-sign.last_ok`), never `seal.last_ok` / `sign.last_ok`.
4. **Each tenant's signing is separate**: its own trust log (the root-signed statement authorizing the key names the tenant), its own attestation log and checkpoint.

## Before any outside tenant is live
Run the Windows read path of #89 on the PC, and the combined drill; the timers keep running `seal.sh` and `attest.sh sign` until someone decides to move them to `jarvisctl tenants`.
