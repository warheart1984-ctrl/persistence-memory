# The writer credential (`EMR_WRITER_API_KEY`)

`EMR_WRITER_API_KEY` is a **scoped** credential for a client that should recall from the ledger and *write draft memories*, and do nothing else: a second machine, a laptop, another assistant.
It exists so those clients do not need the ledger key (`JARVIS_API_KEY`), which is the full operator credential. It follows [the recall credential](recall_credential.md): same pattern, one step wider.

| | ledger key | writer key | recall key |
|---|---|---|---|
| the seven read tools (`emr_recall`, `emr_latest`, `emr_search_ledger`, `search`, `fetch`, `emr_search`, `emr_fetch`) over `/api/jarvis/tools/*` and `/mcp` | yes | **yes** | yes |
| `emr_remember`, `emr_upsert` (draft-only; still need `JARVIS_MCP_WRITE_ENABLED=true` **and** `user_requested=true` + a `user_statement`) | yes | **yes** | no (`AUTHORITY_DENIED` over MCP, 401 over REST) |
| `emr_drift_check`, the drift routes, the call log (`/api/jarvis/tools/calls`, `/verify`), the nx routes | yes | **no** (401 / `AUTHORITY_DENIED`) | no |
| every REST ledger route (memory, blocks, attestations, trust, replay, llm, ...) | yes | **no** (401) | no |
| `tools/list` over MCP | every tool | **the nine** above | the seven reads |

Headers: `Authorization: Bearer <key>` or `X-EMR-Writer-Key: <key>`. The writer key is **not** accepted in `X-API-Key` (that header is the ledger key's). If several credentials are presented, the
ledger key wins. It is not supported on a public deployment (the recall key is the one credential there), and it is ignored there.

**What it does not change.** The write gates are the same for every credential: `JARVIS_MCP_WRITE_ENABLED` is the deployment's switch (off by default), a write is a *draft*, and it is refused without
`user_requested=true`. A writer key does not override any of them. It also does not give the server a way to tell machines apart: records still carry a client-chosen `source_agent`, and the call log
records the client name as the client reports it. What it removes is the ledger key from the second machine.

**Startup checks.** The app refuses to start if `EMR_WRITER_API_KEY` is set and is shorter than 32 characters, equals `JARVIS_API_KEY` or `EMR_RECALL_API_KEY`, or the deployment is public.

## Installing one (on the box)

```
jarvisctl keys writer-key-init        # secrets/writer.key (mode 600) + one EMR_WRITER_API_KEY line in secrets/app.env; never overwritten, never printed
cd deploy/mint && docker compose up -d --force-recreate --no-deps app      # the app reads the key at start
```

Then copy `secrets/writer.key` to the machine that needs it by `scp` (never paste it), and point that machine at it, for example Claude Code on Windows:

```powershell
$k = (Get-Content "$HOME\.jarvis-writer-key" -Raw).Trim()
claude mcp remove jarvis-ledger --scope user
claude mcp add --transport http --scope user jarvis-ledger http://127.0.0.1:8011/mcp --header "Authorization: Bearer $k"
Remove-Variable k
```

The stdio proxy (`mcp_server/emr_stdio.py`) chooses the narrowest key per tool: the recall key for reads, the writer key for `emr_remember` / `emr_upsert`, the ledger key only for the drift check.

To abandon it: delete the `EMR_WRITER_API_KEY` line from `secrets/app.env`, recreate the app, and delete `secrets/writer.key` (and the copy on the other machine). To rotate: do that, then run `writer-key-init` again.

## Pinned by

`tests/test_writer_credential_boundary.py`: every route in the isolation matrix's registry is run with only the writer key and must be refused (401/403) except the read tool routes, the two write tool routes and
the MCP transport; the set of tools it may list or call is written out in the test, so adding one is a decision; the recall key stays read-only with a writer key configured; strict mode binds it as a request
identity; the startup checks. `tests/test_deploy_keys.py` pins `writer-key-init`.
