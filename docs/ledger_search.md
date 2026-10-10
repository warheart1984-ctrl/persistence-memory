# `emr_search_ledger` — ranked word search over the ledger's own records

Searches **this tenant's ledger records** (not files; file search is nx-search, and the combined tool `emr_find` comes in
a separate change). Read-only. One implementation (`app/ledger_search.py::search_ledger`) behind:

| surface | how |
|---|---|
| HTTP | `GET /api/jarvis/memory/search?query=…` (same auth as the other memory reads) |
| HTTP (tool route) | `POST /api/jarvis/tools/emr_search_ledger` with the arguments as the JSON body (what the stdio proxy uses) |
| MCP | tool `emr_search_ledger` on `POST /mcp` and through `mcp_server/emr_stdio.py` |

`emr_recall`, `emr_search` and `/memory/retrieve` are unchanged; whether they should switch to this ranking is a separate
decision, made only after comparing `emr_bench` results.

## Matching

* **Words**: ASCII letters are lower-cased; text is split on every character that is not an ASCII letter or digit and not a
  non-ASCII character. So `emr_latest` is the two words `emr` and `latest`, `Running` matches only `running` (no stemming),
  and non-ASCII text is kept as is (`ÄBC` → `Äbc`; only ASCII is folded, so the result never depends on a database locale).
* **Every query word must appear** in the record's subject, content or tags. At most 16 distinct words, 500 characters.
* The same filters and defaults as `emr_latest`: superseded, archived and `ai-twin` records are excluded unless
  `include_superseded` / `include_archived` / `include_twin` is true; `type` narrows to one record type.

## Ranking (identical on the JSON store and Postgres)

Postgres only narrows the candidates with an index; the score is computed by one Python function for both backends, so
both return the same records in the same order (a test checks this on a shared corpus). Per query word:
subject **6**, tag **4**, content **1** per occurrence up to 3; plus **5** if the whole query appears as a consecutive phrase
in the subject or content. Ties go to the newest record (`created_at DESC, id DESC`).

At most the newest 5000 matching records are ranked (`candidates_capped: true` says the cap was hit); the cap is the same
on both backends.

## Response

```json
{
  "query": "mint deploy",
  "tokens": ["mint", "deploy"],
  "records": [{"id": "mem-…", "created_at": "…Z", "type": "fact", "status": "active",
               "provenance": {"source_agent": "…", "actor": null, "method": null, "evidence_refs": []},
               "supersedes": null, "superseded_by": null, "summary": "…", "score": 17}],
  "candidates_capped": false,
  "tenant": "operator",
  "ledger_head": "seq:42",
  "result_digest": "sha256 of [[id, created_at, status, score], …] in returned order",
  "provenance": "ledger"
}
```

Record fields, `status`, `ledger_head` and the tenant rules are exactly those of [`emr_latest`](emr_latest.md).

## Errors

As for `emr_latest`: the lowercase `code` is unchanged and the reason is added as `reason`.

| reason | code | HTTP |
|---|---|---|
| `QUERY_EMPTY` (missing, blank, or no searchable words) | `invalid_request` | 422 |
| `QUERY_TOO_LONG` (over 500 characters or 16 distinct words) | `invalid_request` | 422 |
| `LIMIT_OUT_OF_RANGE` (`limit` not an integer in 1–50) | `invalid_request` | 422 |
| `AUTHORITY_DENIED` | `denied` | 401/403 |
| `TENANT_UNRESOLVED` | `denied` | 403 |

## Schema V9 (Postgres) and rollback

V9 adds an IMMUTABLE function `jarvis_search_tokens(subject, content, tags)` and a GIN index on it
(`memories_search_idx`). It deliberately adds **no column**: record-history snapshots are the whole `memories` row, so a
column would change every snapshot and make the history verifier report every existing record as drifted. With an index
only, no row, snapshot, row hash or replay state root changes; a test migrates a populated V8 ledger to V9 and checks all
of these, then rolls back and checks again.

Taking a live ledger to V9 is a separate deploy step (run the usual migrate before serving this code). To roll back, run
`app.pg_schema.V9_ROLLBACK` as the migration role in one transaction: it drops the index and the function and removes the
V9 row from `schema_version`; deploy the V8 code alongside.
