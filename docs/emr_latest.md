# `emr_latest` — find the newest memory without an id or a keyword

`emr_latest` lets any connected agent answer "what is the newest memory?" on its own. It is read-only, tenant-scoped, and
deterministic: two agents looking at the same ledger state get the same records in the same order and the same `result_digest`.

Two surfaces, **one implementation** (`app/emr_latest.py`, function `latest_memories`):

| surface | how |
|---|---|
| HTTP | `GET /api/jarvis/memory/latest` (same middleware and auth as the other memory reads) |
| HTTP (tool route) | `POST /api/jarvis/tools/emr_latest` (what the stdio proxy uses; same key rules as `emr_recall`) |
| MCP | tool `emr_latest` on `POST /mcp` (and through `mcp_server/emr_stdio.py`) |

## Paste this into Devin and OpenCode

> Using your memory tools, find the newest memory record. Do not ask me for an ID. Report its id, created_at, status, provenance, and result_digest.

The agent should call `emr_latest` with no arguments. If it asks you for an id, the tool is not wired into that client.

## Parameters

| name | type | default | rule |
|---|---|---|---|
| `limit` | integer | 10 | 1–50. Anything else is refused with `LIMIT_OUT_OF_RANGE` (never clamped; a non-integer is out of range too). |
| `cursor` | string | none | The opaque `next_cursor` of the previous page. Tampered, foreign or mismatched → `CURSOR_INVALID`. |
| `include_superseded` | bool | false | Include records another record supersedes. |
| `include_archived` | bool | false | Include archived records. |
| `include_twin` | bool | false | Include `source_agent="ai-twin"` records. |
| `type` | string | none | Only records of this type. |

## Ordering and paging

* `created_at DESC, id DESC` on the **stored** creation time (never the clock at query time). Records with the same
  `created_at` always come back in the same order (id descending).
* Keyset paging, not OFFSET. `next_cursor` encodes the last `(created_at, id)`, normalised to UTC with microseconds, so the
  JSON store and Postgres agree on where every page ends. It is HMAC-signed and bound to the tenant and to the filters it was
  issued for: a cursor from tenant X, or from a query with different flags, is `CURSOR_INVALID`.
* An empty ledger returns `records: []`, `next_cursor: null` and the digest of the empty list.

## Response

```json
{
  "records": [
    {
      "id": "mem-…",
      "created_at": "2026-10-09T23:56:44.902428Z",
      "type": "fact",
      "status": "active",
      "provenance": {"source_agent": "…", "actor": null, "method": null, "evidence_refs": []},
      "supersedes": null,
      "superseded_by": null,
      "summary": "subject, or the first line of the content"
    }
  ],
  "next_cursor": null,
  "tenant": "operator",
  "ledger_head": "seq:6",
  "result_digest": "sha256 hex",
  "provenance": "ledger"
}
```

* **`status`** is `archived` when the stored status is archived, otherwise `superseded` when another record in the tenant names
  it in `supersedes`, otherwise `active`. (`emr_upsert` archives the record it supersedes, so those show as `archived` with
  `superseded_by` filled in.) A record is hidden by default if it is superseded **or** archived; each flag lifts its own filter.
* **`provenance.actor` and `provenance.method` are `null`**: the ledger does not store them, and nothing is guessed.
  `evidence_refs` are the `ref` values of the record's evidence.
* **`result_digest`** = sha256 of the compact JSON of `[[id, created_at, status], …]` in returned order (UTF-8, no ASCII
  escaping). Same state → same digest.
* **`ledger_head`**: `null` on the JSON store (it has no chain). On Postgres: the operator key sees `block:<hash>` of the newest
  Continuity Block (or `seq:<n>` before the first block); OAuth tenants only ever see `seq:<n>`, their own history counter.
  Block data stays operator-only, as everywhere else.

## Errors

The stable lowercase `code` is unchanged; the spec reason is added as `reason`. Over HTTP both are top-level fields next to
`detail`; over MCP they are in `structuredContent.error`.

| reason | code | HTTP | when |
|---|---|---|---|
| `LIMIT_OUT_OF_RANGE` | `invalid_request` | 422 | `limit` not an integer in 1–50 |
| `CURSOR_INVALID` | `invalid_request` | 422 | cursor tampered, from another key, tenant or filter set |
| `AUTHORITY_DENIED` | `denied` | 401/403 | no or wrong credentials |
| `TENANT_UNRESOLVED` | `denied` | 403 | the caller's tenant cannot be determined (never served unscoped) |
| `CURSOR_KEY_UNAVAILABLE` | `unavailable` | 503 | no key available to sign cursors (see below) |

## Cursor key (operators)

Cursors are signed with a key derived by HKDF-SHA256 with the label `emr-latest-cursor-v1`, so the key is good for cursors
and nothing else. It is taken from `JARVIS_CURSOR_HMAC_KEY` (or the file named by `JARVIS_CURSOR_HMAC_KEY_FILE`) and otherwise
derived from `JARVIS_API_KEY`. With neither set the call fails closed with `CURSOR_KEY_UNAVAILABLE` — in particular, a
deployment that runs with `JARVIS_ALLOW_UNAUTHENTICATED` and no API key must set `JARVIS_CURSOR_HMAC_KEY`, and an OAuth-only
deployment must too. Changing the key invalidates cursors already handed out (callers start again from the first page).

Schema: Postgres gains additive schema **V8** (an index on `memories(tenant_key, supersedes)`); run the usual migrate step
before deploying code that serves `emr_latest`.

## Read-only, and rate limiting

The operation calls only read methods; the tests assert the ledger state (history sequence, block head, attestation head, or the
JSON file's bytes) is unchanged after 100 calls. The MCP route goes through the same `/mcp` path as every other tool, so in a public deployment it is under the existing MCP
rate limiter (`JARVIS_MCP_RATE_LIMIT_PER_MINUTE`; it is not applied in non-public mode). The HTTP memory reads have no limiter today, so this route adds none — and no unlimited
work, because `limit` is capped at 50 and the successor lookup is an index scan.

## Cross-agent acceptance check

`scripts/emr_latest_crosscheck.py` starts its **own scratch server** (ephemeral loopback port, temporary directory, a fresh API
key — it has no option to point at an existing server and refuses ports 8011/8001/8002 on every spelling). Two independent MCP
clients, `devin` and `opencode`, each with their own session, call `emr_latest` with no arguments:

1. seed five records; both clients must report the same top id (the last one seeded) and the same `result_digest`;
2. write one more record; both must now report it as newest, with matching digests that differ from round one.

```bash
python scripts/emr_latest_crosscheck.py --out crosscheck-report.json
```

It runs the JSON backend and, when `JARVIS_TEST_PG_DSN` names a throwaway Postgres on this machine, the Postgres backend too.
The report states per backend whether it **ran**; a backend that could not run is `ran: false` with the reason and is never
counted as a pass (`--backend postgres` exits non-zero if Postgres could not run). Each row has `client`, `top_id`,
`result_digest`, `ledger_head`, `timestamp`.
