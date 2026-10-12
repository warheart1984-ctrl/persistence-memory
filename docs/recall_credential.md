# The recall credential (`EMR_RECALL_API_KEY`)

`EMR_RECALL_API_KEY` is a **scoped, read-only** credential for a client that should recall from the ledger and do nothing else (for example an agent host or
a ChatGPT connector reaching `/mcp`). The ledger key (`JARVIS_API_KEY`) stays the operator credential.

| | ledger key | recall key |
|---|---|---|
| the seven read tools (`emr_recall`, `emr_latest`, `emr_search_ledger`, `search`, `fetch`, `emr_search`, `emr_fetch`) over `/api/jarvis/tools/*` and `/mcp` | yes | **yes** |
| `tools/list` over MCP | every tool | **only those seven** |
| `emr_remember`, `emr_upsert` (still off unless `JARVIS_MCP_WRITE_ENABLED`) and `emr_drift_check` | yes | **no** (tool error `AUTHORITY_DENIED`; over REST 401) |
| call log (`/api/jarvis/tools/calls`, `/verify`), drift routes, the nx routes | yes | **no** (401) |
| every REST ledger route (memory, blocks, attestations, trust, replay, llm, ...) | yes | **no** (401) |

Headers: `Authorization: Bearer <key>` or `X-EMR-Recall-Key: <key>` for the recall key (`X-API-Key` is not accepted for it); the ledger key goes in `X-API-Key` or as a Bearer. If both are presented, the ledger key wins.

**Installing a recall key breaks no existing client.** Before, setting `EMR_RECALL_API_KEY` made the tool routes, MCP and call log accept *only* that key (a ledger-key client got 401), and the
drift routes could not be authenticated at all (they required the ledger key at the middleware and the recall key at the route). Now the ledger key keeps working everywhere and the recall key is
added as a read-only identity. On a **public deployment** nothing changes: the recall key is the one operator credential there.

The boundary is pinned by `tests/test_recall_credential_boundary.py`: every route in the isolation matrix's registry is run with only the recall key and must be refused (401/403) except the seven read
tools and the MCP transport; the set of tools it may list or call is written out in the test, so adding one is a decision, and a route added later is closed to it by default.

Generate a recall key with `python3 -c "import secrets; print(secrets.token_urlsafe(32))"`, put it in `secrets/app.env` as `EMR_RECALL_API_KEY=...`, and give it to the client from a protected file or secret store; never paste it into a profile, a chat or a commit.
